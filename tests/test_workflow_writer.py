"""FW01-04: real files, stock HTTP and known vendor children in owned fixtures."""
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_i7 as i7
from wsflow import fileio, operations, wfhome, wfinit, workflow, writer_admission
from wsflow.server import serve


def tree(root):
    root = Path(root)
    return {'files': {p.relative_to(root).as_posix(): p.read_bytes()
                      for p in root.rglob('*') if p.is_file()},
            'directories': sorted(p.relative_to(root).as_posix()
                                  for p in root.rglob('*') if p.is_dir())}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class WorkflowWriterTests(unittest.TestCase):
    def fixture(self):
        case = i7.Home()
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    @staticmethod
    def epoch(domain):
        return domain.observe().get('epoch', 0)

    @staticmethod
    def pause(arrived, release):
        arrived.set()
        if not release.wait(12):
            raise TimeoutError('owned workflow boundary hold expired')

    @contextmanager
    def worker(self, call, releases, patches):
        outcome = {}
        def run():
            try:
                outcome['result'] = call()
            except BaseException as error:
                outcome['error'] = error
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            thread = threading.Thread(target=run, daemon=True, name='owned-workflow-writer')
            thread.start()
            try:
                yield thread, outcome
            finally:
                for release in releases:
                    release.set()
                thread.join(20)
                self.assertFalse(thread.is_alive(), 'owned writer thread did not finish')
        if 'error' in outcome:
            raise outcome['error']

    def held(self, domain, epoch, thread, outcome):
        self.assertTrue(thread.is_alive())
        self.assertEqual(outcome, {})
        with self.assertRaises(writer_admission.WriterUnavailable) as caught:
            with domain.barrier(0):
                pass
        self.assertEqual(caught.exception.reason, 'busy')
        observed = domain.observe()
        self.assertIsNotNone(observed.get('active'))
        self.assertEqual(observed['epoch'], epoch + 1)
        return observed['active']

    def complete(self, domain, epoch):
        view = domain.observe()
        self.assertEqual(view.get('epoch', 0), epoch + 2)
        self.assertIsNone(view.get('active'))
        with domain.barrier(0) as lease:
            self.assertFalse(lease.view()['snapshot_ready'])

    def memo(self, case):
        memo = wfhome.read_memo(case.home, '261018')
        wfhome.write_memo(case.home, '261018', '\r\n- XXX-11: actual work\r\nPRIVATE\r\n', memo['sha'])
        path = Path(memo['path'])
        return path, workflow._memo(path.read_bytes())

    def batch_result(self, case, result, memo=None):
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(result['committed'])
        state = wfhome.read_state(case.home)
        marker = state['last_batch']
        self.assertEqual(state['watermark'], i7.WHEN.isoformat())
        self.assertEqual(marker['run_id'], result['run_id'])
        self.assertEqual([s['stage'] for s in result['stages']], list(workflow.STAGES))
        self.assertEqual(marker['aggregate_sha256'], sha(next((case.home/'.workflow/agg').glob('*.json')).read_bytes()))
        daily = case.home/'daily/261018.md'
        self.assertEqual(marker['daily_sha256'], sha(daily.read_bytes()))
        if memo is not None:
            self.assertEqual(workflow._memo(daily.read_bytes()), memo)
        self.assertEqual(json.loads((Path(result['run_dir'])/'receipt.json').read_bytes()), result)
        self.assertEqual(workflow.status(case.home)['latest']['run_id'], result['run_id'])

    def test_raw_three_roots_refuse_before_owner_effects_then_release(self):
        for kind in ('state', 'gitignore', 'workflow'):
            with self.subTest(kind=kind):
                case = self.fixture()
                if kind == 'gitignore':
                    (case.home/'.git').mkdir()
                domain = writer_admission.default_domain()
                if kind == 'state':
                    call = lambda: wfinit.write_state(case.home, {**case.state, 'csc_value': 'new-owned'})
                elif kind == 'gitignore':
                    call = lambda: wfinit.gitignore_apply(case.home)
                else:
                    call = case.run_batch
                before = tree(case.home)
                with domain.barrier(0):
                    with self.assertRaises(writer_admission.WriterUnavailable) as caught:
                        call()
                    self.assertEqual(caught.exception.reason, 'reentrant_activity')
                    self.assertEqual(tree(case.home), before)
                epoch = self.epoch(domain)
                result = call()
                if kind == 'state':
                    self.assertEqual(result, str(wfhome.state_path(case.home)))
                    self.assertEqual(wfhome.read_state(case.home)['csc_value'], 'new-owned')
                elif kind == 'gitignore':
                    self.assertEqual(result['added'], list(wfhome.GITIGNORE_NEEDED))
                else:
                    self.batch_result(case, result)
                self.complete(domain, epoch)

    def test_state_actual_write_through_response_preserves_progress_and_merge(self):
        case = self.fixture()
        case.state.update(roots={'owned': {'children': []}}, last_success='old-success',
                          last_batch={'run_id': 'owned'}, custom_owned='keep', override='old')
        case.save_state()
        expected = copy.deepcopy(case.state)
        new = {'csc_value': 'new-owned', 'roots': {}, 'watermark': 'replace-forbidden',
               'last_success': 'replace-forbidden', 'last_batch': {}, 'override': 'new'}
        expected.update(csc_value='new-owned', override='new')
        domain = writer_admission.default_domain(); epoch = self.epoch(domain)
        arrived, release = threading.Event(), threading.Event()
        original = fileio.atomic_write; state_path = wfhome.state_path(case.home)
        def write(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if Path(path) == state_path:
                self.pause(arrived, release)
            return value
        with self.worker(lambda: wfinit.write_state(case.home, new), [release],
                         [mock.patch.object(fileio, 'atomic_write', side_effect=write)]) as (thread, out):
            self.assertTrue(arrived.wait(8), out)
            self.assertEqual(wfhome.read_state(case.home), expected)
            self.held(domain, epoch, thread, out)
        self.assertEqual(out['result'], str(state_path))
        self.complete(domain, epoch)

        # Separate invocation: preserve the original atomic-write window above.
        # This seam is the pure Path-to-string response expression, not file I/O.
        epoch = self.epoch(domain)
        response_arrived, response_release = threading.Event(), threading.Event()
        committed, converted = {}, []
        builtin_str = str
        def completed_write(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if Path(path) == state_path:
                observed = domain.observe()
                committed.update(written=True, active=observed['active'], epoch=observed['epoch'])
            return value
        def response(value):
            rendered = builtin_str(value)
            if isinstance(value, Path) and value == state_path:
                converted.append(rendered)
                self.pause(response_arrived, response_release)
            return rendered
        with self.worker(lambda: wfinit.write_state(case.home, new), [response_release],
                         [mock.patch.object(fileio, 'atomic_write', side_effect=completed_write),
                          mock.patch.object(wfinit, 'str', side_effect=response, create=True)]) as (thread, out):
            self.assertTrue(response_arrived.wait(8), out)
            self.assertTrue(committed['written'])
            self.assertEqual(converted, [builtin_str(state_path)])
            self.assertEqual(wfhome.read_state(case.home), expected)
            active = self.held(domain, epoch, thread, out)
            self.assertEqual(active, committed['active'])
            self.assertEqual(committed['epoch'], epoch + 1)
        self.assertEqual(out['result'], builtin_str(state_path))
        self.complete(domain, epoch)
        print('WF65_STATE_RESPONSE ' + json.dumps({'window': 'pure_path_to_string_response',
              'real_atomic_write_completed': committed['written'], 'actual_builtin_str_executed': True,
              'same_root': active == committed['active'], 'epoch_delta': self.epoch(domain) - epoch}))

    def test_gitignore_actual_append_and_late_report_keep_root(self):
        case = self.fixture(); (case.home/'.git').mkdir()
        path = case.home/'.gitignore'; path.write_bytes(b'owned.log\n# keep LF\n')
        domain = writer_admission.default_domain(); epoch = self.epoch(domain)
        arrived, release = threading.Event(), threading.Event(); calls = []
        original = wfinit.gitignore_report
        def report(*args, **kwargs):
            value = original(*args, **kwargs); calls.append(value)
            if len(calls) == 2:
                self.pause(arrived, release)
            return value
        with self.worker(lambda: wfinit.gitignore_apply(case.home), [release],
                         [mock.patch.object(wfinit, 'gitignore_report', side_effect=report)]) as (thread, out):
            self.assertTrue(arrived.wait(8), out)
            self.assertEqual(calls[1]['missing'], [])
            self.assertTrue(path.read_text(encoding='utf-8').startswith('owned.log\n# keep LF\n'))
            expected = ('owned.log\n# keep LF\n\n# jira-worklog: 업무 기록은 소스가 아니다.\n' +
                        '\n'.join(wfhome.GITIGNORE_NEEDED) + '\n').replace('\n', os.linesep).encode('utf-8')
            self.assertEqual(path.read_bytes(), expected)
            self.held(domain, epoch, thread, out)
        self.assertEqual(out['result']['added'], list(wfhome.GITIGNORE_NEEDED))
        self.complete(domain, epoch)

    def test_gitignore_both_noops_are_admitted_without_owner_changes(self):
        for repository in (False, True):
            with self.subTest(repository=repository):
                case = self.fixture()
                if repository:
                    (case.home/'.git').mkdir()
                    (case.home/'.gitignore').write_text('\n'.join(wfhome.GITIGNORE_NEEDED)+'\n', encoding='utf-8')
                domain = writer_admission.default_domain(); before = tree(case.home)
                with domain.barrier(0):
                    with self.assertRaises(writer_admission.WriterUnavailable):
                        wfinit.gitignore_apply(case.home)
                    self.assertEqual(tree(case.home), before)
                epoch = self.epoch(domain); result = wfinit.gitignore_apply(case.home)
                self.assertEqual(result['added'], [])
                self.assertEqual(result['applicable'], repository)
                self.assertEqual(tree(case.home), before)
                self.complete(domain, epoch)

    def test_real_batch_state_and_terminal_receipt_windows_cover_vendor_completion(self):
        case = self.fixture(); _, memo = self.memo(case)
        domain = writer_admission.default_domain(); epoch = self.epoch(domain)
        arrived = [threading.Event(), threading.Event()]
        releases = [threading.Event(), threading.Event()]
        original_write, original_receipt, original_command = fileio.atomic_write, workflow._write, workflow._command
        children = []; receipt_paths = []
        def command(directory, script, args):
            value = original_command(directory, script, args)
            self.assertTrue((Path(directory)/(script+'.txt')).exists())
            children.append(script)
            return value
        def write(path, *args, **kwargs):
            value = original_write(path, *args, **kwargs)
            if Path(path) == wfhome.state_path(case.home):
                self.pause(arrived[0], releases[0])
            return value
        def receipt(path, value):
            result = original_receipt(path, value)
            if Path(path).name == 'receipt.json' and value.get('status') == 'succeeded':
                receipt_paths.append(Path(path)); self.pause(arrived[1], releases[1])
            return result
        with self.worker(case.run_batch, releases,
                         [mock.patch.object(fileio, 'atomic_write', side_effect=write),
                          mock.patch.object(workflow, '_write', side_effect=receipt),
                          mock.patch.object(workflow, '_command', side_effect=command)]) as (thread, out):
            self.assertTrue(arrived[0].wait(10), out)
            self.assertEqual(children, ['aggregate.py', 'render_daily.py', 'loop_checks.py'])
            state = wfhome.read_state(case.home); marker = state['last_batch']
            stored = json.loads((Path(marker['run_dir'])/'receipt.json').read_bytes())
            self.assertEqual(stored['status'], 'executing'); self.assertFalse(stored['committed'])
            active = self.held(domain, epoch, thread, out)
            releases[0].set(); self.assertTrue(arrived[1].wait(8), out)
            stored = json.loads(receipt_paths[0].read_bytes())
            self.assertEqual(stored['status'], 'succeeded'); self.assertTrue(stored['committed'])
            self.assertEqual(stored['run_id'], marker['run_id'])
            self.assertEqual(self.held(domain, epoch, thread, out), active)
        self.batch_result(case, out['result'], memo)
        self.complete(domain, epoch)

    def test_real_failures_and_lost_final_receipt_keep_business_semantics(self):
        case = self.fixture(); domain = writer_admission.default_domain()
        original_state = wfhome.state_path(case.home).read_bytes(); original_atomic = fileio.atomic_write
        def denied(path, *args, **kwargs):
            if Path(path) == wfhome.state_path(case.home):
                raise OSError('owned state failure')
            return original_atomic(path, *args, **kwargs)
        epoch = self.epoch(domain)
        with mock.patch.object(fileio, 'atomic_write', side_effect=denied):
            with self.assertRaisesRegex(OSError, 'owned state failure'):
                wfinit.write_state(case.home, {'csc_value': 'new'})
        self.assertEqual(wfhome.state_path(case.home).read_bytes(), original_state)
        self.complete(domain, epoch)
        epoch = self.epoch(domain)
        def fail(phase):
            if phase == 'collect':
                raise OSError('owned collect failure')
        failed = case.run_batch(checkpoint=fail)
        self.assertEqual(failed['status'], 'failed'); self.assertFalse(failed['committed'])
        self.assertEqual(failed['failed_stage'], 'collect')
        self.assertEqual(wfhome.state_path(case.home).read_bytes(), original_state)
        self.complete(domain, epoch)
        arrived, release = threading.Event(), threading.Event(); observed = {}
        real_receipt = workflow._write; epoch = self.epoch(domain)
        def lost(path, value):
            if Path(path).name == 'receipt.json' and value.get('status') == 'succeeded' and value.get('committed'):
                observed.update(path=Path(path), run_id=value['run_id'])
                self.pause(arrived, release)
                raise OSError('owned final receipt lost')
            return real_receipt(path, value)
        with self.worker(case.run_batch, [release], [mock.patch.object(workflow, '_write', side_effect=lost)]) as (thread, out):
            self.assertTrue(arrived.wait(10), out)
            self.assertEqual(wfhome.read_state(case.home)['last_batch']['run_id'], observed['run_id'])
            self.assertEqual(json.loads(observed['path'].read_bytes())['status'], 'executing')
            self.held(domain, epoch, thread, out)
        self.assertEqual(out['result']['status'], 'succeeded'); self.assertTrue(out['result']['committed'])
        self.assertEqual(out['result']['receipt_warning'], '작업 기록 저장 실패 — state의 run marker로 결과를 확인하십시오')
        self.assertEqual(workflow.status(case.home)['committed']['run_id'], observed['run_id'])
        self.assertEqual(workflow.status(case.home)['latest']['status'], 'succeeded')
        self.complete(domain, epoch)

    def test_stock_http_setup_compound_keeps_one_root_through_final_detect(self):
        case = self.fixture(); (case.home/'.git').mkdir()
        registry = case.base/'registry/projects.json'
        i7.put(registry, json.dumps({'roots': [], 'projects': [case.project]}))
        provider_attempts = []
        def forbidden(*args, **kwargs):
            provider_attempts.append(True); raise AssertionError('no real provider allowed')
        server = serve(registry, '127.0.0.1', 0, google_transport=forbidden)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def api(action, body):
            request = urllib.request.Request(
                f'http://127.0.0.1:{server.server_address[1]}/api/projects/p/jira/init/{action}',
                data=json.dumps(body).encode('utf-8'), headers={'X-Auth-Token': server.wsflow_token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=15) as response:
                self.assertEqual(response.status, 200); return json.loads(response.read())
        try:
            self.assertEqual(type(server.module_host).__name__, 'ModuleHost')
            body = {'cfg': {'project_key': 'XXX', 'csc_value': 'HTTP-owned'}, 'fix_gitignore': True}
            preview = api('preview', body)
            self.assertTrue(preview['gitignore']['missing'])
            domain = writer_admission.default_domain(); epoch = self.epoch(domain)
            arrived, release = threading.Event(), threading.Event(); state_done = threading.Event(); seen = []
            real_ignore, real_state, real_detect = wfinit.gitignore_apply, wfinit.write_state, wfhome.detect
            def ignore(*args, **kwargs):
                result = real_ignore(*args, **kwargs); seen.append(('gitignore', domain.observe()))
                return result
            def state(*args, **kwargs):
                result = real_state(*args, **kwargs); seen.append(('state', domain.observe())); state_done.set()
                return result
            def detect(*args, **kwargs):
                result = real_detect(*args, **kwargs)
                if state_done.is_set():
                    seen.append(('detect', domain.observe())); self.pause(arrived, release)
                return result
            with self.worker(lambda: api('apply', body), [release],
                             [mock.patch.object(wfinit, 'gitignore_apply', side_effect=ignore),
                              mock.patch.object(wfinit, 'write_state', side_effect=state),
                              mock.patch.object(wfhome, 'detect', side_effect=detect)]) as (worker, out):
                self.assertTrue(arrived.wait(10), out)
                self.assertEqual([label for label, _ in seen], ['gitignore', 'state', 'detect'])
                active = self.held(domain, epoch, worker, out)
                for _, observation in seen:
                    self.assertEqual(observation['active'], active)
                    self.assertEqual(observation['epoch'], epoch + 1)
            self.assertEqual(out['result']['written'], str(wfhome.state_path(case.home)))
            self.assertEqual(out['result']['gitignore']['added'], list(wfhome.GITIGNORE_NEEDED))
            self.assertTrue(out['result']['jira']['enabled']); self.assertTrue(out['result']['jira']['gitignore_ok'])
            self.assertEqual(out['result']['jira']['scope']['csc_value'], 'HTTP-owned')
            self.assertEqual(provider_attempts, [])
            self.complete(domain, epoch)
            print('WF65_HTTP ' + json.dumps({'steps': [label for label, _ in seen], 'same_root': True, 'epoch_delta': 2, 'provider_attempts': 0}))
        finally:
            server.shutdown(); thread.join(5); server.server_close()
            self.assertFalse(thread.is_alive())

    def test_nested_current_domain_and_actual_readers_do_not_add_roots(self):
        case = self.fixture(); (case.home/'.git').mkdir()
        domain = writer_admission.default_domain(); epoch = self.epoch(domain)
        alternative = case.base/'alternative-data'
        with domain.writing():
            active = domain.observe()
            with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(alternative)}):
                wfinit.write_state(case.home, {'custom_owned': 'nested'})
                wfinit.gitignore_apply(case.home)
                result = case.run_batch()
                self.batch_result(case, result)
                self.assertFalse(alternative.exists())
                self.assertEqual(domain.observe()['epoch'], active['epoch'])
                self.assertEqual(domain.observe()['active'], active['active'])
        self.complete(domain, epoch)
        before = tree(case.base), domain.observe()
        wfinit.preview(case.home, wfhome.read_state(case.home)); wfinit.gitignore_report(case.home)
        workflow.status(case.home); workflow.overview([case.project], '2026-10-18')
        self.assertEqual((tree(case.base), domain.observe()), before)

    def test_owned_process_raw_batch_refuses_default_budget_then_completes(self):
        case = self.fixture(); domain = writer_admission.default_domain()
        command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--workflow-writer-actor', str(case.home)]
        observations = []; owned_children = []
        def stop(child):
            if child.poll() is not None:
                return
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            else:
                os.killpg(child.pid, signal.SIGKILL)
        def launch():
            child = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
                                     start_new_session=os.name != 'nt', env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            try:
                stdout, stderr = child.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                stop(child); stdout, stderr = child.communicate(timeout=5)
                self.fail('owned batch child watchdog expired: ' + stderr)
            finally:
                if child.poll() is None:
                    stop(child); child.communicate(timeout=5)
            self.assertIsNotNone(child.returncode); self.assertEqual(child.returncode, 0, stderr)
            owned_children.append({'pid': child.pid, 'returncode': child.returncode, 'reaped': child.returncode is not None})
            value = json.loads(stdout.strip()); observations.append(value); return value
        before = tree(case.home)
        with domain.barrier(0):
            refused = launch()
            self.assertEqual(refused['status'], 'refused')
            self.assertEqual(refused.get('reason'), 'busy')
            self.assertLess(refused['elapsed'], 7.5)
            self.assertEqual(tree(case.home), before)
        epoch = self.epoch(domain); succeeded = launch()
        self.assertEqual(succeeded['status'], 'succeeded'); self.assertTrue(succeeded['committed'])
        self.assertEqual(succeeded['vendor_children_returned'], 3)
        self.assertEqual(wfhome.read_state(case.home)['last_batch']['run_id'], succeeded['run_id'])
        self.complete(domain, epoch)
        print('WF65_PROCESS ' + json.dumps({'observations': observations, 'owned_children': owned_children,
              'owned_children_reaped': sum(child['reaped'] for child in owned_children),
              'residual': sum(not child['reaped'] for child in owned_children),
              'residual_scope': 'owned direct actors after communicate and returncode verification'}))


def actor(home):
    started = time.monotonic(); children = []; original = workflow._command
    def command(*args, **kwargs):
        value = original(*args, **kwargs); children.append(args[1]); return value
    try:
        with mock.patch.object(workflow, '_command', side_effect=command):
            value = workflow.run(Path(home), i7.CREDS, send=i7.Provider(), clock=lambda: i7.WHEN)
        result = {key: value[key] for key in ('status', 'committed', 'run_id')}
    except writer_admission.WriterUnavailable as error:
        result = {'status': 'refused', 'reason': error.reason}
    result.update(elapsed=round(time.monotonic()-started, 3), vendor_children_returned=len(children))
    print(json.dumps(result))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--workflow-writer-actor']:
        actor(sys.argv[2])
    else:
        unittest.main()
