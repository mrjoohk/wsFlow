"""Independent regression cases for P057 delta flow findings D1-02-01..03."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wsflow import module_continuity as continuity, module_host, operations


class ContinuityFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow-continuity-flow-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'; self.root.mkdir()
        self.project = self.root / 'checkout'; self.project.mkdir()
        self.home = self.base / 'separate-home'; self.home.mkdir()
        self.registry = self.base / 'registry.json'
        self.data = self.base / 'userdata'
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR':str(self.data),
            'APPDATA':str(self.base / 'appdata'), 'XDG_CONFIG_HOME':str(self.base / 'xdg'),
            'WSFLOW_GOOGLE_CLIENT_FILE':str(self.base / 'oauth-client.json')})
        patch.start(); self.addCleanup(patch.stop)
        self.write_registry([{'id':'one','path':str(self.project),'wf_home':str(self.home)}])
        self.host = self.data / 'modules' / operations.digest(str(self.registry.resolve()))
        self.current = {ident:{'id':ident,'installed':True,'enabled':True,'version':'1.0.0',
                              'package_digest':'before','requires':[], 'data_schema_version':1}
                        for ident in ('work-calendar','workspace-operations')}

    def write_registry(self, projects):
        self.registry.write_text(json.dumps({'roots':[{'path':str(self.root)}], 'projects':projects}), encoding='utf-8')

    def proposed(self, *changed):
        result = copy.deepcopy(self.current)
        for ident in changed:
            result[ident].update(version='1.1.0', package_digest='after',
                data_compatibility={'version':1,'preserves':[ident + '-v1'],'pending_policy':'block'})
        return result

    def audit(self, *changed):
        changed = changed or ('work-calendar',)
        return continuity.audit_transition(self.registry, self.host, self.current,
                                           self.proposed(*changed), 'update', changed[0])

    def terminal(self, home):
        store = operations.Store(home)
        ident = store.create({'kind':'git'})['operation_id']
        store.approve(ident); store.claim(ident); store.finish(ident, 'succeeded', {'ok':True})
        return store

    def child(self, seed, mode, plan_id=None):
        args = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).with_name('continuity_process.py')),
                str(self.base), mode]
        if plan_id:
            args.append(plan_id)
        child = subprocess.run(args, env={**os.environ, 'PYTHONHASHSEED':str(seed)},
                               capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout)

    def test_separate_home_plan_applies_in_process_with_different_hash_seed(self):
        store = self.terminal(self.home)
        original = {str(path):path.read_bytes() for path in (self.registry, store.path)}
        package = self.base / 'package'
        shutil.copytree(Path(module_host.__file__).parent / 'modules/work_calendar', package,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        manifest = json.loads((package / 'module.json').read_text(encoding='utf-8'))
        manifest['version'] = '1.1.0'
        (package / 'module.json').write_text(json.dumps(manifest), encoding='utf-8')
        previews = [self.child(seed, 'plan') for seed in (1,2,4,8)]
        self.assertTrue(all(plan['ready'] for plan in previews), previews)
        self.assertEqual(len({plan['fingerprint'] for plan in previews}), 1, previews)
        reserved = self.child(4, 'apply', previews[0]['plan_id'])
        self.flow_evidence = {'seeds':[1,2,4,8], 'previews':previews, 'reservation':reserved}
        self.assertEqual(reserved, {'status':200,'revision':1,'active_revision':0})
        self.assertEqual({path:Path(path).read_bytes() for path in original}, original)

    def test_fingerprint_ignores_injected_reverse_store_source_and_blocker_order(self):
        operations.Store(self.home).create({'kind':'git'})
        operations.Store(self.project).create({'kind':'git'})
        normal = self.audit()
        self.assertEqual(normal['pending_count'], 2)
        self.assertGreaterEqual(len(normal['blockers']), 2)
        real_finish = continuity._Audit.finish
        def reverse_observations(audit):
            real_finish(audit)
            audit.stores.reverse()
            audit.blockers.reverse()
            for store in audit.stores:
                store['sources'].reverse()
        with mock.patch.object(continuity._Audit, 'finish', reverse_observations):
            reversed_report = self.audit()
        self.flow_evidence = {'normal_fingerprint':normal['fingerprint'],
                              'reversed_fingerprint':reversed_report['fingerprint'],
                              'normal_pending':normal['pending_count']}
        self.assertEqual(normal['fingerprint'], reversed_report['fingerprint'])
        self.assertEqual(normal['stores'], reversed_report['stores'])
        self.assertEqual(normal['blockers'], reversed_report['blockers'])

    def test_two_checkouts_sharing_home_observe_and_count_one_operation_once(self):
        second = self.root / 'second-checkout'; second.mkdir()
        self.write_registry([{'id':'one','path':str(self.project),'wf_home':str(self.home)},
                             {'id':'two','path':str(second),'wf_home':str(self.home)}])
        store = operations.Store(self.home); store.create({'kind':'git'})
        real_status = continuity._Audit._sqlite_status; visits = []
        def count_analysis(audit, db, descriptor):
            if descriptor['path'] == str(store.path):
                visits.append(descriptor['path'])
            return real_status(audit, db, descriptor)
        with mock.patch.object(continuity._Audit, '_sqlite_status', count_analysis):
            report = self.audit('work-calendar','workspace-operations')
        matching = [item for item in report['stores'] if item['path'] == str(store.path)]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['counts']['operations'], 1)
        self.assertEqual(matching[0]['counts']['operation_states'], {'planned':1})
        self.assertEqual(matching[0]['pending_count'], 1)
        self.assertEqual(report['pending_count'], 1)
        self.assertEqual(len(visits), 1)
        self.assertEqual(set(matching[0]['owners']), {'work-calendar','workspace-operations'})
        self.assertEqual(len(matching[0]['sources']), 3)
        self.assertFalse(report['ready'])
        self.flow_evidence = {'actual_operation_rows':1, 'matching_descriptors':len(matching),
                              'descriptor_pending':matching[0]['pending_count'],
                              'report_pending':report['pending_count'], 'db_analysis_visits':len(visits),
                              'owners':matching[0]['owners'], 'source_observations':len(matching[0]['sources'])}

    def test_skill_assets_match_producer_bytes_without_business_json_schema(self):
        from wsflow.modules.workspace_operations import context_skills
        assets = {'SKILL.md':b'# Example\nUse the bundled assets.\n',
                  'array.json':b'[{"input":"one"},{"input":"two"}]', 'scalar.json':b'42',
                  'image.bin':b'\x00\xff\x80\x01', 'opaque.json':b'\xff\x00\x80',
                  'private/credentials.json':b'PRIVATE SECRET'}
        secret_paths = []
        for relative in ('skills/example','.agents/skills/example','.claude/skills/example'):
            package = self.project / relative
            for name, raw in assets.items():
                path = package / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
                if name.endswith('credentials.json'):
                    secret_paths.append(path)
            # Producer treats all non-Markdown assets as bytes, with no JSON
            # object/array/scalar restriction or decoding requirement.
            accepted = context_skills.package(package, package.parent)
            self.assertEqual(accepted['file_count'], len(assets))
            for name, raw in assets.items():
                self.assertEqual(accepted['files'][name], hashlib.sha256(raw).hexdigest())
        real_open = Path.open
        def reject_secret_read(path, *args, **kwargs):
            self.assertNotIn(path, secret_paths)
            return real_open(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', reject_secret_read):
            report = self.audit('workspace-operations')
        self.assertTrue(report['ready'], report['blockers'])
        self.assertNotIn(hashlib.sha256(b'PRIVATE SECRET').hexdigest(), json.dumps(report))
        for role in ('canonical-skills','projected-skills'):
            observed = [store for store in report['stores'] if store['role'] == role and store['present']]
            self.assertTrue(observed)
            sources = {source['declared_path']:source for store in observed for source in store['sources']}
            selected = (self.project/'skills/example/array.json', assets['array.json']) if role == 'canonical-skills' else (
                        self.project/'.agents/skills/example/image.bin', assets['image.bin'])
            for path, raw in [selected]:
                self.assertEqual(sources[str(path)]['sha256'], hashlib.sha256(raw).hexdigest())
        # The producer's wider 64 MiB package allowance does not enlarge C2's
        # existing 4 MiB per-file observation limit.
        large = self.project / 'skills/example/large.bin'
        large.write_bytes(b'x' * (continuity.MAX_SOURCE_BYTES + 1))
        self.assertIn('large.bin', context_skills.package(large.parent, large.parent.parent)['files'])
        bounded = self.audit('workspace-operations')
        self.assertFalse(bounded['ready'])
        self.assertIn('read_limit', {item['code'] for item in bounded['blockers']})
        self.flow_evidence = {'producer_accepted_packages':3, 'producer_files_per_package':len(assets),
                              'allowed_assets_ready':report['ready'], 'secret_body_reads':0,
                              'larger_than_reader_limit_producer_accepted':True,
                              'larger_than_reader_limit_preflight_ready':bounded['ready']}
