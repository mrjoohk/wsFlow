"""Acceptance tests for source isolation and real failure gates (no live provider)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('ci_runner', SOURCE / 'tools/ci/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow ci acceptance ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        for name in runner.ROOTS:
            (self.root / name).mkdir()
        for name in ('run.py', 'run.cmd'):
            (self.root / name).write_bytes(b'# source\r\n')
        (self.root / 'static/index.html').write_text('synthetic html', encoding='utf-8')
        for name in ('browser-baseline.json',):
            shutil.copyfile(SOURCE / 'ci' / name, self.root / 'ci' / name)
        runner.save(self.root / 'ci/selection.json', {'specs': [{'stage': 'i0', 'spec': 'ci/spec.json', 'count': 1}]})
        runner.save(self.root / 'evidence_pack/metrics.yaml', [dict(metric_id='fixture', value=1, threshold=1,
                    comparison='gte', status='PASS', unit='cases', mode='PLANNED', source='run.py')])
        for name in ('runner.py', 'mutation_runner.py'):
            target = self.root / 'tools/ci' / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(SOURCE / 'tools/ci' / name, target)
        runner.seal(self.root)

    def test_changed_source_rejected(self):
        (self.root / 'run.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            runner.verify(self.root)

    def test_added_and_missing_sources_rejected(self):
        extra = self.root / 'wsflow/unexpected.py'
        extra.write_text('extra')
        with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
            runner.verify(self.root)
        extra.unlink()
        (self.root / 'run.cmd').unlink()
        with self.assertRaises(ValueError):
            runner.verify(self.root)

    def test_line_endings_portable_raw_archive_preserved(self):
        before = runner.verify(self.root)['fingerprint']
        (self.root / 'run.py').write_bytes(b'# source\n')
        self.assertEqual(runner.verify(self.root)['fingerprint'], before)
        result = runner.freeze(self.root, self.base / 'frozen')
        self.assertEqual((self.base / 'frozen/source/run.cmd').read_bytes(), b'# source\r\n')
        self.assertEqual(result['fingerprint'], before)
        with zipfile.ZipFile(self.base / 'frozen/source.zip') as z:
            self.assertEqual(set(z.namelist()), set(result['raw_files']))
            for name, value in result['raw_files'].items():
                self.assertEqual(runner.digest(z.read(name)), value)
            self.assertEqual(z.read('run.cmd'), b'# source\r\n')

    def test_freeze_excludes_user_settings_and_business_files(self):
        for name in ('.codex/token.txt', '.claude/user.txt', 'daily/secret.txt', '.workflow/state.json'):
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_text('synthetic sentinel')
        runner.freeze(self.root, self.base / 'frozen')
        names = [p.relative_to(self.base / 'frozen/source').as_posix() for p in (self.base / 'frozen/source').rglob('*') if p.is_file()]
        self.assertFalse(any(n.startswith(('.codex/', '.claude/', 'daily/', '.workflow/')) for n in names))

    def test_output_cannot_contaminate_source_or_overwrite(self):
        for target in [self.root, self.root / 'wsflow/new', self.root / 'ci/new', self.root / 'tests/new']:
            with self.assertRaisesRegex(ValueError, 'new destination'):
                runner.freeze(self.root, target)
        destination = self.base / 'existing'
        destination.mkdir()
        with self.assertRaises(ValueError):
            runner.freeze(self.root, destination)

    def test_three_failure_modes_execute_real_cli(self):
        env = dict(__import__('os').environ)
        env['PATH'] = str(Path(sys.executable).parent) + __import__('os').pathsep + env.get('PATH', '')
        for mode in ('source_mismatch', 'test_failure', 'mutant_survived'):
            with self.subTest(mode=mode):
                output = self.base / mode
                cp = subprocess.run([sys.executable, str(self.root / 'tools/ci/runner.py'), 'gate',
                                     '--root', str(self.root), '--out', str(output), '--mode', mode],
                                    capture_output=True, text=True, env=env, timeout=90)
                self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
                evidence = runner.read(output / 'gate.json')
                self.assertTrue(evidence['rejected'])
                self.assertEqual(evidence['source'], runner.verify(self.root)['fingerprint'])
                self.assertEqual(evidence['origin'], 'controlled gate fixture')

    def test_g4_requires_nonempty_unique_and_existing_provenance(self):
        path = self.root / 'evidence_pack/metrics.yaml'
        runner.save(path, [])
        with self.assertRaisesRegex(ValueError, 'G4'):
            runner.g4(self.root)
        metric = dict(metric_id='example', value=1, threshold=1, comparison='gte', status='PASS',
                      unit='cases', mode='PLANNED', source='run.py')
        runner.save(path, [metric]);self.assertEqual(runner.g4(self.root)['status'], 'PASS')
        for data in ([metric, metric], [{**metric, 'source': '../absent'}], [{**metric, 'value': True}]):
            runner.save(path, data)
            with self.assertRaisesRegex(ValueError, 'G4'):
                runner.g4(self.root)
        for key in ('value', 'threshold'):
            for value in (float('nan'), float('inf'), -float('inf')):
                runner.save(path, [{**metric, key: value}])
                with self.assertRaisesRegex(ValueError, 'G4'):
                    runner.g4(self.root)

    def test_fingerprint_only_tamper_is_rejected(self):
        d = runner.read(self.root / runner.MANIFEST);d['fingerprint'] = '0' * 64
        runner.save(self.root / runner.MANIFEST, d)
        with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
            runner.verify(self.root)

    def test_windows_junction_source_is_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows junction acceptance')
        target = self.base / 'external';target.mkdir()
        junction = self.root / 'wsflow/escape'
        cp = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)], capture_output=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        with self.assertRaisesRegex(ValueError, 'linked source'):
            runner.files(self.root)

    def test_credentials_are_removed_from_child_environment(self):
        with mock.patch.dict(os.environ, {'JIRA_API_TOKEN': 'synthetic-only', 'GOOGLE_SECRET': 'synthetic-only',
                                         'GITHUB_TOKEN': 'synthetic-only', 'PYTHONPATH': 'synthetic-only'}):
            output = self.base / 'environment';output.mkdir()
            env = runner.clean_env(output, self.base / 'logs')
            cp = subprocess.run([sys.executable, '-c', 'import os;print(any(k in os.environ for k in ("JIRA_API_TOKEN","GOOGLE_SECRET","GITHUB_TOKEN","PYTHONPATH")))'],
                                env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(cp.returncode, 0);self.assertEqual(cp.stdout.strip(), 'False')
            self.assertTrue(Path(env['APPDATA']).is_relative_to(output))
            self.assertEqual(Path(env['GIT_CONFIG_GLOBAL']).read_text(), '')

    def test_owned_child_timeout_and_interrupt_are_cleaned(self):
        # Child is finite even if the watchdog regresses; the outer test process also has a CI deadline.
        r = runner.command([sys.executable, '-c', 'import time;time.sleep(1.5)'], self.root, self.base / 'timeout.log', timeout=.05)
        self.assertEqual(r['exit_code'], 124)
        real = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            if children:
                return real(*args, **kwargs)
            child = real(*args, **kwargs);children.append(child)
            wait = child.wait
            def interrupted(timeout=None):
                child.wait = wait
                raise KeyboardInterrupt()
            child.wait = interrupted
            return child
        with mock.patch.object(runner.subprocess, 'Popen', side_effect=launch):
            with self.assertRaises(KeyboardInterrupt):
                runner.command([sys.executable, '-c', 'import time;time.sleep(10)'], self.root, self.base / 'interrupt.log')
        self.assertIsNotNone(children[0].poll())

    def test_run_evidence_contracts_reject_bad_results(self):
        baseline = runner.read(self.root / 'ci/browser-baseline.json')
        for problem, step in [('runtime', 'unit'), ('command', 'unit'), ('unit', 'unit'), ('browser', 'browser'),
                              ('cases', 'browser'), ('backend', 'browser'), ('mutation', 'mutations'), ('source', 'unit'),
                              ('artifacts', 'unit'), ('valid', 'unit')]:
            with self.subTest(problem=problem):
                out = self.base / ('run-' + problem)
                def controlled(args, cwd, log, env=None, timeout=900):
                    log.parent.mkdir(parents=True, exist_ok=True);log.write_text('controlled fixture', encoding='utf-8')
                    if args[:2] == ['node', '--version']:
                        self.assertEqual(timeout, 30);self.assertIsNotNone(env)
                        log.write_text('v24.14.0', encoding='utf-8')
                    elif args[:2] == ['node', '-e']:
                        runner.save(log, {'playwright': '1.62.1', 'browser': 'synthetic'})
                    elif 'tests/run_tests.py' in args:
                        runner.save(Path(env['WSFLOW_RUN_EVIDENCE_DIR']) / 'unit_evidence.json',
                                    {'ok': True, 'tests_run': 0 if problem == 'unit' else 415,
                                     'failures': 0, 'errors': 0, 'skipped': 0})
                        if problem == 'source':
                            (self.root / 'run.py').write_text('changed by controlled source probe')
                        if problem == 'artifacts':
                            (out / 'artifacts').mkdir()
                        if problem == 'command':
                            return {'exit_code': 1, 'log': str(log)}
                    elif len(args) > 1 and args[1].endswith('_browser.cjs'):
                        n = int(args[1][7]);cases = list(baseline[n-1]['cases'])
                        if problem == 'cases':
                            cases[0] = 'replacement with same count'
                        runner.save(Path(env['WSFLOW_BROWSER_OUTPUT']) / 'evidence.json',
                                    {'cases': cases, 'errors': ['controlled error'] if problem == 'browser' else [],
                                     'source_sha256': runner.digest((self.root / 'static/index.html').read_bytes()),
                                     'backend_hashes': {'missing.py': '0' * 64} if problem == 'backend' else {}})
                    elif 'tools/ci/mutation_runner.py' in args:
                        runner.save(Path(args[args.index('--out')+1]), {'results': {'only': {'status': 'killed', 'verified': False}}})
                    return {'exit_code': 0, 'log': str(log)}
                with mock.patch.object(runner, 'command', side_effect=controlled), \
                     mock.patch.object(runner.sys, 'prefix', 'controlled-venv'), \
                     mock.patch.object(runner.site, 'ENABLE_USER_SITE', False):
                    result = runner.run(self.root, out, '0.0.0' if problem == 'runtime' else runner.platform.python_version(), step, 1)
                self.assertEqual(result['status'], 'PASS' if problem == 'valid' else 'FAIL', result)
                self.assertTrue((out / 'result.json').is_file())
                if problem == 'artifacts':
                    self.assertIn('artifact_error', result)
                if problem == 'source':
                    (self.root / 'run.py').write_bytes(b'# source\r\n')

    def test_mutation_profile_rejects_reused_and_all_skipped_proof(self):
        fixture = self.base / 'mutants';fixture.mkdir()
        (fixture / 'subject.py').write_text('VALUE = False\n', encoding='utf-8')
        test = fixture / 'test_subject.py'
        test.write_text('import unittest,subject\nclass Check(unittest.TestCase):\n def test_value(self):self.assertFalse(subject.VALUE)\n', encoding='utf-8')
        specfile = self.base / 'spec.json'
        runner.save(specfile, {'copy': ['subject.py', 'test_subject.py'], 'test': [sys.executable, '-m', 'unittest', 'discover'],
                              'mutants': [{'id': 'one', 'name': 'one', 'edits': [{'file': 'subject.py', 'old': 'VALUE = False', 'new': 'VALUE = True'}]}]})
        result = self.base / 'mutation.json'
        args = [sys.executable, str(SOURCE / 'tools/ci/mutation_runner.py'), str(specfile), '--root', str(fixture), '--out', str(result), '--strict']
        self.assertEqual(subprocess.run(args, capture_output=True, timeout=30).returncode, 0)
        for suffix in ([], ['--resume'], ['--summary']):
            cp = subprocess.run(args + suffix, capture_output=True, timeout=30)
            self.assertEqual(cp.returncode, 2);self.assertIn(b'fresh --out', cp.stderr)
        test.write_text("import unittest,subject\n@unittest.skip('controlled disabled test')\nclass Check(unittest.TestCase):\n def test_value(self):self.assertFalse(subject.VALUE)\n", encoding='utf-8')
        args[args.index('--out')+1] = str(self.base / 'all-skipped.json')
        cp = subprocess.run(args, capture_output=True, timeout=30)
        self.assertEqual(cp.returncode, 2);self.assertIn(b'baseline ran no tests', cp.stderr + cp.stdout)


if __name__ == '__main__':
    unittest.main()
