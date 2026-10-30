"""P063 workspace roots: actual temporary files/SQLite and complete return windows."""
import base64
from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep the modules, rather than importing their TestCase classes into discovery.
import test_i5 as i5
import test_i6 as i6
from wsflow import context_projection as projection, operations, session_prep as session, writer_admission
from wsflow.store import Store


def tree(root):
    root = Path(root)
    return {'files': {p.relative_to(root).as_posix(): p.read_bytes()
                      for p in root.rglob('*') if p.is_file()},
            'directories': sorted(p.relative_to(root).as_posix()
                                  for p in root.rglob('*') if p.is_dir())}


def sql(store):
    if not store.path.exists():
        return None
    with closing(sqlite3.connect(store.path.as_uri() + '?mode=ro', uri=True)) as db:
        return {name: db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
                for name in ('plans', 'operations', 'events')}


class WorkspaceWriterTests(unittest.TestCase):
    def fixture(self, module):
        case = module.ContextSetupTests() if module is i5 else module.SessionTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    @contextmanager
    def worker(self, call, releases, patches):
        outcome = {}
        def run():
            try:
                outcome['result'] = call()
            except BaseException as error:
                outcome['error'] = error
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            thread = threading.Thread(target=run, daemon=True, name='owned-workspace-writer')
            thread.start()
            try:
                yield thread, outcome
            finally:
                for release in releases:
                    release.set()
                thread.join(15)
                self.assertFalse(thread.is_alive(), 'owned workspace thread did not finish')
        if 'error' in outcome:
            raise outcome['error']

    @staticmethod
    def pause(arrived, release):
        arrived.set()
        if not release.wait(12):
            raise TimeoutError('owned workspace hold expired')

    def holding(self, domain, epoch, thread, outcome):
        self.assertTrue(thread.is_alive())
        self.assertEqual(outcome, {}, 'public producer returned before held boundary')
        with self.assertRaises(writer_admission.WriterUnavailable) as caught:
            with domain.barrier(0):
                pass
        self.assertEqual(caught.exception.reason, 'busy')
        view = domain.observe()
        self.assertIsNotNone(view['active'])
        self.assertEqual(view['epoch'], epoch + 1)

    def completed(self, domain, epoch):
        view = domain.observe()
        self.assertEqual(view['epoch'], epoch + 2)
        self.assertIsNone(view['active'])
        with domain.barrier(0) as lease:
            self.assertFalse(lease.view()['snapshot_ready'])

    def action_bytes(self, service, plan):
        for action in plan['actions']:
            self.assertTrue(service._matches(projection.state(action['path']), action['after']), action['rel'])
            if action.get('data') is not None and not action.get('directory'):
                self.assertEqual(Path(action['path']).read_bytes(), base64.b64decode(action['data']))

    def test_recover_barrier_refuses_before_owner_lockfile_file_and_sql_effects(self):
        case = self.fixture(i5)
        plan = case.plan(kind='root_init', selected_skills=[])
        service = case.service
        service.ops.approve(plan['operation_id']); service.ops.claim(plan['operation_id'])
        domain = service.ops._writer_domain
        before = tree(case.root), sql(service.ops), tree(service.lock_home)
        self.assertFalse(service.lock_home.exists())
        with domain.barrier(0):
            with self.assertRaises(writer_admission.WriterUnavailable) as caught:
                service.apply(case.a['id'], plan['plan_id'], plan['operation_id'], recover=True)
            self.assertEqual(caught.exception.reason, 'reentrant_activity')
            self.assertEqual((tree(case.root), sql(service.ops), tree(service.lock_home)), before)
            self.assertFalse(service.lock_home.exists())
        epoch = domain.observe()['epoch']
        result = service.apply(case.a['id'], plan['plan_id'], plan['operation_id'], recover=True)
        self.assertEqual(result['status'], 'succeeded')
        self.action_bytes(service, plan)
        self.completed(domain, epoch)

    def test_context_preview_holds_real_saved_plan_through_response(self):
        case = self.fixture(i5); service = case.service; domain = service.ops._writer_domain
        arrived, release = threading.Event(), threading.Event(); saved = {}
        original = service.ops.save_plan
        def save(*args, **kwargs):
            value = original(*args, **kwargs); saved.update(value)
            self.pause(arrived, release)
            return value
        before = tree(case.root); epoch = domain.observe()['epoch']
        with self.worker(lambda: case.plan(kind='root_init', selected_skills=[]), [release],
                         [mock.patch.object(service.ops, 'save_plan', side_effect=save)]) as (thread, result):
            self.assertTrue(arrived.wait(8), result)
            self.assertEqual(service.ops.plan(saved['plan_id'])['plan_id'], saved['plan_id'])
            self.assertEqual(len(saved['items']), 1)
            self.assertEqual(tree(case.root), before)
            self.holding(domain, epoch, thread, result)
        self.assertEqual(result['result']['plan_id'], saved['plan_id'])
        self.assertEqual(result['result']['operation_id'], saved['items'][0]['operation_id'])
        self.completed(domain, epoch)

    def test_context_apply_holds_real_file_before_audit_and_terminal_sql(self):
        case = self.fixture(i5); case.root_init(); plan = case.plan()
        service = case.service; domain = service.ops._writer_domain
        arrived = [threading.Event(), threading.Event()]
        releases = [threading.Event(), threading.Event()]; written = []
        real_write, real_finish = projection.fileio.atomic_write, service.ops.finish
        paths = {Path(a['path']) for a in plan['actions']}
        def write(path, data, **kwargs):
            value = real_write(path, data, **kwargs)
            if Path(path) in paths and not written:
                written.append(Path(path))
                self.assertEqual(Path(path).read_bytes(), data)
                self.pause(arrived[0], releases[0])
            return value
        def finish(*args, **kwargs):
            self.pause(arrived[1], releases[1])
            return real_finish(*args, **kwargs)
        epoch = domain.observe()['epoch']
        with self.worker(lambda: case.apply(plan), releases,
                         [mock.patch.object(projection.fileio, 'atomic_write', side_effect=write),
                          mock.patch.object(service.ops, 'finish', side_effect=finish)]) as (thread, result):
            self.assertTrue(arrived[0].wait(8), result)
            events = sql(service.ops)['events']
            details = [json.loads(row[3]) for row in events if row[1] == plan['operation_id'] and row[2] == 'context_file_applied']
            self.assertFalse(any(item['path'] == str(written[0]) for item in details))
            self.holding(domain, epoch, thread, result)
            releases[0].set()
            self.assertTrue(arrived[1].wait(8), result)
            self.action_bytes(service, plan)
            self.assertEqual(service.ops.get(plan['operation_id'])['status'], 'executing')
            self.holding(domain, epoch, thread, result)
        self.assertEqual(result['result']['status'], 'succeeded')
        op = service.ops.get(plan['operation_id'])
        self.assertEqual(op['result'], result['result'])
        events = [row for row in sql(service.ops)['events'] if row[1] == plan['operation_id']]
        self.assertEqual(sum(row[2] == 'context_file_applied' for row in events), len(plan['actions']))
        self.assertEqual([row[2] for row in events if row[2] != 'context_file_applied'],
                         ['planned', 'approved', 'executing', 'succeeded'])
        self.completed(domain, epoch)

    def test_prepare_holds_real_packet_plan_through_template_response(self):
        case = self.fixture(i6); service = case.service; domain = service.ops._writer_domain
        arrived, release = threading.Event(), threading.Event(); saved = {}
        template_arrived, template_release = threading.Event(), threading.Event()
        saved_returned = threading.Event(); template = {}
        original = service.ops.save_plan; real_now = operations.now
        def save(*args, **kwargs):
            value = original(*args, **kwargs); saved.update(value)
            self.pause(arrived, release)
            saved_returned.set()
            return value
        def now():
            value = real_now()
            if saved_returned.is_set() and not template_arrived.is_set():
                template['captured_at'] = value
                self.pause(template_arrived, template_release)
            return value
        before = tree(case.root); epoch = domain.observe()['epoch']
        with self.worker(case.prepare, [release, template_release],
                         [mock.patch.object(service.ops, 'save_plan', side_effect=save),
                          mock.patch.object(operations, 'now', side_effect=now)]) as (thread, result):
            self.assertTrue(arrived.wait(8), result)
            self.assertEqual(service.ops.plan(saved['plan_id'])['kind'], 'session_packet')
            self.assertEqual(tree(case.root), before)
            self.holding(domain, epoch, thread, result)
            saved_sql = sql(service.ops); saved_active = domain.observe()['active']
            release.set()
            self.assertTrue(template_arrived.wait(8), result)
            self.assertTrue(saved_returned.is_set())
            self.assertEqual(sql(service.ops), saved_sql)
            self.assertEqual(tree(case.root), before)
            self.holding(domain, epoch, thread, result)
            self.assertEqual(domain.observe()['active'], saved_active)
        packet = result['result']
        self.assertTrue(packet['ready']); self.assertEqual(packet['packet_id'], saved['plan_id'])
        self.assertEqual(packet['observation_template']['snapshot_id'], packet['snapshot_id'])
        self.assertEqual(packet['observation_template']['captured_at'], template['captured_at'])
        self.assertEqual(packet['recognition'], 'unknown')
        self.completed(domain, epoch)

    def test_upload_holds_committed_metadata_until_actual_evidence_bytes(self):
        case = self.fixture(i6); service = case.service; domain = service.ops._writer_domain
        arrived, release = threading.Event(), threading.Event(); observed = {}
        original = session.fileio.atomic_write; content = 'plain UTF-8 evidence \ud55c\uae00'
        def write(path, data, **kwargs):
            if Path(path).parent == service.evidence_dir and Path(path).suffix == '.txt':
                observed.update(path=Path(path), data=data, plans=service._plans('session_evidence'))
                self.pause(arrived, release)
            return original(path, data, **kwargs)
        epoch = domain.observe()['epoch']
        with self.worker(lambda: service.upload(content), [release],
                         [mock.patch.object(session.fileio, 'atomic_write', side_effect=write)]) as (thread, result):
            self.assertTrue(arrived.wait(8), result)
            self.assertEqual(len(observed['plans']), 1)
            self.assertFalse(observed['path'].exists())
            self.assertEqual(observed['plans'][0]['sha256'], hashlib.sha256(content.encode()).hexdigest())
            self.holding(domain, epoch, thread, result)
        evidence = result['result']; meta, raw = service._evidence(evidence['evidence_id'])
        self.assertEqual(raw, content.encode()); self.assertEqual(observed['path'].read_bytes(), raw)
        self.assertEqual(meta['sha256'], evidence['sha256'])
        self.assertEqual(evidence['size_bytes'], len(raw))
        self.completed(domain, epoch)

    def test_observe_holds_actual_receipt_commit_through_final_reader(self):
        case = self.fixture(i6); service = case.service; packet = case.prepare()
        evidence = service.upload(json.dumps(case.document(packet)))
        body = {'packet_id': packet['packet_id'], 'evidence_id': evidence['evidence_id'],
                'evidence_kind': 'agent_reported'}
        domain = service.ops._writer_domain; arrived, release = threading.Event(), threading.Event()
        observed = {}; real_create, real_finish = service.ops.create, session.Reader.finish
        def create(*args, **kwargs):
            value = real_create(*args, **kwargs); observed['receipt'] = value
            return value
        def finish(reader):
            value = real_finish(reader)
            if 'receipt' in observed and not arrived.is_set():
                observed['reader_sources'] = list(reader.sources)
                self.assertTrue(value)
                self.pause(arrived, release)
            return value
        epoch = domain.observe()['epoch']
        with self.worker(lambda: service.observe(case.a['id'], body), [release],
                         [mock.patch.object(service.ops, 'create', side_effect=create),
                          mock.patch.object(session.Reader, 'finish', new=finish)]) as (thread, result):
            self.assertTrue(arrived.wait(8), result)
            self.assertEqual(service.ops.get(observed['receipt']['operation_id'])['kind'], 'session_receipt')
            self.assertTrue(observed['reader_sources'])
            self.holding(domain, epoch, thread, result)
        receipt = result['result']; self.assertFalse(receipt['independently_verified'])
        self.assertEqual(receipt['evidence_sha256'], evidence['sha256'])
        self.completed(domain, epoch)
        first_sql = sql(service.ops)
        self.assertEqual(service.observe(case.a['id'], body)['receipt_id'], receipt['receipt_id'])
        self.assertEqual(sql(service.ops), first_sql)
        replay_arrived, replay_release = threading.Event(), threading.Event()
        replay_sql_done = threading.Event(); replay_observed = {}
        def replay_create(*args, **kwargs):
            try:
                return real_create(*args, **kwargs)
            finally:
                replay_sql_done.set()
        def replay_finish(reader):
            value = real_finish(reader)
            if replay_sql_done.is_set() and not replay_arrived.is_set():
                replay_observed['reader_sources'] = list(reader.sources)
                self.assertTrue(value)
                self.pause(replay_arrived, replay_release)
            return value
        replay_epoch = domain.observe()['epoch']
        with self.worker(lambda: service.observe(case.a['id'], body), [replay_release],
                         [mock.patch.object(service.ops, 'create', side_effect=replay_create),
                          mock.patch.object(session.Reader, 'finish', new=replay_finish)]) as (thread, replay):
            self.assertTrue(replay_arrived.wait(8), replay)
            self.assertTrue(replay_sql_done.is_set())
            self.assertTrue(replay_observed['reader_sources'])
            self.assertEqual(sql(service.ops), first_sql)
            self.assertEqual(service.ops.get(receipt['receipt_id'])['kind'], 'session_receipt')
            self.holding(domain, replay_epoch, thread, replay)
        self.assertEqual(replay['result']['receipt_id'], receipt['receipt_id'])
        self.assertEqual(replay['result']['evidence_sha256'], evidence['sha256'])
        self.assertEqual(sql(service.ops), first_sql)
        self.completed(domain, replay_epoch)

    def test_bound_domain_env_change_nested_epochs_and_readonly_all_five(self):
        case = self.fixture(i6); service = case.service; context = service.context
        domain = service.ops._writer_domain; packet = case.prepare()
        evidence = service.upload(json.dumps(case.document(packet)))
        body = {'packet_id': packet['packet_id'], 'evidence_id': evidence['evidence_id'],
                'evidence_kind': 'agent_reported'}
        plan = context.preview(case.a['id'], {'backend': 'managed-copy', 'selected_skills': ['common:alpha']})
        self.assertEqual(plan['blockers'], [])
        alternate = case.base / 'other_userdata'
        with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(alternate)}):
            epoch = domain.observe()['epoch']
            applied = context.apply(case.a['id'], plan['plan_id'], plan['operation_id'])
            self.assertEqual(applied['status'], 'succeeded'); self.completed(domain, epoch)
            self.assertFalse(alternate.exists(), 'bound methods redirected to changed environment')
            before = tree(case.root), sql(context.ops), sql(service.ops)
            calls = [lambda: context.preview(case.a['id'], {'backend': 'managed-copy', 'selected_skills': ['common:alpha']}),
                     lambda: context.apply(case.a['id'], plan['plan_id'], plan['operation_id'], recover=True),
                     case.prepare, lambda: service.upload('blocked'),
                     lambda: service.observe(case.a['id'], body)]
            with domain.barrier(0):
                for call in calls:
                    with self.assertRaises(writer_admission.WriterUnavailable):
                        call()
                self.assertEqual((tree(case.root), sql(context.ops), sql(service.ops)), before)
            epoch = domain.observe()['epoch']
            with domain.writing():
                active = domain.observe()
                nested_plan = context.preview(case.a['id'], {'backend': 'managed-copy', 'selected_skills': ['common:alpha']})
                context.apply(case.a['id'], nested_plan['plan_id'], nested_plan['operation_id'])
                case.prepare(); service.upload('nested'); service.observe(case.a['id'], body)
                self.assertEqual(domain.observe()['epoch'], active['epoch'])
                self.assertEqual(domain.observe()['active'], active['active'])
            self.completed(domain, epoch); self.assertFalse(alternate.exists())
            before = tree(case.base), sql(context.ops), sql(service.ops), domain.observe()
            context.setup(case.a['id']); context.status(case.a['id'], plan['operation_id'])
            service.inspect(case.a['id'], 'codex'); service.status(case.a['id']); service.dashboard()
            self.assertEqual((tree(case.base), sql(context.ops), sql(service.ops), domain.observe()), before)

    def test_invalid_session_storage_refuses_all_writers_without_control_files(self):
        case = self.fixture(i6)
        packet = case.prepare()
        evidence = case.service.upload(json.dumps(case.document(packet)))
        body = {'packet_id': packet['packet_id'], 'evidence_id': evidence['evidence_id'],
                'evidence_kind': 'agent_reported'}
        userdata = case.home / 'user-data'
        before = tree(case.base)
        unchanged = []
        with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(userdata)}):
            service = session.Service(case.registry)
            self.assertEqual(service.inspect(case.a['id'], 'codex')['schema_version'], 1)
            self.assertEqual(service.status(case.a['id'])['schema_version'], 1)
            self.assertEqual(tree(case.base), before, 'invalid storage readers created files')
            calls = [lambda: service.prepare(case.a['id'], {'tool': 'codex', 'version': 'test-1', 'surface': 'cli'}),
                     lambda: service.upload('invalid authority must not publish evidence'),
                     lambda: service.observe(case.a['id'], body)]
            for call in calls:
                with self.assertRaises(operations.StateConflict) as caught:
                    call()
                self.assertIn('ROOT/프로젝트 밖', str(caught.exception))
                unchanged.append(tree(case.base) == before)
            self.assertEqual(unchanged, [True, True, True], 'prepare/upload/observe created control or business files')
            self.assertFalse(userdata.exists())

    def test_owned_process_raw_recover_refuses_five_second_budget_then_succeeds(self):
        case = self.fixture(i5); case.root_init(); plan = case.plan(); service = case.service
        service.ops.approve(plan['operation_id']); service.ops.claim(plan['operation_id'])
        domain = service.ops._writer_domain
        command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                   '--workspace-writer-actor', str(case.store.file), case.a['id'],
                   plan['plan_id'], plan['operation_id']]
        observations = []
        def launch():
            child = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            try:
                stdout, stderr = child.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill(); stdout, stderr = child.communicate(timeout=5)
                self.fail('owned raw workspace child watchdog expired: ' + stderr)
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate(timeout=5)
            self.assertIsNotNone(child.returncode)
            self.assertEqual(child.returncode, 0, stderr)
            value = json.loads(stdout.strip()); observations.append(value)
            return value
        before = tree(case.root), sql(service.ops)
        with domain.barrier(0):
            refused = launch()
            self.assertEqual(refused['reason'], 'busy')
            # Controlled owned child: default5s admission budget plus scheduler margin.
            self.assertLess(refused['elapsed'], 7.5)
            self.assertEqual((tree(case.root), sql(service.ops)), before)
        epoch = domain.observe()['epoch']; succeeded = launch()
        self.assertEqual(succeeded['status'], 'succeeded'); self.action_bytes(service, plan)
        self.completed(domain, epoch)
        print('WS63_PROCESS ' + json.dumps({'observations': observations, 'owned_children_reaped': 2, 'residual': 0}))


def actor(arguments):
    registry, pid, plan, operation = arguments
    service = projection.Service(Store(registry)); started = time.monotonic()
    try:
        value = service.apply(pid, plan, operation, recover=True)
        result = {'status': value['status'], 'reason': None}
    except writer_admission.WriterUnavailable as error:
        result = {'status': 'refused', 'reason': error.reason}
    result['elapsed'] = round(time.monotonic() - started, 3)
    print(json.dumps(result))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--workspace-writer-actor']:
        actor(sys.argv[2:])
    else:
        unittest.main()
