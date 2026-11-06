"""UF-GL-01..03: local Git mutations retain one cooperative writer root."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from wsflow import gitops, writer_admission
from wsflow.operations import StateConflict


class GitLocalWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow-git-local-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        config = self.base / 'gitconfig'
        config.write_bytes(b'')
        patch = mock.patch.dict(os.environ, {
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(config),
            'GIT_CONFIG_COUNT': '0', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
            'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
            'WSFLOW_DATA_DIR': str(self.base / 'userdata')})
        patch.start(); self.addCleanup(patch.stop)
        self.repo = self.base / 'repo'; self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'work.txt').write_bytes(b'first\n')
        self.git('add', 'work.txt'); self.git('commit', '-q', '-m', 'first')
        self.git('remote', 'add', 'origin', str(self.base / 'local-target'))
        self.git('remote', 'add', 'extra', str(self.base / 'extra-target'))
        (self.repo / 'pending.txt').write_bytes(b'pending\n')
        self.domain = writer_admission.default_domain()
        with self.domain.barrier(0): pass

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.repo, capture_output=True,
                                encoding='utf8', errors='replace', timeout=20)
        self.assertEqual(0, result.returncode, (args, result.stderr))
        return result.stdout.strip()

    def calls(self, repo=None):
        repo = self.repo if repo is None else repo
        return [
            ('remote_add', lambda: gitops.remote_add(repo, name='added', url=str(self.base / 'extra'))),
            ('remote_set_url', lambda: gitops.remote_set_url(repo, 'origin', str(self.base / 'changed'))),
            ('remote_remove', lambda: gitops.remote_remove(repo, name='extra')),
            ('add', lambda: gitops.add(repo, paths=['pending.txt'])),
            ('commit', lambda: gitops.commit(repo, message='한글 변경\n\n본문', add_all=True)),
            ('clean_locks', lambda: gitops.clean_locks(repo, min_age_sec=5))]

    def business(self):
        return {p.relative_to(self.repo).as_posix(): p.read_bytes()
                for p in self.repo.rglob('*') if p.is_file()}

    def completed(self, epoch, domain=None):
        domain = self.domain if domain is None else domain
        self.assertEqual(epoch + 2, domain.observe()['epoch'])
        self.assertIsNone(domain.observe()['active'])
        with domain.barrier(0): pass

    def holding(self, epoch, thread, outcome):
        self.assertTrue(thread.is_alive()); self.assertEqual({}, outcome)
        self.assertEqual(epoch + 1, self.domain.observe()['epoch'])
        with self.assertRaises(writer_admission.WriterUnavailable) as error:
            with self.domain.barrier(0): pass
        self.assertEqual('busy', error.exception.reason)

    @contextmanager
    def worker(self, call, patches=()):
        entered, release, outcome = threading.Event(), threading.Event(), {}
        def pause():
            entered.set()
            if not release.wait(10): raise TimeoutError('owned local Git hold expired')
        def run():
            try: outcome['value'] = call()
            except BaseException as error: outcome['error'] = error
        with ExitStack() as stack:
            for patch in patches: stack.enter_context(patch(pause))
            thread = threading.Thread(target=run, daemon=True, name='owned-local-git')
            thread.start()
            try:
                self.assertTrue(entered.wait(8), outcome)
                yield thread, outcome
            finally:
                release.set(); thread.join(15)
                self.assertFalse(thread.is_alive())
        if 'error' in outcome: raise outcome['error']

    def test_barrier_refuses_all_six_before_git_temp_or_unlink(self):
        with self.domain.barrier(0):
            for name, call in self.calls():
                with self.subTest(function=name):
                    if name == 'clean_locks':
                        lock = self.repo / '.git/index.lock'; lock.write_bytes(b'owned old lock')
                        at = time.time() - 60; os.utime(lock, (at, at))
                    before = self.business()
                    with mock.patch.object(gitops, 'run_git', wraps=gitops.run_git) as git_calls, \
                            mock.patch.object(gitops.tempfile, 'mkstemp', wraps=tempfile.mkstemp) as temps, \
                            mock.patch.object(gitops.os, 'unlink', wraps=os.unlink) as unlinks, \
                            mock.patch.object(gitops, '_git_running', return_value=False):
                        with self.assertRaises(writer_admission.WriterUnavailable): call()
                    self.assertEqual(0, git_calls.call_count)
                    self.assertEqual(0, temps.call_count); self.assertEqual(0, unlinks.call_count)
                    self.assertEqual(before, self.business())

    def test_invalid_userdata_refuses_all_six_without_any_files(self):
        for name, _ in self.calls():
            for location in ('same', 'inside'):
                with self.subTest(function=name, location=location):
                    repo = self.base / (name + '-' + location); repo.mkdir()
                    data = repo if location == 'same' else repo / 'userdata'
                    call = dict(self.calls(repo))[name]
                    with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(data)}), \
                            mock.patch.object(gitops, 'run_git', wraps=gitops.run_git) as calls:
                        with self.assertRaises(StateConflict): call()
                    self.assertEqual(0, calls.call_count); self.assertEqual([], list(repo.rglob('*')))

    def test_each_git_effect_remains_admitted_through_return(self):
        original = gitops.run_git
        for name, call in self.calls()[:-1]:
            with self.subTest(function=name):
                epoch = self.domain.observe()['epoch']
                def patch(pause):
                    def run(repo, *args, **kwargs):
                        result = original(repo, *args, **kwargs)
                        selected = ('remote' in args if name.startswith('remote_') else name in args)
                        if selected: pause()
                        return result
                    return mock.patch.object(gitops, 'run_git', side_effect=run)
                with self.worker(call, [patch]) as (thread, outcome): self.holding(epoch, thread, outcome)
                self.completed(epoch)
                if name != 'commit': self.assertIsNone(outcome['value'])
                else: self.assertEqual(self.git('rev-parse', 'HEAD'), outcome['value']['hash'])
        self.assertEqual('한글 변경\n\n본문', self.git('log', '-1', '--format=%B'))
        self.assertEqual('', self.git('status', '--porcelain'))
        self.assertEqual(str(self.base / 'changed'), self.git('remote', 'get-url', 'origin'))
        self.assertEqual('added\norigin', self.git('remote'))

    def test_commit_holds_actual_utf8_message_cleanup_after_git_effect(self):
        original = os.unlink; messages = []
        def patch(pause):
            def unlink(path, *args, **kwargs):
                if Path(path).name.startswith('wsm_commit_'):
                    messages.append(Path(path)); pause()
                return original(path, *args, **kwargs)
            return mock.patch.object(gitops.os, 'unlink', side_effect=unlink)
        old = self.git('rev-parse', 'HEAD'); epoch = self.domain.observe()['epoch']
        with self.worker(lambda: gitops.commit(self.repo, '한글 변경\n\n본문', add_all=True), [patch]) as (thread, outcome):
            self.holding(epoch, thread, outcome)
            self.assertNotEqual(old, self.git('rev-parse', 'HEAD'))
            self.assertEqual('한글 변경\n\n본문\n'.encode('utf8'), messages[0].read_bytes())
        self.assertFalse(messages[0].exists()); self.completed(epoch)

    def test_commit_holds_final_head_read_after_temp_is_removed(self):
        original, original_temp, messages = gitops.run_git, tempfile.mkstemp, []
        def temp(*args, **kwargs):
            result = original_temp(*args, **kwargs)
            if Path(result[1]).name.startswith('wsm_commit_'): messages.append(Path(result[1]))
            return result
        def patch(pause):
            def run(repo, *args, **kwargs):
                result = original(repo, *args, **kwargs)
                if args == ('rev-parse', 'HEAD'): pause()
                return result
            return mock.patch.object(gitops, 'run_git', side_effect=run)
        epoch = self.domain.observe()['epoch']
        with mock.patch.object(gitops.tempfile, 'mkstemp', side_effect=temp):
            with self.worker(lambda: gitops.commit(self.repo, message='final', add_all=True), [patch]) as (thread, outcome):
                self.holding(epoch, thread, outcome)
                self.assertEqual(1, len(messages)); self.assertFalse(messages[0].exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), outcome['value']['hash']); self.completed(epoch)

    def test_lock_cleanup_holds_unlink_and_preserves_fresh_and_objects(self):
        old, fresh = self.repo / '.git/index.lock', self.repo / '.git/HEAD.lock'
        ignored = self.repo / '.git/objects/keep.lock'
        for path in (old, fresh, ignored): path.write_bytes(b'owned lock')
        at = time.time() - 60; os.utime(old, (at, at)); os.utime(ignored, (at, at))
        original = os.unlink
        def patch(pause):
            def unlink(path, *args, **kwargs):
                if Path(path) == old: pause()
                return original(path, *args, **kwargs)
            return mock.patch.object(gitops.os, 'unlink', side_effect=unlink)
        epoch = self.domain.observe()['epoch']
        with mock.patch.object(gitops, '_git_running', return_value=False):
            with self.worker(lambda: gitops.clean_locks(self.repo, min_age_sec=30), [patch]) as (thread, outcome):
                self.holding(epoch, thread, outcome); self.assertTrue(old.exists())
        self.assertEqual([str(old)], outcome['value']['removed'])
        self.assertEqual([str(fresh)], outcome['value']['skipped'])
        self.assertTrue(fresh.exists()); self.assertTrue(ignored.exists()); self.completed(epoch)

    def test_git_failure_preserves_config_and_removes_commit_temp(self):
        before = (self.repo / '.git/config').read_bytes(); epoch = self.domain.observe()['epoch']
        with self.assertRaises(gitops.GitError): gitops.remote_add(self.repo, 'origin', 'unused')
        self.assertEqual(before, (self.repo / '.git/config').read_bytes()); self.completed(epoch)
        epoch = self.domain.observe()['epoch']; original, messages = tempfile.mkstemp, []
        def temp(*args, **kwargs):
            result = original(*args, **kwargs)
            if Path(result[1]).name.startswith('wsm_commit_'): messages.append(Path(result[1]))
            return result
        with mock.patch.object(gitops.tempfile, 'mkstemp', side_effect=temp):
            with self.assertRaises(gitops.GitError): gitops.commit(self.repo, 'nothing staged')
        self.assertEqual(1, len(messages)); self.assertFalse(messages[0].exists()); self.completed(epoch)

    def test_nested_six_reuse_outer_domain_when_userdata_changes(self):
        epoch = self.domain.observe()['epoch']; alternate = self.base / 'alternate'
        with self.domain.writing():
            active = self.domain.observe()
            with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(alternate)}), \
                    mock.patch.object(gitops, '_git_running', return_value=False):
                for name, call in self.calls():
                    with self.subTest(function=name):
                        call(); self.assertEqual(active, self.domain.observe())
                        self.assertEqual('missing', writer_admission.default_domain().observe()['status'])
        self.assertFalse(alternate.exists()); self.completed(epoch)

    def test_independent_six_resolve_userdata_at_call_time(self):
        epoch = self.domain.observe()['epoch']
        for name, call in self.calls():
            with self.subTest(function=name), mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / name)}):
                domain = writer_admission.default_domain(); self.assertEqual('missing', domain.observe()['status'])
                call(); self.completed(0, domain)
        self.assertEqual(epoch, self.domain.observe()['epoch'])

    def test_lock_busy_skip_and_unlink_failure_finish_root_without_losing_files(self):
        lock = self.repo / '.git/index.lock'; lock.write_bytes(b'owned')
        at = time.time() - 60; os.utime(lock, (at, at)); epoch = self.domain.observe()['epoch']
        with mock.patch.object(gitops, '_git_running', return_value=True):
            result = gitops.clean_locks(self.repo)
        self.assertEqual([], result['removed']); self.assertEqual([str(lock)], result['skipped'])
        self.assertEqual(b'owned', lock.read_bytes()); self.completed(epoch)
        epoch = self.domain.observe()['epoch']; original = os.unlink
        def denied(path, *args, **kwargs):
            if Path(path) == lock: raise PermissionError(13, 'owned denial')
            return original(path, *args, **kwargs)
        with mock.patch.object(gitops, '_git_running', return_value=False), mock.patch.object(gitops.os, 'unlink', side_effect=denied):
            result = gitops.clean_locks(self.repo)
        self.assertEqual([], result['removed']); self.assertIn('owned denial', result['skipped'][0])
        self.assertEqual(b'owned', lock.read_bytes()); self.completed(epoch)


if __name__ == '__main__': unittest.main()
