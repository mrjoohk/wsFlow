"""Reservation boundaries for business-module data continuity preflight."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from wsflow import operations
from wsflow.module_host import ModuleError, ModuleHost

ROOT = Path(__file__).resolve().parents[1]


class ModuleTransitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow-transition-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / 'workspace' / 'Project Alpha'
        self.project.mkdir(parents=True)
        self.registry = self.base / 'registry' / 'projects.json'
        self.registry.parent.mkdir()
        self.registry.write_text(json.dumps({'roots': [{'path': str(self.project.parent)}],
            'projects': [{'id': 'p', 'path': str(self.project), 'alias': 'p', 'tags': []}]}), encoding='utf-8')
        env = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / 'userdata')})
        env.start(); self.addCleanup(env.stop)
        self.host = ModuleHost(self.registry)
        self.addCleanup(self.host.close)

    def package(self, ident='work-calendar', version='1.1.0', omit_contract=False):
        folder = self.base / 'sources' / ident / version
        shutil.copytree(ROOT / 'wsflow/modules' / ident.replace('-', '_'), folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        path = folder / 'module.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['version'] = version
        if omit_contract:
            manifest.pop('data_compatibility', None)
        path.write_text(json.dumps(manifest), encoding='utf-8')
        return folder

    def preview(self, folder=None, ident='work-calendar'):
        return self.host.plan({'action': 'update', 'module_id': ident,
            'expected_revision': self.host.snapshot()['revision'],
            'source_path': str(folder or self.package(ident)), 'trusted': True})

    def pending(self, state='planned'):
        store = operations.Store(self.project)
        operation = store.create({'kind': 'jira_comment', 'synthetic': True})
        if state != 'planned':
            store.approve(operation['operation_id'])
        if state in ('executing', 'uncertain', 'succeeded'):
            store.claim(operation['operation_id'])
        if state in ('uncertain', 'succeeded'):
            store.finish(operation['operation_id'], state, result={'synthetic': True})
        return store

    def assert_reservation_refused(self, plan):
        with self.assertRaises(ModuleError) as caught:
            self.host.apply(plan['plan_id'])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.host.snapshot()['revision'], plan['expected_revision'])

    def test_clear_preview_reports_preflight_scope_and_can_be_reserved(self):
        plan = self.preview()
        self.assertTrue(plan['can_apply'])
        self.assertTrue(plan['continuity']['ready'])
        self.assertEqual(plan['continuity']['scope'], 'preflight_only')
        self.assertIn('work-calendar', plan['continuity']['affected_modules'])
        result = self.host.apply(plan['plan_id'])
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['active_revision'], 0)

    def test_all_pending_common_operation_states_block_without_modifying_business_data(self):
        folder = self.package()
        for state in ('planned', 'approved', 'executing', 'uncertain'):
            with self.subTest(state=state):
                store = self.pending(state)
                before = store.path.read_bytes()
                plan = self.preview(folder)
                self.assertFalse(plan['can_apply'])
                self.assertTrue(plan['continuity']['blockers'])
                self.assert_reservation_refused(plan)
                self.assertEqual(store.path.read_bytes(), before)

    def test_operation_created_after_preview_refuses_before_package_staging(self):
        plan = self.preview()
        self.pending()
        self.assert_reservation_refused(plan)
        record = plan['modules']['work-calendar']
        self.assertFalse((self.host.root / record['package_path']).exists())

    def test_terminal_data_added_after_preview_still_requires_fresh_preview(self):
        plan = self.preview()
        self.pending('succeeded')
        self.assert_reservation_refused(plan)
        fresh = self.preview(Path(plan['source_path']))
        self.assertTrue(fresh['can_apply'])
        self.host.apply(fresh['plan_id'])

    def test_pending_introduced_during_staging_is_rechecked_before_configuration_write(self):
        plan = self.preview()
        original = self.host._stage_package
        def stage(*args):
            result = original(*args)
            self.pending()
            return result
        with mock.patch.object(self.host, '_stage_package', side_effect=stage):
            self.assert_reservation_refused(plan)
        self.assertFalse(self.host.state_path.exists())
        self.assertFalse((self.host.root / 'history/0.json').exists())

    def test_existing_data_requires_explicit_target_preservation_contract(self):
        self.pending('succeeded')
        plan = self.preview(self.package(omit_contract=True))
        self.assertFalse(plan['can_apply'])
        self.assert_reservation_refused(plan)

    def test_legacy_code_change_plan_without_preflight_cannot_be_reserved(self):
        plan = self.preview()
        path = self.host.root / 'plans' / (plan['plan_id'] + '.json')
        data = json.loads(path.read_bytes())
        data.pop('continuity', None); data.pop('can_apply', None)
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assert_reservation_refused(plan)

    def test_already_reserved_plan_is_idempotent_even_when_later_work_exists(self):
        plan = self.preview()
        first = self.host.apply(plan['plan_id'])
        self.pending()
        self.assertEqual(self.host.apply(plan['plan_id']), first)

    def test_restore_code_version_is_checked_against_pending_operations(self):
        plan = self.preview()
        self.host.apply(plan['plan_id'])
        self.pending('uncertain')
        restore = self.host.plan({'action': 'restore', 'expected_revision': 1, 'target_revision': 0})
        self.assertFalse(restore['can_apply'])
        self.assert_reservation_refused(restore)

    def test_disabling_remains_available_without_discarding_pending_data(self):
        store = self.pending('uncertain')
        before = store.path.read_bytes()
        plan = self.host.plan({'action': 'disable', 'module_id': 'work-calendar', 'expected_revision': 0})
        self.assertTrue(plan['can_apply'])
        self.host.apply(plan['plan_id'])
        self.assertEqual(store.path.read_bytes(), before)

    def test_source_package_mutation_is_still_rejected(self):
        plan = self.preview()
        with (Path(plan['source_path']) / 'backend.py').open('a', encoding='utf-8') as f:
            f.write('\n# later edit\n')
        self.assert_reservation_refused(plan)

    def test_preflight_does_not_import_builtin_business_implementations(self):
        import subprocess
        import sys
        code = "import sys; from wsflow import module_continuity; assert not any(n.startswith('wsflow.modules.') for n in sys.modules)"
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
