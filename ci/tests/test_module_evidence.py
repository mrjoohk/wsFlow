"""Module producer snapshots must bind cases, package bytes and observed shutdowns."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zlib
import module_fixture

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('module_evidence_runner', ROOT/'tools/ci/runner.py')
runner = importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

class ModuleEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='module-evidence-')
        self.addCleanup(temp.cleanup);self.base=Path(temp.name);self.root=self.base/'source'
        for name in runner.ROOTS:(self.root/name).mkdir(parents=True)
        for name in ('run.py','run.cmd','static/index.html'):(self.root/name).write_text('synthetic')
        module_fixture.prepare_inputs(runner,self.root,ROOT)
        # A nested implementation is part of the source contract, independent of aliases.
        p=self.root/'wsflow/modules/work_calendar/domain.py';p.parent.mkdir(parents=True);p.write_text('VERSION=1')
        (p.parent/'ui.js').write_text('synthetic UI')
        (p.parent/'module.json').write_text('{}')
        self.folder=self.base/'evidence';module_fixture.snapshot(runner,self.root,self.folder)

    def change(self,name,edit):
        p=self.folder/name;d=runner.read(p);edit(d);runner.save(p,d)

    def test_finished_snapshot_covers_all_cases_and_eight_shutdowns(self):
        result=runner.validate_module_browser(self.root,self.folder)
        self.assertEqual(result,dict(cases=12,starts=8,provider_attempts=0,source_files=9,shutdowns=8))
        hashes=runner.read(self.folder/'result.json')['source_sha256']
        for name in ('wsflow/modules/work_calendar/ui.js','wsflow/modules/work_calendar/module.json'):
            self.assertIn(name,hashes)
            self.assertEqual(hashes[name],hashlib.sha256((self.root/name).read_bytes()).hexdigest())

    def test_missing_replaced_duplicate_and_failed_cases_are_rejected(self):
        original=runner.read(self.folder/'result.json')
        for edit in (lambda d:d.update(ok=False),
                     lambda d:d['cases'].pop(),
                     lambda d:d['cases'].append(d['cases'][0]),
                     lambda d:d['cases'][0].update(id='foreign'),
                     lambda d:d['cases'][0].update(description='foreign'),
                     lambda d:d.update(pageErrors=['error']),
                     lambda d:d.update(unexpectedConsoleErrors=['error']),
                     lambda d:d.update(unexpectedResponses=[{'status':500}]),
                     lambda d:d.update(forbidden=['provider'])):
            with self.subTest(edit=edit):
                d=copy.deepcopy(original);edit(d);runner.save(self.folder/'result.json',d)
                with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)

    def test_unobserved_start_and_provider_counts_are_rejected(self):
        original=runner.read(self.folder/'result.json')
        wrong=copy.deepcopy(original['fixtureRuntimes']);wrong[0]['python']='0.0.0'
        foreign=copy.deepcopy(original['fixtureRuntimes']);foreign[0]['executable']='foreign-python'
        for field,value in [('fixtureStarts',7),('providerAttempts',1),('providerAttempts',False),
                            ('fixtureRuntimes',wrong),('fixtureRuntimes',foreign)]:
            with self.subTest(field=field,value=value):
                d=copy.deepcopy(original);d[field]=value;runner.save(self.folder/'result.json',d)
                with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)

    def test_actual_checkpoint_provider_and_worker_failure_override_result_zero(self):
        p=self.folder/'restart-2.json';original=runner.read(p)
        for edit in (lambda d:d['previous_shutdown'].update(provider_attempts=1),
                     lambda d:d['previous_shutdown'].update(preserved=False),
                     lambda d:d['previous_shutdown'].update(event='foreign'),
                     lambda d:d['previous_shutdown'].update(starts=7),
                     lambda d:d['previous_shutdown']['shutdown']['work-calendar'].update(workers_stopped=False),
                     lambda d:d['previous_shutdown']['shutdown'].pop('work-calendar'),
                     lambda d:d.update(starts=7)):
            with self.subTest(edit=edit):
                d=copy.deepcopy(original);edit(d);runner.save(p,d)
                with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)

    def test_missing_restart_and_final_observation_are_rejected(self):
        for name in ('restart-4.json','shutdown.json','final-state.json'):
            p=self.folder/name;raw=p.read_bytes();p.unlink()
            with self.assertRaises(FileNotFoundError):runner.validate_module_browser(self.root,self.folder)
            p.write_bytes(raw)
        stopped_path=self.folder/'shutdown.json';stopped=runner.read(stopped_path)
        for field,value in (('event','starting'),('starts',1)):
            runner.save(stopped_path,{**stopped,field:value})
            with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)
            runner.save(stopped_path,stopped)
        original=runner.read(self.folder/'final-state.json')
        for edit in (lambda d:d.update(provider_attempts=1),lambda d:d.update(event='foreign')):
            d=copy.deepcopy(original);edit(d);runner.save(self.folder/'final-state.json',d)
            with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)

    def test_package_fixture_start_end_and_missing_source_hashes_are_rejected(self):
        original=runner.read(self.folder/'result.json')
        name='wsflow/modules/work_calendar/domain.py'
        edits=[lambda d:d['source_sha256'].__setitem__(name,'0'*64),
               lambda d:d['source_sha256_end'].__setitem__(name,'0'*64),
               lambda d:d['source_sha256'].pop(name),
               lambda d:d['source_sha256'].__setitem__('tests/modules_fixture.py','0'*64),
               lambda d:d['source_sha256'].__setitem__('wsflow/modules/work_calendar/ui.js','0'*64),
               lambda d:d['source_sha256_end'].pop('wsflow/modules/work_calendar/module.json')]
        for edit in edits:
            with self.subTest(edit=edit):
                d=copy.deepcopy(original);edit(d);runner.save(self.folder/'result.json',d)
                with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)
        runner.save(self.folder/'result.json',original)
        for name in ('wsflow/modules/work_calendar/ui.js','wsflow/modules/work_calendar/module.json'):
            path=self.root/name;before=path.read_bytes();path.write_bytes(b'changed actual package bytes')
            with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)
            path.write_bytes(before)

    def test_missing_truncated_and_crc_corrupt_png_are_rejected(self):
        p=self.folder/'erp-module-view.png';raw=p.read_bytes();p.unlink()
        with self.assertRaises(FileNotFoundError):runner.validate_module_browser(self.root,self.folder)
        wrong_header=bytearray(raw);wrong_header[12:16]=b'JHDR'
        wrong_header[29:33]=zlib.crc32(wrong_header[12:29]).to_bytes(4,'big')
        zero_width=bytearray(raw);zero_width[16:20]=b'\0\0\0\0'
        zero_width[29:33]=zlib.crc32(zero_width[12:29]).to_bytes(4,'big')
        for bad in (raw[:16],raw[:-5],raw[:-13]+bytes([raw[-13]^1])+raw[-12:],bytes(wrong_header),bytes(zero_width)):
            p.write_bytes(bad)
            with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)

    def test_consumer_can_read_original_zip_bytes_after_extracted_files_change(self):
        originals={p:p.read_bytes() for p in self.folder.iterdir()}
        (self.folder/'result.json').write_text('foreign unsealed extraction')
        import json
        result=runner.validate_module_browser(self.root,self.folder,
                load=lambda p:json.loads(originals[p]),blob=lambda p:originals[p])
        self.assertEqual(result['cases'],12)
        with self.assertRaises(ValueError):runner.validate_module_browser(self.root,self.folder)

if __name__=='__main__':unittest.main()

