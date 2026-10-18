"""F044/F045: isolated local transitions, exact UTC oracle and synthetic Jira only."""
import concurrent.futures
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import re
import threading
import urllib.request
import urllib.error
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from test_i7 import Home, Provider, CREDS, WHEN, STATE
from wsflow import fileio, jiraapi, jirawrite, operations, wfhome, wfinit, wf_migrate, workflow


class MigrationTests(Home):
    def setUp(self):
        super().setUp()
        self.state.update(schema_version=3, epic_keys=None, csc_field=None, csc_value=None,
                          custom_unknown={'text':'사용자 원문', 'none':None, 'values':[0,False,{}]},
                          last_success='old', last_batch={'run_id':'old-marker'})
        self.state['roots']={'R-001':{'jira_key':'XXX-11','custom':'preserve'}}
        self.original=b'\xef\xbb\xbf'+json.dumps(self.state,ensure_ascii=False,indent=1).replace('\n','\r\n').encode()
        wfhome.state_path(self.home).write_bytes(self.original)
        self.daily=self.home/'daily/261018.md';self.daily.parent.mkdir()
        self.daily.write_bytes(b'# user\r\n<!-- MEMO:START -->\r\nKEEP BYTES\r\n<!-- MEMO:END -->\r\n')
        self.user=self.home/'USER.txt';self.user.write_bytes(b'user\x00bytes')

    def plan(self):return wf_migrate.preview(self.project,self.home)
    def apply_plan(self,p):return wf_migrate.apply(self.project,self.home,p['plan_id'],p['operation_id'])

    def test_schema_only_transition_and_preview_never_mutate_user_files(self):
        before={p.relative_to(self.home).as_posix():p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        p=self.plan()
        self.assertEqual(before,{p.relative_to(self.home).as_posix():p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        result=self.apply_plan(p)
        self.assertEqual(wfhome.read_state(self.home),{**self.state,'schema_version':4})
        self.assertEqual(Path(result['backup']).read_bytes(),self.original)
        self.assertEqual(self.daily.read_bytes(),before['daily/261018.md'])
        self.assertEqual(self.user.read_bytes(),before['USER.txt'])
        self.assertEqual(result['changed_fields'],['schema_version'])
        self.assertTrue(wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])['committed'])
        self.assertTrue(self.apply_plan(p)['reused'])
        self.assertEqual(jirawrite.operation_status(self.home),[])
        remote=operations.Store(self.home).create({'kind':'comment','target':'XXX-11','payload':{'text':'synthetic'}})
        self.assertEqual([op['operation_id'] for op in jirawrite.operation_status(self.home)],[remote['operation_id']])
        with self.assertRaises(operations.StateConflict):jirawrite.repair(self.home,p['operation_id'])

    def test_gitignore_target_prefers_home_repo_over_parent(self):
        (self.base/'.git').mkdir();(self.home/'.git').mkdir()
        self.assertEqual(wfinit.gitignore_target(self.home),self.home)
        self.assertEqual(wfinit.gitignore_target(self.home/'daily'),self.home)

    def test_numbers_that_would_change_are_refused_with_original_bytes_intact(self):
        text=json.dumps(self.state,ensure_ascii=False)
        for literal in ['0.10000000000000001','9007199254740993.5','1e309','1e-400']:
            with self.subTest(number=literal):
                data=(text[:-1]+',"unknown_number":'+literal+'}').encode()
                wfhome.state_path(self.home).write_bytes(data)
                with self.assertRaises(operations.StateConflict):self.plan()
                self.assertEqual(wfhome.state_path(self.home).read_bytes(),data)
        self.state['ordinary_decimal']=0.1;self.save_state()
        p=self.plan();self.apply_plan(p)
        self.assertEqual(wfhome.read_state(self.home)['ordinary_decimal'],0.1)

    def test_status_never_reports_commit_for_changed_state_or_backup(self):
        p=self.plan();state_file=wfhome.state_path(self.home)
        before=wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])
        self.assertTrue(before['source_unchanged']);self.assertFalse(before['committed']);self.assertFalse(before['backup_verified'])
        result=self.apply_plan(p);target=state_file.read_bytes();backup=Path(result['backup'])
        backup.write_bytes(b'corrupted')
        status=wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])
        self.assertFalse(status['committed']);self.assertFalse(status['backup_verified']);self.assertFalse(status['source_unchanged'])
        backup.write_bytes(self.original);state_file.write_bytes(target+b' ')
        status=wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])
        self.assertFalse(status['committed']);self.assertTrue(status['backup_verified']);self.assertFalse(status['source_unchanged'])
        state_file.write_bytes(self.original)
        status=wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])
        self.assertFalse(status['committed']);self.assertTrue(status['source_unchanged'])

    def test_unsupported_or_ambiguous_source_is_refused(self):
        inputs=[{**self.state,'schema_version':n} for n in (True,4,99)]
        inputs += [{**self.state,'watermark':'2026-01-01T00:00:00'},{**self.state,'roots':[]}]
        text=json.dumps(self.state)
        raw=[json.dumps(s).encode() for s in inputs]+[(text[:-1]+',"duplicate":1,"duplicate":2}').encode(),(text[:-1]+',"x":NaN}').encode()]
        for data in raw:
            with self.subTest(data=data):
                wfhome.state_path(self.home).write_bytes(data)
                with self.assertRaises((ValueError,operations.StateConflict)):self.plan()
                self.assertEqual(wfhome.state_path(self.home).read_bytes(),data)

    def test_binding_id_and_expiry_are_checked_before_change(self):
        p=self.plan()
        with self.assertRaises(operations.StateConflict):wf_migrate.apply({**self.project,'id':'other'},self.home,p['plan_id'],p['operation_id'])
        with self.assertRaises(operations.StateConflict):wf_migrate.apply(self.project,self.home,p['plan_id'],'other-operation')
        with mock.patch.object(wf_migrate.time,'time',return_value=p['expires_at']+1):
            with self.assertRaises(operations.StateConflict):self.apply_plan(p)
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),self.original)

    def test_stale_source_and_external_edit_at_replace_are_preserved(self):
        p=self.plan();edited={**self.state,'external':'new'};raw=json.dumps(edited).encode()
        wfhome.state_path(self.home).write_bytes(raw)
        with self.assertRaises(operations.StateConflict):self.apply_plan(p)
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),raw)
        wfhome.state_path(self.home).write_bytes(self.original);real=fileio.atomic_write
        def edit(path,data,*args,**kwargs):
            if Path(path)==wfhome.state_path(self.home):wfhome.state_path(self.home).write_bytes(raw)
            return real(path,data,*args,**kwargs)
        with mock.patch.object(fileio,'atomic_write',side_effect=edit):
            with self.assertRaises(operations.StateConflict):self.apply_plan(p)
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),raw)

    def test_backup_failure_prevents_replacement_and_retry_is_safe(self):
        p=self.plan();real=fileio.atomic_write
        def fail(path,*args,**kwargs):
            if Path(path).name=='state.before.json':raise OSError('backup unavailable')
            return real(path,*args,**kwargs)
        with mock.patch.object(fileio,'atomic_write',side_effect=fail):
            with self.assertRaises(OSError):self.apply_plan(p)
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),self.original)
        self.assertEqual(self.apply_plan(p)['status'],'succeeded')

    def test_lost_replace_response_is_reconciled_without_replacing_again(self):
        p=self.plan();real=fileio.atomic_write
        def lost(path,*args,**kwargs):
            real(path,*args,**kwargs)
            if Path(path)==wfhome.state_path(self.home):raise OSError('response lost after replacement')
        with mock.patch.object(fileio,'atomic_write',side_effect=lost):
            with self.assertRaises(OSError):self.apply_plan(p)
        with mock.patch.object(fileio,'atomic_write',side_effect=AssertionError('must not replace again')):
            self.assertTrue(self.apply_plan(p)['reconciled'])
        self.assertEqual(wfhome.read_state(self.home),{**self.state,'schema_version':4})

    def test_lost_operation_receipt_and_corrupted_backup_do_not_overwrite(self):
        p=self.plan();real=operations.Store.finish
        def fail(store,ident,status,*args,**kwargs):
            if status=='succeeded':raise OSError('DB acknowledgement lost')
            return real(store,ident,status,*args,**kwargs)
        with mock.patch.object(operations.Store,'finish',fail):
            with self.assertRaises(OSError):self.apply_plan(p)
        status=wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])
        self.assertTrue(status['committed']);Path(status['backup']).write_bytes(b'tampered')
        before=wfhome.state_path(self.home).read_bytes()
        with self.assertRaises(operations.StateConflict):self.apply_plan(p)
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),before)

    def test_concurrent_apply_has_one_transition_and_same_result(self):
        p=self.plan()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.apply_plan(p),range(2)))
        self.assertEqual([r['operation_id'] for r in results],[p['operation_id']]*2)
        self.assertEqual(sum(bool(r.get('reused')) for r in results),1)
        self.assertEqual(len(list(self.home.glob('.workflow/migrations/*/state.before.json'))),1)

    def test_migrated_copy_runs_daily_without_remote_writes(self):
        p=self.plan();self.apply_plan(p);memo=workflow._memo(self.daily.read_bytes())
        result=self.run_batch()
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual([s['stage'] for s in result['stages']],list(workflow.STAGES))
        self.assertEqual(wfhome.read_state(self.home)['watermark'],WHEN.isoformat())
        self.assertEqual(workflow._memo(self.daily.read_bytes()),memo)
        self.assertTrue(all(method=='GET' or '/search/jql' in url for url,_,method in self.provider.calls))
        self.assertEqual(Path(wf_migrate.status(self.project,self.home,p['plan_id'],p['operation_id'])['backup']).read_bytes(),self.original)


class AbsoluteTimeTests(Home):
    def test_epoch_queries_keep_legacy_queries_and_reject_naive_since(self):
        legacy=jiraapi.build_queries(self.state)[0]['jql']
        self.assertIn('updated >= "2026-10-01 00:00"',legacy)
        dt=datetime.fromisoformat('2026-03-08T06:59:00+00:00')
        query=jiraapi.build_queries(self.state,since=dt)[0]['jql']
        self.assertIn('updated >= 1772953140000 ',query)
        self.assertNotIn('"1772953140000"',query)
        with self.assertRaises(jiraapi.JiraError):jiraapi.build_queries(self.state,since=datetime(2026,1,1))

    def test_offset_equivalence_and_random_absolute_thresholds(self):
        rng=random.Random(261018025);epoch=datetime(1970,1,1,tzinfo=timezone.utc)
        for _ in range(700):
            seconds=rng.randrange(1767225600,1798761600);micros=rng.randrange(1000000)
            dt=epoch+timedelta(seconds=seconds,microseconds=micros)
            expected=seconds*1000+micros//1000
            outputs=[]
            for zone in ['UTC','Asia/Seoul','America/New_York','Australia/Lord_Howe']:
                q=jiraapi.build_queries(self.state,since=dt.astimezone(ZoneInfo(zone)))[0]['jql']
                outputs.append(int(re.search(r'updated >= (\d+)',q)[1]))
            self.assertEqual(outputs,[expected]*4)

    def test_dst_batch_uses_exact_utc_overlap_and_correct_account_day(self):
        scenarios=[('America/New_York','2026-03-08T07:01:30Z'),('America/New_York','2026-11-01T06:01:30Z'),
                   ('Australia/Lord_Howe','2026-10-03T15:31:30Z'),('Australia/Lord_Howe','2026-04-04T15:01:30Z'),
                   ('Europe/Berlin','2026-03-29T01:01:30Z'),('UTC','2026-10-18T00:00:30Z')]
        for zone,wm in scenarios:
            with self.subTest(zone=zone,watermark=wm):
                previous=datetime.fromisoformat(wm.replace('Z','+00:00'));self.state['watermark']=previous.isoformat();self.save_state()
                real=Provider();real.open=[];real.changes=[]
                def provider(url,*args):return {'accountId':'me','timeZone':zone} if url.endswith('/myself') else real(url,*args)
                result=workflow.run(self.home,CREDS,send=provider,clock=lambda:previous+timedelta(minutes=10))
                self.assertEqual(result['status'],'succeeded',result)
                q=next(body['jql'] for url,body,_ in real.calls if body and 'updated >=' in body['jql'])
                actual=int(re.search(r'updated >= (\d+)',q)[1])
                # Independent integer Unix-seconds oracle, no local datetime subtraction.
                expected=((int(previous.timestamp())-120)//60)*60000
                self.assertEqual(actual,expected)
                self.assertEqual(result['date'],(previous+timedelta(minutes=10)).astimezone(ZoneInfo(zone)).date().isoformat())
                self.assertEqual(wfhome.read_state(self.home)['watermark'],(previous+timedelta(minutes=10)).astimezone(ZoneInfo(zone)).isoformat())

    def test_naive_clock_and_epoch_search_failure_keep_state(self):
        original=wfhome.state_path(self.home).read_bytes()
        r=workflow.run(self.home,CREDS,send=self.provider,clock=lambda:datetime(2026,10,18))
        self.assertEqual(r['status'],'failed');self.assertEqual(wfhome.state_path(self.home).read_bytes(),original)
        def fail(url,*args):
            if '/search/jql' in url:raise jiraapi.JiraError('controlled search failure')
            return self.provider(url,*args)
        self.assertEqual(workflow.run(self.home,CREDS,send=fail,clock=lambda:WHEN)['status'],'failed')
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),original)


class MigrationApiTests(Home):
    def test_api_requires_server_ids_and_retains_schema_gate(self):
        from wsflow.server import serve
        from wsflow.store import Store
        from wsflow import auth
        self.state['schema_version']=3;self.save_state()
        checkout=self.base/'checkout';checkout.mkdir()
        registry=Store(self.base/'registry.json');pr=registry.add_project(str(checkout))
        registry.data['projects'][0]['wf_home']=str(self.home);registry._save()
        with mock.patch.object(auth,'load',side_effect=AssertionError('migration needs no credentials')):
            httpd=serve(registry.file,'127.0.0.1',0);threading.Thread(target=httpd.serve_forever,daemon=True).start()
            try:
                def call(path,body,method='POST'):
                    req=urllib.request.Request('http://127.0.0.1:'+str(httpd.server_address[1])+f'/api/projects/{pr["id"]}/jira/'+path,
                        data=json.dumps(body).encode() if method!='GET' else None,method=method,headers={'X-Auth-Token':httpd.wsflow_token,'Content-Type':'application/json'})
                    try:
                        with urllib.request.urlopen(req,timeout=10) as r:return r.status,json.load(r)
                    except urllib.error.HTTPError as e:
                        with e:return e.code,json.load(e)
                before=wfhome.state_path(self.home).read_bytes()
                self.assertEqual(call('batch/run',{})[0],409)
                self.assertEqual(call('migration/plan',{'schema_version':4})[0],400)
                code,p=call('migration/plan',{});self.assertEqual(code,200)
                ids={k:p[k] for k in ['plan_id','operation_id']}
                self.assertEqual(call('migration/apply',{**ids,'cfg':{'watermark':'2099'}})[0],400)
                self.assertEqual(wfhome.state_path(self.home).read_bytes(),before)
                # An unrelated checkout repository must not block the external nonrepo home.
                (checkout/'.git').mkdir()
                self.assertEqual(call('migration/apply',ids)[0],200)
                wfhome.state_path(self.home).write_bytes(before)
                code,p=call('migration/plan',{});self.assertEqual(code,200)
                ids={k:p[k] for k in ['plan_id','operation_id']}
                # WF_HOME is nested in a repository, while checkout is a different repository.
                (self.base/'.git').mkdir()
                self.assertEqual(call('migration/apply',ids)[0],409)
                self.assertEqual(wfhome.state_path(self.home).read_bytes(),before)
                code,gi=call('gitignore',None,method='GET');self.assertEqual(code,200)
                self.assertEqual(gi['path'],str(self.base/'.gitignore'))
                self.assertEqual(call('gitignore/fix',{})[0],200)
                self.assertFalse((checkout/'.gitignore').exists());self.assertFalse((self.home/'.gitignore').exists())
                self.assertEqual(call('migration/apply',ids)[0],200)
                self.assertEqual(wfhome.read_state(self.home)['schema_version'],4)
                self.assertEqual(call('operations/'+p['operation_id']+'/repair',{})[0],409)
            finally:httpd.shutdown();httpd.server_close()


if __name__=='__main__':unittest.main()
