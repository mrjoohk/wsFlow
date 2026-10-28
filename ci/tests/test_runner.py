"""Acceptance tests for source isolation and real failure gates (no live provider)."""
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile
import module_fixture

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
        module_fixture.prepare_inputs(runner, self.root, SOURCE)
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

    def test_g4_accepts_canonical_equivalent_root(self):
        equivalent = self.root / '..' / self.root.name
        self.assertEqual(runner.g4(equivalent)['status'], 'PASS')

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
                                         'GITHUB_TOKEN': 'synthetic-only', 'PYTHONPATH': 'synthetic-only',
                                         'TMPDIR': str(self.root / '..' / self.root.name)}), \
             mock.patch.object(runner.tempfile, 'gettempdir', return_value=str(self.root / '..' / self.root.name)):
            output = self.base / 'environment';output.mkdir()
            env = runner.clean_env(output, self.base / 'logs')
            cp = subprocess.run([sys.executable, '-c', 'import os;print(any(k in os.environ for k in ("JIRA_API_TOKEN","GOOGLE_SECRET","GITHUB_TOKEN","PYTHONPATH")))'],
                                env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(cp.returncode, 0);self.assertEqual(cp.stdout.strip(), 'False')
            self.assertTrue(Path(env['APPDATA']).is_relative_to(output))
            self.assertEqual(Path(env['GIT_CONFIG_GLOBAL']).read_text(), '')
            cp = subprocess.run([sys.executable, '-c', 'import json,os,tempfile;print(json.dumps([os.environ[k] for k in ("TEMP","TMP","TMPDIR")]+[tempfile.gettempdir()]))'],
                                env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(cp.returncode, 0);self.assertEqual(json.loads(cp.stdout), [str(self.root.resolve())] * 4)

    def test_owned_child_timeout_and_interrupt_are_cleaned(self):
        # Child is finite even if the watchdog regresses; the outer test process also has a CI deadline.
        # Use the real base interpreter to test the owned Popen handle, independently of a venv redirector.
        executable = getattr(sys, '_base_executable', sys.executable)
        with mock.patch.object(runner.subprocess, 'run', side_effect=OSError('controlled taskkill denial')):
            r = runner.command([executable, '-c', 'import time;time.sleep(4)'], self.root, self.base / 'timeout.log', timeout=.05)
        self.assertEqual(r['exit_code'], 124)
        self.assertLess(r['seconds'], 3)
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
        start = time.monotonic()
        with mock.patch.object(runner.subprocess, 'Popen', side_effect=launch), \
             mock.patch.object(runner.subprocess, 'run', side_effect=OSError('controlled taskkill denial')):
            with self.assertRaises(KeyboardInterrupt):
                runner.command([executable, '-c', 'import time;time.sleep(4)'], self.root, self.base / 'interrupt.log')
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNotNone(children[0].poll())

    def test_pinned_python_dependency_hash_is_complete(self):
        line = (SOURCE / 'ci/python-requirements.txt').read_text(encoding='utf-8').strip()
        package, digest = line.split(' --hash=sha256:')
        self.assertEqual(package, 'tzdata==2025.3')
        self.assertIsNotNone(re.fullmatch('[0-9a-f]{64}', digest))

    def test_ambient_output_paths_and_workflow_home_are_not_inherited(self):
        values = {key:'outside-sentinel' for key in (*runner.OUTPUT_ENV, 'WF_HOME', 'WSFLOW_PYTHON')}
        (self.base/'ambient').mkdir()
        with mock.patch.dict(os.environ, values):
            env = runner.clean_env(self.base/'ambient', self.base/'logs')
        self.assertNotIn('WSFLOW_BROWSER_OUTPUT', env)
        self.assertNotIn('WSFLOW_GRAPH_EVIDENCE', env)
        self.assertNotIn('WF_HOME', env)
        self.assertNotIn('WSFLOW_PYTHON', env)
        self.assertNotIn('WSFLOW_MODULES_BROWSER_BASE', env)
        for key in ('WSFLOW_RUN_EVIDENCE_DIR','WSFLOW_TEST_EVIDENCE_DIR'):
            self.assertNotEqual(env[key], 'outside-sentinel')

    def test_browser_snapshot_is_fresh_and_does_not_follow_later_writer(self):
        source=self.base/'browser';source.mkdir()
        runner.save(source/'evidence.json',{'cases':['original']})
        target=self.base/'sealed'
        hashes=runner.preserve_browser(source,target)
        runner.save(source/'evidence.json',{'cases':['foreign']})
        self.assertEqual(runner.tree_hashes(target),hashes)
        self.assertNotEqual(runner.tree_hashes(source),hashes)
        with self.assertRaises(FileExistsError):runner.preserve_browser(source,target)

    def test_browser_copy_race_is_rejected(self):
        source=self.base/'copy-race';source.mkdir();(source/'evidence.json').write_text('original')
        copy=runner.shutil.copytree
        def raced(src,dst):
            copy(src,dst);(src/'evidence.json').write_text('late change')
        with mock.patch.object(runner.shutil,'copytree',side_effect=raced):
            with self.assertRaisesRegex(ValueError,'changed during copy'):
                runner.preserve_browser(source,self.base/'copy-target')

    def test_browser_target_copy_race_is_rejected(self):
        source=self.base/'target-race';source.mkdir();(source/'evidence.json').write_text('original')
        copy=runner.shutil.copytree
        def raced(src,dst):
            copy(src,dst);(dst/'evidence.json').write_text('changed target')
        with mock.patch.object(runner.shutil,'copytree',side_effect=raced):
            with self.assertRaisesRegex(ValueError,'changed during copy'):
                runner.preserve_browser(source,self.base/'target-changed')

    def test_real_nested_mutation_writer_cannot_replace_normal_browser_evidence(self):
        self.run_output_isolation('normal')

    def test_late_raw_writer_is_rejected_and_original_snapshot_survives(self):
        self.run_output_isolation('raw')

    def test_late_snapshot_writer_is_rejected(self):
        self.run_output_isolation('sealed')

    def test_validation_to_snapshot_gap_cannot_seal_foreign_bytes(self):
        self.run_output_isolation('validation_gap')

    def test_late_module_raw_writer_is_rejected(self):
        self.run_output_isolation('module_raw')

    def test_late_module_snapshot_writer_is_rejected(self):
        self.run_output_isolation('module_sealed')

    def run_output_isolation(self, fault):
        baseline=runner.read(self.root/'ci/browser-baseline.json')
        # Actual mutation_runner descendants write through inherited environment,
        # exactly as mixed Python/browser specs do. All files/data are synthetic.
        (self.root/'subject.py').write_text('VALUE = False\n')
        (self.root/'test_subject.py').write_text(
            'import os,unittest\nfrom pathlib import Path\nimport subject\n'
            'class Check(unittest.TestCase):\n def test_value(self):\n'
            '  p=Path(os.environ.get("WSFLOW_BROWSER_OUTPUT","private-output"));p.mkdir(exist_ok=True,parents=True)\n'
            '  (p/"evidence.json").write_text("foreign writer")\n'
            '  for k,n in [("WSFLOW_RUN_EVIDENCE_DIR","unit_evidence.json"),("WSFLOW_TEST_EVIDENCE_DIR","owner.json")]:\n'
            '   if os.environ.get(k):\n'
            '    d=Path(os.environ[k]);d.mkdir(parents=True,exist_ok=True);(d/n).write_text("foreign writer")\n'
            '  self.assertFalse(subject.VALUE)\n')
        specfile=self.root/'ci/spec.json'
        runner.save(specfile,{'copy':['subject.py','test_subject.py'],
            'test':[sys.executable,'-m','unittest','discover'],
            'mutants':[{'id':'one','name':'one','edits':[{'file':'subject.py','old':'VALUE = False','new':'VALUE = True'}]}]})
        runner.save(self.root/'ci/selection.json',{'specs':[{'stage':'v2_fixes','spec':'ci/spec.json','count':1}]})
        (self.root/runner.MANIFEST).unlink();runner.seal(self.root);out=self.base/('isolation-'+fault)
        original=runner.command;normal_folders=[];module_folders=[];original_read=runner.read
        def read_with_race(path):
            result=original_read(path)
            if fault=='validation_gap' and Path(path).name=='evidence.json' and Path(path).parent.name=='i7':
                (normal_folders[-1]/'evidence.json').write_text('foreign writer')
            return result
        def dispatch(args,cwd,log,env=None,timeout=900):
            log.parent.mkdir(parents=True,exist_ok=True);log.write_text('synthetic stage\n')
            if args[:2]==['node','--version']:log.write_text('v24.14.0')
            elif args[:2]==['node','-e']:runner.save(log,{'browser':'synthetic','playwright':'1.62.1'})
            elif 'tests/run_tests.py' in args:
                runner.save(Path(env['WSFLOW_RUN_EVIDENCE_DIR'])/'unit_evidence.json',
                            dict(ok=True,tests_run=415,failures=0,errors=0,skipped=0))
                runner.save(Path(env['WSFLOW_TEST_EVIDENCE_DIR'])/'owner.json',{'owner':'unit'})
            elif args[:2]==['node','tests/modules_browser.cjs']:
                self.assertEqual(env['WSFLOW_PYTHON'],runner.sys.executable)
                folder=Path(env['WSFLOW_BROWSER_OUTPUT']);module_folders.append(folder)
                module_fixture.snapshot(runner,self.root,folder)
            elif len(args)>1 and args[1].endswith('_browser.cjs'):
                n=int(args[1][7]);folder=Path(env['WSFLOW_BROWSER_OUTPUT']);normal_folders.append(folder)
                data={'cases':baseline[n-1]['cases'],'errors':[],
                      'source_sha256':runner.digest((self.root/'static/index.html').read_bytes())}
                program='import os,json;from pathlib import Path;p=Path(os.environ["WSFLOW_BROWSER_OUTPUT"]);p.mkdir(parents=True);(p/"evidence.json").write_text('+repr(json.dumps(data))+')'
                return original([sys.executable,'-c',program],cwd,log,env,10)
            elif 'tools/ci/mutation_runner.py' in args:
                actual=list(args);actual[3]=str(SOURCE/'tools/ci/mutation_runner.py')
                result=original(actual,cwd,log,env,30)
                if fault in ('raw','sealed'):
                    folder=normal_folders[-1] if fault=='raw' else out/'artifacts/browser/i7'
                    (folder/'evidence.json').write_text('foreign writer')
                if fault in ('module_raw','module_sealed'):
                    folder=module_folders[-1] if fault=='module_raw' else out/'artifacts/browser/modules'
                    (folder/'result.json').write_text('foreign writer')
                return result
            return dict(exit_code=0,log=str(log))
        with mock.patch.object(runner,'command',side_effect=dispatch), \
             mock.patch.object(runner,'read',side_effect=read_with_race), \
             mock.patch.object(runner,'gates',return_value={}), \
             mock.patch.object(runner.sys,'prefix','controlled-venv'), \
             mock.patch.object(runner.site,'ENABLE_USER_SITE',False):
            result=runner.run(self.root,out,runner.platform.python_version(),'all',4)
        self.assertEqual(result['status'],'PASS' if fault=='normal' else 'FAIL',result)
        mutation=runner.read(out/'v2_fixes.json')
        self.assertEqual(mutation['meta']['runs'][0]['jobs'],2)
        self.assertEqual(mutation['results']['one']['timeout'],120)
        self.assertEqual(runner.read(out/'artifacts/unit/unit_evidence.json')['ok'],True)
        self.assertEqual(runner.read(out/'artifacts/details/owner.json'),{'owner':'unit'})
        if fault!='normal':self.assertIn('changed after stage',result['error'])
        if fault!='sealed':
            for stage in result['browser']:
                self.assertEqual(runner.tree_hashes(out/'artifacts/browser'/('i'+str(stage['stage']))),stage['files'])
        self.assertEqual(runner.read(out/'artifacts/browser/i7/evidence.json')['cases'],baseline[6]['cases']) if fault!='sealed' else None

    def test_run_evidence_contracts_reject_bad_results(self):
        baseline = runner.read(self.root / 'ci/browser-baseline.json')
        for problem, step in [('runtime', 'unit'), ('command', 'unit'), ('unit', 'unit'), ('browser', 'browser'),
                              ('cases', 'browser'), ('duplicate', 'browser'), ('ambiguous', 'browser'),
                              ('backend', 'browser'), ('mutation', 'mutations'), ('source', 'unit'),
                              ('artifacts', 'unit'), ('module_runtime', 'browser'), ('valid', 'unit')]:
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
                    elif args[:2] == ['node', 'tests/modules_browser.cjs']:
                        module_fixture.snapshot(runner,self.root,Path(env['WSFLOW_BROWSER_OUTPUT']))
                        if problem == 'module_runtime':
                            path=Path(env['WSFLOW_BROWSER_OUTPUT'])/'result.json'
                            data=runner.read(path);data['browser']='foreign';runner.save(path,data)
                    elif len(args) > 1 and args[1].endswith('_browser.cjs'):
                        n = int(args[1][7]);cases = list(baseline[n-1]['cases'])
                        if problem == 'cases':
                            cases[0] = 'replacement with same count'
                        if problem == 'duplicate': cases.append(cases[0])
                        runner.save(Path(env['WSFLOW_BROWSER_OUTPUT']) / 'evidence.json',
                                    {'cases': cases, 'errors': ['controlled error'] if problem == 'browser' else [],
                                     'source_sha256': runner.digest((self.root / 'static/index.html').read_bytes()),
                                     'backend_hashes': {'missing.py': '0' * 64} if problem == 'backend' else {}})
                        if problem == 'ambiguous':
                            shutil.copyfile(Path(env['WSFLOW_BROWSER_OUTPUT'])/'evidence.json',
                                            Path(env['WSFLOW_BROWSER_OUTPUT'])/'second_evidence.json')
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
