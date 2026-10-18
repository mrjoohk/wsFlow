"""I2 acceptance: local temporary DB/files, no external provider."""
import concurrent.futures
import json
import os
import tempfile
import threading
import urllib.request
import urllib.error
import urllib.parse
import unittest
from pathlib import Path
from unittest import mock

from wsflow import calendar_store, calendar_view, operations
from wsflow.server import serve
from wsflow.wfhome import MEMO_START, MEMO_END


class CalendarHome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.scope = self.root / 'workspace' / 'projects.json'
        self.scope.parent.mkdir(); self.project = {'id':'a','path':str(self.root/'a')}
        Path(self.project['path']).mkdir()
        env = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR':str(self.root/'userdata')})
        env.start(); self.addCleanup(env.stop)
        self.store = calendar_store.Store(self.scope)
        self.projects = [self.project]

    def event(self, **extra):
        return {'title':'meeting','body':'details','location':'room','all_day':True,
                'start_date':'2026-10-17','end_date':'2026-10-18','project_ids':['a'],**extra}

    def plan(self, payload=None, kind='event', **extra):
        return self.store.preview({'action':'create','kind':kind,'payload':payload or self.event(),**extra}, self.projects)

    def apply(self, plan):
        return self.store.apply(plan['plan_id'], plan['operation_id'], self.projects)

    def query(self, **extra):
        return calendar_view.query(self.projects,self.store,'2026-10-17','2026-10-20',**extra)

class CalendarTests(CalendarHome):
    def test_read_does_not_create_db_or_daily(self):
        self.assertFalse(self.store.path.exists())
        result=self.query()
        self.assertEqual([d['event_count'] for d in result['days']],[0,0,0])
        self.assertFalse(self.store.path.exists());self.assertFalse((Path(self.project['path'])/'daily').exists())

    def test_all_day_end_exclusive_and_note_count_is_identity_not_lines(self):
        self.apply(self.plan(self.event(end_date='2026-10-19')))
        self.apply(self.plan({'date':'2026-10-17','body':'one\ntwo','project_ids':['a']},'note'))
        result=self.query();self.assertEqual([d['event_count'] for d in result['days']],[1,1,0])
        self.assertEqual([d['memo_count'] for d in result['days']],[1,0,0])
        self.assertFalse(result['coverage']['google_calendar'])

    def test_midnight_and_timezone_conversion(self):
        event=self.event(all_day=False);event.pop('start_date');event.pop('end_date')
        event.update(start_at='2026-10-17T23:00:00+09:00',end_at='2026-10-18T00:00:00+09:00',timezone='Asia/Seoul')
        self.apply(self.plan(event))
        self.assertEqual([d['event_count'] for d in self.query()['days']],[1,0,0])

    def test_same_plan_replay_and_concurrent_revision_have_one_winner(self):
        plan=self.plan();first=self.apply(plan);self.assertEqual(self.apply(plan),first)
        record=first['record'];plans=[self.plan(self.event(title=t),action='update',id=record['id'],expected_revision=1) for t in ('first','second')]
        barrier=threading.Barrier(2)
        def save(p):
            barrier.wait()
            try:return self.apply(p)
            except operations.StateConflict:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(save,plans))
        self.assertEqual(results.count('conflict'),1)
        self.assertEqual(self.query()['days'][0]['events'][0]['revision'],2)

    def test_stale_delete_and_transaction_rollback_preserve_record(self):
        record=self.apply(self.plan())['record']
        delete=self.plan(action='delete',id=record['id'],expected_revision=1)
        with mock.patch.object(self.store,'_event',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.apply(delete)
        self.assertEqual(self.query()['days'][0]['event_count'],1)
        self.apply(delete);self.apply(delete)
        self.assertEqual(self.query()['days'][0]['event_count'],0)
        self.assertTrue(self.store.records()[0]['deleted'])

    def test_bad_dates_and_wrong_zone_offset_have_no_plan(self):
        for event in [self.event(start_date='2026-02-30'), self.event(end_date='2026-10-17'),self.event(project_ids=['foreign'])]:
            with self.subTest(event=event), self.assertRaises(ValueError):self.plan(event)
        timed=self.event(all_day=False);timed.pop('start_date');timed.pop('end_date')
        timed.update(start_at='2026-10-17T12:00:00+00:00',end_at='2026-10-17T13:00:00+00:00',timezone='Asia/Seoul')
        with self.assertRaises(ValueError):self.plan(timed)
        timed.update(start_at='2100-01-01T00:00:00+09:00',end_at='2100-01-01T01:00:00+09:00')
        with self.assertRaises(ValueError):self.plan(timed)

    def test_legacy_unique_tasks_common_block_and_notes_share_counts(self):
        home=Path(self.project['path']);(home/'.workflow').mkdir()
        (home/'.workflow/state.json').write_text(json.dumps({'issue_type_map':{'site_url':'https://example.atlassian.net'}}),encoding='utf-8')
        (home/'daily').mkdir();path=home/'daily/261017.md'
        path.write_text(f'# before\n{MEMO_START}\n- XXX-1: first\n- XXX-1: second\n- XXX-99: archived\ncommon\nother line\n{MEMO_END}\n# after',encoding='utf-8')
        original=path.read_bytes()
        self.projects.append({'id':'alias','path':str(home)})
        self.apply(self.plan({'date':'2026-10-17','body':'standalone\nsecond line','project_ids':['a','alias']},'note'))
        day=self.query()['days'][0]
        self.assertEqual((day['task_count'],day['memo_count']),(2,2));self.assertEqual(len(day['tasks'][0]['records']),2)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.query(source_ids=['local'])['days'][0]['task_count'],0)
        self.assertEqual(self.query(source_ids=['legacy_daily'])['days'][0]['memo_count'],1)

    def test_filters_and_unlinked_records_do_not_mix_projects(self):
        self.apply(self.plan())
        self.apply(self.plan(self.event(title='global',project_ids=[])))
        other={'id':'b','path':str(self.root/'b')};Path(other['path']).mkdir();self.projects.append(other)
        self.apply(self.plan(self.event(title='other',project_ids=['b'])))
        self.assertEqual(self.query()['days'][0]['event_count'],3)
        self.assertEqual(self.query(project_ids=['a'])['days'][0]['event_count'],1)
        self.assertEqual(self.query(project_ids=['b'])['days'][0]['events'][0]['title'],'other')
        self.assertEqual(self.query(source_ids=['legacy_daily'])['days'][0]['event_count'],0)

    def test_timezone_dates_dst_fold_and_nonexistent_local_time(self):
        event=self.event(all_day=False);event.pop('start_date');event.pop('end_date')
        event.update(start_at='2026-10-17T00:00:00+09:00',end_at='2026-10-17T01:00:00+09:00',timezone='Asia/Seoul')
        self.apply(self.plan(event))
        self.assertEqual(self.query()['days'][0]['event_count'],1)
        days=calendar_view.query(self.projects,self.store,'2026-10-16','2026-10-19','UTC')['days']
        self.assertEqual([d['event_count'] for d in days],[1,0,0])
        event.update(start_at='2026-11-01T01:30:00-04:00',end_at='2026-11-01T01:15:00-05:00',timezone='America/New_York')
        self.apply(self.plan(event))
        day=calendar_view.query(self.projects,self.store,'2026-11-01','2026-11-02','America/New_York')['days'][0]
        self.assertEqual(day['event_count'],1)
        event.update(start_at='2026-03-08T02:30:00-05:00',end_at='2026-03-08T04:00:00-04:00')
        with self.assertRaises(ValueError):self.plan(event)

    def test_unknown_read_failure_is_not_zero_and_query_change_is_detected(self):
        import sqlite3
        with mock.patch.object(self.store,'records',side_effect=sqlite3.OperationalError('unreadable')):
            day=self.query()['days'][0]
        self.assertFalse(day['complete']);self.assertIsNone(day['event_count']);self.assertIsNone(day['memo_count'])
        record=self.apply(self.plan())['record']
        with mock.patch.object(self.store,'records',side_effect=[[record],[]]):
            day=self.query()['days'][0]
        self.assertFalse(day['complete']);self.assertIn('local_changed_during_query',day['errors'])

    def test_snapshot_preview_stability_expiry_scope_and_registry_isolation(self):
        self.apply(self.plan());before=self.query()['snapshot_id'];plan=self.plan()
        self.assertEqual(self.query()['snapshot_id'],before)
        with mock.patch('wsflow.calendar_store.time.time',return_value=plan['expires_at']+1):
            with self.assertRaises(operations.StateConflict):self.apply(plan)
        projects=[{**self.project,'path':str(self.root/'moved')}]
        with self.assertRaises(operations.StateConflict):self.store.apply(plan['plan_id'],plan['operation_id'],projects)
        other=calendar_store.Store(self.scope.with_name('other.json'))
        with self.assertRaises(operations.StateConflict):other.apply(plan['plan_id'],plan['operation_id'],self.projects)
        record=self.query()['days'][0]['events'][0]
        self.apply(self.plan(self.event(title='changed'),action='update',id=record['id'],expected_revision=1))
        self.assertNotEqual(self.query()['snapshot_id'],before)

    def test_failure_after_record_write_rolls_back_audit_effect_and_claim(self):
        plan=self.plan();original=self.store._event
        def event(db,ident,status,detail=None):
            if status=='succeeded':raise OSError('commit interrupted')
            return original(db,ident,status,detail)
        with mock.patch.object(self.store,'_event',event):
            with self.assertRaises(OSError):self.apply(plan)
        self.assertEqual(self.store.records(),[])
        self.assertEqual(self.store.get(plan['operation_id'])['status'],'planned')
        self.apply(plan);self.assertEqual(len(self.store.records()),1)

    def test_store_must_be_outside_project_and_unknown_schema_is_unchanged(self):
        with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(Path(self.project['path'])/'userdata')}):
            outside=calendar_store.Store(self.scope)
            with self.assertRaises(operations.StateConflict):outside.preview({'action':'create','kind':'event','payload':self.event()},self.projects)
            self.assertFalse(outside.path.exists())
        self.apply(self.plan());import sqlite3
        db=sqlite3.connect(self.store.path)
        try:db.execute('PRAGMA user_version=99');db.commit()
        finally:db.close()
        before=self.store.path.read_bytes()
        self.assertFalse(self.query()['complete'])
        with self.assertRaises(operations.StateConflict):self.plan()
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_reused_project_id_does_not_reassign_historical_local_record(self):
        record=self.apply(self.plan())['record']
        old_path=record['project_references']['a']
        self.projects=[{**self.project,'path':str(self.root/'different-checkout')}]
        all_days=self.query()['days'];self.assertEqual(all_days[0]['event_count'],1)
        self.assertEqual(all_days[0]['events'][0]['unavailable_project_ids'],['a'])
        self.assertEqual(self.query(project_ids=['a'])['days'][0]['event_count'],0)
        updated=self.apply(self.plan(self.event(title='retained history'),action='update',id=record['id'],expected_revision=1))
        self.assertEqual(updated['record']['project_references']['a'],old_path)

    def test_same_site_task_across_distinct_homes_counts_once(self):
        other={'id':'b','path':str(self.root/'b')};self.projects.append(other)
        for project in self.projects:
            home=Path(project['path']);home.mkdir(exist_ok=True);(home/'.workflow').mkdir();(home/'daily').mkdir()
            (home/'.workflow/state.json').write_text(json.dumps({'issue_type_map':{'site_url':'https://example.atlassian.net'}}),encoding='utf-8')
            (home/'daily/261017.md').write_text(f'{MEMO_START}\n- XXX-1: {project["id"]} work\n{MEMO_END}',encoding='utf-8')
        day=self.query()['days'][0]
        self.assertEqual(day['task_count'],1);self.assertEqual(len(day['tasks'][0]['records']),2)


class CalendarHttpTests(CalendarHome):
    def setUp(self):
        super().setUp()
        self.scope.write_text(json.dumps({'roots':[],'projects':self.projects}),encoding='utf-8')
        self.httpd=serve(self.scope,'127.0.0.1',0);threading.Thread(target=self.httpd.serve_forever,daemon=True).start()
        self.addCleanup(self.httpd.server_close);self.addCleanup(self.httpd.shutdown)

    def api(self,method,path,body=None,status=200,token=True):
        url=f'http://127.0.0.1:{self.httpd.server_address[1]}'+path
        headers={'Content-Type':'application/json'}
        if token:headers['X-Auth-Token']=self.httpd.wsflow_token
        req=urllib.request.Request(url,method=method,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        try:r=urllib.request.urlopen(req,timeout=10)
        except urllib.error.HTTPError as exc:r=exc
        with r:
            value=json.loads(r.read().decode());self.assertEqual(r.status,status,value);return value

    def test_http_guard_plan_only_snapshot_and_v1_preservation(self):
        prefix='/api/v2/calendar/'
        self.api('POST',prefix+'plan',{'action':'create','kind':'event','payload':self.event()},403,False)
        self.api('POST',prefix+'apply',{'payload':self.event()},409)
        plan=self.api('POST',prefix+'plan',{'action':'create','kind':'event','payload':self.event()})
        self.api('POST',prefix+'apply',{'plan_id':plan['plan_id'],'operation_id':'foreign'},409)
        self.api('POST',prefix+'apply',{'plan_id':plan['plan_id'],'operation_id':plan['operation_id'],'payload':{}},409)
        first=self.api('POST',prefix+'apply',{'plan_id':plan['plan_id'],'operation_id':plan['operation_id']})
        params={'start':'2026-10-17','end':'2026-10-20'}
        snapshot=self.api('GET',prefix+'range?'+urllib.parse.urlencode(params))
        day=self.api('GET',prefix+'day?'+urllib.parse.urlencode({**params,'date':'2026-10-17','snapshot_id':snapshot['snapshot_id']}))
        self.assertEqual(day['day'],snapshot['days'][0])
        v1=self.api('GET','/api/v1/calendar/range?'+urllib.parse.urlencode(params))
        self.assertIsNone(v1['days'][0]['event_count']);self.assertFalse(v1['coverage']['local_events'])
        self.apply(self.plan())
        self.api('GET',prefix+'day?'+urllib.parse.urlencode({**params,'date':'2026-10-17','snapshot_id':snapshot['snapshot_id']}),status=409)
        self.api('GET',prefix+'range?'+urllib.parse.urlencode({**params,'project_id':'unknown'}),status=404)
        old=first['record'];update=self.plan(self.event(title='updated'),action='update',id=old['id'],expected_revision=1);self.apply(update)
        result=self.api('POST',prefix+'plan',{'kind':'event','action':'update','id':old['id'],'expected_revision':1,'payload':self.event()},409)
        self.assertEqual(result['current']['title'],'updated')
