"""Acceptance tests for source isolation and real failure gates (no live provider)."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
