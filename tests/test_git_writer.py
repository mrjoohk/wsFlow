"""UF-GT-01/02: real local Git effects stay inside the cooperative writer root."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsflow import gitops, writer_admission


class GitWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow-git-writer-')
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
        patch.start()
        self.addCleanup(patch.stop)
        self.repo, self.seed, self.bare, self.old, self.new = self.fixture('git')
        self.domain = writer_admission.default_domain()
        with self.domain.barrier(0):
            pass

    def git(self, repo, *args):
        result = subprocess.run(['git', *args], cwd=repo, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(0, result.returncode, (args, result.stderr))
        return result.stdout.strip()

    def fixture(self, name):
        base = self.base / name
        base.mkdir()
        seed, bare, repo = base / 'seed', base / 'remote.git', base / 'client'
        seed.mkdir()
        self.git(seed, 'init', '-q', '-b', 'main')
        (seed / 'work.txt').write_bytes(b'first\n')
        self.git(seed, 'add', 'work.txt')
        self.git(seed, 'commit', '-q', '-m', 'first')
        self.git(base, 'init', '-q', '--bare', '-b', 'main', str(bare))
        self.git(seed, 'remote', 'add', 'origin', str(bare))
        self.git(seed, 'push', '-q', '-u', 'origin', 'main')
        self.git(base, 'clone', '-q', str(bare), str(repo))
        old = self.git(repo, 'rev-parse', 'HEAD')
        (seed / 'work.txt').write_bytes(b'second\n')
        self.git(seed, 'commit', '-q', '-a', '-m', 'second')
        self.git(seed, 'push', '-q')
        return repo, seed, bare, old, self.git(seed, 'rev-parse', 'HEAD')

    @staticmethod
    def tree(root):
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob('*') if p.is_file()}

    def completed(self, epoch, domain=None):
        domain = domain or self.domain
        state = domain.observe()
        self.assertEqual(epoch + 2, state['epoch'])
        self.assertIsNone(state['active'])
        with domain.barrier(0) as lease:
            self.assertFalse(lease.view()['snapshot_ready'])

    def holding(self, epoch, thread, outcome):
        self.assertTrue(thread.is_alive())
        self.assertEqual({}, outcome)
        with self.assertRaises(writer_admission.WriterUnavailable) as error:
            with self.domain.barrier(0):
                pass
        self.assertEqual('busy', error.exception.reason)
        self.assertEqual(epoch + 1, self.domain.observe()['epoch'])
        self.assertIsNotNone(self.domain.observe()['active'])

    @staticmethod
    def pause(arrived, release):
        arrived.set()
        if not release.wait(15):
            raise TimeoutError('owned Git test hold expired')

    @contextmanager
    def worker(self, call, release, patches=()):
        outcome = {}
        def run():
            try:
                outcome['result'] = call()
            except BaseException as error:
                outcome['error'] = error
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            thread = threading.Thread(target=run, daemon=True, name='owned-git-writer')
            thread.start()
            try:
                yield thread, outcome
            finally:
                release.set()
                thread.join(20)
                self.assertFalse(thread.is_alive(), 'owned Git worker did not finish')
        if 'error' in outcome:
            raise outcome['error']

    def test_barrier_rejects_both_before_git_or_sync_effects(self):
        arrived, release = threading.Event(), threading.Event()
        errors = []
        def barrier():
            try:
                with self.domain.barrier(0):
                    self.pause(arrived, release)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=barrier, daemon=True, name='owned-git-barrier')
        thread.start()
        try:
            self.assertTrue(arrived.wait(5))
            for function in (gitops.fetch, gitops.pull_ff):
                with self.subTest(function=function.__name__):
                    # The held admission lock is intentionally unreadable on Windows.
                    # Observe business files separately from that control resource.
                    business = gitops._sync_path(self.repo).parent
                    before = self.tree(self.repo), self.tree(business)
                    with mock.patch.object(gitops, 'run_git', wraps=gitops.run_git) as calls:
                        with self.assertRaises(writer_admission.WriterUnavailable) as error:
                            function(self.repo)
                    self.assertEqual('busy', error.exception.reason)
                    self.assertEqual(0, calls.call_count)
                    self.assertEqual(before, (self.tree(self.repo), self.tree(business)))
        finally:
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

    def test_invalid_userdata_refuses_before_control_or_git_files(self):
        from wsflow.operations import StateConflict
        for function in (gitops.fetch, gitops.pull_ff):
            for name in ('same', 'inside'):
                with self.subTest(function=function.__name__, location=name):
                    repo = self.base / (function.__name__ + '-' + name)
                    repo.mkdir()
                    data = repo if name == 'same' else repo / 'userdata'
                    with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(data)}):
                        with mock.patch.object(gitops, 'run_git', wraps=gitops.run_git) as calls:
                            with self.assertRaises(StateConflict):
                                function(repo)
                        self.assertEqual(0, calls.call_count)
                    self.assertEqual([], list(repo.rglob('*')))

    def test_fetch_holds_actual_git_child(self):
        marker, exited = self.base / 'entered', self.base / 'exited'
        gate = self.base / 'release'
        helper = self.base / 'upload.py'
        helper.write_text('import os,subprocess,sys,time\nfrom pathlib import Path\n'
                          + 'marker=Path(' + repr(str(marker)) + ');gate=Path(' + repr(str(gate)) + ');exited=Path(' + repr(str(exited)) + ')\n'
                          + 'marker.write_text(str(os.getpid()));deadline=time.monotonic()+15\n'
                          + 'while not gate.exists():\n if time.monotonic()>deadline:sys.exit(3)\n time.sleep(.01)\n'
                          + "code=subprocess.call(['git','upload-pack',*sys.argv[1:]]);exited.write_text(str(code));sys.exit(code)\n",
                          encoding='utf-8')
        self.git(self.repo, 'config', 'remote.origin.uploadpack',
                 '"' + Path(sys.executable).as_posix() + '" "' + helper.as_posix() + '"')
        release = threading.Event()
        processes = []
        original = subprocess.Popen
        def popen(*args, **kwargs):
            child = original(*args, **kwargs)
            processes.append(child)
            return child
        epoch = self.domain.observe()['epoch']
        with self.worker(lambda: gitops.fetch(self.repo), release,
                         [mock.patch.object(gitops.subprocess, 'Popen', side_effect=popen)]) as (thread, outcome):
            try:
                deadline = time.monotonic() + 8
                while not marker.exists() and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists(), outcome)
                live = [child for child in processes if child.poll() is None]
                self.assertEqual(1, len(live))
                self.assertIn('fetch', live[0].args)
                self.holding(epoch, thread, outcome)
            finally:
                gate.write_bytes(b'go')
        self.assertTrue(outcome['result']['ok'], outcome)
        self.assertTrue(all(child.poll() is not None for child in processes))
        self.assertEqual('0', exited.read_text())
        self.assertEqual(self.new, self.git(self.repo, 'rev-parse', 'refs/remotes/origin/main'))
        self.assertEqual(self.old, self.git(self.repo, 'rev-parse', 'HEAD'))
        self.completed(epoch)

    def test_fetch_holds_sync_json_replacement(self):
        arrived, release = threading.Event(), threading.Event()
        target, original = gitops._sync_path(self.repo).resolve(), gitops.fileio.atomic_write
        def write(path, *args, **kwargs):
            if Path(path).resolve() == target:
                self.pause(arrived, release)
            return original(path, *args, **kwargs)
        epoch = self.domain.observe()['epoch']
        with self.worker(lambda: gitops.fetch(self.repo), release,
                         [mock.patch.object(gitops.fileio, 'atomic_write', side_effect=write)]) as (thread, outcome):
            self.assertTrue(arrived.wait(8))
            self.holding(epoch, thread, outcome)
            self.assertEqual(self.new, self.git(self.repo, 'rev-parse', 'refs/remotes/origin/main'))
            self.assertFalse(target.exists())
        self.assertTrue(outcome['result']['ok'])
        self.assertEqual(outcome['result']['sync'], json.loads(target.read_bytes()))
        self.completed(epoch)

    def test_pull_holds_final_status_after_real_ff(self):
        arrived, release = threading.Event(), threading.Event()
        original, calls = gitops.status, []
        def status(repo):
            result = original(repo)
            calls.append(result)
            if len(calls) == 3:
                self.pause(arrived, release)
            return result
        epoch = self.domain.observe()['epoch']
        with self.worker(lambda: gitops.pull_ff(self.repo), release,
                         [mock.patch.object(gitops, 'status', side_effect=status)]) as (thread, outcome):
            self.assertTrue(arrived.wait(8))
            self.holding(epoch, thread, outcome)
            self.assertEqual(self.new, self.git(self.repo, 'rev-parse', 'HEAD'))
            self.assertEqual(b'second\n', (self.repo / 'work.txt').read_bytes())
        self.assertTrue(outcome['result']['ok'])
        self.assertEqual(calls[-1], outcome['result']['status'])
        self.completed(epoch)

    def test_nested_calls_reuse_outer_domain(self):
        alternate = self.base / 'alternate'
        epoch = self.domain.observe()['epoch']
        with self.domain.writing():
            active = self.domain.observe()
            with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(alternate)}):
                for function in (gitops.fetch, gitops.pull_ff):
                    self.assertTrue(function(self.repo)['ok'])
                    self.assertEqual(active, self.domain.observe())
                    self.assertEqual('missing', writer_admission.default_domain().observe()['status'])
        self.completed(epoch)

    def test_independent_calls_resolve_userdata_at_call_time(self):
        for name, function in (('first', gitops.fetch), ('second', gitops.pull_ff)):
            with self.subTest(function=function.__name__), mock.patch.dict(
                    os.environ, {'WSFLOW_DATA_DIR': str(self.base / name)}):
                domain = writer_admission.default_domain()
                self.assertEqual('missing', domain.observe()['status'])
                self.assertTrue(function(self.repo)['ok'])
                self.completed(0, domain)
        self.assertEqual(0, self.domain.observe()['epoch'])

    def test_git_failure_and_save_exception_finish_root(self):
        self.assertTrue(gitops.fetch(self.repo)['ok'])
        successful_at = gitops.sync_info(self.repo)['last_fetch_at']
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.base / 'absent.git'))
        epoch = self.domain.observe()['epoch']
        result = gitops.fetch(self.repo)
        self.assertFalse(result['ok'])
        self.assertEqual(successful_at, result['sync']['last_fetch_at'])
        self.assertTrue(result['sync']['last_error'])
        self.completed(epoch)
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.bare))
        target, original = gitops._sync_path(self.repo).resolve(), gitops.fileio.atomic_write
        def write(path, *args, **kwargs):
            if Path(path).resolve() == target:
                raise OSError('owned sync save failure')
            return original(path, *args, **kwargs)
        epoch = self.domain.observe()['epoch']
        with mock.patch.object(gitops.fileio, 'atomic_write', side_effect=write):
            with self.assertRaisesRegex(OSError, 'owned sync save failure'):
                gitops.fetch(self.repo)
        self.assertEqual(self.new, self.git(self.repo, 'rev-parse', 'refs/remotes/origin/main'))
        self.completed(epoch)

    def test_dirty_and_non_ff_preserve_user_files_index_and_head(self):
        for name in ('dirty', 'non_ff'):
            with self.subTest(reason=name):
                repo = self.repo if name == 'dirty' else self.fixture('diverged')[0]
                (repo / 'user.txt').write_bytes(b'USER OWNED\n')
                if name == 'non_ff':
                    self.git(repo, 'add', 'user.txt')
                    self.git(repo, 'commit', '-q', '-m', 'local divergence')
                before = (self.git(repo, 'rev-parse', 'HEAD'), (repo / '.git/index').read_bytes(),
                          (repo / '.git/config').read_bytes(),
                          {n: data for n, data in self.tree(repo).items() if not n.startswith('.git/')})
                epoch = self.domain.observe()['epoch']
                result = gitops.pull_ff(repo)
                self.assertEqual(name, result['reason'])
                self.assertIsNone(result['command'])
                self.assertEqual(before, (self.git(repo, 'rev-parse', 'HEAD'), (repo / '.git/index').read_bytes(),
                                         (repo / '.git/config').read_bytes(),
                                         {n: data for n, data in self.tree(repo).items() if not n.startswith('.git/')}))
                self.completed(epoch)

    def test_pull_retains_fixed_safe_git_options(self):
        with mock.patch.object(gitops, 'run_git', wraps=gitops.run_git) as calls:
            self.assertTrue(gitops.pull_ff(self.repo)['ok'])
        arguments = next(call.args[1:] for call in calls.call_args_list if 'pull' in call.args)
        self.assertEqual(('-c', 'merge.autoStash=false', '-c', 'rebase.autoStash=false',
                          'pull', '--ff-only', '--no-rebase', '--no-autostash'), arguments)

    def test_read_only_git_does_not_advance_epoch(self):
        before = self.domain.observe()['epoch']
        with self.domain.barrier(0):
            self.assertEqual('main', gitops.status(self.repo)['branch'])
            self.assertEqual(1, len(gitops.log(self.repo)))
            self.assertEqual('origin', gitops.remotes(self.repo)[0]['name'])
            self.assertIsNone(gitops.sync_info(self.repo)['last_fetch_at'])
        self.assertEqual(before, self.domain.observe()['epoch'])


if __name__ == '__main__':
    unittest.main()
