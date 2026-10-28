"""Physical work-calendar package and legacy API compatibility contracts."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from wsflow.store import Store

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / 'wsflow/modules/work_calendar'
DOMAIN = ('auth', 'jiraapi', 'jirawrite', 'wfhome', 'wfinit', 'newtask', 'closeflow',
          'writeplans', 'workflow', 'wf_migrate', 'calendar_query', 'calendar_store',
          'calendar_view', 'google_calendar', 'calendar_sync')


class WorkCalendarPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow_work_calendar_package_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.registry = self.base / 'registry' / 'projects.json'
        self.registry.parent.mkdir()
        self.registry.write_text(json.dumps({'roots': [], 'projects': []}), encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / 'userdata')})
        patch.start(); self.addCleanup(patch.stop)
        self.services = SimpleNamespace(store=Store(self.registry), google_transport=mock.Mock(side_effect=AssertionError('provider forbidden')))

    def load_copy(self):
        folder = self.base / ('copy-' + uuid4().hex)
        shutil.copytree(PACKAGE, folder, ignore=shutil.ignore_patterns('__pycache__'))
        name = 'work_calendar_package_probe_' + uuid4().hex
        spec = importlib.util.spec_from_file_location(name, folder / 'backend.py', submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        self.addCleanup(lambda: [sys.modules.pop(key, None) for key in list(sys.modules) if key == name or key.startswith(name + '.')])
        return folder, module

    def test_legacy_imports_are_the_same_implementation_modules_for_mock_compatibility(self):
        for name in DOMAIN:
            with self.subTest(name=name):
                legacy = importlib.import_module('wsflow.' + name)
                implementation = importlib.import_module('wsflow.modules.work_calendar.' + name)
                self.assertIs(legacy, implementation)
                self.assertEqual(Path(implementation.__file__).resolve().parent, PACKAGE.resolve())
        legacy = importlib.import_module('wsflow.auth')
        with mock.patch.object(legacy, 'load', return_value={'sentinel': True}):
            self.assertEqual(importlib.import_module('wsflow.modules.work_calendar.auth').load(), {'sentinel': True})

    def test_vendor_resources_are_owned_by_the_package_and_byte_identical(self):
        from wsflow.modules.work_calendar import workflow, closeflow, newtask
        self.assertEqual(workflow.VENDOR, PACKAGE / 'vendor/jira_worklog')
        self.assertEqual(closeflow.VENDOR, workflow.VENDOR)
        self.assertEqual(newtask.NEW_TITLE, workflow.VENDOR / 'new_title.py')
        for old in (ROOT / 'vendor/jira_worklog').glob('*.py'):
            self.assertEqual(hashlib.sha256(old.read_bytes()).digest(), hashlib.sha256((workflow.VENDOR / old.name).read_bytes()).digest())
        self.assertEqual(workflow.MANIFEST.read_bytes(), (ROOT / 'tools/jira_vendor_manifest.json').read_bytes())
        self.assertTrue(workflow.vendor_status()['identical'])

    def test_package_factory_has_no_provider_or_worker_side_effect_before_start(self):
        from wsflow.modules.work_calendar import backend, google_calendar
        with mock.patch.object(google_calendar, 'Service') as constructor:
            service = backend.create_module({'services': self.services})
            self.assertIsNone(service.google)
            self.assertFalse(service.started)
            self.assertTrue(service.stop())
            constructor.assert_not_called()
        self.services.google_transport.assert_not_called()

    def test_package_lifecycle_owns_google_worker_and_stops_without_revocation(self):
        from wsflow.modules.work_calendar import backend
        service = backend.create_module({'services': self.services})
        service.start()
        self.addCleanup(service.stop)
        self.assertIsNotNone(service.google)
        thread = service.google.refresh_thread
        self.assertTrue(thread.is_alive())
        service.start()
        self.assertIs(thread, service.google.refresh_thread)
        self.assertTrue(service.stop())
        self.assertFalse(thread.is_alive())
        self.services.google_transport.assert_not_called()

    def test_copied_package_dispatches_its_own_changed_route_without_rebinding_core(self):
        folder = self.base / 'route-update'
        shutil.copytree(PACKAGE, folder, ignore=shutil.ignore_patterns('__pycache__'))
        routes = folder / 'routes.py'
        text = routes.read_text(encoding='utf-8')
        marker = 'return self.runtime.workflow_status(day)'
        self.assertEqual(text.count(marker), 1)
        routes.write_text(text.replace(marker, "return {'package_route': 'updated-copy'}"), encoding='utf-8')
        name = 'work_calendar_route_update_' + uuid4().hex
        spec = importlib.util.spec_from_file_location(name, folder / 'backend.py', submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec);sys.modules[name] = module
        spec.loader.exec_module(module)
        self.addCleanup(lambda: [sys.modules.pop(key, None) for key in list(sys.modules) if key == name or key.startswith(name + '.')])
        service = module.create_module({'services': self.services})
        self.addCleanup(service.stop)
        value = service.dispatch(SimpleNamespace(), 'GET', '/api/v1/workflow/status', {'date': ['2026-10-28']}, {})
        self.assertEqual(value, {'package_route': 'updated-copy'})
        from wsflow.modules.work_calendar import backend
        original = backend.create_module({'services': self.services})
        self.addCleanup(original.stop)
        self.assertEqual(original.dispatch(SimpleNamespace(), 'GET', '/api/v1/workflow/status', {'date': ['2026-10-28']}, {})['date'], '2026-10-28')
        self.assertEqual(Path(module.google_calendar.__file__).resolve().parent, folder.resolve())
        self.services.google_transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
