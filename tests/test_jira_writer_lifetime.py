"""Jira writer lifetimes with actual SQLite/files and an owned transport."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wsflow import fileio, operations, writer_admission
from wsflow.module_host import ModuleHost
from wsflow.module_services import ModuleServices
from wsflow.modules.work_calendar import closeflow, jirawrite, newtask, wfhome, writeplans
from wsflow.modules.work_calendar.backend import create_module
from wsflow.modules.work_calendar.routes import Routes
from wsflow.server import make_handler
from wsflow.store import Store

SITE = 'https://jira.example.invalid'
CREDS = {'email': 'test@example.invalid', 'api_token': 'fixture'}
PAYLOAD = {'text': '합성 작업'}
TARGET = 'XXX-1'


def file_bytes(path):
    return {p.relative_to(path).as_posix(): p.read_bytes()
            for p in path.rglob('*') if p.is_file()}


class JiraWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow-jira-writer-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / 'home'
        self.project_dir = self.base / 'project'
        self.project_dir.mkdir()
        self.project = {'id': 'p', 'path': str(self.project_dir), 'wf_home': str(self.home)}
        self.home.mkdir()
        state = wfhome.state_path(self.home)
        state.parent.mkdir()
        state.write_text(json.dumps({'schema_version': wfhome.SCHEMA_VERSION,
                                     'roots': {}, 'project_key': 'XXX',
                                     'issue_type_map': {'site_url': SITE}}), encoding='utf8')
        env = patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / 'userdata')})
        env.start()
        self.addCleanup(env.stop)
        self.store = operations.Store(self.home)
        self.domain = writer_admission.default_domain()

    def approval(self):
        return jirawrite.approve(self.home, 'comment', TARGET, PAYLOAD, site=SITE)['id']

    def forbidden_send(self, *args):
        self.fail('unexpected synthetic Jira request')

    def raw_calls(self):
        return {
            'approve': lambda: self.approval(),
            'execute': lambda: jirawrite.execute(self.home, CREDS, SITE, 'comment',
                                                TARGET, PAYLOAD, self.forbidden_send),
            'reconcile': lambda: jirawrite.reconcile(self.home, CREDS, SITE, 'missing',
                                                    '9', self.forbidden_send),
            'repair': lambda: jirawrite.repair(self.home, 'missing'),
        }

    def caller_calls(self):
        return {
            'preview_close': lambda: writeplans.preview_close(self.project, self.home, '2026-11-07', []),
            'preview_new': lambda: writeplans.preview_new(self.project, self.home, {}, {'summary': 'new'}),
            'apply_close': lambda: writeplans.apply_close(self.project, self.home, 'missing', [], {}, []),
            'apply_new': lambda: writeplans.apply_new(self.project, self.home, 'missing', 'missing', {}, []),
            'create': lambda: newtask.create(self.home, CREDS, SITE, {}, self.forbidden_send),
            'record': lambda: newtask.record(self.home, {'title': 'synthetic'}, 'XXX-9',
                                             approved_at='2026-11-07', operation_id='fixture'),
        }

    def check_barrier(self, calls):
        before = file_bytes(self.home)
        for name, call in calls.items():
            with self.subTest(name=name), self.domain.barrier(0):
                with patch.object(fileio, 'locked', wraps=fileio.locked) as locks:
                    with self.assertRaises(writer_admission.WriterUnavailable):
                        call()
                    locks.assert_not_called()
        self.assertEqual(before, file_bytes(self.home))
        self.assertFalse(self.store.path.exists())

    def test_raw_barrier_precedes_workflow_lock(self):
        self.check_barrier(self.raw_calls())

    def test_caller_barrier_precedes_workflow_lock(self):
        self.check_barrier(self.caller_calls())

    def test_invalid_userdata_precedes_both_lock_layers(self):
        before = file_bytes(self.home)
        for data in (self.home, self.home / 'userdata'):
            with patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(data)}):
                for name, call in {**self.raw_calls(), **self.caller_calls()}.items():
                    with self.subTest(data=data, name=name):
                        with patch.object(fileio, 'locked', wraps=fileio.locked) as locks:
                            with self.assertRaises(operations.StateConflict):
                                call()
                            locks.assert_not_called()
        self.assertEqual(before, file_bytes(self.home))
        self.assertFalse((self.home / 'userdata').exists())

    def epoch(self):
        return self.domain.observe().get('epoch', 0)

    def assert_live(self, epoch):
        view = self.domain.observe()
        self.assertEqual(epoch + 1, view['epoch'])
        self.assertIsNotNone(view['active'])
        results = []

        def peer():
            try:
                with self.domain.barrier(0):
                    results.append('admitted')
            except writer_admission.WriterUnavailable as error:
                results.append(error.reason)

        thread = threading.Thread(target=peer)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(['busy'], results)

    def assert_finished(self, epoch):
        self.assertEqual(epoch + 2, self.epoch())
        self.assertIsNone(self.domain.observe()['active'])

    def test_approval_export_is_inside_same_root_as_sql(self):
        epoch = self.epoch()
        append = jirawrite._append

        def export(path, row):
            self.assertEqual('approved', self.store.get(row['id'])['status'])
            self.assert_live(epoch)
            return append(path, row)

        with patch.object(jirawrite, '_append', side_effect=export) as exported:
            ident = self.approval()
        exported.assert_called_once()
        self.assertEqual(ident, jirawrite.find_approval(self.home, 'comment', TARGET, PAYLOAD)['id'])
        self.assert_finished(epoch)

    def test_execute_holds_one_root_through_post_finish_and_export(self):
        ident = self.approval()
        epoch = self.epoch()
        append = jirawrite._append
        effects = []

        def send(url, headers, payload, method):
            self.assertEqual('POST', method)
            self.assertEqual('executing', self.store.get(ident)['status'])
            self.assert_live(epoch)
            effects.append(json.loads(payload))
            return {'id': '9'}

        def export(path, row):
            self.assertEqual('pushed', row['event'])
            self.assertEqual('succeeded', self.store.get(ident)['status'])
            self.assert_live(epoch)
            return append(path, row)

        with patch.object(jirawrite, '_append', side_effect=export) as exported:
            result = jirawrite.execute(self.home, CREDS, SITE, 'comment', TARGET, PAYLOAD, send)
        exported.assert_called_once()
        self.assertEqual({'id': '9'}, result)
        self.assertEqual(1, len(effects))
        self.assertEqual(ident, effects[0]['properties'][0]['value']['operation_id'])
        self.assert_finished(epoch)

    def test_response_loss_retains_effect_and_reconciles_without_replay(self):
        ident = self.approval()
        epoch = self.epoch()
        remote = {}

        def lost(url, headers, payload, method):
            self.assertEqual('POST', method)
            self.assert_live(epoch)
            remote.update(json.loads(payload))
            raise TimeoutError('synthetic response lost after commit')

        with self.assertRaises(jirawrite.UncertainWrite) as caught:
            jirawrite.execute(self.home, CREDS, SITE, 'comment', TARGET, PAYLOAD, lost)
        self.assertFalse(caught.exception.to_dict()['retry_allowed'])
        self.assertEqual('uncertain', self.store.get(ident)['status'])
        self.assert_finished(epoch)
        with self.assertRaises(jirawrite.UncertainWrite):
            jirawrite.execute(self.home, CREDS, SITE, 'comment', TARGET, PAYLOAD,
                              self.forbidden_send, operation_id=ident)

        reads = []
        epoch = self.epoch()
        append = jirawrite._append

        def readback(url, headers, payload, method):
            self.assertEqual('GET', method)
            self.assert_live(epoch)
            reads.append(url)
            if '/properties/' in url:
                return {'value': remote['properties'][0]['value']}
            return {'id': '9', 'body': remote['body']}

        def export(path, row):
            self.assert_live(epoch)
            return append(path, row)

        with patch.object(jirawrite, '_append', side_effect=export) as exported:
            self.assertEqual({'id': '9'}, jirawrite.reconcile(
                self.home, CREDS, SITE, ident, '9', readback))
        exported.assert_called_once()
        self.assertEqual(2, len(reads))
        self.assertEqual('succeeded', self.store.get(ident)['status'])
        self.assert_finished(epoch)

    def test_finish_failure_keeps_claim_and_prevents_resend(self):
        ident = self.approval()
        epoch = self.epoch()
        effects = []

        def send(url, headers, payload, method):
            effects.append(method)
            return {'id': '9'}

        def fail_finish(store, *args, **kwargs):
            self.assert_live(epoch)
            raise OSError('fixture SQLite finish failure')

        with patch.object(operations.Store, 'finish', fail_finish):
            with self.assertRaises(jirawrite.UncertainWrite):
                jirawrite.execute(self.home, CREDS, SITE, 'comment', TARGET, PAYLOAD, send)
        self.assert_finished(epoch)
        self.assertEqual('executing', self.store.get(ident)['status'])
        with self.assertRaises(jirawrite.UncertainWrite):
            jirawrite.execute(self.home, CREDS, SITE, 'comment', TARGET, PAYLOAD,
                              self.forbidden_send, operation_id=ident)
        self.assertEqual(['POST'], effects)

    def test_repair_retains_root_across_local_record_and_is_idempotent(self):
        intent = {'kind': 'create_issue', 'target': 'XXX',
                  'payload': {'fields': {'summary': 'synthetic'}}, 'title': 'synthetic'}
        ident = self.store.create(intent)['operation_id']
        self.store.approve(ident)
        self.store.claim(ident)
        result = {'key': 'XXX-9', 'id': '9'}
        self.store.finish(ident, 'succeeded', result=result)
        epoch = self.epoch()

        def fail_flag(store, *args):
            self.assert_live(epoch)
            self.assertEqual(1, len(wfhome.read_state(self.home)['roots']))
            raise OSError('fixture local flag failure')

        with patch.object(jirawrite, '_send', self.forbidden_send):
            with patch.object(operations.Store, 'local_recorded', fail_flag):
                with self.assertRaises(OSError):
                    jirawrite.repair(self.home, ident)
            self.assert_finished(epoch)
            self.assertFalse(self.store.get(ident)['local_recorded'])
            epoch = self.epoch()
            self.assertEqual(result, jirawrite.repair(self.home, ident))
            self.assert_finished(epoch)
        self.assertEqual(1, len(wfhome.read_state(self.home)['roots']))
        self.assertTrue(self.store.get(ident)['local_recorded'])
        self.assertEqual(1, sum(r['event'] == 'pushed' for r in
                                jirawrite._read(jirawrite.approvals_path(self.home))))

    def host(self):
        registry = self.base / 'registry.json'
        registry.write_text(json.dumps({'roots': [], 'projects': [self.project]}), encoding='utf8')
        host = ModuleHost(registry)
        services = ModuleServices(Store(registry), host)
        backend = create_module({'services': services})
        return host, backend, services

    @contextmanager
    def running(self, host, backend, services):
        host.services = services
        host.backends['work-calendar'] = backend
        host.started_backends.add('work-calendar')
        with ThreadingHTTPServer(('127.0.0.1', 0),
                                 make_handler(services.store, 'fixture', modules=host)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server.server_port
            finally:
                server.shutdown()
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def post(self, port, path):
        req = Request(f'http://127.0.0.1:{port}{path}', method='POST', data=b'{}',
                      headers={'X-Auth-Token': 'fixture', 'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_host_preflight_precedes_first_control_io(self):
        paths = ['/api/projects/p/jira/' + tail for tail in (
            'close/plan', 'close/apply', 'new/plan', 'new/create',
            'operations/missing/reconcile', 'operations/missing/repair')]
        paths.append('/api//projects/p/jira/operations/missing/repair/')
        paths.append('/api/projects/p/jira/new/plan/legacy-tail')
        for data in (self.home, self.home / 'userdata'):
            with patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(data)}):
                host, backend, services = self.host()
                before = file_bytes(self.home)
                request = SimpleNamespace(_project=services.project)
                for path in paths:
                    with self.subTest(data=data, path=path):
                        with patch.object(fileio, 'locked', wraps=fileio.locked) as locks:
                            with self.assertRaises(operations.StateConflict):
                                backend.dispatch(request, 'POST', path, {}, {})
                            locks.assert_not_called()
                self.assertEqual(before, file_bytes(self.home))
                with self.running(host, backend, services) as port:
                    initialized = file_bytes(self.home)
                    for path in paths:
                        with self.subTest(http=path, data=data):
                            with patch.object(fileio, 'locked', wraps=fileio.locked) as locks:
                                status, body = self.post(port, path)
                            self.assertEqual(409, status)
                            self.assertIn('작업 DB는 WF_HOME 밖', body['error'])
                            locks.assert_not_called()
                    self.assertEqual(initialized, file_bytes(self.home))

    def test_route_rechecks_changed_home_before_workflow_lock(self):
        switched = self.base / 'other-home'
        switched.mkdir()
        data = switched / 'userdata'
        actual = {**self.project, 'wf_home': str(switched)}
        with patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(data)}):
            _, backend, _ = self.host()
            request = SimpleNamespace()
            with patch.object(request, '_project', create=True,
                              side_effect=[self.project, actual]):
                with patch.object(fileio, 'locked', wraps=fileio.locked) as locks:
                    with patch.object(Routes, '_jira_write_admitted') as admitted:
                        with self.assertRaises(operations.StateConflict):
                            backend.dispatch(request, 'POST', '/api/projects/p/jira/new/plan', {}, {})
                    admitted.assert_not_called()
            self.assertEqual([data / 'writer-admission'],
                             [Path(call.args[0]) for call in locks.call_args_list])
            self.assertFalse((switched / '.workflow').exists())

    def test_standalone_rechecks_alias_after_admission(self):
        alias = self.base / 'alias'
        switched = self.base / 'switched'
        for path in (alias, switched):
            state = wfhome.state_path(path)
            state.parent.mkdir(parents=True)
            state.write_bytes(wfhome.state_path(self.home).read_bytes())
        data = switched / 'userdata'
        project = {**self.project, 'wf_home': str(alias)}
        request = SimpleNamespace(_project=lambda _: project)
        backend = create_module({'services': SimpleNamespace()})
        target = self.home
        real_resolve = type(alias).resolve
        real_locked = writer_admission.WriterDomain._locked

        def resolve(path, *args, **kwargs):
            return real_resolve(target if path == alias else path, *args, **kwargs)

        @contextmanager
        def change_alias_at_admission(domain, timeout):
            nonlocal target
            target = switched
            with real_locked(domain, timeout):
                yield

        with (patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(data)}),
              patch.object(type(alias), 'resolve', resolve),
              patch.object(writer_admission.WriterDomain, '_locked', change_alias_at_admission),
              patch.object(fileio, 'locked', wraps=fileio.locked) as locks):
            with self.assertRaisesRegex(operations.StateConflict, '작업 DB는 WF_HOME 밖'):
                backend.dispatch(request, 'POST', '/api/projects/p/jira/operations/missing/repair', {}, {})
        self.assertEqual(switched, target)
        self.assertEqual([data / 'writer-admission'],
                         [Path(call.args[0]) for call in locks.call_args_list])
        for path in (alias, switched):
            self.assertFalse((wfhome.state_path(path).parent / 'wsflow.lock').exists())

    def successful_comment(self):
        plan = self.store.save_plan(
            {'binding': writeplans.binding(self.project, self.home), 'site': SITE},
            [{'kind': 'comment', 'target': TARGET, 'payload': PAYLOAD}])
        ident = plan['items'][0]['operation_id']
        self.store.approve(ident)
        self.store.claim(ident)
        self.store.finish(ident, 'succeeded', result={'id': '9'})
        return ident

    def test_standalone_backend_refuses_before_workflow_lock(self):
        ident = self.successful_comment()
        backend = create_module({'services': SimpleNamespace()})
        request = SimpleNamespace(_project=lambda project_id: self.project)
        path = f'/api/projects/p/jira/operations/{ident}/repair'
        before = file_bytes(self.home)
        with self.domain.barrier(0):
            with patch.object(fileio, 'locked', wraps=fileio.locked) as locks:
                with self.assertRaises(writer_admission.WriterUnavailable):
                    backend.dispatch(request, 'POST', path, {}, {})
                locks.assert_not_called()
        self.assertEqual(before, file_bytes(self.home))

    def test_close_batches_keep_one_root_between_items(self):
        items = [{'kind': 'comment', 'target': f'XXX-{n}', 'payload': PAYLOAD} for n in range(1, 4)]
        epoch = self.epoch()
        append = jirawrite._append

        def approval_export(path, row):
            self.assert_live(epoch)
            return append(path, row)

        with patch.object(jirawrite, '_append', side_effect=approval_export):
            approvals = closeflow.approve(self.home, items)
        self.assert_finished(epoch)
        self.assertEqual(3, len(approvals))
        epoch = self.epoch()
        effects = []

        def send(url, headers, payload, method):
            self.assert_live(epoch)
            effects.append(method)
            return {'id': str(len(effects))}

        result = closeflow.apply(self.home, CREDS, SITE, items, send)
        self.assert_finished(epoch)
        self.assertEqual(['POST'] * 3, effects)
        self.assertEqual([item['target'] for item in items],
                         [item['target'] for item in result['done']])
        self.assertEqual([], result['failed'])

    def test_create_keeps_root_through_local_record(self):
        intent = {'kind': 'create_issue', 'target': 'XXX',
                  'payload': {'fields': {'summary': 'synthetic'}}, 'title': 'synthetic'}
        ident = jirawrite.approve(self.home, intent['kind'], intent['target'],
                                 intent['payload'], site=SITE)['id']
        epoch = self.epoch()
        effects = []
        record = newtask.record
        update = operations.Store.local_recorded

        def send(url, headers, payload, method):
            self.assert_live(epoch)
            effects.append(method)
            return {'key': 'XXX-9', 'id': '9'}

        def record_state(*args, **kwargs):
            self.assert_live(epoch)
            state = record(*args, **kwargs)
            self.assert_live(epoch)
            return state

        def record_flag(store, operation_id):
            self.assert_live(epoch)
            return update(store, operation_id)

        with patch.object(newtask, 'record', side_effect=record_state) as recorded:
            with patch.object(operations.Store, 'local_recorded', record_flag):
                result = newtask.create(self.home, CREDS, SITE, intent, send, operation_id=ident)
        recorded.assert_called_once()
        self.assertEqual(['POST'], effects)
        self.assertEqual('XXX-9', result['key'])
        self.assertTrue(self.store.get(ident)['local_recorded'])
        self.assert_finished(epoch)

    def test_http_repair_uses_same_root_as_result_export(self):
        ident = self.successful_comment()
        host, backend, services = self.host()
        append = jirawrite._append
        epoch = self.epoch()

        def export(path, row):
            self.assert_live(epoch)
            return append(path, row)

        with self.running(host, backend, services) as port:
            with patch.object(jirawrite, '_append', side_effect=export) as exported:
                status, body = self.post(port, f'/api/projects/p/jira/operations/{ident}/repair')
        self.assertEqual(200, status, body)
        self.assertEqual({'id': '9'}, body['result'])
        exported.assert_called_once()
        self.assert_finished(epoch)
