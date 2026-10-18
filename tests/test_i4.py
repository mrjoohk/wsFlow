"""I4 durable write/pull/conflict recovery with a synthetic Google boundary."""
import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from wsflow import calendar_store, calendar_view, google_calendar as google, calendar_sync, operations
from wsflow.server import serve


class WriteProvider:
    def __init__(self):
        self.events={};self.calls=[];self.version=0;self.drop=None;self.fail=None;self.during_write=None
        self.scopes=[*google.SCOPES,google.WRITE_SCOPE];self.role='owner';self.account='account-A'
    def __call__(self,method,url,data=None,headers=None):
        path=urllib.parse.urlsplit(url).path;q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.calls.append((method,path,copy.deepcopy(data),dict(headers or {})))
        if path.endswith('/token'):return {'access_token':'synthetic','refresh_token':'synthetic-refresh','expires_in':3600,'scope':' '.join(self.scopes)}
        if path.endswith('/userinfo'):return {'sub':self.account,'email':'tester@example.test'}
        if path.endswith('/revoke'):return {}
        if self.fail:raise google.GoogleError(self.fail)
        if path.endswith('/calendarList'):return {'items':[{'id':'cal','summary':'Team','accessRole':self.role}]}
        ident=urllib.parse.unquote(path.rsplit('/',1)[-1])
        if method=='GET' and ident=='events':return {'items':list(copy.deepcopy(self.events).values()),'nextSyncToken':'cursor-'+str(self.version)}
        if method=='GET':
            if ident not in self.events:raise google.GoogleError(404)
            return copy.deepcopy(self.events[ident])
        if self.during_write:
            callback,self.during_write=self.during_write,None;callback()
        if method=='POST':
            ident=data['id']
            if ident in self.events:raise google.GoogleError(409)
            event=copy.deepcopy(data)
        else:
            if ident not in self.events:raise google.GoogleError(404)
            if headers.get('If-Match')!=self.events[ident]['etag']:raise google.GoogleError(412)
            event={**self.events[ident],**copy.deepcopy(data or {})}
        self.version+=1
        if method=='DELETE':self.events.pop(ident,None);result={}
        else:
            event.update(id=ident,etag='"v'+str(self.version)+'"',status='confirmed')
            self.events[ident]=event;result=copy.deepcopy(event)
        if self.drop==method:
            self.drop=None;raise google.GoogleError(503)
        return result
    def edit(self,ident,**fields):
        self.version+=1;self.events[ident].update(**fields,etag='"v'+str(self.version)+'"')
    def writes(self):return [c for c in self.calls if '/calendar/v3/' in c[1] and c[0]!='GET']


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);workspace=self.root/'workspace';workspace.mkdir()
        self.registry=workspace/'projects.json';self.registry.write_text(json.dumps({'projects':[],'roots':[]}))
        data=self.root/'userdata';data.mkdir();(data/'google-client.json').write_text(json.dumps({'installed':{'client_id':'test.apps.googleusercontent.com','client_secret':'synthetic'}}))
        patch=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(data)});patch.start();self.addCleanup(patch.stop)
        self.provider=WriteProvider();self.google=google.Service(self.registry,transport=self.provider);self.addCleanup(self.google.close)
        start=self.google.connect(write=True);q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        with urllib.request.urlopen(q['redirect_uri'][0]+'?'+urllib.parse.urlencode({'state':q['state'][0],'code':'synthetic'})):pass
        self.google.calendars();self.google.select(['cal']);self.local=calendar_store.Store(self.registry)
        self.engine=calendar_sync.Engine(self.local,self.google)
    def event(self,**extra):return {'title':'meeting','body':'private','location':'room','project_ids':[],
                                  'all_day':True,'start_date':'2026-10-18','end_date':'2026-10-19',**extra}
    def create(self,**extra):
        p=self.local.preview({'kind':'event','action':'create','payload':self.event(**extra)},[])
        return self.local.apply(p['plan_id'],p['operation_id'],[])['record']
    def publish(self,record=None):
        record=record or self.create();plan=self.engine.preview({'action':'publish','local_id':record['id'],
            'expected_revision':record['revision'],'calendar_id':'cal'},[])
        result=self.engine.apply(plan['plan_id'],plan['operation_id'],[]);return record,result['google_operation_id']
    def update(self,record,**extra):
        p=self.local.preview({'kind':'event','action':'update','id':record['id'],
            'expected_revision':record['revision'],'payload':self.event(**extra)},[])
        return self.local.apply(p['plan_id'],p['operation_id'],[])
    def current(self,ident):return next(r for r in self.local.records() if r['id']==ident)

    def test_publish_has_durable_id_before_send_and_replay_is_one_effect(self):
        record,op=self.publish();queued=self.engine.operation(op)
        self.assertEqual(queued['state'],'queued');self.assertEqual(self.provider.writes(),[])
        remote=queued['intent']['remote_id'];self.assertRegex(remote,r'^[0-9a-v]{5,1024}$')
        self.assertEqual(self.engine.run(op)['state'],'synced');self.assertEqual(self.engine.run(op)['state'],'synced')
        self.assertEqual(len(self.provider.writes()),1);self.assertIn(remote,self.provider.events)
        self.assertEqual(self.engine.links()[0]['base_etag'],self.provider.events[remote]['etag'])

    def test_creation_with_google_destination_is_atomic_and_note_upload_rejected(self):
        target=self.engine.target('cal')
        p=self.local.preview({'kind':'event','action':'create','payload':self.event()},[],google_target=target)
        result=self.local.apply(p['plan_id'],p['operation_id'],[])
        self.assertEqual(len(self.engine.links()),1);self.assertEqual(self.engine.operation(result['google_operation_id'])['state'],'queued')
        with self.assertRaises(ValueError):self.local.preview({'kind':'note','action':'create','payload':{'date':'2026-10-18','body':'no','project_ids':[]}},[],google_target=target)

    def test_post_response_loss_reconcile_reads_without_second_post(self):
        record,op=self.publish();self.provider.drop='POST'
        self.assertEqual(self.engine.run(op)['state'],'uncertain')
        self.engine.run(op);self.assertEqual(len(self.provider.writes()),1)
        restarted=calendar_sync.Engine(calendar_store.Store(self.registry),self.google)
        self.assertEqual(restarted.reconcile(op)['state'],'synced');self.assertEqual(len(self.provider.writes()),1)

    def test_uncertain_absent_insert_can_retry_only_same_remote_id_after_lookup(self):
        record,op=self.publish()
        with self.local.connection(True) as db:db.execute("UPDATE calendar_outbox SET state='syncing' WHERE id=?",(op,))
        self.engine.run(op);self.assertEqual(self.provider.writes(),[])
        self.assertEqual(self.engine.reconcile(op)['state'],'error')
        ident=self.engine.operation(op)['intent']['remote_id'];self.engine.run(op)
        self.assertEqual(set(self.provider.events),{ident})

    def test_update_has_if_match_and_unknown_fields_survive(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.provider.edit(remote,reminders={'useDefault':False},colorId='4');self.google.sync();self.engine.pull()
        result=self.update(self.current(record['id']),title='changed');self.engine.run(result['google_operation_id'])
        call=self.provider.writes()[-1];self.assertEqual(call[0],'PATCH');self.assertIn('If-Match',call[3])
        self.assertEqual(self.provider.events[remote]['colorId'],'4');self.assertEqual(self.provider.events[remote]['summary'],'changed')

    def test_remote_pull_preserves_local_links_and_deduplicates_display(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.provider.edit(remote,summary='from Google');self.google.sync();self.engine.pull()
        current=self.current(record['id']);self.assertEqual(current['title'],'from Google');self.assertGreater(current['revision'],record['revision'])
        remote_result=self.google.range('2026-10-18','2026-10-20')
        view=calendar_view.query([],self.local,'2026-10-18','2026-10-20',google_result=remote_result,google_links=self.engine.links())
        self.assertEqual(view['days'][0]['event_count'],1);self.assertEqual(view['days'][0]['events'][0]['id'],record['id'])

    def test_both_changed_conflict_keeps_local_and_remote_no_patch(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        result=self.update(record,title='local edit');self.provider.edit(remote,summary='remote edit')
        state=self.engine.run(result['google_operation_id']);self.assertEqual(state['state'],'conflict')
        self.assertEqual(self.current(record['id'])['title'],'local edit')
        conflict=self.engine.links()[0]['conflict'];self.assertEqual(conflict['remote']['summary'],'remote edit')
        self.assertEqual(len(self.provider.writes()),1)

    def test_late_write_success_preserves_new_local_revision_and_queues_followup(self):
        record,op=self.publish()
        self.provider.during_write=lambda:self.update(record,title='typed later')
        self.assertEqual(self.engine.run(op)['state'],'synced')
        current=self.current(record['id']);self.assertEqual(current['title'],'typed later')
        follow=[o for o in self.engine.status()['operations'] if o['state']=='queued'];self.assertEqual(len(follow),1)
        self.engine.run(follow[0]['id']);remote=self.engine.operation(op)['intent']['remote_id']
        self.assertEqual(self.provider.events[remote]['summary'],'typed later');self.assertEqual(len(self.provider.events),1)

    def test_patch_response_loss_marker_and_body_must_both_match(self):
        record,op=self.publish();self.engine.run(op);result=self.update(record,title='edited');oid=result['google_operation_id']
        self.provider.drop='PATCH';self.assertEqual(self.engine.run(oid)['state'],'uncertain')
        self.assertEqual(self.engine.reconcile(oid)['state'],'synced');self.assertEqual(len(self.provider.writes()),2)

    def test_permissions_account_selection_and_no_scope_block_remote_write(self):
        record,op=self.publish();self.provider.role='reader';self.engine.run(op);self.assertEqual(self.provider.writes(),[])
        self.provider.role='owner';token=self.google.credentials.load();token['sub']='other';self.google.credentials.save(token)
        self.engine.run(op);self.assertEqual(self.provider.writes(),[])
        token['sub']='account-A';token['scopes']=list(google.SCOPES);self.google.credentials.save(token)
        self.engine.run(op);self.assertEqual(self.provider.writes(),[])

    def test_hide_is_local_only_and_explicit_google_delete_is_conditional(self):
        record,op=self.publish();self.engine.run(op)
        plan=self.local.preview({'action':'delete','kind':'event','id':record['id'],'expected_revision':record['revision']},[])
        self.local.apply(plan['plan_id'],plan['operation_id'],[]);self.assertEqual(len(self.provider.writes()),1)
        current=self.current(record['id']);p=self.engine.preview({'action':'delete','local_id':record['id'],'expected_revision':current['revision']},[])
        result=self.engine.apply(p['plan_id'],p['operation_id'],[]);self.engine.run(result['google_operation_id'])
        self.assertEqual(self.provider.events,{});self.assertEqual(self.provider.writes()[-1][0],'DELETE')

    def test_delete_response_loss_recovers_absence_without_second_delete(self):
        record,op=self.publish();self.engine.run(op)
        p=self.engine.preview({'action':'delete','local_id':record['id'],'expected_revision':record['revision']},[])
        result=self.engine.apply(p['plan_id'],p['operation_id'],[]);oid=result['google_operation_id'];self.provider.drop='DELETE'
        self.assertEqual(self.engine.run(oid)['state'],'uncertain');self.assertEqual(self.engine.reconcile(oid)['state'],'synced')
        self.assertEqual(len([c for c in self.provider.writes() if c[0]=='DELETE']),1);self.assertTrue(self.current(record['id'])['deleted'])

    def test_conflict_resolution_remote_and_stale_revision_guard(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.update(record,title='local');self.provider.edit(remote,summary='remote');self.google.sync();self.engine.pull()
        link=self.engine.links()[0];current=self.current(record['id'])
        req={'action':'resolve','resolution':'remote','local_id':record['id'],'expected_revision':current['revision'],'remote_etag':link['conflict']['remote']['etag']}
        plan=self.engine.preview(req,[]);self.provider.edit(remote,summary='newer')
        with self.assertRaises(operations.StateConflict):self.engine.apply(plan['plan_id'],plan['operation_id'],[])
        self.google.sync();self.engine.pull();req['remote_etag']=self.engine.links()[0]['conflict']['remote']['etag']
        plan=self.engine.preview(req,[]);self.engine.apply(plan['plan_id'],plan['operation_id'],[])
        self.assertEqual(self.current(record['id'])['title'],'newer')

    def test_repeat_attendee_special_event_blocks_changes(self):
        for fields in ({'recurrence':['RRULE:FREQ=DAILY']},{'attendees':[{'email':'x@example.test'}]},{'eventType':'outOfOffice'}):
            record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
            self.provider.edit(remote,**fields);result=self.update(record,title='unsafe')
            count=len(self.provider.writes());self.engine.run(result['google_operation_id']);self.assertEqual(len(self.provider.writes()),count)

    def test_conflict_resolution_local_uses_latest_etag_and_preserves_remote_extras(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.update(record,title='chosen local');self.provider.edit(remote,summary='other side',colorId='3')
        self.google.sync();self.engine.pull();current=self.current(record['id'])
        p=self.engine.preview({'action':'resolve','resolution':'local','local_id':record['id'],
            'expected_revision':current['revision'],'remote_etag':self.provider.events[remote]['etag']},[])
        queued=self.engine.apply(p['plan_id'],p['operation_id'],[])['google_operation_id']
        self.assertEqual(self.engine.run(queued)['state'],'synced')
        self.assertEqual(self.provider.events[remote]['summary'],'chosen local');self.assertEqual(self.provider.events[remote]['colorId'],'3')

    def test_412_between_preflight_and_write_preserves_remote(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        result=self.update(record,title='local newer')
        self.provider.during_write=lambda:self.provider.edit(remote,summary='concurrent remote')
        self.assertEqual(self.engine.run(result['google_operation_id'])['state'],'conflict')
        self.assertEqual(self.provider.events[remote]['summary'],'concurrent remote')

    def test_409_foreign_id_collision_is_not_adopted_or_recreated(self):
        record,op=self.publish();remote=self.engine.operation(op)['intent']['remote_id']
        self.provider.events[remote]={'id':remote,'etag':'"foreign"',**calendar_sync.body(record)}
        self.assertEqual(self.engine.run(op)['state'],'conflict');count=len(self.provider.writes())
        self.engine.run(op);self.assertEqual(len(self.provider.writes()),count);self.assertEqual(len(self.provider.events),1)

    def test_uncertain_marker_with_changed_body_becomes_conflict(self):
        record,op=self.publish();self.provider.drop='POST';self.engine.run(op)
        remote=self.engine.operation(op)['intent']['remote_id'];self.provider.edit(remote,summary='edited after lost response')
        self.assertEqual(self.engine.reconcile(op)['state'],'conflict');self.assertEqual(len(self.provider.writes()),1)

    def test_missing_etag_reply_requires_reconcile(self):
        record,op=self.publish();original=self.provider.__call__
        def missing(method,url,data=None,headers=None):
            result=original(method,url,data,headers)
            if method=='POST' and '/calendar/v3/' in url:result.pop('etag',None)
            return result
        self.google.transport=missing;self.assertEqual(self.engine.run(op)['state'],'uncertain')
        self.assertEqual(self.engine.reconcile(op)['state'],'synced');self.assertEqual(len(self.provider.writes()),1)

    def test_refresh_reduced_scope_prevents_write(self):
        record,op=self.publish();token=self.google.credentials.load();token['expires_at']=0;self.google.credentials.save(token)
        self.provider.scopes=list(google.SCOPES)
        self.assertEqual(self.engine.run(op)['state'],'error');self.assertEqual(self.provider.writes(),[])
        self.assertNotIn(google.WRITE_SCOPE,self.google.credentials.load()['scopes'])

    def test_atomic_destination_queue_failure_rolls_back_record_link_and_approval(self):
        target=self.engine.target('cal');p=self.local.preview({'kind':'event','action':'create','payload':self.event()},[],google_target=target)
        with mock.patch.object(calendar_sync,'queue',side_effect=RuntimeError('injected local failure')):
            with self.assertRaises(RuntimeError):self.local.apply(p['plan_id'],p['operation_id'],[])
        self.assertEqual(self.local.records(),[]);self.assertEqual(self.engine.links(),[])
        self.assertEqual(self.local.get(p['operation_id'])['status'],'planned')

    def test_remote_success_local_commit_failure_reconciles_without_resend(self):
        record,op=self.publish()
        with self.local.connection(True) as db:
            db.execute("CREATE TRIGGER fail_complete BEFORE UPDATE ON calendar_outbox WHEN new.state='synced' BEGIN SELECT RAISE(ABORT,'injected'); END;")
        with self.assertRaises(sqlite3.Error):self.engine.run(op)
        self.assertEqual(self.engine.operation(op)['state'],'syncing');self.assertEqual(len(self.provider.events),1)
        with self.local.connection(True) as db:db.execute('DROP TRIGGER fail_complete')
        self.assertEqual(self.engine.reconcile(op)['state'],'synced');self.assertEqual(len(self.provider.writes()),1)

    def test_cache_failure_does_not_turn_known_success_into_retry(self):
        record,op=self.publish()
        original=self.google.store.put
        def unavailable(values):
            if any('"mirror"' in k for k in values):raise ValueError('cache unavailable')
            return original(values)
        with mock.patch.object(self.google.store,'put',side_effect=unavailable):
            self.assertEqual(self.engine.run(op)['state'],'synced')
        self.engine.run(op);self.assertEqual(len(self.provider.writes()),1)

    def test_concurrent_instances_claim_only_one_remote_effect(self):
        import concurrent.futures
        record,op=self.publish();other_google=google.Service(self.registry,transport=self.provider);self.addCleanup(other_google.close)
        other=calendar_sync.Engine(calendar_store.Store(self.registry),other_google)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda engine:engine.run(op),[self.engine,other]))
        self.assertTrue(all(r['state']=='synced' for r in results));self.assertEqual(len(self.provider.writes()),1)

    def test_hide_deduplicates_cached_remote_and_show_restores_one_identity(self):
        record,op=self.publish();self.engine.run(op);self.google.sync()
        p=self.local.preview({'kind':'event','action':'delete','id':record['id'],'expected_revision':record['revision']},[])
        self.local.apply(p['plan_id'],p['operation_id'],[]);remote=self.google.range('2026-10-18','2026-10-20')
        view=calendar_view.query([],self.local,'2026-10-18','2026-10-20',google_result=remote,google_links=self.engine.links())
        self.assertEqual(view['days'][0]['event_count'],0);self.assertEqual(len(self.provider.events),1)
        current=self.current(record['id']);p=self.engine.preview({'action':'show','local_id':record['id'],'expected_revision':current['revision']},[])
        self.engine.apply(p['plan_id'],p['operation_id'],[]);self.assertFalse(self.current(record['id'])['deleted'])
        self.assertEqual(len(self.provider.writes()),1)

    def test_remote_deletion_pulls_without_automatic_resurrection(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.provider.events.pop(remote);self.google.sync();self.engine.pull()
        self.assertTrue(self.current(record['id'])['deleted']);self.assertEqual(len(self.provider.writes()),1)

    def test_private_and_shared_properties_are_preserved(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        props=self.provider.events[remote]['extendedProperties'];props['private']['otherApp']='keep';props['shared']={'otherShared':'keep'}
        self.provider.edit(remote,extendedProperties=props);self.google.sync();self.engine.pull()
        result=self.update(self.current(record['id']),title='changed');self.engine.run(result['google_operation_id'])
        after=self.provider.events[remote]['extendedProperties'];self.assertEqual(after['private']['otherApp'],'keep');self.assertEqual(after['shared'],{'otherShared':'keep'})

    def http(self):
        httpd=serve(self.registry,port=0,token='startup',google_transport=self.provider)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        self.addCleanup(httpd.server_close);self.addCleanup(httpd.shutdown)
        base='http://127.0.0.1:'+str(httpd.server_port)+'/api/v2/calendar/'
        def call(method,path,body=None,token='startup'):
            req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                                       headers={'X-Auth-Token':token,'Content-Type':'application/json'},method=method)
            try:
                with urllib.request.urlopen(req) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:
                try:return exc.code,json.load(exc)
                finally:exc.close()
        return call

    def test_http_google_approval_guards_and_raw_payload_rejection(self):
        call=self.http();record=self.create()
        status,plan=call('POST','google/write/plan',{'action':'publish','local_id':record['id'],'expected_revision':1,'calendar_id':'cal'})
        self.assertEqual(status,200)
        ids={k:plan[k] for k in ('plan_id','operation_id')}
        self.assertEqual(call('POST','google/write/apply',{**ids,'payload':{'summary':'injected'}})[0],409)
        self.assertEqual(call('POST','google/write/apply',ids,token='wrong')[0],403)
        self.assertEqual(self.provider.writes(),[])
        status,result=call('POST','google/write/apply',ids);self.assertEqual(status,200);self.assertEqual(result['google']['state'],'synced')
        call('POST','google/write/apply',ids);self.assertEqual(len(self.provider.writes()),1)
        self.assertEqual(call('POST','google/outbox/run',{'operation_id':result['google_operation_id'],'remote_id':'injected'})[0],409)

    def test_http_destination_and_note_rejection(self):
        call=self.http();status,plan=call('POST','plan',{'action':'create','kind':'event','payload':self.event(),'destination':{'calendar_id':'cal'}})
        self.assertEqual(status,200)
        status,result=call('POST','apply',{k:plan[k] for k in ('plan_id','operation_id')});self.assertEqual(status,200)
        self.assertEqual(result['google']['state'],'synced');self.assertEqual(len(self.provider.events),1)
        self.assertEqual(call('POST','plan',{'action':'create','kind':'note','payload':{'date':'2026-10-18','body':'no','project_ids':[]},'destination':{'calendar_id':'cal'}})[0],400)
        self.assertEqual(len(self.provider.events),1)

    def test_http_lost_remote_response_retry_is_lookup_only(self):
        call=self.http();record=self.create();_,plan=call('POST','google/write/plan',{'action':'publish','local_id':record['id'],'expected_revision':1,'calendar_id':'cal'})
        ids={k:plan[k] for k in ('plan_id','operation_id')};self.provider.drop='POST'
        _,result=call('POST','google/write/apply',ids);self.assertEqual(result['google']['state'],'uncertain')
        _,again=call('POST','google/write/apply',ids);self.assertEqual(again['google']['state'],'uncertain');self.assertEqual(len(self.provider.writes()),1)
        _,recovered=call('POST','google/outbox/reconcile',{'operation_id':result['google_operation_id']})
        self.assertEqual(recovered['state'],'synced');self.assertEqual(len(self.provider.writes()),1)

    def test_range_get_never_flushes_pending_and_sync_explicitly_does(self):
        record,op=self.publish();call=self.http()
        self.assertEqual(call('GET','range?start=2026-10-18&end=2026-10-20')[0],200)
        self.assertEqual(self.provider.writes(),[])
        self.assertEqual(call('POST','google/sync',{})[0],200);self.assertEqual(len(self.provider.writes()),1)

    def test_plan_selection_change_and_expiry_have_no_remote_effect(self):
        record=self.create();p=self.engine.preview({'action':'publish','local_id':record['id'],'expected_revision':1,'calendar_id':'cal'},[])
        self.google.select([])
        with self.assertRaises(operations.StateConflict):self.engine.apply(p['plan_id'],p['operation_id'],[])
        self.assertEqual(self.engine.links(),[]);self.assertEqual(self.provider.writes(),[])
        self.google.select(['cal'])
        with mock.patch.object(calendar_sync.time,'time',return_value=p['expires_at']+1):
            with self.assertRaises(operations.StateConflict):self.engine.apply(p['plan_id'],p['operation_id'],[])

    def test_outbox_intent_is_immutable_and_old_operation_does_not_send_new_body(self):
        record,op=self.publish();result=self.update(record,title='superseding local')
        with self.local.connection(True) as db:
            with self.assertRaises(sqlite3.Error):db.execute("UPDATE calendar_outbox SET intent='{}' WHERE id=?",(op,))
        self.assertEqual(self.engine.run(op)['state'],'cancelled');self.assertEqual(self.provider.writes(),[])
        new=result['google_operation_id'];self.engine.run(new);self.assertEqual(next(iter(self.provider.events.values()))['summary'],'superseding local')

    def test_write_grant_implies_event_read_without_redundant_readonly_scope(self):
        self.provider.scopes=[*google.SCOPES[:3],google.WRITE_SCOPE]
        start=self.google.connect(write=True);q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        self.assertNotIn(google.SCOPES[3],q['scope'][0].split())
        with urllib.request.urlopen(q['redirect_uri'][0]+'?'+urllib.parse.urlencode({'state':q['state'][0],'code':'synthetic'})):pass
        self.assertTrue(self.google.status()['can_write']);record,op=self.publish();self.assertEqual(self.engine.run(op)['state'],'synced')

    def test_reconcile_read_failure_leaves_uncertain_and_does_not_send(self):
        record,op=self.publish();self.provider.drop='POST';self.engine.run(op);self.provider.fail=503
        with self.assertRaises(google.GoogleError):self.engine.reconcile(op)
        self.assertEqual(self.engine.operation(op)['state'],'uncertain');self.engine.run(op);self.assertEqual(len(self.provider.writes()),1)
        self.provider.fail=None;self.assertEqual(self.engine.reconcile(op)['state'],'synced')

    def test_changed_local_after_publish_plan_is_rejected(self):
        record=self.create();p=self.engine.preview({'action':'publish','local_id':record['id'],'expected_revision':1,'calendar_id':'cal'},[])
        self.update(record,title='later local')
        with self.assertRaises(operations.StateConflict):self.engine.apply(p['plan_id'],p['operation_id'],[])
        self.assertEqual(self.provider.writes(),[]);self.assertEqual(self.engine.links(),[])

    def test_google_410_preserves_actual_link_and_pending_outbox_bytes(self):
        self.google.sync();record,op=self.publish();before=self.local.path.read_bytes();original=self.provider.__call__
        def expired(method,url,data=None,headers=None):
            if 'syncToken=' in url:raise google.GoogleError(410)
            return original(method,url,data,headers)
        self.google.transport=expired;self.assertTrue(self.google.sync()['complete'])
        self.assertEqual(self.local.path.read_bytes(),before);self.assertEqual(self.engine.operation(op)['state'],'queued')

    def test_remote_deleted_while_local_dirty_requires_explicit_resolution(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.update(record,title='keep local input');self.provider.events.pop(remote);self.google.sync();self.engine.pull()
        self.assertEqual(self.current(record['id'])['title'],'keep local input');self.assertEqual(self.engine.links()[0]['state'],'conflict')
        with self.assertRaises(operations.StateConflict):self.engine.preview({'action':'resolve','resolution':'local','local_id':record['id'],
            'expected_revision':self.current(record['id'])['revision'],'remote_etag':None},[])
        self.assertEqual(len(self.provider.writes()),1)

    def test_equivalent_utc_response_body_matches_original_offset(self):
        payload=self.event(all_day=False,start_at='2026-10-18T09:00:00+09:00',end_at='2026-10-18T10:00:00+09:00',timezone='Asia/Seoul')
        payload.pop('start_date');payload.pop('end_date')
        p=self.local.preview({'kind':'event','action':'create','payload':payload},[]);record=self.local.apply(p['plan_id'],p['operation_id'],[])['record']
        record,op=self.publish(record);self.provider.drop='POST';self.engine.run(op)
        remote=self.engine.operation(op)['intent']['remote_id'];self.provider.events[remote]['start']['dateTime']='2026-10-18T00:00:00Z';self.provider.events[remote]['end']['dateTime']='2026-10-18T01:00:00Z'
        self.assertEqual(self.engine.reconcile(op)['state'],'synced')

    def test_linked_project_google_filter_uses_local_mapping_without_external_records(self):
        project={'id':'p','path':str(self.registry.parent/'p')};Path(project['path']).mkdir()
        p=self.local.preview({'kind':'event','action':'create','payload':self.event(project_ids=['p'])},[project]);record=self.local.apply(p['plan_id'],p['operation_id'],[project])['record']
        p=self.engine.preview({'action':'publish','local_id':record['id'],'expected_revision':1,'calendar_id':'cal'},[project]);self.engine.apply(p['plan_id'],p['operation_id'],[project])
        calls=len(self.provider.calls)
        view=calendar_view.query([project],self.local,'2026-10-18','2026-10-20',project_ids=['p'],source_ids=['google'],google_result=self.engine.scope(),google_links=self.engine.links())
        self.assertEqual(view['days'][0]['event_count'],1);self.assertEqual(len(self.provider.calls),calls)
        token=self.google.credentials.load();token['sub']='other';self.google.credentials.save(token)
        view=calendar_view.query([project],self.local,'2026-10-18','2026-10-20',project_ids=['p'],source_ids=['google'],google_result=self.engine.scope(),google_links=self.engine.links())
        self.assertEqual(view['days'][0]['event_count'],0)

    def test_wrong_operation_marker_prevents_adoption(self):
        record,op=self.publish();intent=self.engine.operation(op)['intent'];remote=intent['remote_id']
        self.provider.events[remote]={'id':remote,'etag':'"foreign"',**calendar_sync.body(record),
            'extendedProperties':{'private':{'wsflowOperation':'different-operation','wsflowLocal':record['id'],'wsflowPayload':intent['payload_hash']}}}
        self.assertEqual(self.engine.run(op)['state'],'conflict')

    def test_account_switch_even_with_same_calendar_id_blocks(self):
        record,op=self.publish();token=self.google.credentials.load();token['sub']='account-B';self.google.credentials.save(token)
        self.google.select(['cal']);self.assertEqual(self.engine.run(op)['state'],'error');self.assertEqual(self.provider.writes(),[])

    def test_unsupported_delete_plan_is_rejected(self):
        for fields in ({'recurrence':['RRULE:FREQ=DAILY']},{'attendees':[{'email':'x@example.test'}]},{'eventType':'outOfOffice'}):
            record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id'];self.provider.edit(remote,**fields)
            with self.assertRaises(operations.StateConflict):self.engine.preview({'action':'delete','local_id':record['id'],'expected_revision':1},[])

    def test_known_commit_ack_error_never_downgrades_synced(self):
        record,op=self.publish();original=self.engine._complete
        def interrupted(ident,remote):
            original(ident,remote);raise operations.StateConflict('injected after commit')
        with mock.patch.object(self.engine,'_complete',side_effect=interrupted):self.assertEqual(self.engine.run(op)['state'],'synced')
        self.engine.run(op);self.assertEqual(len(self.provider.writes()),1)

    def test_unpublished_hidden_event_can_be_shown_offline_without_upload(self):
        record,op=self.publish();p=self.local.preview({'action':'delete','kind':'event','id':record['id'],'expected_revision':1},[])
        self.local.apply(p['plan_id'],p['operation_id'],[]);self.assertEqual(self.engine.operation(op)['state'],'cancelled')
        current=self.current(record['id']);self.provider.fail=503
        p=self.engine.preview({'action':'show','local_id':record['id'],'expected_revision':current['revision']},[])
        self.engine.apply(p['plan_id'],p['operation_id'],[]);self.assertFalse(self.current(record['id'])['deleted'])
        self.assertEqual(self.provider.writes(),[])

    def test_hide_show_preserves_uncertain_operation_for_later_recovery(self):
        record,op=self.publish();self.provider.drop='POST';self.engine.run(op)
        p=self.local.preview({'action':'delete','kind':'event','id':record['id'],'expected_revision':1},[]);self.local.apply(p['plan_id'],p['operation_id'],[])
        current=self.current(record['id']);p=self.engine.preview({'action':'show','local_id':record['id'],'expected_revision':current['revision']},[])
        self.engine.apply(p['plan_id'],p['operation_id'],[])
        self.assertEqual(self.engine.links()[0]['pending'],op);self.assertEqual(self.engine.operation(op)['state'],'uncertain')
        self.engine.reconcile(op);self.assertEqual(len(self.provider.writes()),1)
        self.assertFalse(any(o['state']=='queued' for o in self.engine.status()['operations']))

    def test_showing_remote_deleted_archive_never_recreates_on_edit(self):
        record,op=self.publish();self.engine.run(op);remote=self.engine.operation(op)['intent']['remote_id']
        self.provider.events.pop(remote);self.google.sync();self.engine.pull();current=self.current(record['id'])
        p=self.engine.preview({'action':'show','local_id':record['id'],'expected_revision':current['revision']},[])
        self.engine.apply(p['plan_id'],p['operation_id'],[]);current=self.current(record['id'])
        self.update(current,title='keep archive edits');self.assertEqual(self.engine.links()[0]['state'],'conflict')
        self.engine.after_sync();self.assertEqual(self.provider.events,{});self.assertEqual(len(self.provider.writes()),1)


if __name__=='__main__':unittest.main()
