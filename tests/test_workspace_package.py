"""Workspace package ownership, public aliases and HTTP adapter contracts."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SERVICES = ('gitops', 'scanner', 'deploy', 'context_inventory', 'context_rules',
            'context_skills', 'context_projection', 'session_prep')
ROOT = Path(__file__).resolve().parent.parent


class WorkspacePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wsflow-workspace-package-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'workspace' / 'project'; self.project.mkdir(parents=True)
        self.registry = self.root / 'registry' / 'projects.json'; self.registry.parent.mkdir()
        self.record = {'id': 'p', 'path': str(self.project), 'alias': 'Project', 'tags': []}
        self.registry.write_text(json.dumps({'roots': [], 'projects': [self.record]}), encoding='utf-8')
        patcher = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.root / 'userdata')})
        patcher.start(); self.addCleanup(patcher.stop)

    def services(self, feature=None):
        from wsflow.store import Store
        return SimpleNamespace(store=Store(self.registry),
                               module_host=SimpleNamespace(builtin_active=lambda ident: ident == 'workspace-operations'),
                               feature=lambda ident: feature if ident == 'work-calendar' else None)

    def backend(self, feature=None):
        from wsflow.modules.workspace_operations.backend import create_module
        return create_module({'services': self.services(feature)})

    def request(self):
        def project(pid):
            if pid != 'p':
                from wsflow.module_services import ApiError
                raise ApiError(404, 'missing')
            return self.record
        return SimpleNamespace(_project=project)

    def test_public_modules_alias_the_canonical_package_and_share_monkeypatches(self):
        for name in SERVICES:
            old = importlib.import_module('wsflow.' + name)
            actual = importlib.import_module('wsflow.modules.workspace_operations.' + name)
            self.assertIs(old, actual)
            self.assertEqual(Path(actual.__file__).parent.name, 'workspace_operations')
        gitops = importlib.import_module('wsflow.modules.workspace_operations.gitops')
        with mock.patch('wsflow.gitops.repo_kind', return_value='none') as patched:
            self.assertEqual(gitops.repo_kind(self.project), 'none')
            patched.assert_called_once_with(self.project)

    def test_package_import_does_not_load_the_other_business_module(self):
        script = '''import importlib,importlib.abc,sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith('wsflow.modules.work_calendar'):
   raise AssertionError('other business module imported: '+fullname)
sys.meta_path.insert(0,Block())
for name in %r:
 importlib.import_module('wsflow.modules.workspace_operations.'+name)
importlib.import_module('wsflow.modules.workspace_operations.backend')
''' % (SERVICES,)
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_backend_routes_own_registry_mutations_and_local_project_summary(self):
        backend = self.backend()
        self.assertTrue(backend.start())
        root = self.root / 'workspace'
        result = backend.dispatch(self.request(), 'POST', '/api/roots', {}, {'path': str(root)})
        self.assertEqual(Path(result['path']), root)
        self.assertEqual(backend.summary(self.record)['git'], 'none')
        self.assertTrue(backend.stop())

    def test_context_read_is_owned_by_the_package_and_remains_readonly(self):
        before = self.registry.read_bytes()
        result = self.backend().dispatch(self.request(), 'GET', '/api/v1/projects/p/context', {}, {})
        self.assertIsInstance(result, dict)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.project / '.wsflow').exists())

    def test_git_writer_uses_the_current_package_adapter(self):
        from wsflow.modules.workspace_operations import gitops
        from wsflow.modules.workspace_operations.routes import Routes
        sentinel = object()
        with mock.patch('wsflow.common_writes.Service', return_value=sentinel) as service:
            self.assertIs(Routes(self.request(), self.services())._writes(self.record), sentinel)
        service.assert_called_once_with(self.record, 'git', None, git_adapter=gitops)

    def test_generic_write_engine_executes_the_package_git_adapter(self):
        from wsflow.modules.workspace_operations.routes import Routes
        class PackageAdapterSelected(RuntimeError):
            pass
        def inspect_repo(path):
            raise PackageAdapterSelected('current package adapter')
        adapter = SimpleNamespace(repo_kind=inspect_repo)
        with mock.patch('wsflow.modules.workspace_operations.routes.gitops', adapter):
            writer = Routes(self.request(), self.services())._writes(self.record)
            with self.assertRaisesRegex(PackageAdapterSelected, 'current package adapter'):
                writer.preview('remote_add', {'name': 'origin', 'url': 'synthetic-local-target'})
        self.assertEqual(self.registry.read_text(encoding='utf-8'), json.dumps({'roots': [], 'projects': [self.record]}))

    def test_dashboard_uses_the_optional_feature_contract(self):
        work = SimpleNamespace(calendar_status=mock.Mock(return_value={'state': 'ok', 'warnings': [], 'outbox_counts': {}, 'last_sync': None}),
                               workflow_status=mock.Mock(return_value={'projects': [], 'date': 'synthetic'}))
        result = self.backend(work).dispatch(self.request(), 'GET', '/api/v1/context/dashboard', {}, {})
        work.calendar_status.assert_called_once_with()
        self.assertEqual(work.workflow_status.call_count, 1)
        self.assertEqual(result['workflow']['date'], 'synthetic')
        inactive = self.backend().dispatch(self.request(), 'GET', '/api/v1/context/dashboard', {}, {})
        self.assertTrue(inactive['workflow']['module_inactive'])

    def test_package_normalizes_git_failure_without_losing_operation_details(self):
        from wsflow.module_services import ApiError
        from wsflow.modules.workspace_operations import gitops
        error = gitops.GitError('failed', 12, 'out', 'detail')
        error.plan_id = 'plan'; error.operation_id = 'operation'
        with mock.patch('wsflow.modules.workspace_operations.routes.Routes.dispatch', side_effect=error):
            with self.assertRaises(ApiError) as raised:
                self.backend().dispatch(self.request(), 'GET', '/api/projects/p/status', {}, {})
        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(raised.exception.extra['plan_id'], 'plan')
        self.assertEqual(raised.exception.extra['operation_id'], 'operation')


if __name__ == '__main__':
    unittest.main()
