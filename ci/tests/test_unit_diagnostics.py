"""Actual tiny unit children and producer artifacts for optional unit diagnostics."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('diagnostic_ci_runner', SOURCE / 'tools/ci/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
BASE_PYTHON = getattr(sys, '_base_executable', sys.executable)


class UnitDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow unit diagnostics ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        (self.root / 'tests').mkdir(parents=True)
        shutil.copyfile(SOURCE / 'tests/run_tests.py', self.root / 'tests/run_tests.py')
        self.evidence = self.base / 'completed'
        self.diagnostics = self.base / 'diagnostics'
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                        WSFLOW_RUN_EVIDENCE_DIR=str(self.evidence),
                        WSFLOW_DATA_DIR=str(self.base / 'userdata'))
        self.env.pop('WSFLOW_UNIT_DIAGNOSTICS', None)

    def fixture(self, body):
        (self.root / 'tests/test_tiny.py').write_text(body, encoding='utf-8')

    def launch(self, enabled=True):
        env = dict(self.env)
        if enabled:
            env['WSFLOW_UNIT_DIAGNOSTICS'] = str(self.diagnostics)
        return subprocess.run([sys.executable, '-B', '-X', 'utf8', 'tests/run_tests.py'],
                              cwd=self.root, env=env, capture_output=True, timeout=15)

    def events(self, folder=None):
        return [json.loads(line) for line in ((folder or self.diagnostics) / 'progress.jsonl').read_text().splitlines()]

    def test_opt_in_and_external_completed_evidence_keep_original_results(self):
        self.fixture('import unittest\nclass Tiny(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n')
        diagnostic_names = {'progress.jsonl', 'tracebacks.log'}
        before = {str(p.relative_to(self.base)) for p in self.base.rglob('*') if p.name in diagnostic_names}
        self.assertEqual(before, set())
        p = self.launch(enabled=False)
        after = {str(p.relative_to(self.base)) for p in self.base.rglob('*') if p.name in diagnostic_names}
        self.assertEqual(after, before)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(self.diagnostics.exists())
        files = list(self.evidence.glob('*_evidence.json'))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_bytes())
        self.assertEqual((data['tests_run'], data['failures'], data['errors'], data['skipped']), (1, 0, 0, 0))
        self.assertTrue(data['ok'])
        self.assertIn(b'Ran 1 test', p.stdout)

    def test_failure_error_skip_and_subtest_counters_match_completed_evidence(self):
        self.fixture('''import unittest,time,json
from pathlib import Path
class Tiny(unittest.TestCase):
 def test_a_ok(self):
  started=time.monotonic()
  deadline=started+.12
  while time.monotonic()<deadline: time.sleep(.005)
  Path('timing.json').write_text(json.dumps({'body_seconds':time.monotonic()-started}))
  self.assertTrue(True)
 def test_b_fail(self): self.fail('controlled failure')
 def test_c_error(self): raise ValueError('controlled error')
 @unittest.skip('controlled skip')
 def test_d_skip(self): pass
 def test_e_subtest(self):
  for value in (0,1):
   with self.subTest(value=value): self.assertEqual(value,0)
''')
        wall_before = time.time()
        monotonic_before = time.monotonic()
        p = self.launch()
        monotonic_after = time.monotonic()
        wall_after = time.time()
        self.assertEqual(p.returncode, 1)
        files = list(self.evidence.glob('*_evidence.json'))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_bytes())
        self.assertEqual((data['tests_run'], data['failures'], data['errors'], data['skipped']), (5, 2, 1, 1))
        self.assertFalse(data['ok'])
        events = self.events()
        self.assertEqual(events[0]['event'], 'suite_start')
        self.assertEqual(events[-1]['event'], 'suite_complete')
        starts = [v for v in events if v['event'] == 'start']
        stops = [v for v in events if v['event'] == 'stop']
        self.assertEqual(len(starts), 5)
        self.assertEqual([v['test'] for v in starts], [v['test'] for v in stops])
        self.assertEqual([v['tests_run'] for v in stops], list(range(1,6)))
        self.assertTrue(all(v['seconds'] >= 0 for v in stops))
        for key in ('tests_run', 'failures', 'errors', 'skipped'):
            self.assertEqual(events[-1][key], data[key])
            self.assertEqual(stops[-1][key], data[key])
        self.assertFalse(events[-1]['ok'])
        self.assertTrue((self.diagnostics / 'tracebacks.log').is_file())
        expected_ids = ['test_tiny.Tiny.test_' + name for name in ('a_ok', 'b_fail', 'c_error', 'd_skip', 'e_subtest')]
        self.assertEqual([v['test'] for v in starts], expected_ids)
        self.assertEqual([v['test'] for v in stops], expected_ids)
        self.assertEqual([v['tests_run'] for v in starts], list(range(1, 6)))
        self.assertEqual([(v['failures'], v['errors'], v['skipped']) for v in stops],
                         [(0, 0, 0), (1, 0, 0), (1, 1, 0), (1, 1, 1), (2, 1, 1)])
        self.assertTrue(all(wall_before <= v['at'] <= wall_after for v in events))
        elapsed = [v['elapsed_seconds'] for v in events]
        self.assertEqual(elapsed, sorted(elapsed))
        self.assertGreaterEqual(elapsed[0], 0)
        self.assertLessEqual(elapsed[-1], monotonic_after - monotonic_before + .01)
        body_seconds = json.loads((self.root/'timing.json').read_bytes())['body_seconds']
        self.assertGreaterEqual(body_seconds, .12)
        self.assertGreaterEqual(stops[0]['seconds'], body_seconds - .001)
        self.assertLessEqual(stops[0]['seconds'], monotonic_after - monotonic_before + .01)
        self.assertGreater(stops[0]['elapsed_seconds'] - starts[0]['elapsed_seconds'], .1)
        for start, stop in zip(starts, stops):
            self.assertAlmostEqual(stop['elapsed_seconds'] - start['elapsed_seconds'], stop['seconds'], delta=.01)

    def test_live_progress_survives_real_timeout_without_completion(self):
        self.fixture('import unittest,time\nclass Tiny(unittest.TestCase):\n def test_wait(self): time.sleep(5)\n')
        env = dict(self.env, WSFLOW_UNIT_DIAGNOSTICS=str(self.diagnostics))
        outcome = {}
        def call():
            try:
                outcome['result'] = runner.command([BASE_PYTHON, '-B', 'tests/run_tests.py'], self.root,
                    self.base / 'timeout.log', env, timeout=1.5)
            except BaseException as exc:
                outcome['error'] = exc
        thread = threading.Thread(target=call)
        thread.start()
        try:
            deadline = time.monotonic() + 1.3
            events = []
            while time.monotonic() < deadline:
                if (self.diagnostics / 'progress.jsonl').exists():
                    events = self.events()
                    if any(v['event'] == 'start' for v in events):
                        break
                time.sleep(.01)
            self.assertTrue(any(v['event'] == 'start' for v in events), events)
            self.assertTrue(thread.is_alive(), 'progress was only available after completion')
            self.assertFalse(any(v['event'] == 'suite_complete' for v in events))
        finally:
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', outcome)
        result = outcome['result']
        self.assertEqual(result['exit_code'], 124)
        self.assertTrue(result['process']['direct_child_reaped'])
        self.assertEqual(result['process']['termination']['reason'], 'timeout')
        self.assertFalse(any(v['event'] == 'suite_complete' for v in self.events()))
        self.assertEqual(list(self.evidence.glob('*_evidence.json')), [])

    def test_existing_progress_or_stack_file_is_not_overwritten(self):
        self.fixture('import unittest\nclass Tiny(unittest.TestCase):\n def test_ok(self): pass\n')
        for name in ('progress.jsonl', 'tracebacks.log'):
            with self.subTest(name=name):
                self.diagnostics = self.base / name.replace('.', '_')
                self.diagnostics.mkdir()
                sentinel = self.diagnostics / name
                sentinel.write_bytes(b'owned original sentinel')
                p = self.launch()
                self.assertNotEqual(p.returncode, 0)
                self.assertEqual(sentinel.read_bytes(), b'owned original sentinel')
                self.assertEqual(list(self.evidence.glob('*_evidence.json')), [])

    def test_recording_path_failure_is_not_reported_as_success(self):
        self.fixture('import unittest\nclass Tiny(unittest.TestCase):\n def test_ok(self): pass\n')
        self.diagnostics.write_bytes(b'owned path blocker')
        p = self.launch()
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.diagnostics.read_bytes(), b'owned path blocker')
        self.assertEqual(list(self.evidence.glob('*_evidence.json')), [])

    def test_discovery_interrupt_records_abort_without_complete_result(self):
        self.fixture("def load_tests(loader, tests, pattern):\n raise KeyboardInterrupt('owned discovery interruption')\n")
        p = self.launch()
        self.assertNotEqual(p.returncode, 0)
        events = self.events()
        self.assertEqual(events[0]['event'], 'suite_start')
        self.assertEqual(events[-1]['event'], 'suite_aborted')
        self.assertEqual(events[-1]['error_type'], 'KeyboardInterrupt')
        self.assertFalse(any(v['event'] == 'suite_complete' for v in events))
        self.assertEqual(list(self.evidence.glob('*_evidence.json')), [])

    def test_short_stack_timer_is_cancelled_and_files_closed_on_exception(self):
        script = '''import importlib.util,json,time,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('subject',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
p=Path(sys.argv[2])
cancelled=[];original_cancel=m.faulthandler.cancel_dump_traceback_later
def cancelled_timer():
 cancelled.append(True)
 return original_cancel()
m.faulthandler.cancel_dump_traceback_later=cancelled_timer
scheduled=[];original_schedule=m.faulthandler.dump_traceback_later
def scheduled_timer(interval, *args, **kwargs):
 scheduled.append({'interval':interval,'repeat':kwargs.get('repeat')})
 return original_schedule(interval,*args,**kwargs)
m.faulthandler.dump_traceback_later=scheduled_timer
with m._unit_diagnostics(p/'default'):
 pass
assert scheduled==[{'interval':120,'repeat':True}]
assert cancelled==[True]
cancelled.clear()
def wait_for_repeated_stack(folder):
 deadline=time.monotonic()+3
 while time.monotonic()<deadline:
  if (folder/'tracebacks.log').read_bytes().count(b'Timeout')>=2: return
  time.sleep(.01)
 raise AssertionError('two periodic stack dumps were not observed before deadline')
try:
 with m._unit_diagnostics(p, interval=.05):
  wait_for_repeated_stack(p)
  raise ValueError('owned context failure')
except ValueError: pass
assert cancelled==[True]
stack=p/'tracebacks.log';before=stack.read_bytes();time.sleep(.16)
assert before==stack.read_bytes() and b'Timeout' in before
assert before.count(b'Timeout')>=2
normal=p/'normal';cancelled.clear()
with m._unit_diagnostics(normal, interval=.05):
 wait_for_repeated_stack(normal)
assert cancelled==[True]
normal_before=(normal/'tracebacks.log').read_bytes()
deadline=time.monotonic()+.16
while time.monotonic()<deadline:
 assert (normal/'tracebacks.log').read_bytes()==normal_before
 time.sleep(.01)
assert normal_before.count(b'Timeout')>=2
for name in ('progress.jsonl','tracebacks.log'):
 original=normal/name;renamed=normal/(name+'.closed');original.rename(renamed);renamed.rename(original)
for name in ('progress.jsonl','tracebacks.log'):
 original=p/name;renamed=p/(name+'.closed');original.rename(renamed);renamed.rename(original)
print(json.dumps({'stack_bytes':len(before),'timer_stopped':True,'files_closed':True}))
'''
        p = subprocess.run([sys.executable, '-B', '-c', script, str(self.root/'tests/run_tests.py'), str(self.diagnostics)],
                           capture_output=True, env=self.env, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertTrue(result['timer_stopped'] and result['files_closed'])
        self.assertGreater(result['stack_bytes'], 0)
        self.assertEqual(self.events()[-1]['error_type'], 'ValueError')

    def test_owned_command_records_normal_exit_and_direct_reaping(self):
        log = self.base / 'normal.log'
        requested_timeout = 4.25
        result = runner.command([BASE_PYTHON, '-c', 'import os;print(os.getpid(),flush=True);raise SystemExit(7)'],
                                self.root, log, self.env, timeout=requested_timeout)
        self.assertEqual(result['exit_code'], 7)
        process = result['process']
        self.assertGreater(process['pid'], 0)
        self.assertEqual(process['pid'], int(log.read_text().strip()))
        self.assertEqual(process['timeout_seconds'], requested_timeout)
        self.assertEqual(process['returncode'], 7)
        self.assertTrue(process['direct_child_reaped'])
        self.assertFalse(process['descendants_verified'])
        self.assertIsNone(process['termination'])
        self.assertEqual(json.loads(log.with_suffix('.process.json').read_bytes()), process)

    def test_taskkill_denial_records_fallback_without_claiming_all_descendants(self):
        log = self.base / 'denied.log'
        real_popen = runner.subprocess.Popen
        def ready_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            deadline = time.monotonic() + 2
            try:
                while time.monotonic() < deadline:
                    if log.read_text().strip():
                        return child
                    if child.poll() is not None:
                        raise AssertionError('owned child exited before ready PID')
                    time.sleep(.01)
                raise AssertionError('owned child PID was not observed before deadline')
            except BaseException:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                raise
        requested_timeout = .1
        with mock.patch.object(runner.subprocess, 'run', side_effect=OSError('owned taskkill denial')), \
             mock.patch.object(runner.subprocess, 'Popen', side_effect=ready_child):
            result = runner.command([BASE_PYTHON, '-c', 'import os,time;print(os.getpid(),flush=True);time.sleep(5)'],
                                    self.root, log, self.env, timeout=requested_timeout)
        self.assertEqual(result['exit_code'], 124)
        self.assertLess(result['seconds'], 3)
        process = result['process']
        self.assertEqual(process['pid'], int(log.read_text().strip()))
        self.assertEqual(process['timeout_seconds'], requested_timeout)
        self.assertTrue(process['direct_child_reaped'])
        self.assertFalse(process['descendants_verified'])
        if os.name == 'nt':
            self.assertEqual(process['termination']['tree_stop_error_type'], 'OSError')
            self.assertTrue(process['termination']['owned_handle_fallback'])
        self.assertEqual(json.loads(log.with_suffix('.process.json').read_bytes()), process)

    def test_actual_runner_preserves_partial_unit_diagnostics_on_timeout(self):
        self.fixture('import unittest,time\nclass Tiny(unittest.TestCase):\n def test_wait(self): time.sleep(5)\n')
        for name in runner.ROOTS:
            (self.root / name).mkdir(exist_ok=True)
        for name in ('run.py', 'run.cmd'):
            (self.root / name).write_text('# owned source\n')
        runner.save(self.root/'ci/selection.json', {'specs': []})
        runner.save(self.root/'evidence_pack/metrics.yaml', [dict(metric_id='tiny', value=1, threshold=1,
            comparison='eq', status='PASS', unit='test', source='run.py', mode='PLANNED')])
        manifest = runner.seal(self.root)
        real_command = runner.command
        def bounded(args, cwd, log, env=None, timeout=900):
            if args[-1] == 'tests/run_tests.py':
                args = [BASE_PYTHON, *args[1:]]
                timeout = 1.5  # This finite diagnostic fixture alone; product default stays900.
            return real_command(args, cwd, log, env, timeout)
        ambient = self.base / 'ambient-sentinel'
        ambient.mkdir();(ambient/'sentinel').write_bytes(b'unchanged')
        with mock.patch.dict(os.environ, {'WSFLOW_UNIT_DIAGNOSTICS':str(ambient)}), \
             mock.patch.object(runner, 'command', side_effect=bounded):
            clean_output = self.base/'clean-environment'
            clean_output.mkdir()
            cleaned = runner.clean_env(clean_output, self.base/'clean-logs')
            environment_log = self.base/'non-unit-environment.log'
            environment_probe = real_command([BASE_PYTHON, '-c',
                "import json,os;print(json.dumps({'present':'WSFLOW_UNIT_DIAGNOSTICS' in os.environ}))"],
                self.root, environment_log, cleaned, timeout=5)
            self.assertEqual(environment_probe['exit_code'], 0)
            self.assertFalse(json.loads(environment_log.read_bytes())['present'])
            self.assertNotIn('WSFLOW_UNIT_DIAGNOSTICS', cleaned)
            result = runner.run(self.root, self.base/'runner-output', __import__('platform').python_version(), 'unit', 1)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['source'], manifest['fingerprint'])
        self.assertEqual(result['steps'][-1]['exit_code'], 124)
        self.assertTrue(result['steps'][-1]['process']['direct_child_reaped'])
        output = self.base/'runner-output/artifacts/unit_diagnostics'
        events = self.events(output)
        self.assertTrue(any(v['event'] == 'start' for v in events))
        self.assertFalse(any(v['event'] == 'suite_complete' for v in events))
        self.assertTrue((output/'tracebacks.log').is_file())
        self.assertEqual(list((self.base/'runner-output/artifacts/unit').glob('*_evidence.json')), [])
        self.assertEqual(sorted(p.name for p in ambient.iterdir()), ['sentinel'])
        self.assertEqual((ambient/'sentinel').read_bytes(), b'unchanged')
        self.assertEqual(runner.verify(self.root)['fingerprint'], manifest['fingerprint'])


if __name__ == '__main__':
    unittest.main()
