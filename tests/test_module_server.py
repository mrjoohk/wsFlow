"""Integration-module HTTP ownership and local worker shutdown contracts.

Every server, registry and module state used here is temporary.  No account
credentials are copied and the provider boundary rejects any network request.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from wsflow import google_calendar
from wsflow.builtin_modules import WORK_CALENDAR, WORKSPACE_OPERATIONS, route_owner
from wsflow.module_host import ModuleHost
from wsflow.server import serve


class ModuleServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wsflow_module_http_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / 'registry' / 'projects.json'
        self.registry.parent.mkdir()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.registry.write_text(json.dumps({'roots': [], 'projects': [
            {'id': 'p', 'path': str(self.project), 'alias': 'Project', 'tags': []}
        ]}), encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.root / 'userdata')})
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = mock.Mock(side_effect=AssertionError('provider request forbidden'))

    def configure(self, module_ids, action='disable'):
        host = ModuleHost(self.registry)
        for module_id in module_ids:
            plan = host.plan({'action': action, 'module_id': module_id,
                              'expected_revision': host.snapshot()['revision']})
            host.apply(plan['plan_id'])

    def package(self, capabilities=None):
        source = self.root / 'erp-source'
        source.mkdir()
        manifest = {'schema_version': 1, 'id': 'erp-test', 'name': 'ERP local fixture',
                    'version': '1.0.0', 'host_api': 1, 'data_schema_version': 1,
                    'capabilities': capabilities or ['erp'], 'requires': [],
                    'entrypoint': 'backend.py:create_module',
                    'ui': {'entry': 'view.js', 'label': 'ERP fixture'}}
        (source / 'module.json').write_text(json.dumps(manifest), encoding='utf-8')
        (source / 'backend.py').write_text(
            "def create_module(context):\n"
            "    class Service:\n"
            "        def handle(self, method, segments, query, body):\n"
            "            return {'module_id': context['module_id'], 'segments': segments, 'method': method}\n"
            "        def stop(self):\n"
            "            return True\n"
            "    return Service()\n", encoding='utf-8')
        (source / 'view.js').write_text('export function mount() { return "ERP fixture"; }\n', encoding='utf-8')
        return source

    def install_package(self, source, action='install', module_id=None):
        host = ModuleHost(self.registry)
        body = {'action': action, 'source_path': str(source), 'trusted': True,
                'expected_revision': host.snapshot()['revision']}
        if module_id is not None:
            body['module_id'] = module_id
        plan = host.plan(body)
        host.apply(plan['plan_id'])

    @contextmanager
    def running(self):
        server = serve(self.registry, port=0, token='module-test', google_transport=self.provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.provider.assert_not_called()

    def request(self, method, path, body=None, status=200, token='module-test', headers=None):
        data = json.dumps(body).encode() if body is not None else None
        actual_headers = {'Content-Type': 'application/json', **(headers or {})}
        if token is not None:
            actual_headers['X-Auth-Token'] = token
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.server.server_port}{path}',
            method=method, data=data, headers=actual_headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as response:
            raw = response.read()
            code = response.code
            response.close()
        else:
            with response:
                raw, code = response.read(), response.status
        value = json.loads(raw)
        self.assertEqual(code, status, value)
        return value

    def test_failed_extension_returns_service_unavailable_and_keeps_core_available(self):
        package = self.package()
        (package / 'backend.py').write_text('raise RuntimeError("fixture startup failure")\n', encoding='utf-8')
        self.install_package(package)
        with self.running():
            result = self.request('GET', '/api/v1/modules/erp-test/api/ping', status=503)
            self.assertIn('기동', result['error'])
            self.request('GET', '/api/v1/modules/erp-test/assets/view.js', status=503)
            self.request('GET', '/api/v1/modules')
            self.request('GET', '/api/config')
            self.request('GET', '/api/projects')

    def test_default_routes_and_module_management_keep_auth_guard(self):
        with self.running():
            state = self.request('GET', '/api/v1/modules')
            self.assertTrue(all(self.server.module_host.builtin_active(module_id)
                                for module_id in (WORK_CALENDAR, WORKSPACE_OPERATIONS)))
            self.assertIn('modules', state)
            self.request('GET', '/api/v1/modules', status=403, token=None)
            self.request('POST', '/api/v1/modules/plan', {}, status=403, token='wrong')
            self.request('POST', '/api/v1/modules/plan', {}, status=403,
                         headers={'Origin': 'https://untrusted.invalid'})
            self.request('POST', '/api/v1/modules/apply',
                         {'plan_id': 'not-issued', 'action': 'disable'}, status=400)
            self.request('POST', '/api/v1/modules/plan', [], status=400)
            self.request('GET', '/api/no-such-route', status=404)

    def test_all_disabled_keep_core_reads_and_block_every_legacy_owner(self):
        self.configure((WORK_CALENDAR, WORKSPACE_OPERATIONS))
        before = self.registry.read_bytes()
        with mock.patch('wsflow.server.google_calendar.Service') as google_factory, \
             mock.patch('wsflow.server.gitops.status', side_effect=AssertionError('inactive Git')), \
             mock.patch('wsflow.server.gitops.repo_kind', side_effect=AssertionError('inactive Git')):
            with self.running():
                google_factory.assert_not_called()
                self.assertIsNone(self.server.google_calendar)
                self.assertIsNone(self.server.calendar_writer)
                self.assertIn('roots', self.request('GET', '/api/config'))
                projects = self.request('GET', '/api/projects')['projects']
                self.assertEqual(projects[0]['git'], 'unavailable')
                self.assertEqual(self.request('GET', '/api/projects/p')['id'], 'p')
                self.request('GET', '/api/v1/modules')
                cases = [
                    ('GET', '/api/projects/p/status', None, WORKSPACE_OPERATIONS),
                    ('GET', '/api/projects/p/rules', None, WORKSPACE_OPERATIONS),
                    ('POST', '/api/roots', {}, WORKSPACE_OPERATIONS),
                    ('DELETE', '/api/roots', {}, WORKSPACE_OPERATIONS),
                    ('POST', '/api/projects/new', {}, WORKSPACE_OPERATIONS),
                    ('POST', '/api/projects', {}, WORKSPACE_OPERATIONS),
                    ('PATCH', '/api/projects/p', {}, WORKSPACE_OPERATIONS),
                    ('DELETE', '/api/projects/p', {}, WORKSPACE_OPERATIONS),
                    ('POST', '/api/deploy/apply', {}, WORKSPACE_OPERATIONS),
                    ('GET', '/api/scan', None, WORKSPACE_OPERATIONS),
                    ('GET', '/api/locks', None, WORKSPACE_OPERATIONS),
                    ('GET', '/api/v1/projects/p/context', None, WORKSPACE_OPERATIONS),
                    ('GET', '/api/v1/projects/p/session/status', None, WORKSPACE_OPERATIONS),
                    ('GET', '/api/v1/context/dashboard', None, WORKSPACE_OPERATIONS),
                    ('POST', '/api/v1/roots/context/plan', {}, WORKSPACE_OPERATIONS),
                    ('POST', '/api/v1/projects/p/writes/git/plan', {}, WORKSPACE_OPERATIONS),
                    ('GET', '/api/projects/p/jira', None, WORK_CALENDAR),
                    ('GET', '/api/jira/status', None, WORK_CALENDAR),
                    ('GET', '/api/jira/status/legacy-tail', None, WORK_CALENDAR),
                    ('POST', '/api/projects/p/jira/close/apply', {}, WORK_CALENDAR),
                    ('GET', '/api/v1/workflow/status', None, WORK_CALENDAR),
                    ('GET', '/api/v1/calendar/range', None, WORK_CALENDAR),
                    ('GET', '/api//v1/calendar/range/', None, WORK_CALENDAR),
                    ('GET', '/api/v2/calendar/range', None, WORK_CALENDAR),
                    ('POST', '/api/v2/calendar/google/connect', {'write': False}, WORK_CALENDAR),
                    ('POST', '/api/v1/projects/p/writes/memo/apply', {}, WORK_CALENDAR),
                ]
                for method, path, body, owner in cases:
                    with self.subTest(path=path, method=method):
                        value = self.request(method, path, body, status=409)
                        self.assertEqual(value['module_id'], owner)
                self.request('GET', '/api/no-such-route', status=404)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_work_calendar_can_run_without_workspace_operations(self):
        self.configure((WORKSPACE_OPERATIONS,))
        with self.running():
            self.assertIsNotNone(self.server.google_calendar)
            value = self.request('GET', '/api/v2/calendar/range?start=2026-10-28&end=2026-10-29&timezone=UTC')
            self.assertIn('days', value)
            self.request('GET', '/api/projects/p/jira')
            self.request('GET', '/api/projects/p/status', status=409)

    def test_workspace_dashboard_does_not_read_inactive_work_calendar(self):
        self.configure((WORK_CALENDAR,))
        with mock.patch('wsflow.server.workflow.overview', side_effect=AssertionError('inactive workflow')):
            with self.running():
                value = self.request('GET', '/api/v1/context/dashboard')
                self.assertTrue(value['workflow']['module_inactive'])
                self.assertEqual(value['calendar']['state'], 'module_inactive')

    def test_configuration_apply_is_explicitly_pending_until_next_server(self):
        with self.running():
            state = self.request('GET', '/api/v1/modules')
            plan = self.request('POST', '/api/v1/modules/plan', {
                'action': 'disable', 'module_id': WORK_CALENDAR,
                'expected_revision': state['revision']})
            result = self.request('POST', '/api/v1/modules/apply', {'plan_id': plan['plan_id']})
            self.assertTrue(result['restart_required'])
            self.assertTrue(self.server.module_host.builtin_active(WORK_CALENDAR))
            self.request('GET', '/api/v2/calendar/range?start=2026-10-28&end=2026-10-29&timezone=UTC')
        with self.running():
            self.assertFalse(self.server.module_host.builtin_active(WORK_CALENDAR))
            self.request('GET', '/api/v2/calendar/range', status=409)

    def test_external_module_api_and_assets_are_authenticated_http_contributions(self):
        self.install_package(self.package())
        with self.running():
            value = self.request('POST', '/api/v1/modules/erp-test/api/inventory', {'query': 'local'})
            self.assertEqual(value, {'module_id': 'erp-test', 'segments': ['inventory'], 'method': 'POST'})
            self.request('GET', '/api/v1/modules/erp-test/api/inventory', token=None, status=403)
            self.request('GET', '/api/v1/modules/erp-test/assets/view.js', token=None, status=403)
            request = urllib.request.Request(
                f'http://127.0.0.1:{self.server.server_port}/api/v1/modules/erp-test/assets/view.js',
                headers={'X-Auth-Token': 'module-test'})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn('javascript', response.headers['Content-Type'])
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn(b'export function mount', response.read())
            self.request('GET', '/api/v1/modules/erp-test/assets/../backend.py', status=400)
            self.request('GET', '/api/v1/modules/erp-test/assets/backend.py', status=404)
        self.assertTrue(self.server.module_shutdown['extensions']['workers_stopped'])

    def test_alternative_provider_does_not_reenable_reserved_builtin_routes(self):
        source = self.package(['work-items', 'calendar'])
        workflow_data = self.project / '.workflow' / 'state.json'
        workflow_data.parent.mkdir()
        workflow_data.write_bytes(b'{"kept":true}')
        self.install_package(source, action='replace', module_id=WORK_CALENDAR)
        with mock.patch('wsflow.server.google_calendar.Service') as google_factory:
            with self.running():
                self.request('GET', '/api/projects/p/jira', status=409)
                self.request('GET', '/api/v2/calendar/range', status=409)
                self.assertEqual(self.request('GET', '/api/v1/modules/erp-test/api/ping')['module_id'], 'erp-test')
                google_factory.assert_not_called()
        self.assertEqual(workflow_data.read_bytes(), b'{"kept":true}')


class ModuleOwnershipTests(unittest.TestCase):
    def test_unknown_routes_keep_legacy_dispatch_and_core_registry_is_independent(self):
        for method, path in [('GET', '/api/config'), ('GET', '/api/projects'),
                             ('GET', '/api/projects/p'), ('POST', '/api/pick-folder'),
                             ('GET', '/api/v1/modules'), ('GET', '/api/no-such')]:
            self.assertIsNone(route_owner(method, path), (method, path))
        self.assertEqual(route_owner('POST', '/api/projects/new'), WORKSPACE_OPERATIONS)
        self.assertEqual(route_owner('POST', '/api//projects/p/jira/close/apply/'), WORK_CALENDAR)


class GoogleModuleShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wsflow_module_shutdown_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / 'registry' / 'projects.json'
        self.registry.parent.mkdir()
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.root / 'userdata')})
        patch.start()
        self.addCleanup(patch.stop)
        self.transport = mock.Mock(side_effect=AssertionError('network forbidden'))
        self.service = google_calendar.Service(self.registry, transport=self.transport)

    def test_local_stop_preserves_credentials_and_does_not_revoke(self):
        credentials = self.service.credentials.path
        credentials.parent.mkdir(parents=True)
        credentials.write_bytes(b'local-credential-sentinel')
        self.service.start_refresh()
        first = self.service.refresh_thread
        self.service.start_refresh()
        self.assertIs(first, self.service.refresh_thread)
        self.assertTrue(self.service.close())
        self.assertFalse(first.is_alive())
        self.assertEqual(credentials.read_bytes(), b'local-credential-sentinel')
        self.transport.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.start_refresh()
        with self.assertRaises(ValueError):
            self.service.connect()

    def test_busy_worker_reports_incomplete_shutdown_until_it_really_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def busy():
            entered.set()
            release.wait(5)
        self.service.refresh_if_due = busy
        with mock.patch.object(google_calendar, 'CACHE_SECONDS', .001):
            self.service.start_refresh()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.service.close(timeout=0))
                self.assertTrue(self.service.refresh_thread.is_alive())
            finally:
                release.set()
                self.assertTrue(self.service.close(timeout=2))
        self.transport.assert_not_called()
