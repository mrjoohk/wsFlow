"""Actual package producer admission with synthetic stores/provider and Event barriers."""
from __future__ import annotations

from contextlib import closing, contextmanager
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.error
import urllib.request

from wsflow.modules.work_calendar import backend, calendar_store, calendar_sync, google_calendar as google
from wsflow.modules.workspace_operations import backend as workspace
from wsflow.store import Store


class PackageActivityTests(unittest.TestCase):
    def setUp(self):
        evidence=Path(__file__).resolve().parents[1]/'logs/test_evidence/261029_P060_runtime/packages'
        evidence.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(prefix='fixture-',dir=evidence)
        self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        self.registry=self.base/'registry/projects.json';self.registry.parent.mkdir()
        self.registry.write_text('{"projects":[],"roots":[]}',encoding='utf8')
        self.data=self.base/'userdata';self.data.mkdir()
        (self.data/'google-client.json').write_text(json.dumps({'installed':{'client_id':'synthetic.apps.googleusercontent.com','client_secret':'synthetic'}}),encoding='utf8')
        patch=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.data),'WSFLOW_GOOGLE_CLIENT_FILE':str(self.data/'google-client.json')})
        patch.start();self.addCleanup(patch.stop)
        self.calls=[];self.events={};self.credentials_saves=[]
        self.token={'sub':'synthetic','access_token':'synthetic','expires_at':time.time()+3600,'scopes':[*google.SCOPES,google.WRITE_SCOPE]}

    def gate(self):
        from wsflow.runtime_activity import ActivityGate
        return ActivityGate()

    def transport(self,method,url,data=None,headers=None):
        self.calls.append((method,url,copy.deepcopy(data)))
        path=urlsplit(url).path
        if url==google.TOKEN_URL:return {'access_token':'synthetic','scope':' '.join(self.token['scopes']),'expires_in':3600}
        if url==google.USER_URL:return {'sub':'synthetic','email':'synthetic@example.test'}
        if path.endswith('/calendarList'):return {'items':[{'id':'cal','accessRole':'owner'}]}
        if method=='GET' and path.endswith('/events'):return {'items':list(self.events.values()),'nextSyncToken':'synthetic-cursor'}
        if method=='POST' and path.endswith('/events'):
            value={**data,'etag':'"synthetic"','status':'confirmed'};self.events[value['id']]=value;return value
        ident=path.rsplit('/',1)[-1]
        if method=='GET' and ident in self.events:return self.events[ident]
        raise AssertionError('unexpected fake-provider request '+method+' '+url)

    def service(self,activity=None):
        value=google.Service(self.registry,transport=self.transport,activity=activity)
        value.credentials=SimpleNamespace(load=lambda:self.token,save=lambda token:self.credentials_saves.append(dict(token)),path=self.data/'absent-synthetic-credential')
        self.addCleanup(value.close)
        return value

    def services(self,activity=None):
        values={'store':Store(self.registry),'google_transport':self.transport}
        if activity is not None:values['activity']=activity
        return SimpleNamespace(**values)

    def launch(self,fn):
        outcome={}
        def run():
            try:outcome['value']=fn()
            except BaseException as exc:outcome['error']=exc
        thread=threading.Thread(target=run);thread.start()
        self.addCleanup(lambda:thread.join(3))
        return thread,outcome

    def block(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        return entered,release

    def assert_paused(self,fn):
        from wsflow.runtime_activity import ActivityUnavailable
        with self.assertRaises(ActivityUnavailable):fn()

    def test_backend_protocol_and_legacy_unbound(self):
        legacy=backend.Service({'services':self.services()})
        self.assertEqual(legacy.runtime_quiescence(),{'api':1,'activity_bound':False,'owns_runtime':True})
        self.assertIsNone(legacy.google)
        borrowed=SimpleNamespace(close=mock.Mock())
        legacy.use_runtime(borrowed)
        self.assertEqual(legacy.runtime_quiescence(),{'api':1,'activity_bound':False,'owns_runtime':False})
        self.assertTrue(legacy.stop());borrowed.close.assert_not_called()
        other=workspace.Backend({'services':self.services()})
        self.assertEqual(other.runtime_quiescence(),{'api':1,'activity_bound':False,'owns_runtime':True})
        self.assertTrue(other.stop(timeout=.01))

    def test_google_and_engine_accept_callable_activity(self):
        roots=[]
        @contextmanager
        def activity():
            roots.append('enter')
            try:yield
            finally:roots.append('exit')
        service=self.service(activity)
        engine=calendar_sync.Engine(calendar_store.Store(self.registry),service,activity=activity)
        self.assertEqual(engine.links(),[])
        self.assertEqual(roots,['enter','exit'])
        for value in (False,123,'activity'):
            with self.subTest(value=value),self.assertRaises(ValueError):google.Service(self.registry,activity=value)
            with self.subTest(engine_value=value),self.assertRaises(ValueError):calendar_sync.Engine(calendar_store.Store(self.registry),service,activity=value)

    def test_backend_binds_owned_worker_and_legacy_stop_arguments(self):
        gate=self.gate();service=backend.Service({'services':self.services(gate.activity)})
        service.start();self.addCleanup(service.stop)
        self.assertTrue(service.runtime_quiescence()['activity_bound'])
        self.assertTrue(service.google.refresh_thread.is_alive())
        self.assertIs(service.google._activity,service._activity)
        self.assertIs(service.writer._activity,service._activity)
        self.assertTrue(gate.pause(.1))
        for method in (service.start,service.calendar_status,lambda:service.workflow_status('2026-10-29'),lambda:service.use_runtime(None)):
            self.assert_paused(method)
        self.assertTrue(service.stop(timeout=.2));self.assertFalse(service.google.refresh_thread.is_alive())
        legacy=backend.Service({'services':self.services()});legacy.google=SimpleNamespace(close=mock.Mock(return_value=True))
        self.assertTrue(legacy.stop());legacy.google.close.assert_called_once_with()
        explicit=backend.Service({'services':self.services()});explicit.google=SimpleNamespace(close=mock.Mock(return_value=True))
        self.assertTrue(explicit.stop(timeout=.01));explicit.google.close.assert_called_once_with(timeout=.01)

    def test_google_public_calls_reject_before_provider_or_storage(self):
        gate=self.gate();service=self.service(gate.activity)
        self.assertTrue(gate.pause(.1));before=self.registry.read_bytes()
        calls=[service.client,service.status,service.calendars,lambda:service.select([]),lambda:service.mirror('cal'),service.sync,
               lambda:service.range('2026-10-29','2026-10-30'),service.connect,service.disconnect,service.refresh_if_due,service.start_refresh]
        for call in calls:
            with self.subTest(method=call):self.assert_paused(call)
        with self.assertRaises(Exception) as raised:
            with service.locked():self.fail('lock admitted')
        from wsflow.runtime_activity import ActivityUnavailable
        self.assertIsInstance(raised.exception,ActivityUnavailable)
        self.assertEqual(self.calls,[]);self.assertFalse(service.store.path.exists());self.assertEqual(self.registry.read_bytes(),before)

    def test_google_event_drain_tracks_cache_write_and_nested_completion(self):
        gate=self.gate();service=self.service(gate.activity);entered,release=self.block()
        original=self.transport
        def blocked(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(2));return original(*args,**kwargs)
        service.transport=blocked
        thread,result=self.launch(service.calendars);self.assertTrue(entered.wait(1))
        self.assertEqual(gate.snapshot()['in_flight'],1)
        self.assertFalse(gate.pause(.01));self.assert_paused(service.status)
        self.assertFalse(service.store.path.exists())
        release.set();thread.join(2);self.assertNotIn('error',result)
        self.assertEqual(result['value']['account_id'],'synthetic');self.assertEqual(gate.snapshot()['in_flight'],0)
        with closing(sqlite3.connect(service.store.path)) as db:
            rows=db.execute('SELECT value FROM google_values').fetchall()
        self.assertEqual(json.loads(rows[0][0])[0]['id'],'cal')

    def test_google_locked_enters_activity_before_mutex(self):
        gate=self.gate();service=self.service(gate.activity);attempted=threading.Event()
        service.mutex.acquire();held=[True]
        self.addCleanup(lambda:service.mutex.release() if held[0] else None)
        def waiter():
            attempted.set()
            with service.locked():return True
        thread,result=self.launch(waiter);self.assertTrue(attempted.wait(1))
        deadline=time.monotonic()+1
        while gate.snapshot()['in_flight']==0 and time.monotonic()<deadline:threading.Event().wait(.001)
        self.assertEqual(gate.snapshot()['in_flight'],1);self.assertFalse(gate.pause(.01))
        service.mutex.release();held[0]=False
        thread.join(2);self.assertEqual(result.get('value'),True)

    def test_engine_event_holds_lease_through_plan_and_outbox_effects(self):
        gate=self.gate();service=self.service(gate.activity)
        service.calendars();service.select(['cal'])
        local=calendar_store.Store(self.registry)
        plan=local.preview({'kind':'event','action':'create','payload':{'title':'synthetic','body':'','location':'','project_ids':[],'all_day':True,'start_date':'2026-10-29','end_date':'2026-10-30'}},[])
        record=local.apply(plan['plan_id'],plan['operation_id'],[])['record']
        engine=calendar_sync.Engine(local,service,activity=gate.activity)
        entered,release=self.block();original=local.save_plan
        def blocked(*args,**kwargs):entered.set();self.assertTrue(release.wait(2));return original(*args,**kwargs)
        with mock.patch.object(local,'save_plan',side_effect=blocked):
            thread,result=self.launch(lambda:engine.preview({'action':'publish','local_id':record['id'],'expected_revision':record['revision'],'calendar_id':'cal'},[]))
            self.assertTrue(entered.wait(1));self.assertEqual(gate.snapshot()['in_flight'],1)
            self.assertFalse(gate.pause(.01));release.set();thread.join(2)
        self.assertNotIn('error',result);self.assertEqual(gate.snapshot()['in_flight'],0)
        before=local.path.read_bytes();calls=len(self.calls)
        methods=[engine.links,engine.scope,lambda:engine.operation('missing'),engine.status,lambda:engine.target('cal'),
                 lambda:engine.preview({},[]),lambda:engine.apply('p','o',[]),lambda:engine.run('o'),lambda:engine.reconcile('o'),engine.pull,engine.after_sync]
        for method in methods:
            with self.subTest(method=method):self.assert_paused(method)
        self.assertEqual(before,local.path.read_bytes());self.assertEqual(calls,len(self.calls))
        gate.resume();p=result['value'];applied=engine.apply(p['plan_id'],p['operation_id'],[])
        self.assertEqual(engine.operation(applied['google_operation_id'])['state'],'queued')

    def test_workspace_actual_registry_dispatch_and_summary_are_admitted(self):
        gate=self.gate();service=workspace.Backend({'services':self.services(gate.activity)})
        path=self.base/'project';path.mkdir();entered,release=self.block();original=service.services.store.add_root
        def blocked(*args,**kwargs):entered.set();self.assertTrue(release.wait(2));return original(*args,**kwargs)
        with mock.patch.object(service.services.store,'add_root',side_effect=blocked):
            thread,result=self.launch(lambda:service.dispatch(SimpleNamespace(),'POST','/api/roots',{}, {'path':str(path)}))
            self.assertTrue(entered.wait(1));self.assertFalse(gate.pause(.01));release.set();thread.join(2)
        self.assertNotIn('error',result)
        before=self.registry.read_bytes()
        self.assert_paused(lambda:service.dispatch(SimpleNamespace(),'POST','/api/roots',{}, {'path':str(path)}))
        self.assert_paused(lambda:service.summary({'path':str(path)}));self.assert_paused(service.start)
        self.assertEqual(before,self.registry.read_bytes());self.assertTrue(service.stop(timeout=.01))

    def test_oauth_callback_pause_rejects_before_pending_consumption(self):
        gate=self.gate();service=self.service(gate.activity);service.credentials.load=lambda:None
        connected=service.connect();query=parse_qs(urlsplit(connected['authorization_url']).query)
        pending=copy.deepcopy(service.pending);epoch=service.store.get_value('oauth_epoch')
        self.assertTrue(gate.pause(.1));self.calls.clear()
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(query['redirect_uri'][0]+'?'+urlencode({'state':query['state'][0],'code':'synthetic'}),timeout=2)
        self.assertEqual(error.exception.code,503);error.exception.close();self.assertEqual(service.pending,pending)
        self.assertEqual(service.store.get_value('oauth_epoch'),epoch);self.assertEqual(self.calls,[]);self.assertEqual(self.credentials_saves,[])
        self.assertTrue(service.close(timeout=.5))

    def test_oauth_admitted_callback_finishes_after_pause(self):
        gate=self.gate();service=self.service(gate.activity);service.credentials.load=lambda:None
        connected=service.connect();query=parse_qs(urlsplit(connected['authorization_url']).query)
        entered,release=self.block();original=self.transport
        def blocked(method,url,*args,**kwargs):
            if url==google.TOKEN_URL:entered.set();self.assertTrue(release.wait(2))
            return original(method,url,*args,**kwargs)
        service.transport=blocked
        def callback():
            with urllib.request.urlopen(query['redirect_uri'][0]+'?'+urlencode({'state':query['state'][0],'code':'synthetic'}),timeout=2) as response:
                return response.status
        thread,result=self.launch(callback);self.assertTrue(entered.wait(1))
        self.assertEqual(gate.snapshot()['in_flight'],1);self.assertFalse(gate.pause(.01))
        self.assert_paused(service.status);release.set();thread.join(2)
        self.assertEqual(result.get('value'),200);self.assertEqual(len(self.credentials_saves),1)
        self.assertEqual(gate.snapshot()['in_flight'],0);self.assertTrue(service.close(timeout=.5))

    def test_oauth_partial_header_close_timeout_then_socket_release_retry(self):
        gate=self.gate();service=self.service(gate.activity)
        service.connect()
        listener=service.listener[0];worker=service.listener_threads[-1]
        accepted=threading.Event();header_wait=threading.Event()
        actual_get_request=listener.get_request;actual_parse_headers=http.client.parse_headers
        reads={'entered':0,'completed':0}
        def observed_get_request():
            result=actual_get_request();accepted.set();return result
        def observed_parse_headers(stream,*args,**kwargs):
            class ObservedReader:
                def readline(self,limit=-1):
                    reads['entered']+=1
                    if reads['entered']==2:header_wait.set()
                    line=stream.readline(limit);reads['completed']+=1
                    return line
            return actual_parse_headers(ObservedReader(),*args,**kwargs)
        before={path:path.read_bytes() for path in (self.registry,service.store.path)}
        with mock.patch.object(listener,'get_request',side_effect=observed_get_request),mock.patch.object(http.client,'parse_headers',side_effect=observed_parse_headers),mock.patch.object(listener,'handle_error') as socket_errors:
            client=socket.create_connection(('127.0.0.1',listener.server_port),timeout=1)
            try:
                # A complete request line and Host header reach the real HTTP
                # parser; the second header has no line ending or final blank line.
                client.sendall(b'GET /oauth/callback HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Incomplete: ')
                self.assertTrue(accepted.wait(1));self.assertTrue(header_wait.wait(1))
                self.assertEqual(reads['completed'],1);self.assertTrue(worker.is_alive())
                self.assertTrue(gate.pause(.1));self.assertEqual(gate.snapshot()['in_flight'],0)
                started=time.monotonic();first=service.close(timeout=.05);elapsed=time.monotonic()-started
                self.assertFalse(first);self.assertGreaterEqual(elapsed,.035);self.assertLess(elapsed,.5)
                self.assertFalse(service.workers_stopped);self.assertTrue(worker.is_alive())
                self.listener_timeout_observation={'accepted':accepted.is_set(),'header_read_wait_observed':header_wait.is_set(),
                    'header_reads_completed_before_close':reads['completed'],'gate_after_pause':gate.snapshot(),
                    'worker_alive_at_first_close':worker.is_alive(),'first_close_result':first,
                    'requested_timeout_seconds':.05,'first_close_elapsed_seconds':elapsed}
            finally:
                client.close()
                retry_started=time.monotonic();retry=service.close(timeout=.5);retry_elapsed=time.monotonic()-retry_started
                worker.join(.5)
            self.assertTrue(retry);self.assertTrue(service.workers_stopped);self.assertFalse(worker.is_alive())
            self.assertEqual(before,{path:path.read_bytes() for path in before})
            self.assertEqual(self.calls,[]);self.assertEqual(self.credentials_saves,[])
            self.listener_timeout_observation.update(retry_close_result=retry,retry_close_elapsed_seconds=retry_elapsed,
                worker_alive_after_retry=worker.is_alive(),provider_calls=len(self.calls),credential_saves=len(self.credentials_saves),
                registry_and_db_bytes_unchanged=True,peer_release_socket_error_callbacks=socket_errors.call_count)

    def test_provider_exception_releases_bound_service_activity(self):
        gate=self.gate();service=self.service(gate.activity)
        with mock.patch.object(service,'transport',side_effect=google.GoogleError(503)):
            with self.assertRaises(google.GoogleError):service.calendars()
        self.assertEqual(gate.snapshot()['in_flight'],0);self.assertTrue(gate.pause(.1))
        self.assertFalse(service.store.path.exists())

    def test_refresh_worker_effect_is_admitted_and_can_stop_after_timeout(self):
        gate=self.gate();service=self.service(gate.activity);entered,release=self.block();original=service.status
        def blocked():entered.set();self.assertTrue(release.wait(2));return original()
        with mock.patch.object(google,'CACHE_SECONDS',.005),mock.patch.object(service,'status',side_effect=blocked):
            service.start_refresh();self.assertTrue(entered.wait(1));self.assertEqual(gate.snapshot()['in_flight'],1)
            self.assertFalse(gate.pause(.01));self.assertFalse(service.close(timeout=.01))
            release.set();self.assertTrue(service.close(timeout=.5))
        self.assertFalse(service.refresh_thread.is_alive());self.assertEqual(gate.snapshot()['in_flight'],0)
        calls=len(self.calls);self.assert_paused(service.refresh_if_due);self.assertEqual(calls,len(self.calls))


if __name__=='__main__':unittest.main()
