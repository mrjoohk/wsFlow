"""Generation-owned vendor imports and declared lifecycle dependencies."""
import json
from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

from wsflow.module_host import ModuleHost, PACKAGE_NAMES
from wsflow.module_services import ModuleServices
from wsflow.store import Store

ROOT = Path(__file__).resolve().parents[1]


class PackageGenerationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wsflow-generation-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.registry = self.base / 'registry/projects.json'
        self.registry.parent.mkdir()
        self.registry.write_text('{"roots": [], "projects": []}', encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / 'userdata')})
        patch.start(); self.addCleanup(patch.stop)
        self.transport = mock.Mock(side_effect=AssertionError('provider forbidden'))
        self.addCleanup(self.transport.assert_not_called)

    def package(self, ident, requires=()):
        folder = self.base / 'sources' / ident
        shutil.copytree(ROOT / 'wsflow/modules' / PACKAGE_NAMES[ident], folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        path = folder / 'module.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest.update(version='1.1.0', requires=list(requires))
        path.write_text(json.dumps(manifest), encoding='utf-8')
        return folder

    def install(self, ident, folder):
        host = ModuleHost(self.registry)
        self.addCleanup(host.close)
        plan = host.plan({'action': 'update', 'module_id': ident, 'trusted': True,
                          'source_path': str(folder), 'expected_revision': host.snapshot()['revision']})
        host.apply(plan['plan_id'])

    def bind(self):
        host = ModuleHost(self.registry)
        self.addCleanup(host.close)
        services = ModuleServices(Store(self.registry), host, self.transport)
        host.bind_services(services)
        return host, services

    def inline_get(self, path, **runtime):
        from wsflow.server import make_handler
        provider = mock.Mock()
        handler = make_handler(Store(self.registry), 'inline-test', **(runtime or {'google': provider}))
        with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with closing(http.client.HTTPConnection(*server.server_address, timeout=10)) as connection:
                    connection.request('GET', path, headers={'X-Auth-Token': 'inline-test'})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
            finally:
                server.shutdown(); thread.join(5)
                provider.start_refresh.assert_not_called()
                provider.close.assert_not_called()

    def test_vendor_imports_execute_current_bytes_with_private_sibling_imports(self):
        from wsflow.modules.work_calendar import wfhome, closeflow
        old_modules = {name: sys.modules.get(name) for name in ('aggregate', 'jira_fields', 'draft_lint')}
        old_path = list(sys.path)
        folder = self.package('work-calendar')
        vendor = folder / 'vendor/jira_worklog'
        # Synthetic package versions: the shipped upstream files remain unchanged.
        additions = {
            'aggregate': "\ndef compile_patterns(*args): return 'new-aggregate'\n",
            'jira_fields': "\ndef require_schema(*args): return 'new-fields'\n",
            'draft_lint': "\ndef lint(*args): return 'new-lint'\n",
        }
        for name, code in additions.items():
            path = vendor / (name + '.py')
            path.write_bytes(path.read_bytes() + code.encode())
        self.install('work-calendar', folder)
        host, _ = self.bind()
        self.assertNotIn('work-calendar', host.errors)
        backend = sys.modules[type(host.backends['work-calendar']).__module__]
        current = backend.wfhome
        current_close = sys.modules[backend.__name__ + '.closeflow']
        self.assertEqual(current.AGG.compile_patterns([], 'title_include'), 'new-aggregate')
        self.assertEqual(current.AGG.require_schema({}), 'new-fields')
        self.assertEqual(current.JF.require_schema({}), 'new-fields')
        self.assertEqual(current_close.LINT.lint({}), 'new-lint')
        self.assertIsNot(current.AGG, wfhome.AGG)
        self.assertIsNot(current_close.LINT, closeflow.LINT)
        self.assertEqual(wfhome.AGG.compile_patterns([], 'title_include'), [])
        self.assertEqual(sys.path, old_path)
        self.assertEqual({name: sys.modules.get(name) for name in old_modules}, old_modules)

    def test_dependency_starts_first_and_features_are_exposed_only_after_start(self):
        folder = self.package('work-calendar', requires=['workspace-operations'])
        path = folder / 'backend.py'
        text = path.read_text(encoding='utf-8')
        old = '        store = self.services.store'
        self.assertEqual(text.count(old), 1)
        text = text.replace(old, "        assert self.services.feature('workspace-operations').started\n" + old)
        path.write_text(text, encoding='utf-8')
        self.install('work-calendar', folder)
        host, services = self.bind()
        self.assertIsNone(services.feature('work-calendar'))
        self.assertIsNone(services.feature('workspace-operations'))
        host.start_backends()
        self.assertEqual(host.errors, {})
        self.assertTrue(services.feature('work-calendar').started)
        self.assertTrue(services.feature('workspace-operations').started)
        order = []
        for ident, backend in host.backends.items():
            stop = backend.stop
            def stopped(ident=ident, stop=stop):
                order.append(ident); return stop()
            backend.stop = stopped
        self.assertTrue(host.close())
        self.assertEqual(order, ['work-calendar', 'workspace-operations'])
        self.assertIsNone(services.feature('work-calendar'))

    def test_failed_dependency_never_starts_consumer(self):
        folder = self.package('work-calendar', requires=['workspace-operations'])
        self.install('work-calendar', folder)
        host, services = self.bind()
        consumer = host.backends['work-calendar']
        with mock.patch.object(host.backends['workspace-operations'], 'start', side_effect=RuntimeError('dependency failed')):
            with mock.patch.object(consumer, 'start') as start:
                host.start_backends()
                start.assert_not_called()
        self.assertIn('dependency failed', host.errors['workspace-operations'])
        self.assertIn('필수 모듈', host.errors['work-calendar'])
        self.assertIsNone(services.feature('workspace-operations'))
        self.assertIsNone(services.feature('work-calendar'))

    def test_legacy_handler_uses_supplied_runtime_without_starting_or_owning_workers(self):
        from wsflow.server import make_handler
        from wsflow.modules.work_calendar.backend import Service
        provider = mock.Mock()
        provider.status.return_value = {'connected': True, 'runtime_marker': 'caller-owned'}
        with mock.patch.object(Service, 'start') as start:
            handler = make_handler(Store(self.registry), 'legacy-test', provider)
            with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with closing(http.client.HTTPConnection(*server.server_address, timeout=10)) as connection:
                        connection.request('GET', '/api/v2/calendar/google/status', headers={'X-Auth-Token': 'legacy-test'})
                        response = connection.getresponse()
                        body = json.loads(response.read())
                    self.assertEqual(response.status, 200, body)
                    self.assertEqual(body['runtime_marker'], 'caller-owned')
                finally:
                    server.shutdown(); thread.join(5)
            start.assert_not_called()
        provider.start_refresh.assert_not_called()
        service = Service({'services': object()})
        service.use_runtime(provider)
        self.assertTrue(service.stop())
        provider.close.assert_not_called()

    def test_legacy_runtime_unsupported_hook_is_isolated_from_core(self):
        folder = self.package('work-calendar')
        path = folder / 'backend.py'
        path.write_bytes(path.read_bytes() + b'\nService.use_runtime = None\n')
        self.install('work-calendar', folder)
        self.assertEqual(self.inline_get('/api/config')[0], 200)
        status, body = self.inline_get('/api/v2/calendar/google/status')
        self.assertEqual(status, 503, body)
        self.assertEqual(body['module_id'], 'work-calendar')
        modules = self.inline_get('/api/v1/modules')[1]['modules']
        self.assertIn('supplied runtime failed', next(row for row in modules if row['id'] == 'work-calendar')['error'])

    def test_legacy_runtime_does_not_expose_consumer_before_dependency_start(self):
        folder = self.package('work-calendar', requires=['workspace-operations'])
        path = folder / 'backend.py'
        text = path.read_text(encoding='utf-8')
        text = text.replace('return Routes(request, self.services, self).dispatch(method, path, query, body)',
                            "assert self.services.feature('workspace-operations') is not None\n            return {'ready': True}")
        path.write_text(text, encoding='utf-8')
        self.install('work-calendar', folder)
        status, body = self.inline_get('/api/v2/calendar/google/status')
        self.assertEqual(status, 503, body)
        modules = self.inline_get('/api/v1/modules')[1]['modules']
        self.assertIn('의존 모듈', next(row for row in modules if row['id'] == 'work-calendar')['error'])
        self.assertEqual(self.inline_get('/api/config')[0], 200)

    def test_legacy_runtime_passes_writer_and_setup_error_to_business_routes(self):
        provider = mock.Mock()
        writer = mock.Mock()
        writer.status.return_value = {'operations': [], 'writer_marker': 'caller-writer'}
        status, body = self.inline_get('/api/v2/calendar/google/outbox', google=provider, writer=writer)
        self.assertEqual(status, 200, body)
        self.assertEqual(body['writer_marker'], 'caller-writer')
        writer.status.assert_called_once_with()
        status, body = self.inline_get('/api/v2/calendar/google/status', google_error='caller-setup-error')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['setup_error'], 'caller-setup-error')


if __name__ == '__main__':
    unittest.main()
