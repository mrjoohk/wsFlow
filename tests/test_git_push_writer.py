"""UF-GP-01/02: actual local push effects, complete roots and no replay."""
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

from wsflow import common_writes, fileio, gitops, operations, writer_admission


class GitPushWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow-push-writer-')
        self.addCleanup(temporary.cleanup); self.base = Path(temporary.name)
        config = self.base / 'gitconfig'; config.write_bytes(b'')
        patch = mock.patch.dict(os.environ, {
            'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':str(config), 'GIT_CONFIG_COUNT':'0',
            'GIT_TERMINAL_PROMPT':'0', 'GIT_AUTHOR_NAME':'fixture',
            'GIT_AUTHOR_EMAIL':'fixture@example.invalid', 'GIT_COMMITTER_NAME':'fixture',
            'GIT_COMMITTER_EMAIL':'fixture@example.invalid', 'WSFLOW_DATA_DIR':str(self.base/'userdata')})
        patch.start(); self.addCleanup(patch.stop)
        self.repo, self.bare = self.base/'repo', self.base/'remote.git'; self.repo.mkdir()
        self.git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo/'work.txt').write_bytes(b'owned work\n')
        self.git(self.repo, 'add', 'work.txt'); self.git(self.repo, 'commit', '-q', '-m', 'first')
        self.git(self.base, 'init', '-q', '--bare', '-b', 'main', str(self.bare))
        self.git(self.repo, 'remote', 'add', 'origin', str(self.bare))
        self.oid = self.git(self.repo, 'rev-parse', 'HEAD')
        self.domain = writer_admission.default_domain()
        with self.domain.barrier(0): pass

    def git(self, repo, *args):
        cp = subprocess.run(['git', *args], cwd=repo, capture_output=True, encoding='utf8', errors='replace', timeout=20)
        self.assertEqual(0, cp.returncode, (args, cp.stderr)); return cp.stdout.strip()

    def remote_ref(self):
        cp = subprocess.run(['git','rev-parse','--verify','refs/heads/main'], cwd=self.bare,
                            capture_output=True, encoding='utf8', errors='replace', timeout=20)
        return None if cp.returncode else cp.stdout.strip()

    def raw(self): return gitops.push(self.repo, remote='origin', branch='main', set_upstream=True)

    def service(self): return common_writes.Service({'id':'synthetic-push','path':str(self.repo),'alias':'fixture'}, 'git')

    def plan(self, service): return service.preview('push', {'remote':'origin','branch':'main','set_upstream':True})

    def completed(self, epoch, domain=None):
        domain = self.domain if domain is None else domain
        self.assertEqual(epoch+2, domain.observe()['epoch']); self.assertIsNone(domain.observe()['active'])
        with domain.barrier(0): pass

    def holding(self, epoch, thread, outcome):
        self.assertTrue(thread.is_alive()); self.assertEqual({}, outcome)
        self.assertEqual(epoch+1, self.domain.observe()['epoch'])
        with self.assertRaises(writer_admission.WriterUnavailable) as error:
            with self.domain.barrier(0): pass
        self.assertEqual('busy', error.exception.reason)

    @contextmanager
    def worker(self, call, patches):
        entered, release, outcome = threading.Event(), threading.Event(), {}
        def pause():
            entered.set()
            if not release.wait(12): raise TimeoutError('owned push hold expired')
        def run():
            try: outcome['value'] = call()
            except BaseException as error: outcome['error'] = error
        with ExitStack() as stack:
            for patch in patches: stack.enter_context(patch(pause))
            thread = threading.Thread(target=run, daemon=True, name='owned-push-writer'); thread.start()
            try:
                self.assertTrue(entered.wait(8), outcome); yield thread, outcome
            finally:
                release.set(); thread.join(18); self.assertFalse(thread.is_alive())
        if 'error' in outcome: raise outcome['error']

    def test_raw_barrier_refuses_before_git_or_remote_effect(self):
        before = (self.repo/'.git/config').read_bytes()
        with self.domain.barrier(0), mock.patch.object(gitops,'run_git',wraps=gitops.run_git) as calls:
            with self.assertRaises(writer_admission.WriterUnavailable): self.raw()
        self.assertEqual(0, calls.call_count); self.assertIsNone(self.remote_ref())
        self.assertEqual(before, (self.repo/'.git/config').read_bytes())

    def test_raw_invalid_userdata_refuses_without_control_files(self):
        for location in ('same','inside'):
            with self.subTest(location=location):
                repo = self.base/location; repo.mkdir(); data = repo if location=='same' else repo/'userdata'
                with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(data)}), mock.patch.object(gitops,'run_git',wraps=gitops.run_git) as calls:
                    with self.assertRaises(operations.StateConflict): gitops.push(repo, branch='main')
                self.assertEqual([], list(repo.rglob('*'))); self.assertEqual(0, calls.call_count)
        self.assertIsNone(self.remote_ref())

    def test_raw_holds_actual_receive_process(self):
        marker, release, exited = self.base/'entered', self.base/'release', self.base/'exited'
        helper = self.base/'receive.py'
        helper.write_text('import os,subprocess,sys,time\nfrom pathlib import Path\n'
            + 'marker=Path('+repr(str(marker))+');release=Path('+repr(str(release))+');exited=Path('+repr(str(exited))+')\n'
            + 'marker.write_text(str(os.getpid()));deadline=time.monotonic()+15\n'
            + 'while not release.exists():\n if time.monotonic()>deadline:sys.exit(3)\n time.sleep(.01)\n'
            + "code=subprocess.call(['git','receive-pack',*sys.argv[1:]]);exited.write_text(str(code));sys.exit(code)\n", encoding='utf8')
        self.git(self.repo,'config','remote.origin.receivepack','"'+Path(sys.executable).as_posix()+'" "'+helper.as_posix()+'"')
        actual, children, outcome = subprocess.Popen, [], {}
        def popen(*args,**kwargs):
            child=actual(*args,**kwargs);children.append(child);return child
        def run():
            try:outcome['value']=self.raw()
            except BaseException as error:outcome['error']=error
        epoch=self.domain.observe()['epoch']
        with mock.patch.object(gitops.subprocess,'Popen',side_effect=popen):
            thread=threading.Thread(target=run,daemon=True);thread.start()
            try:
                deadline=time.monotonic()+8
                while not marker.exists() and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(marker.exists(),outcome)
                live=[child for child in children if child.poll() is None];self.assertEqual(1,len(live));self.assertIn('push',live[0].args)
                self.holding(epoch,thread,outcome);self.assertIsNone(self.remote_ref())
            finally:
                release.write_bytes(b'go');thread.join(20);self.assertFalse(thread.is_alive())
        if 'error' in outcome:raise outcome['error']
        self.assertTrue(outcome['value']['ok']);self.assertEqual('0',exited.read_text())
        self.assertTrue(all(child.poll() is not None for child in children));self.assertEqual(self.oid,self.remote_ref());self.completed(epoch)

    def test_raw_root_includes_result_fields_after_real_push(self):
        original = gitops.run_git
        for field in ('returncode','stdout','stderr'):
            with self.subTest(field=field):
                sends=[]
                def patch(pause):
                    def run(repo,*args,**kwargs):
                        cp=original(repo,*args,**kwargs);sends.append(args)
                        class Observed:
                            def __getattr__(self,name):
                                if name==field:pause()
                                return getattr(cp,name)
                        return Observed()
                    return mock.patch.object(gitops,'run_git',side_effect=run)
                epoch=self.domain.observe()['epoch']
                with self.worker(self.raw,[patch]) as (thread,outcome):
                    self.holding(epoch,thread,outcome);self.assertEqual(self.oid,self.remote_ref())
                    self.assertEqual('origin/main',self.git(self.repo,'rev-parse','--abbrev-ref','main@{upstream}'))
                self.assertEqual([('push','-u','origin','main')],sends)
                self.assertTrue(outcome['value']['ok']);self.assertEqual('git push -u origin main',outcome['value']['command']);self.completed(epoch)

    def test_raw_nonzero_result_finishes_without_replay(self):
        self.git(self.repo,'remote','set-url','origin',str(self.base/'absent.git'))
        epoch=self.domain.observe()['epoch']
        with mock.patch.object(gitops,'run_git',wraps=gitops.run_git) as calls:result=self.raw()
        self.assertFalse(result['ok']);self.assertNotEqual(0,result['returncode']);self.assertEqual(1,calls.call_count)
        self.assertIsNone(self.remote_ref());self.completed(epoch)

    def test_raw_error_after_remote_effect_preserves_error_and_never_replays(self):
        original,error,sends=gitops.run_git,gitops.GitError('owned lost push response',124),[]
        def lose(repo,*args,**kwargs):
            cp=original(repo,*args,**kwargs);self.assertEqual(0,cp.returncode);sends.append(args);raise error
        epoch=self.domain.observe()['epoch']
        with mock.patch.object(gitops,'run_git',side_effect=lose), self.assertRaises(gitops.GitError) as caught:self.raw()
        self.assertIs(error,caught.exception);self.assertEqual(1,len(sends));self.assertEqual(self.oid,self.remote_ref());self.completed(epoch)

    def test_raw_nested_and_independent_calls_resolve_expected_domains(self):
        epoch=self.domain.observe()['epoch'];alternate=self.base/'alternate'
        with self.domain.writing():
            active=self.domain.observe()
            with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(alternate)}):
                self.assertTrue(self.raw()['ok']);self.assertEqual(active,self.domain.observe())
                self.assertEqual('missing',writer_admission.default_domain().observe()['status'])
        self.assertFalse(alternate.exists());self.completed(epoch)
        with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'independent')}):
            domain=writer_admission.default_domain();self.assertTrue(self.raw()['ok']);self.completed(0,domain)
        self.assertEqual(self.oid,self.remote_ref())

    def test_raw_invalid_names_preserve_existing_errors_before_git(self):
        for values in ({'remote':'--bad','branch':'main'},{'remote':'origin','branch':'--bad-branch'}):
            with self.subTest(values=values):
                epoch=self.domain.observe()['epoch']
                with mock.patch.object(gitops,'run_git',wraps=gitops.run_git) as calls:
                    with self.assertRaises(gitops.GitError):gitops.push(repo=self.repo,**values)
                self.assertEqual(0,calls.call_count);self.completed(epoch)
        self.assertIsNone(self.remote_ref())

    def test_service_apply_holds_receipt_after_real_remote_effect(self):
        service=self.service();plan=self.plan(service);target,_=service._paths(plan['operation_id']);original=fileio.atomic_write
        def patch(pause):
            def write(path,*args,**kwargs):
                if Path(path)==target:pause()
                return original(path,*args,**kwargs)
            return mock.patch.object(fileio,'atomic_write',side_effect=write)
        epoch=self.domain.observe()['epoch']
        with self.worker(lambda:service.apply(plan['plan_id'],plan['operation_id']),[patch]) as (thread,outcome):
            self.holding(epoch,thread,outcome);self.assertEqual(self.oid,self.remote_ref());self.assertFalse(target.exists())
            self.assertEqual('executing',service.status(plan['operation_id'])['status'])
        self.assertEqual('succeeded',outcome['value']['status']);self.assertEqual(plan['operation_id'],json.loads(target.read_bytes())['operation_id']);self.completed(epoch)

    def test_service_receipt_repairs_sql_loss_without_remote_resend(self):
        service=self.service();plan=self.plan(service);error=OSError('owned result SQL loss');epoch=self.domain.observe()['epoch']
        with mock.patch.object(service.store,'finish',side_effect=error):
            with self.assertRaises(OSError) as caught:service.apply(plan['plan_id'],plan['operation_id'])
        self.assertIs(error,caught.exception);self.assertEqual(self.oid,self.remote_ref());self.completed(epoch)
        self.assertEqual('executing',service.status(plan['operation_id'])['status'])
        receipt,_=service._paths(plan['operation_id']);self.assertTrue(receipt.is_file())
        epoch=self.domain.observe()['epoch'];original=gitops.run_git
        def no_send(repo,*args,**kwargs):
            self.assertNotEqual('push',args[0]);return original(repo,*args,**kwargs)
        with mock.patch.object(gitops,'run_git',side_effect=no_send):result=service.check(plan['operation_id'])
        self.assertEqual('succeeded',result['status']);self.assertTrue(result['reconciled']);self.completed(epoch)

    def test_service_uncertain_blocks_retry_and_confirms_only_by_readback(self):
        service=self.service();plan=self.plan(service);original=service._execute;sends=[]
        def lose(value):
            result=original(value);self.assertTrue(result['ok']);sends.append(value['operation_id'] if 'operation_id' in value else value['plan_id']);raise TimeoutError('owned remote effect without receipt')
        with mock.patch.object(service,'_execute',side_effect=lose):
            with self.assertRaises(TimeoutError):service.apply(plan['plan_id'],plan['operation_id'])
        self.assertEqual(self.oid,self.remote_ref());self.assertEqual('uncertain',service.status(plan['operation_id'])['status'])
        with mock.patch.object(service,'_execute',side_effect=AssertionError('must not resend')):
            with self.assertRaises(operations.StateConflict):service.apply(plan['plan_id'],plan['operation_id'])
        with self.assertRaises(operations.StateConflict):self.plan(service)
        epoch=self.domain.observe()['epoch'];original_git=gitops.run_git
        def no_send(repo,*args,**kwargs):
            self.assertNotEqual('push',args[0]);return original_git(repo,*args,**kwargs)
        with mock.patch.object(gitops,'run_git',side_effect=no_send):result=service.check(plan['operation_id'])
        self.assertEqual(1,len(sends));self.assertEqual('succeeded',result['status']);self.assertTrue(result['reconciled']);self.completed(epoch)


if __name__=='__main__':unittest.main()
