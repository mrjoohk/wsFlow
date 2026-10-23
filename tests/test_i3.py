"""I3 protocol and fault tests. Synthetic provider only, no account traffic."""
import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from wsflow import google_calendar as google, calendar_store, calendar_view
from wsflow.server import serve


def open_url(*args,**kwargs):
    kwargs.setdefault('timeout',5)
    try:
        return urllib.request.urlopen(*args,**kwargs)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise


def event(ident='one', **extra):
    return {'id':ident,'summary':'Google meeting','status':'confirmed',
            'start':{'date':'2026-10-18'},'end':{'date':'2026-10-19'},**extra}


class Provider:
    def __init__(self):
        self.calls=[]; self.pages={}; self.fail=None; self.scope=google.SCOPES
        self.access_token='access-SENSITIVE'
    def __call__(self, method, url, data=None, headers=None):
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        path=urllib.parse.urlsplit(url).path
        self.calls.append((method,path,q,data,headers))
        if path.endswith('/token'):
            return {'access_token':self.access_token,'refresh_token':'refresh-SENSITIVE',
                    'expires_in':3600,'scope':' '.join(self.scope)}
        if path.endswith('/revoke'):return {}
        if (headers or {}).get('Authorization') != 'Bearer '+self.access_token:raise google.GoogleError(401)
        if path.endswith('/userinfo'):return {'sub':'account-A','email':'tester@example.test'}
        if self.fail:raise google.GoogleError(self.fail)
        if path.endswith('/calendarList'):
            return {'items':[{'id':'cal-A','summary':'Team','accessRole':'reader'},
                             {'id':'free','summary':'Busy','accessRole':'freeBusyReader'}]}
        key=('range' if 'timeMin' in q else 'sync',q.get('pageToken',[''])[0])
        value=copy.deepcopy(self.pages.get(key,{'items':[event()],'nextSyncToken':'cursor-1'}))
        if isinstance(value,int):raise google.GoogleError(value)
        return value


class GoogleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.registry=self.root/'workspace'/'projects.json'
        self.registry.parent.mkdir(); self.data=self.root/'userdata'
        self.env=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.data)},clear=False)
        self.env.start();self.addCleanup(self.env.stop)
        self.data.mkdir();(self.data/'google-client.json').write_text(json.dumps({'installed':{
            'client_id':'test.apps.googleusercontent.com','client_secret':'client-SENSITIVE'}}))
        self.provider=Provider();self.service=google.Service(self.registry,transport=self.provider)
        self.addCleanup(self.service.close)
    def connect(self):
        start=self.service.connect();q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        callback=q['redirect_uri'][0]+'?'+urllib.parse.urlencode({'state':q['state'][0],'code':'code-SENSITIVE'})
        with open_url(callback) as response:self.assertEqual(response.status,200)
        self.assertTrue(self.service.status()['connected']);return q,callback
    def ready(self):
        self.connect(); self.service.calendars();self.service.select(['cal-A'])

    def test_status_readonly_and_secrets_outside_workspace(self):
        self.assertFalse(self.service.status()['connected']);self.assertFalse(self.service.store.path.exists())
        self.connect();status=json.dumps(self.service.status())
        self.assertNotIn('SENSITIVE',status)
        raw=self.service.credentials.path.read_bytes()
        if os.name=='nt':self.assertNotIn(b'refresh-SENSITIVE',raw)
        else:self.assertEqual(self.service.credentials.path.stat().st_mode&0o777,0o600)
        self.assertFalse(any(self.registry.parent.rglob('*.bin')))

    def test_pkce_exact_callback_state_ttl_replay_and_no_api_bypass(self):
        start=self.service.connect();q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        self.assertEqual(q['code_challenge_method'],['S256']);self.assertEqual(set(q['scope'][0].split()),set(google.SCOPES))
        redirect=q['redirect_uri'][0]
        for suffix in ('/wrong?state='+q['state'][0]+'&code=x','?state=wrong&code=x'):
            with self.assertRaises(urllib.error.HTTPError):open_url(redirect+suffix)
        self.assertFalse(any(c[1].endswith('/token') for c in self.provider.calls))
        self.service.pending['expires_at']=0
        with self.assertRaises(urllib.error.HTTPError):open_url(redirect+'?state='+q['state'][0]+'&code=x')
        q,callback=self.connect()
        token_calls=[c for c in self.provider.calls if c[1].endswith('/token')]
        verifier=token_calls[-1][3]['code_verifier']
        self.assertEqual(google.challenge(verifier),q['code_challenge'][0])
        try:
            with self.assertRaises((urllib.error.HTTPError,urllib.error.URLError,ConnectionError)):
                open_url(callback,timeout=1)
        except TimeoutError:pass
        self.assertEqual(len([c for c in self.provider.calls if c[1].endswith('/token')]),1)

    def test_missing_scope_denied_and_callback_error_redacted(self):
        self.provider.scope=['openid','email'];start=self.service.connect()
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            open_url(q['redirect_uri'][0]+'?'+urllib.parse.urlencode({'state':q['state'][0],'code':'secret'}))
        self.assertNotIn(b'SENSITIVE',ctx.exception.read());self.assertFalse(self.service.status()['connected'])

    def test_refresh_restart_disconnect_and_late_callback(self):
        self.ready();token=self.service.credentials.load();token['expires_at']=0;self.service.credentials.save(token)
        self.provider.access_token='rotated-access-SENSITIVE'
        self.service.calendars()
        self.assertTrue(any(c[3] and c[3].get('grant_type')=='refresh_token' for c in self.provider.calls))
        self.assertEqual(self.service.credentials.load()['access_token'],self.provider.access_token)
        other=google.Service(self.registry,transport=self.provider);self.addCleanup(other.close)
        self.assertTrue(other.status()['connected'])
        start=self.service.connect();self.service.disconnect()
        self.assertFalse(other.status()['connected']);self.assertFalse(self.service.credentials.path.exists())
        self.assertTrue(any(c[1].endswith('/revoke') for c in self.provider.calls))
        self.assertIsNotNone(start['authorization_url'])

    def test_calendar_permissions_selection_and_account_isolation(self):
        self.ready();self.assertEqual(self.service.status()['selected_calendar_ids'],['cal-A'])
        with self.assertRaises(ValueError):self.service.select(['free'])
        self.assertFalse(self.service.calendars()['calendars'][0]['can_write'])
        token=self.service.credentials.load();token['sub']='account-B';self.service.credentials.save(token)
        self.assertEqual(self.service.status()['selected_calendar_ids'],[])
        self.assertEqual(self.service.range('2026-10-18','2026-10-20')['events'],[])

    def test_sync_all_pages_fixed_profile_failure_no_cursor_advance(self):
        self.ready();self.service.sync();old=self.service.mirror('cal-A')
        self.provider.pages[('sync','')]={'items':[event('two')],'nextPageToken':'page2'}
        self.provider.pages[('sync','page2')]=503
        result=self.service.sync();self.assertFalse(result['complete'])
        self.assertEqual(self.service.mirror('cal-A')['cursor'],old['cursor'])
        self.assertEqual(self.service.mirror('cal-A')['items'],old['items'])
        self.provider.pages[('sync','page2')]={'items':[event('three')],'nextSyncToken':'cursor-2'}
        self.assertTrue(self.service.sync()['complete']);mirror=self.service.mirror('cal-A')
        self.assertEqual(mirror['cursor'],'cursor-2');self.assertEqual(set(mirror['items']),{'one','two','three'})
        calls=[c for c in self.provider.calls if c[1].endswith('/events')]
        self.assertTrue(all(c[2].get('singleEvents')==['false'] and 'timeMin' not in c[2] for c in calls))
        self.assertEqual(calls[-1][2]['syncToken'],['cursor-1'])

    def test_410_rebuild_generation_preserves_local_and_other_data(self):
        self.ready();self.service.sync();before=self.service.mirror('cal-A')
        self.service.store.put({'future_outbox':{'pending':['operation-id']},'future_mapping':{'local-id':'remote-id'}})
        local=calendar_store.Store(self.registry);plan=local.preview({'kind':'note','action':'create',
            'payload':{'date':'2026-10-18','body':'local pending','project_ids':[]}},[])
        local_before=local.path.read_bytes()
        original=self.provider.__call__
        def expired(method,url,data=None,headers=None):
            if 'syncToken=' in url:raise google.GoogleError(410)
            return original(method,url,data,headers)
        self.service.transport=expired
        self.provider.pages[('sync','')]={'items':[event('new')],'nextSyncToken':'fresh'}
        self.assertTrue(self.service.sync()['complete'])
        after=self.service.mirror('cal-A');self.assertNotEqual(before['generation'],after['generation'])
        self.assertEqual(set(after['items']),{'new'});self.assertEqual(local.path.read_bytes(),local_before)
        self.assertEqual(local.get(plan['operation_id'])['status'],'planned')
        self.assertEqual(self.service.store.get_value('future_outbox'),{'pending':['operation-id']})
        self.assertEqual(self.service.store.get_value('future_mapping'),{'local-id':'remote-id'})

    def test_range_expansion_cancellations_failure_unknown_and_cache(self):
        self.ready();self.service.sync()
        self.provider.pages[('range','')]={'items':[event('instance',recurringEventId='master'),event('cancel',status='cancelled')],
                                         'nextPageToken':'r2'}
        self.provider.pages[('range','r2')]={'items':[event('timed',start={'dateTime':'2026-10-18T23:00:00+09:00'},
                                                                       end={'dateTime':'2026-10-19T00:00:00+09:00'})]}
        result=self.service.range('2026-10-18','2026-10-20');self.assertTrue(result['complete'])
        self.assertEqual({e['remote_id'] for e in result['events']},{'instance','timed'})
        count=len(self.provider.calls);self.service.range('2026-10-18','2026-10-20');self.assertEqual(len(self.provider.calls),count)
        self.provider.fail=503
        result=self.service.range('2026-10-18','2026-10-20',force=True)
        self.assertFalse(result['complete']);self.assertEqual(len(result['events']),2)
        view=calendar_view.query([],calendar_store.Store(self.registry),'2026-10-18','2026-10-20',google_result=result)
        self.assertIsNone(view['days'][0]['event_count']);self.assertEqual(view['days'][0]['memo_count'],0)
        self.assertEqual(len(view['days'][0]['events']),2)

    def test_pagination_cycle_missing_cursor_malformed_and_revoked_fail_closed(self):
        self.ready()
        for page in ({'items':[],'nextPageToken':'same'},{'items':[]},{'items':[{}],'nextSyncToken':'x'}):
            self.provider.pages[('sync','')]=page;self.provider.pages[('sync','same')]=page
            self.assertFalse(self.service.sync()['complete']);self.assertIsNone(self.service.mirror('cal-A').get('cursor'))
        self.provider.fail=403
        self.assertFalse(self.service.range('2026-10-18','2026-10-20')['complete'])

    def test_client_path_within_project_rejected(self):
        p=self.registry.parent/'client.json';p.write_text((self.data/'google-client.json').read_text())
        with mock.patch.dict(os.environ,{'WSFLOW_GOOGLE_CLIENT_FILE':str(p)}):
            with self.assertRaises(ValueError):self.service.connect()

    def test_410_full_failure_keeps_generation_cursor_and_master_exception(self):
        self.ready()
        self.provider.pages[('sync','')]={'items':[event('master',recurrence=['RRULE:FREQ=DAILY']),
            {'id':'exception','status':'cancelled','recurringEventId':'master','originalStartTime':{'date':'2026-10-18'}}],
            'nextSyncToken':'old'}
        self.service.sync();old=self.service.mirror('cal-A')
        original=self.provider.__call__
        def expired(method,url,data=None,headers=None):
            if 'syncToken=' in url:raise google.GoogleError(410)
            return original(method,url,data,headers)
        self.service.transport=expired;self.provider.pages[('sync','')]={'items':[event('new')],'nextPageToken':'p2'}
        self.provider.pages[('sync','p2')]=503
        self.assertFalse(self.service.sync()['complete'])
        current=self.service.mirror('cal-A')
        for key in ('cursor','generation','items'):self.assertEqual(current[key],old[key])
        self.assertEqual(current['items']['exception']['status'],'cancelled')

    def test_calendar_list_middle_failure_does_not_replace_cached_list(self):
        self.ready();old=self.service.store.get_value(self.service._key(self.service.credentials.load(),'calendars'))
        original=self.provider.__call__
        def broken(method,url,data=None,headers=None):
            if '/calendarList' in url:
                if 'pageToken' in url:raise google.GoogleError(503)
                return {'items':[{'id':'new','accessRole':'owner'}],'nextPageToken':'next'}
            return original(method,url,data,headers)
        self.service.transport=broken
        with self.assertRaises(google.GoogleError):self.service.calendars()
        self.assertEqual(self.service.store.get_value(self.service._key(self.service.credentials.load(),'calendars')),old)

    def test_wrong_host_and_duplicate_state_do_not_consume_callback(self):
        start=self.service.connect();q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        callback=q['redirect_uri'][0]+'?state='+q['state'][0]+'&code=x'
        with self.assertRaises(urllib.error.HTTPError):
            open_url(urllib.request.Request(callback,headers={'Host':'evil.example'}))
        with self.assertRaises(urllib.error.HTTPError):open_url(callback+'&state='+q['state'][0])
        self.assertTrue(self.service.status()['pending']);self.assertFalse(self.provider.calls)
        with open_url(callback+'&iss=https%3A%2F%2Faccounts.google.com&hd=example.test') as response:
            self.assertEqual(response.status,200)

    def test_process_epoch_blocks_old_callback_after_other_instance_disconnect(self):
        start=self.service.connect();q=urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorization_url']).query)
        other=google.Service(self.registry,transport=self.provider);self.addCleanup(other.close);other.disconnect()
        with self.assertRaises(urllib.error.HTTPError):
            open_url(q['redirect_uri'][0]+'?state='+q['state'][0]+'&code=x')
        self.assertFalse(self.service.status()['connected']);self.assertFalse(any(c[1].endswith('/token') for c in self.provider.calls))

    def test_permission_loss_and_bad_range_do_not_become_zero(self):
        self.ready();self.service.sync();self.service.range('2026-10-18','2026-10-20')
        token=self.service.credentials.load();self.service.store.put({self.service._key(token,'calendars'):[]})
        self.assertFalse(self.service.range('2026-10-18','2026-10-20')['complete'])
        count=len(self.provider.calls)
        with self.assertRaises(ValueError):self.service.range('2026-10-32','2026-11-01')
        self.assertEqual(len(self.provider.calls),count)

    def test_http_guards_v1_readonly_source_filter_and_snapshot(self):
        self.ready();self.service.sync();self.registry.write_text(json.dumps({'roots':[],'projects':[]}))
        httpd=serve(self.registry,port=0,token='startup',google_transport=self.provider)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        self.addCleanup(httpd.server_close);self.addCleanup(httpd.shutdown)
        base='http://127.0.0.1:'+str(httpd.server_port)
        def get(path,headers=None):
            with open_url(urllib.request.Request(base+path,headers=headers or {'X-Auth-Token':'startup'})) as response:
                return json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as ctx:get('/api/v2/calendar/google/status',{'X-Auth-Token':'wrong'})
        self.assertEqual(ctx.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):get('/api/v2/calendar/google/status',{'X-Auth-Token':'startup','Origin':'https://evil.example'})
        params='?start=2026-10-18&end=2026-10-20&source=google'
        result=get('/api/v2/calendar/range'+params)
        self.assertTrue(result['coverage']['google_calendar']);self.assertEqual(result['days'][0]['event_count'],1)
        day=get('/api/v2/calendar/day'+params+'&date=2026-10-18&snapshot_id='+result['snapshot_id'])
        self.assertEqual(day['day'],result['days'][0]);self.assertTrue(day['day']['events'][0]['readonly'])
        self.assertFalse(get('/api/v1/calendar/range?start=2026-10-18&end=2026-10-20')['coverage']['google_calendar'])
        with self.assertRaises(ValueError):
            calendar_store.Store(self.registry).preview({'kind':'event','action':'delete',
                'id':result['days'][0]['events'][0]['id'],'expected_revision':1},[])
        self.provider.fail=503;self.service.sync()
        result=get('/api/v2/calendar/range'+params)
        self.assertIsNone(result['days'][0]['event_count']);self.assertEqual(result['days'][0]['memo_count'],0)

    def test_invalid_google_storage_does_not_block_existing_server(self):
        with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.registry.parent/'bad')}):
            httpd=serve(self.registry,port=0,token='startup');self.addCleanup(httpd.server_close)
            self.assertIsNone(httpd.google_calendar)

    def test_revoke_failure_still_removes_credentials_and_preserves_mirror(self):
        self.ready();self.service.sync();before=self.service.store.path.read_bytes()
        original=self.provider.__call__
        def offline(method,url,data=None,headers=None):
            if url==google.REVOKE_URL:raise google.GoogleError(503)
            return original(method,url,data,headers)
        self.service.transport=offline;result=self.service.disconnect()
        self.assertFalse(result['revoked']);self.assertFalse(self.service.credentials.path.exists())
        self.assertEqual(self.service.store.get_value(google.operations.encoded(['account-A','mirror','cal-A']))['cursor'],'cursor-1')

    def test_range_middle_page_failure_and_changed_profile_never_reuse_wrong_cache(self):
        self.ready();self.service.sync();self.service.range('2026-10-18','2026-10-20')
        self.provider.pages[('range','')]={'items':[event('partial')],'nextPageToken':'bad'}
        self.provider.pages[('range','bad')]=503
        result=self.service.range('2026-10-18','2026-10-20',force=True)
        self.assertFalse(result['complete']);self.assertEqual([e['remote_id'] for e in result['events']],['one'])
        other=self.service.range('2026-10-20','2026-10-21')
        self.assertFalse(other['complete']);self.assertEqual(other['events'],[])

    def test_changed_collection_profile_requires_new_full_generation(self):
        self.ready();self.service.sync();old=self.service.mirror('cal-A')
        token=self.service.credentials.load()
        self.service.store.put({self.service._key(token,'mirror','cal-A'):{**old,'profile':{'singleEvents':'true'}}})
        self.provider.pages[('sync','')]={'items':[event('new')],'nextSyncToken':'new-profile'}
        self.service.sync();current=self.service.mirror('cal-A')
        call=[c for c in self.provider.calls if c[1].endswith('/events')][-1]
        self.assertNotIn('syncToken',call[2]);self.assertNotEqual(current['generation'],old['generation'])
        self.assertEqual(set(current['items']),{'new'})

    def test_production_transport_has_fixed_hosts_no_redirect_and_redacted_errors(self):
        with mock.patch.object(google,'build_opener') as opener:
            with self.assertRaises(ValueError):google.request('GET','https://evil.example/collect')
            opener.assert_not_called()
        self.assertIsNone(google.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))
        with mock.patch.object(google,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.HTTPError(google.TOKEN_URL,400,'SENSITIVE-BODY',{},None)
            with self.assertRaises(google.GoogleError) as ctx:google.request('POST',google.TOKEN_URL,{'refresh_token':'SENSITIVE'})
        self.assertNotIn('SENSITIVE',str(ctx.exception))

    def test_sync_refresh_failure_marks_mirror_incomplete_without_losing_state(self):
        self.ready();self.service.sync()
        before=copy.deepcopy(self.service.mirror('cal-A'))
        token=self.service.credentials.load();token['expires_at']=0;self.service.credentials.save(token)
        before_b={**copy.deepcopy(before),'items':{'other':event('other')},'cursor':'cal-B-cursor',
                  'generation':'cal-B-generation','range_epoch':'cal-B-range'}
        self.service.store.put({self.service._key(token,'selected'):['cal-A','cal-B'],
                                self.service._key(token,'mirror','cal-B'):before_b})
        credential_bytes=self.service.credentials.path.read_bytes()
        other_key=google.operations.encoded(['other-account','mirror','cal-A'])
        other={'complete':True,'sentinel':'unchanged'}
        unselected_key=self.service._key(token,'mirror','cal-C')
        self.service.store.put({other_key:other,unselected_key:other})
        with mock.patch.object(self.service,'after_sync') as after_sync:
            for status in (400,401,403,502):
                with self.subTest(status=status):
                    key=self.service._key(token,'mirror','cal-A')
                    self.service.store.put({key:before,self.service._key(token,'mirror','cal-B'):before_b})
                    calls=[]
                    def denied(method,url,data=None,headers=None):
                        calls.append((method,url))
                        raise google.GoogleError(status)
                    self.service.transport=denied
                    with self.assertRaises(google.GoogleError) as ctx:self.service.sync()
                    self.assertEqual(ctx.exception.status,status)
                    after=self.service.mirror('cal-A')
                    self.assertFalse(after['complete']);self.assertTrue(after['error'])
                    self.assertEqual({k:v for k,v in after.items() if k not in ('complete','error')},
                                     {k:v for k,v in before.items() if k not in ('complete','error')})
                    self.assertEqual(calls,[('POST',google.TOKEN_URL)])
                    self.assertEqual(self.service.credentials.path.read_bytes(),credential_bytes)
                    self.assertEqual(self.service.store.get_value(other_key),other)
                    self.assertEqual(self.service.store.get_value(unselected_key),other)
                    state=self.service.status()
                    self.assertFalse(state['complete']);self.assertEqual(state['state'],'needs_sync')
                    self.assertEqual(state['last_sync'],before['last_sync'])
                    other_mirror=self.service.mirror('cal-B')
                    self.assertFalse(other_mirror['complete']);self.assertTrue(other_mirror['error'])
                    self.assertEqual({k:v for k,v in other_mirror.items() if k not in ('complete','error')},
                                     {k:v for k,v in before_b.items() if k not in ('complete','error')})
                    after_sync.assert_not_called()

    def test_expired_token_refresh_failure_uses_cache_as_incomplete(self):
        self.ready();self.service.sync();self.service.range('2026-10-18','2026-10-20')
        token=self.service.credentials.load();token['expires_at']=0;self.service.credentials.save(token)
        original=self.provider.__call__
        def revoked(method,url,data=None,headers=None):
            if url==google.TOKEN_URL:raise google.GoogleError(400)
            return original(method,url,data,headers)
        self.service.transport=revoked
        result=self.service.range('2026-10-18','2026-10-20');self.assertFalse(result['complete']);self.assertEqual(len(result['events']),1)

    def test_concurrent_instances_serialize_refresh_and_cursor_writes(self):
        import concurrent.futures
        self.ready();token=self.service.credentials.load();token['expires_at']=0;self.service.credentials.save(token)
        other=google.Service(self.registry,transport=self.provider);self.addCleanup(other.close)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            values=list(pool.map(lambda s:s.sync(),[self.service,other]))
        self.assertTrue(all(v['complete'] for v in values))
        refreshes=[c for c in self.provider.calls if c[3] and c[3].get('grant_type')=='refresh_token']
        self.assertEqual(len(refreshes),1)

    def test_app_background_refresh_due_and_shutdown(self):
        self.assertIsNone(self.service.refresh_if_due());self.ready()
        self.assertTrue(self.service.refresh_if_due()['complete'])
        count=len(self.provider.calls);self.assertIsNone(self.service.refresh_if_due());self.assertEqual(len(self.provider.calls),count)
        token=self.service.credentials.load();mirror=self.service.mirror('cal-A')
        self.service.store.put({self.service._key(token,'mirror','cal-A'):{**mirror,'last_sync':'2026-01-01T00:00:00+00:00'}})
        self.assertTrue(self.service.refresh_if_due()['complete']);self.service.start_refresh();self.service.close()
        self.assertTrue(self.service.refresh_stop.is_set())

    def test_sync_commit_failure_rolls_back_cursor_and_records(self):
        self.ready();self.service.sync();before=self.service.mirror('cal-A')
        self.provider.pages[('sync','')]={'items':[event('new')],'nextSyncToken':'new'}
        with self.service.store.connection(True) as db:
            db.execute("CREATE TRIGGER reject_google_commit BEFORE UPDATE ON google_values WHEN old.key LIKE '%mirror%' BEGIN SELECT RAISE(ABORT,'injected commit failure'); END;")
        with self.assertRaises(sqlite3.Error):self.service.sync()
        self.assertEqual(self.service.mirror('cal-A'),before)


if __name__=='__main__':unittest.main()
