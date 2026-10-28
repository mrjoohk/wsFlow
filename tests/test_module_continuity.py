"""C2 read-only, fail-closed observations of real synthetic persistence."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import hashlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import closing

from wsflow import module_continuity as continuity, operations


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow-continuity-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'; self.root.mkdir()
        self.project = self.root / 'checkout'; self.project.mkdir()
        self.registry = self.root / 'projects.json'
        self.registry.write_text(json.dumps({'roots':[{'path':str(self.root)}],
                                             'projects':[{'id':'sample','path':str(self.project)}]}), encoding='utf-8')
        self.data = self.base / 'userdata'
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR':str(self.data), 'APPDATA':str(self.base / 'appdata')})
        patch.start(); self.addCleanup(patch.stop)
        self.host = self.data / 'modules' / operations.digest(str(self.registry.resolve()))
        self.current = {'modules':{key:{'id':key,'installed':True,'enabled':True,'version':'1.0.0',
                                       'package_digest':'a','requires':[], 'data_schema_version':1}
                                  for key in ('work-calendar','workspace-operations')}}
        self.proposed = copy.deepcopy(self.current)
        self.proposed['modules']['work-calendar'].update(version='1.1.0', package_digest='b')

    def audit(self, action='update', ident='work-calendar'):
        return continuity.audit_transition(self.registry, self.host, self.current, self.proposed, action, ident)

    def preserve(self):
        for ident, record in self.proposed['modules'].items():
            record['data_compatibility'] = {'version':1, 'preserves':[ident + '-v1'], 'pending_policy':'block'}

    @staticmethod
    def codes(report):
        return {item['code'] for item in report['blockers']}

    def calendar(self):
        from wsflow.modules.work_calendar.calendar_store import Store
        from wsflow.modules.work_calendar.calendar_sync import ensure
        store = Store(self.registry)
        with store.connection(True) as db:
            ensure(db)
        return store

    def terminal(self, store=None, kind='git'):
        store = store or operations.Store(self.project)
        ident = store.create({'kind':kind, 'payload':{'private':'PRIVATE PAYLOAD'}})['operation_id']
        store.approve(ident); store.claim(ident); store.finish(ident, 'succeeded', {'private':'PRIVATE RESULT'})
        return store, ident

    def source_snapshot(self):
        return {str(path.relative_to(self.base)):hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.base.rglob('*') if path.is_file()}

    def test_absent_stores_create_no_files(self):
        before = set(self.base.rglob('*'))
        report = self.audit()
        self.assertTrue(report['ready'], report)
        self.assertEqual(set(self.base.rglob('*')), before)
        self.assertEqual(report['scope'], 'preflight_only')

    def test_pending_shared_operation_blocks(self):
        operations.Store(self.project).create({'kind':'git','payload':{'message':'PRIVATE BODY'}})
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('pending_operations', {b['code'] for b in report['blockers']})
        self.assertNotIn('PRIVATE BODY', json.dumps(report))

    def test_contract_is_strict(self):
        good = {'version':1,'preserves':['work-calendar-v1'],'pending_policy':'block'}
        self.assertEqual(continuity.validate_contract(good), good)
        self.assertEqual(continuity.validate_contract(None), {})
        for value in ({}, {**good,'version':True}, {**good,'preserves':['unknown']},
                      {**good,'preserves':['work-calendar-v1']*2}, {**good,'pending_policy':'allow'},
                      {**good,'extra':True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                continuity.validate_contract(value)

    def test_all_common_statuses_and_unknown_are_fail_closed(self):
        self.preserve()
        store, ident = self.terminal()
        # These literals are the operation contract oracle, independent of the
        # scanner constants whose accidental omissions this test must detect.
        pending = {'planned', 'approved', 'executing', 'uncertain'}
        terminal = {'succeeded', 'failed'}
        for state in ('planned', 'approved', 'executing', 'uncertain', 'succeeded', 'failed', 'private_unknown'):
            with self.subTest(state=state):
                with store.connection(True) as db:
                    db.execute('UPDATE operations SET status=? WHERE id=?', (state, ident))
                report = self.audit()
                if state in terminal:
                    self.assertTrue(report['ready'], report['blockers'])
                else:
                    self.assertFalse(report['ready'])
                    self.assertIn('pending_operations' if state in pending else 'unknown_operation_state', self.codes(report))
        self.assertNotIn('private_unknown', json.dumps(report))

    def test_all_outbox_states_including_queued_auto_send(self):
        self.preserve(); store = self.calendar()
        with store.connection(True) as db:
            db.execute("INSERT INTO calendar_outbox VALUES('GOP-test','E-test','{}','queued',NULL,NULL)")
        pending = {'queued', 'syncing', 'uncertain', 'error', 'conflict'}
        terminal = {'synced', 'cancelled'}
        for state in ('queued', 'syncing', 'uncertain', 'error', 'conflict', 'synced', 'cancelled', 'private_unknown'):
            with self.subTest(state=state):
                with store.connection(True) as db:
                    db.execute('UPDATE calendar_outbox SET state=?', (state,))
                report = self.audit()
                if state in terminal:
                    self.assertTrue(report['ready'], report['blockers'])
                else:
                    self.assertFalse(report['ready'])
                    self.assertIn('pending_outbox' if state in pending else 'unknown_outbox_state', self.codes(report))

    def test_uncheckpointed_wal_is_read_from_owned_clone_only_and_sources_unchanged(self):
        self.preserve(); store = self.calendar()
        writer = sqlite3.connect(str(store.path)); self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL'); writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        baseline = store.path.read_bytes()
        writer.execute("INSERT INTO calendar_outbox VALUES('GOP-wal','E-test','{}','queued',NULL,NULL)"); writer.commit()
        self.assertEqual(store.path.read_bytes(), baseline)
        self.assertGreater(Path(str(store.path) + '-wal').stat().st_size, 0)
        before = self.source_snapshot()
        original_connect = sqlite3.connect; opened = []
        def guarded(path, *args, **kwargs):
            opened.append(Path(path))
            self.assertIn('wsflow-continuity-sqlite-', str(path))
            self.assertNotEqual(Path(path), store.path)
            return original_connect(path, *args, **kwargs)
        with mock.patch.object(continuity.sqlite3, 'connect', side_effect=guarded):
            report = self.audit()
        self.assertTrue(opened)
        self.assertIn('pending_outbox', self.codes(report))
        calendar = next(s for s in report['stores'] if s['role'] == 'calendar')
        self.assertEqual(calendar['counts']['outbox_states'], {'queued':1})
        self.assertEqual(self.source_snapshot(), before)
        self.assertFalse(any(path.exists() for path in opened))

    def test_terminal_stores_are_readonly_and_fingerprint_is_stable(self):
        self.preserve(); self.terminal(); before = self.source_snapshot()
        one, two = self.audit(), self.audit()
        self.assertTrue(one['ready'], one['blockers'])
        self.assertEqual(one['fingerprint'], two['fingerprint'])
        self.assertEqual(before, self.source_snapshot())
        self.assertNotIn('PRIVATE PAYLOAD', json.dumps(one))
        self.assertNotIn('PRIVATE RESULT', json.dumps(one))

    def test_nonempty_builtin_requires_own_preservation_declaration(self):
        self.terminal()
        self.assertIn('preservation_contract_required', self.codes(self.audit()))
        self.proposed['modules']['work-calendar']['data_compatibility'] = {'version':1,'preserves':['module-data-v1'],'pending_policy':'block'}
        self.assertIn('preservation_contract_required', self.codes(self.audit()))
        self.preserve(); self.assertTrue(self.audit()['ready'])

    def test_different_id_replace_retains_old_data_without_automatic_adoption(self):
        self.terminal(); before = self.source_snapshot()
        self.proposed['modules']['work-calendar'].update(installed=False, enabled=False)
        self.proposed['modules']['other-calendar'] = {'id':'other-calendar', 'installed':True, 'enabled':True,
            'version':'1.0.0', 'package_digest':'other', 'requires':[], 'data_schema_version':1}
        report = self.audit('replace')
        self.assertTrue(report['ready'], report['blockers'])
        self.assertEqual(set(report['affected_modules']), {'work-calendar','other-calendar'})
        self.assertEqual(before, self.source_snapshot())

    def test_missing_required_sqlite_schema_blocks(self):
        self.preserve(); store, ident = self.terminal()
        with store.connection(True) as db:
            db.execute('DROP TABLE events')
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('unknown_sqlite_schema', self.codes(report))

    def test_unknown_user_version_in_otherwise_intact_database_blocks(self):
        self.preserve(); store, ident = self.terminal()
        self.assertTrue(self.audit()['ready'])
        with closing(sqlite3.connect(str(store.path))) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {'plans', 'operations', 'events'})
            db.execute('PRAGMA user_version=99')
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('unknown_sqlite_schema', self.codes(report))

    def test_unknown_sqlite_table_on_an_intact_terminal_store_blocks(self):
        self.preserve(); store, _ = self.terminal()
        self.assertTrue(self.audit()['ready'])
        with store.connection(True) as db:
            db.execute('CREATE TABLE future_jobs(payload TEXT)')
            db.execute("INSERT INTO future_jobs VALUES('awaiting_retry')")
        before = self.source_snapshot()
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('unknown_sqlite_schema', self.codes(report))
        self.assertEqual(self.source_snapshot(), before)

    def test_extra_sqlite_column_on_an_intact_terminal_store_blocks(self):
        self.preserve(); store, _ = self.terminal()
        self.assertTrue(self.audit()['ready'])
        with store.connection(True) as db:
            db.execute('ALTER TABLE operations ADD COLUMN future_state TEXT')
            db.execute("UPDATE operations SET future_state='awaiting_retry'")
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('unknown_sqlite_schema', self.codes(report))

    def test_invalid_local_recorded_metadata_on_an_intact_terminal_store_blocks(self):
        self.preserve(); store, ident = self.terminal()
        self.assertTrue(self.audit()['ready'])
        with store.connection(True) as db:
            db.execute('UPDATE operations SET local_recorded=77 WHERE id=?', (ident,))
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('unknown_sqlite_schema', self.codes(report))

    def test_calendar_missing_required_records_or_partial_outbox_blocks(self):
        self.preserve(); store = self.calendar()
        with store.connection(True) as db:
            db.execute('DROP TABLE calendar_outbox')
        self.assertIn('unknown_sqlite_schema', self.codes(self.audit()))
        with store.connection(True) as db:
            db.execute('DROP TABLE calendar_links'); db.execute('DROP TABLE calendar_records')
        self.assertIn('unknown_sqlite_schema', self.codes(self.audit()))

    def test_empty_or_invalid_existing_sqlite_is_not_absent_success(self):
        path = self.data / 'calendar' / (operations.digest(str(self.registry.resolve())) + '.sqlite3')
        path.parent.mkdir(parents=True)
        for raw in (b'', b'not a sqlite database'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.assertIn('unknown_sqlite_schema', self.codes(self.audit()))

    def test_orphan_wal_blocks_without_creating_database(self):
        path = self.data / 'calendar' / (operations.digest(str(self.registry.resolve())) + '.sqlite3')
        path.parent.mkdir(parents=True); Path(str(path) + '-wal').write_bytes(b'orphan')
        before = self.source_snapshot()
        self.assertIn('orphan_sqlite_sidecar', self.codes(self.audit()))
        self.assertEqual(before, self.source_snapshot()); self.assertFalse(path.exists())

    def test_legacy_unknown_and_missing_success_exports_block(self):
        self.preserve(); path = self.project / '.workflow/approvals.jsonl'; path.parent.mkdir()
        path.write_text(json.dumps({'event':'approved','id':'OLD-private'}) + '\n', encoding='utf-8')
        self.assertIn('legacy_uncertain', self.codes(self.audit()))
        store, ident = self.terminal(kind='comment')
        path.write_text(json.dumps({'event':'approved','id':ident}) + '\n', encoding='utf-8')
        self.assertIn('unrecorded_success', self.codes(self.audit()))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'event':'pushed','id':ident}) + '\n')
        self.assertTrue(self.audit()['ready'], self.audit()['blockers'])
        self.assertNotIn('OLD-private', json.dumps(self.audit()))

    def test_successful_create_with_export_but_missing_local_record_blocks(self):
        self.preserve(); store, ident = self.terminal(kind='create_issue')
        path = self.project / '.workflow/approvals.jsonl'; path.parent.mkdir()
        path.write_text(json.dumps({'event':'pushed','id':ident}) + '\n', encoding='utf-8')
        self.assertIn('unrecorded_success', self.codes(self.audit()))
        store.local_recorded(ident)
        self.assertTrue(self.audit()['ready'], self.audit()['blockers'])

    def test_malformed_legacy_log_blocks(self):
        path = self.project / '.workflow/approvals.jsonl'; path.parent.mkdir()
        for raw in ('{broken', '{"event":"invented","id":"x"}', '[]'):
            path.write_text(raw, encoding='utf-8')
            self.assertIn('unknown_legacy_approval', self.codes(self.audit()))

    def test_workflow_batch_uses_actual_state_marker_terminal_rule(self):
        self.preserve()
        folder = self.project / '.workflow/batches/run1'; folder.mkdir(parents=True)
        state = folder.parent.parent / 'state.json'
        state.write_text(json.dumps({'schema_version':4}), encoding='utf-8')
        receipt = folder / 'receipt.json'
        receipt.write_text(json.dumps({'schema_version':1,'run_id':'run1','status':'executing',
                                       'created_at':'2026-10-28T00:00:00+00:00'}), encoding='utf-8')
        self.assertIn('pending_batch', self.codes(self.audit()))
        state.write_text(json.dumps({'schema_version':4,'last_batch':{'run_id':'run1'}}), encoding='utf-8')
        self.assertTrue(self.audit()['ready'], self.audit()['blockers'])
        state.write_text(json.dumps({'schema_version':99}), encoding='utf-8')
        self.assertIn('unknown_schema', self.codes(self.audit()))

    def test_workflow_receipt_metadata_is_validated_independently(self):
        self.preserve()
        workflow = self.project / '.workflow'
        receipt = workflow / 'batches' / 'run1' / 'receipt.json'
        receipt.parent.mkdir(parents=True)
        (workflow / 'state.json').write_text(json.dumps({'schema_version':4}), encoding='utf-8')
        valid = {'schema_version':1, 'run_id':'run1', 'status':'failed',
                 'created_at':'2026-10-28T00:00:00+00:00'}
        receipt.write_text(json.dumps(valid), encoding='utf-8')
        self.assertTrue(self.audit()['ready'])
        for change in ({'status':'invented'}, {'schema_version':99}, {'run_id':None},
                       {'created_at':'2026-10-28T00:00:00'}):
            with self.subTest(change=change):
                receipt.write_text(json.dumps({**valid, **change}), encoding='utf-8')
                report = self.audit()
                self.assertFalse(report['ready'])
                self.assertIn('unknown_schema', self.codes(report))

    def test_internal_directory_listing_bound_is_separate_from_file_read_bound(self):
        self.preserve()
        daily = self.project / 'daily'
        daily.mkdir()
        for number in range(129):
            (daily / ('empty-' + str(number))).mkdir()
        self.assertTrue(self.audit()['ready'])
        with mock.patch.object(continuity, 'MAX_FILES', 128):
            report = self.audit()
        self.assertFalse(report['ready'])
        self.assertIn('namespace_unknown', self.codes(report))
        self.assertNotIn('file_limit', self.codes(report))

    def test_multiple_outbox_rows_report_exact_state_counts_and_pending_total(self):
        self.preserve(); store = self.calendar()
        with store.connection(True) as db:
            for number, state in enumerate(('queued','queued','error','synced','synced','synced')):
                db.execute('INSERT INTO calendar_outbox VALUES(?,?,?,?,NULL,NULL)',
                           ('GOP-' + str(number), 'E-test', '{}', state))
        report = self.audit()
        calendar = next(s for s in report['stores'] if s['role'] == 'calendar')
        self.assertEqual(calendar['counts']['outbox_states'], {'queued':2,'error':1,'synced':3})
        self.assertEqual(calendar['pending_count'], 3)
        self.assertEqual(report['pending_count'], 3)
        self.assertFalse(report['ready'])

    def test_restore_reenable_and_dependency_closure(self):
        self.current['modules']['work-calendar']['enabled'] = False
        self.proposed = copy.deepcopy(self.current)
        self.proposed['modules']['work-calendar']['enabled'] = True
        self.current['modules']['consumer'] = {'id':'consumer','installed':True,'enabled':True,
            'package_digest':'consumer','version':'1.0.0','requires':['work-calendar']}
        self.proposed['modules']['consumer'] = copy.deepcopy(self.current['modules']['consumer'])
        self.current['modules']['second'] = {'id':'second','installed':True,'enabled':False,
            'package_digest':'second','version':'1.0.0','requires':['consumer']}
        self.proposed['modules']['second'] = copy.deepcopy(self.current['modules']['second'])
        report = self.audit('restore')
        self.assertEqual(set(report['affected_modules']), {'work-calendar','consumer','second'})

    def test_disable_uninstall_configure_and_enable_do_not_scan(self):
        operations.Store(self.project).create({'kind':'git'})
        for action in ('disable','uninstall','configure','enable'):
            report = self.audit(action)
            self.assertTrue(report['ready']); self.assertEqual(report['stores'], [])

    def test_install_reregisters_same_package_and_pinning_is_not_code_change(self):
        self.current['modules']['work-calendar']['installed'] = False
        self.proposed = copy.deepcopy(self.current)
        self.proposed['modules']['work-calendar']['installed'] = True
        self.assertEqual(self.audit('install')['affected_modules'], ['work-calendar'])
        self.current = copy.deepcopy(self.proposed)
        self.proposed['modules']['work-calendar'].update(source_kind='managed',package_path='cache/pinned')
        self.assertEqual(self.audit('restore')['affected_modules'], [])

    def test_session_receipts_are_passive_only_in_dedicated_sessions_store(self):
        self.preserve()
        self.proposed['modules']['workspace-operations'].update(version='1.1.0', package_digest='c')
        store = operations.Store(str(self.registry.resolve()) + '.sessions')
        op = store.create({'kind':'session_receipt','notes':'PRIVATE OBSERVATION'}, ident='R-test')
        report = self.audit()
        self.assertTrue(report['ready'], report['blockers'])
        session = next(s for s in report['stores'] if s['role'] == 'session-operations')
        self.assertEqual(session['counts']['passive_session_receipts'], 1)
        self.assertNotIn('PRIVATE OBSERVATION', json.dumps(report))
        store.approve(op['operation_id'])
        self.assertIn('unknown_session_operation', self.codes(self.audit()))
        operations.Store(self.project).create({'kind':'session_receipt'})
        self.assertIn('pending_operations', self.codes(self.audit()))

    def test_session_receipt_in_shared_store_remains_an_unfinished_operation(self):
        self.preserve()
        operations.Store(self.project).create({'kind':'session_receipt'})
        report = self.audit()
        shared = next(s for s in report['stores'] if s['role'] == 'shared-operations' and s['present'])
        self.assertFalse(report['ready'])
        self.assertEqual(shared['counts']['operation_states'], {'planned':1})
        self.assertEqual(shared['pending_count'], 1)
        self.assertNotIn('passive_session_receipts', shared['counts'])
        self.assertEqual(self.codes(report), {'pending_operations'})

    def test_opaque_namespace_blocks_without_opening_or_hashing_secret_body(self):
        path = self.host / 'data/work-calendar/credentials.bin'; path.parent.mkdir(parents=True)
        path.write_bytes(b'PRIVATE TOKEN VALUE')
        original = Path.open
        def guarded(item, *args, **kwargs):
            self.assertNotEqual(item, path)
            return original(item, *args, **kwargs)
        with mock.patch.object(Path, 'open', guarded):
            report = self.audit()
        self.assertIn('opaque_data', self.codes(report))
        self.assertNotIn(hashlib.sha256(path.read_bytes()).hexdigest(), json.dumps(report))

    def test_credentials_and_oauth_client_are_references_only(self):
        self.preserve()
        paths = [self.data / 'google' / operations.digest(str(self.registry.resolve())) / 'credentials.bin',
                 self.base / 'appdata/wsFlow/credentials.json', self.data / 'google-client.json']
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'PRIVATE CREDENTIAL')
        original = Path.open
        def guarded(item, *args, **kwargs):
            self.assertNotIn(item, paths)
            return original(item, *args, **kwargs)
        with mock.patch.object(Path, 'open', guarded):
            report = self.audit()
        self.assertTrue(report['ready'], report['blockers'])
        self.assertNotIn(hashlib.sha256(b'PRIVATE CREDENTIAL').hexdigest(), json.dumps(report))
        self.assertNotIn('PRIVATE CREDENTIAL', json.dumps(report))

    def test_uid_and_external_context_changes_are_fingerprinted(self):
        self.preserve(); self.proposed['modules']['workspace-operations'].update(version='1.1.0',package_digest='c')
        profile = self.project / '.wsflow/project.json'; profile.parent.mkdir()
        profile.write_text(json.dumps({'schema_version':1,'project_uid':'before'}), encoding='utf-8')
        rules = self.project / 'PROJECT_RULES.md'; rules.write_text('first', encoding='utf-8')
        first = self.audit()['fingerprint']
        profile.write_text(json.dumps({'schema_version':1,'project_uid':'after'}), encoding='utf-8')
        second = self.audit()['fingerprint']; self.assertNotEqual(first, second)
        rules.write_text('second', encoding='utf-8'); self.assertNotEqual(second, self.audit()['fingerprint'])

    def test_size_and_total_budget_fail_closed(self):
        path = self.project / 'daily/261028.md'; path.parent.mkdir(); path.write_bytes(b'x' * (continuity.MAX_SOURCE_BYTES + 1))
        self.assertIn('read_limit', self.codes(self.audit()))
        path.write_bytes(b'x' * 10)
        with mock.patch.object(continuity, 'MAX_READ_BYTES', 16):
            self.assertIn('read_limit', self.codes(self.audit()))

    def test_registry_and_external_namespace_limits_fail_closed(self):
        with mock.patch.object(continuity, 'MAX_PROJECTS', 0):
            self.assertIn('registry_limit_or_schema', self.codes(self.audit()))
        folder = self.host / 'data/work-calendar'; folder.mkdir(parents=True)
        for name in ('one','two'): (folder/name).write_bytes(b'x')
        with mock.patch.object(continuity, 'MAX_EXTERNAL_FILES', 1):
            self.assertIn('namespace_unknown', self.codes(self.audit()))

    def test_source_change_even_with_restored_mtime_and_size_blocks(self):
        self.preserve(); path = self.project / 'daily/261028.md'; path.parent.mkdir(); path.write_bytes(b'first')
        before = path.stat(); original_read = continuity.Reader.read; reads = 0
        def changing(reader, item, *args, **kwargs):
            nonlocal reads
            if Path(item) == path:
                reads += 1
                if reads == 2:
                    path.write_bytes(b'after'); os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return original_read(reader, item, *args, **kwargs)
        with mock.patch.object(continuity.Reader, 'read', changing):
            report = self.audit()
        self.assertIn('source_changed', self.codes(report))

    def test_new_files_during_observation_block(self):
        self.preserve(); folder = self.project / 'daily'; folder.mkdir()
        original_finish = continuity._Audit.finish
        def changing(audit):
            (folder / '261028.md').write_bytes(b'new')
            return original_finish(audit)
        with mock.patch.object(continuity._Audit, 'finish', changing):
            self.assertIn('source_changed', self.codes(self.audit()))

    def test_link_boundary_and_read_errors_never_become_empty_success(self):
        original = continuity.plain_path
        def deny(path, owner):
            if 'calendar' in Path(path).parts:
                raise ValueError('linked_path')
            return original(path, owner)
        with mock.patch.object(continuity, 'plain_path', side_effect=deny):
            self.assertIn('path_or_read_error', self.codes(self.audit()))
        path = self.project / 'daily/261028.md'; path.parent.mkdir(); path.write_bytes(b'denied')
        original_open = Path.open
        def denied(item, *args, **kwargs):
            if item == path: raise PermissionError('synthetic denied')
            return original_open(item, *args, **kwargs)
        with mock.patch.object(Path, 'open', denied):
            self.assertIn('unreadable_source', self.codes(self.audit()))

    def test_core_import_does_not_load_any_business_package(self):
        code = "import sys; import wsflow.module_continuity; assert not any(k.startswith('wsflow.modules.') for k in sys.modules)"
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_json_inside_known_sqlite_schema_blocks(self):
        self.preserve(); store, ident = self.terminal()
        with store.connection(True) as db:
            db.execute("INSERT INTO plans VALUES('bad','{broken')")
        self.assertIn('unknown_sqlite_schema', self.codes(self.audit()))

    def test_google_mirror_actual_path_and_required_schema(self):
        from wsflow.modules.work_calendar.google_calendar import MirrorStore
        self.preserve(); store = MirrorStore(self.registry)
        store.put({'oauth_epoch':'PRIVATE MIRROR VALUE'})
        before = self.source_snapshot(); report = self.audit()
        self.assertTrue(report['ready'], report['blockers'])
        mirror = next(s for s in report['stores'] if s['role'] == 'google-mirror')
        self.assertEqual(mirror['path'], str(store.path))
        self.assertEqual(mirror['counts']['google_values'], 1)
        self.assertEqual(before, self.source_snapshot()); self.assertNotIn('PRIVATE MIRROR VALUE', json.dumps(report))
        with store.connection(True) as db:
            db.execute('DROP TABLE google_values')
        self.assertIn('unknown_sqlite_schema', self.codes(self.audit()))

    def test_file_descriptor_and_sqlite_row_budgets_fail_closed(self):
        self.preserve(); self.terminal()
        for name in ('MAX_FILES','MAX_DESCRIPTORS','MAX_ROWS'):
            with self.subTest(limit=name), mock.patch.object(continuity, name, 0):
                self.assertFalse(self.audit()['ready'])

    def test_plain_path_rejects_link_component_without_following_it(self):
        from wsflow import path_policy
        target = self.project / 'daily'; target.mkdir()
        (target / '261028.md').write_bytes(b'private')
        real = path_policy.linked
        with mock.patch.object(path_policy, 'linked', side_effect=lambda path: Path(path) == target or real(path)):
            self.assertIn('namespace_unknown', self.codes(self.audit()))

    def test_shared_operations_and_receipts_are_deduplicated_and_scoped(self):
        self.preserve(); self.proposed['modules']['workspace-operations'].update(version='1.1.0',package_digest='c')
        store, ident = self.terminal(kind='local_write')
        folder = self.data / 'operations/common-receipts' / ident; folder.mkdir(parents=True)
        receipt = {'plan_id':'P-test','operation_id':ident,'binding':{},'status':'succeeded','result':{'private':'PRIVATE RECEIPT'}}
        (folder/'receipt.json').write_text(json.dumps({**receipt,'sha256':operations.digest(receipt)}), encoding='utf-8')
        (folder/'before.bin').write_bytes(b'PRIVATE BACKUP')
        other = folder.parent / 'unrelated'; other.mkdir(); (other/'unread.bin').write_bytes(b'UNRELATED')
        report = self.audit()
        self.assertTrue(report['ready'], report['blockers'])
        shared = [s for s in report['stores'] if s['role'] == 'shared-operations']
        self.assertEqual(len(shared), 1)
        self.assertEqual(set(shared[0]['owners']), {'work-calendar','workspace-operations'})
        self.assertNotIn('PRIVATE RECEIPT', json.dumps(report)); self.assertNotIn('PRIVATE BACKUP', json.dumps(report))
        self.assertFalse(any(str(other) in s['path'] for s in report['stores']))

    def test_schema_change_restore_is_refused(self):
        self.proposed['modules']['work-calendar']['data_schema_version'] = 2
        self.assertIn('data_schema_change', self.codes(self.audit('restore')))

    def test_checkout_directory_replacement_changes_fingerprint_without_data_bytes(self):
        before = self.audit()
        self.assertTrue(before['ready'])
        self.project.rename(self.root / 'old-checkout')
        self.project.mkdir()
        after = self.audit()
        self.assertTrue(after['ready'], after['blockers'])
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_nonregular_source_is_fail_closed(self):
        path = self.data / 'calendar' / (operations.digest(str(self.registry.resolve())) + '.sqlite3')
        path.mkdir(parents=True)
        self.assertIn('unsupported_source', self.codes(self.audit()))
