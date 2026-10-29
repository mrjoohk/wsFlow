"""P062 representative raw roots: real registry, memo, operation and calendar effects."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from wsflow import common_writes, operations, wfhome
from wsflow.modules.work_calendar import calendar_store
from wsflow.store import Store as Registry


class WriterRoots(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow writer roots ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.userdata = self.base / 'userdata'
        environment = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.userdata)})
        environment.start()
        self.addCleanup(environment.stop)
        self.registry_path = self.base / 'registry/projects.json'
        self.registry_path.parent.mkdir()
        self.home = self.base / 'project'
        self.home.mkdir()
        (self.home / '.workflow').mkdir()
        (self.home / '.workflow/state.json').write_text(
            json.dumps({'schema_version': 4, 'project_key': 'XXX', 'roots': {}}), encoding='utf-8')
        self.project = {'id': 'p', 'path': str(self.home), 'alias': 'test'}

    def api(self):
        from wsflow import writer_admission
        return writer_admission

    def prepare_memo(self):
        memo = wfhome.read_memo(self.home, '261029')
        service = common_writes.Service(self.project, 'memo', self.home)
        payload = dict(ymd='261029', text='- XXX-1: 저장 완료', sha=memo['sha'],
                       document_id=memo['document_id'])
        return service, payload

    def test_stale_registry_instances_preserve_each_others_projects(self):
        first, second = Registry(self.registry_path), Registry(self.registry_path)
        another = self.base / 'another'
        another.mkdir()
        a = first.add_project(self.home)
        b = second.add_project(another)
        saved = json.loads(self.registry_path.read_text(encoding='utf-8'))
        self.assertEqual({a['id'], b['id']}, {p['id'] for p in saved['projects']})

    def test_stale_registry_updates_and_removals_do_not_resurrect_entries(self):
        first = Registry(self.registry_path)
        a = first.add_project(self.home)
        another = self.base / 'another'
        another.mkdir()
        b = first.add_project(another)
        second = Registry(self.registry_path)
        first.remove_project(a['id'])
        second.update_project(b['id'], alias='latest')
        self.assertEqual([(b['id'], 'latest')],
                         [(p['id'], p['alias']) for p in Registry(self.registry_path).projects()])
        third = Registry(self.registry_path)
        first.add_root(self.base)
        third.remove_root(str(self.base))
        self.assertEqual([], Registry(self.registry_path).roots())

    def test_create_project_refreshes_roots_and_nested_registration_completes(self):
        first, stale = Registry(self.registry_path), Registry(self.registry_path)
        first.add_root(self.base)
        result, errors = [], []
        def create():
            try:
                result.append(stale.create_project(str(self.base), 'created'))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=create, daemon=True)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive(), 'recursive registration must not deadlock')
        self.assertEqual([], errors)
        self.assertEqual('created', result[0]['id'])
        self.assertTrue((self.base / 'created').is_dir())
        self.assertEqual([result[0]], Registry(self.registry_path).projects())

    def test_barrier_refuses_raw_sql_before_mkdir_open_or_ddl(self):
        api = self.api()
        store = operations.Store(self.home)
        with api.default_domain().barrier(timeout=0):
            with self.assertRaises(api.WriterUnavailable):
                with store.connection(True):
                    self.fail('write SQL connection entered')
            with self.assertRaises(api.WriterUnavailable):
                store.create({'kind': 'test'})
            self.assertFalse(store.path.parent.exists())

    def test_operation_root_includes_post_commit_readback(self):
        api = self.api()
        store = operations.Store(self.home)
        reached, release = threading.Event(), threading.Event()
        results, errors = [], []
        original_get = store.get
        def held_get(ident):
            reached.set()
            if not release.wait(5):
                raise TimeoutError('test readback watchdog')
            return original_get(ident)
        def create():
            try:
                results.append(store.create({'kind': 'readback'}))
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(store, 'get', held_get):
            worker = threading.Thread(target=create, daemon=True)
            worker.start()
            try:
                self.assertTrue(reached.wait(3))
                with closing(sqlite3.connect(store.path)) as db:
                    self.assertEqual('planned', db.execute('SELECT status FROM operations').fetchone()[0])
                with self.assertRaises(api.WriterUnavailable):
                    with api.default_domain().barrier(timeout=0):
                        self.fail('barrier passed before create returned its readback')
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual('planned', results[0]['status'])
        with api.default_domain().barrier(timeout=0):
            self.assertEqual(results[0], original_get(results[0]['operation_id']))

    def test_approve_root_includes_committed_result_readback(self):
        api=self.api();domain=api.default_domain();store=operations.Store(self.home)
        operation=store.create({'kind':'standalone approval'})
        original_get=store.get;observed=[];before=domain.observe()['epoch']
        def readback(ident):
            value=original_get(ident)
            observed.append(value['status'])
            with self.assertRaises(api.WriterUnavailable):
                with domain.barrier(0):self.fail('approve returned its lease before readback')
            return value
        with mock.patch.object(store,'get',readback):
            result=store.approve(operation['operation_id'])
        self.assertEqual(observed,['approved']);self.assertEqual(result['status'],'approved')
        self.assertEqual(domain.observe()['epoch']-before,2)
        self.assertIsNone(domain.observe()['active'])

    def test_legacy_root_spans_preview_and_apply_without_barrier_gap(self):
        api=self.api();domain=api.default_domain();service,payload=self.prepare_memo()
        original_preview=service.preview;seen=[];before=domain.observe()['epoch']
        def preview(*args,**kwargs):
            result=original_preview(*args,**kwargs)
            seen.append(result['operation_id'])
            with self.assertRaises(api.WriterUnavailable):
                with domain.barrier(0):self.fail('legacy released between preview and apply')
            return result
        with mock.patch.object(service,'preview',preview):
            result=service.legacy('save',payload)
        self.assertEqual(seen,[result['operation_id']])
        self.assertTrue(result['legacy_single_request']);self.assertEqual(result['status'],'succeeded')
        self.assertEqual(service.store.get(result['operation_id'])['status'],'succeeded')
        self.assertEqual(domain.observe()['epoch']-before,2)
        self.assertIsNone(domain.observe()['active'])

    def test_barrier_refuses_registry_mutations_before_effect(self):
        api = self.api()
        registry = Registry(self.registry_path)
        registry.add_root(self.base)
        project = registry.add_project(self.home)
        before = self.registry_path.read_bytes()
        another = self.base / 'another'
        another.mkdir()
        actions = [lambda: registry.add_root(another), lambda: registry.remove_root(str(self.base)),
                   lambda: registry.add_project(another),
                   lambda: registry.create_project(str(self.base), 'blocked'),
                   lambda: registry.update_project(project['id'], alias='blocked'),
                   lambda: registry.remove_project(project['id'])]
        with api.default_domain().barrier(timeout=0):
            for action in actions:
                with self.subTest(action=actions.index(action)), self.assertRaises(api.WriterUnavailable):
                    action()
            self.assertEqual(before, self.registry_path.read_bytes())
            self.assertFalse((self.base / 'blocked').exists())

    def test_barrier_refuses_daily_creation_read_and_memo_write(self):
        api = self.api()
        memo = wfhome.read_memo(self.home, '261029')
        daily = wfhome.daily_path(self.home, '261029')
        before = daily.read_bytes()
        actions = [lambda: wfhome.ensure_daily(self.home, '261030'),
                   lambda: wfhome.read_memo(self.home, '261030'),
                   lambda: wfhome.write_memo(self.home, '261029', 'blocked', memo['sha'], memo['document_id'])]
        with api.default_domain().barrier(timeout=0):
            for action in actions:
                with self.subTest(action=actions.index(action)), self.assertRaises(api.WriterUnavailable):
                    action()
            self.assertFalse(wfhome.daily_path(self.home, '261030').exists())
            self.assertEqual(before, daily.read_bytes())

    def test_common_preview_apply_check_and_legacy_refuse_before_effect(self):
        api = self.api()
        service, payload = self.prepare_memo()
        plan = service.preview('save', payload)
        daily = wfhome.daily_path(self.home, '261029')
        before = daily.read_bytes()
        actions = [lambda: service.preview('save', payload),
                   lambda: service.apply(plan['plan_id'], plan['operation_id']),
                   lambda: service.check(plan['operation_id']), lambda: service.legacy('save', payload)]
        with api.default_domain().barrier(timeout=0):
            for action in actions:
                with self.subTest(action=actions.index(action)), self.assertRaises(api.WriterUnavailable):
                    action()
            self.assertEqual(before, daily.read_bytes())
            self.assertEqual(1, len(service.store.list()))
            self.assertEqual('planned', service.store.get(plan['operation_id'])['status'])

    def test_bound_common_preview_refuses_before_lockfile_creation(self):
        api = self.api()
        # Constructing a service reads no memo and creates neither SQL nor lock files.
        service = common_writes.Service(self.project, 'memo', self.home)
        lock = self.home / '.workflow/wsflow.lock'
        with api.default_domain().barrier(timeout=0):
            with self.assertRaises(api.WriterUnavailable):
                service.preview('save', dict(ymd='261029', text='blocked', sha='x', document_id='x'))
            self.assertFalse(lock.exists())
            self.assertFalse(service.store.path.exists())

    def test_registry_mutation_refuses_corrupt_newer_disk_without_overwrite(self):
        registry = Registry(self.registry_path)
        registry.add_project(self.home)
        broken = b'{"roots":'
        self.registry_path.write_bytes(broken)
        with self.assertRaises(json.JSONDecodeError):
            registry.update_project('project', alias='must not replace corruption')
        self.assertEqual(broken, self.registry_path.read_bytes())

    def test_memo_saga_holds_domain_through_receipt_and_sql_terminal(self):
        api = self.api()
        service, payload = self.prepare_memo()
        plan = service.preview('save', payload)
        ident = plan['operation_id']
        target = wfhome.daily_path(self.home, '261029')
        before = target.read_bytes()
        source = service.store.plan(plan['plan_id'])['source']
        reached, release = threading.Event(), threading.Event()
        results, errors = [], []
        original_finish = service.store.finish
        def held_finish(*args, **kwargs):
            reached.set()
            if not release.wait(5):
                raise TimeoutError('test saga watchdog')
            return original_finish(*args, **kwargs)
        def apply():
            try:
                results.append(service.apply(plan['plan_id'], ident))
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(service.store, 'finish', held_finish):
            worker = threading.Thread(target=apply, daemon=True)
            worker.start()
            try:
                self.assertTrue(reached.wait(3), 'real effect and receipt must precede terminal hook')
                receipt, backup = service._paths(ident)
                self.assertEqual(before, backup.read_bytes())
                saved = json.loads(receipt.read_text(encoding='utf-8'))
                checksum = saved.pop('sha256')
                self.assertEqual(operations.digest(saved), checksum)
                self.assertEqual('succeeded', saved['status'])
                self.assertEqual(source['target_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
                self.assertEqual('executing', service.store.get(ident)['status'])
                with self.assertRaises(api.WriterUnavailable):
                    with api.default_domain().barrier(timeout=0):
                        self.fail('barrier passed an unfinished saga')
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual('succeeded', results[0]['status'])
        self.assertEqual('succeeded', service.store.get(ident)['status'])
        with service.store.connection() as db:
            events = [row[0] for row in db.execute(
                'SELECT status FROM events WHERE operation_id=? ORDER BY sequence', (ident,))]
        self.assertEqual(['planned', 'approved', 'executing', 'succeeded'], events)
        self.assertEqual(source['target_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        with api.default_domain().barrier(timeout=0):
            self.assertEqual('succeeded', service.status(ident)['status'])

    def test_calendar_preview_apply_participate_and_commit_exact_record(self):
        api = self.api()
        store = calendar_store.Store(self.registry_path)
        request = {'action': 'create', 'kind': 'note',
                   'payload': {'date': '2026-10-29', 'body': 'local calendar', 'project_ids': []}}
        with api.default_domain().barrier(timeout=0):
            with self.assertRaises(api.WriterUnavailable):
                store.preview(request, [])
            self.assertFalse(store.path.exists())
        plan = store.preview(request, [])
        with api.default_domain().barrier(timeout=0):
            with self.assertRaises(api.WriterUnavailable):
                store.apply(plan['plan_id'], plan['operation_id'], [])
            self.assertEqual([], store.records())
        result = store.apply(plan['plan_id'], plan['operation_id'], [])
        self.assertEqual([result['record']], store.records())
        self.assertEqual('local calendar', result['record']['body'])
        self.assertEqual('succeeded', store.get(plan['operation_id'])['status'])
        with store.connection() as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM calendar_records').fetchone()[0])

    def test_independent_process_registry_writer_refuses_then_succeeds(self):
        api = self.api()
        first = Registry(self.registry_path).add_project(self.home)
        another = self.base / 'another'
        another.mkdir()
        before = self.registry_path.read_bytes()
        actor = Path(__file__).with_name('writer_roots_actor.py')
        command = [getattr(sys, '_base_executable', sys.executable), '-B', '-X', 'utf8',
                   str(actor), str(self.registry_path), str(another)]
        def run():
            # This owned stdlib actor creates no descendants. Timeout cleanup is direct and bounded.
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8')
            try:
                output, error = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
                raise
            self.assertEqual(0, process.returncode, error)
            return json.loads(output)
        with api.default_domain().barrier(timeout=0):
            self.assertEqual('blocked', run()['outcome'])
            self.assertEqual(before, self.registry_path.read_bytes())
        done = run()
        self.assertEqual('saved', done['outcome'])
        self.assertEqual({first['id'], done['project']['id']},
                         {p['id'] for p in Registry(self.registry_path).projects()})

    def test_unwrapped_sqlite_bypass_remains_outside_cooperative_scope(self):
        api = self.api()
        store = operations.Store(self.home)
        operation = store.create({'kind': 'test'})
        with api.default_domain().barrier(timeout=0):
            with closing(sqlite3.connect(store.path)) as raw:
                raw.execute('UPDATE operations SET local_recorded=99 WHERE id=?', (operation['operation_id'],))
                raw.commit()
            with store.connection() as db:
                self.assertEqual(99, db.execute('SELECT local_recorded FROM operations').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
