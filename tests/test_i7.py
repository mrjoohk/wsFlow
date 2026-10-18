"""I7 contracts: real local Git/files/vendor; synthetic Jira at transport boundary."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import subprocess
import concurrent.futures
import unittest
import threading
import urllib.request
import urllib.error
from urllib.parse import parse_qs,urlsplit
from unittest import mock

from helpers import git, make_bare, make_repo
from wsflow import auth, calendar_query, fileio, gitops, jiraapi, newtask, operations, wfhome, writeplans
from wsflow import workflow

CREDS={'email':'fixture@example.invalid','api_token':'synthetic','source':'synthetic'}
BASE='https://example.atlassian.net'
WHEN=datetime.fromisoformat('2026-10-18T10:30:00+09:00')
STATE={'schema_version':4,'project_key':'XXX','watermark':'2026-10-01T00:00:00+09:00',
       'roots':{},'epic_keys':['XXX-10'],'csc_field':'components','csc_value':'CSC',
       'title_include':[],'title_exclude':[],'sprint_mode':'active',
       'issue_type_map':{'site_url':BASE,'task_types':['Task']}}


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(value if isinstance(value,bytes) else value.encode('utf-8'))


def issue(key='XXX-11',title='work',done=False,assignee='me'):
    return {'id':key.split('-')[1],'key':key,'fields':{'summary':title,'assignee':{'accountId':assignee},
            'project':{'key':'XXX'},'issuetype':{'name':'Task','subtask':False,'hierarchyLevel':0},
            'status':{'name':'Done' if done else 'Doing','statusCategory':{'key':'done' if done else 'indeterminate'}},
            'parent':{'key':'XXX-10','fields':{'summary':'Epic'}},'components':[{'name':'CSC'}],
            'updated':'2026-10-18T09:00:00+09:00','priority':{'name':'Medium'},'labels':[],
            'duedate':None,'comment':{'comments':[]}},'changelog':{'histories':[]}}


class Provider:
    def __init__(self):
        self.calls=[];self.open=[issue()];self.changes=[issue()];self.titles=[issue(title='--009-- done',done=True,assignee='other')]
        self.created=issue('XXX-20','created');self.truncated=False;self.visible=True

    def __call__(self,url,headers,payload,method):
        body=json.loads(payload) if payload else None;self.calls.append((url,body,method))
        if url.endswith('/myself'):return {'accountId':'me','timeZone':'Asia/Seoul'}
        if '/issue/XXX-10?' in url:return {'key':'XXX-10','fields':{'summary':'Epic','issuetype':{'hierarchyLevel':1}}}
        if '/search/jql' in url:
            jql=body['jql']
            rows=self.titles if 'parent = ' in jql and 'currentUser' not in jql else [] if 'IS EMPTY' in jql else self.changes if 'updated >=' in jql else self.open
            if 'key = ' in jql:rows=[self.created] if self.visible else []
            rows=copy.deepcopy(rows)
            for row in rows:row['fields']={k:v for k,v in row['fields'].items() if k in body['fields']}
            return {'issues':rows,'isLast':not self.truncated,**({'nextPageToken':'again'} if self.truncated else {})}
        if '/issue/XXX-20?' in url:
            row=copy.deepcopy(self.created);requested=parse_qs(urlsplit(url).query)['fields'][0].split(',')
            row['fields']={k:v for k,v in row['fields'].items() if k in requested};return row
        if url.endswith('/issue') and method=='POST':
            self.created['fields'].update(body['fields']);return {'key':'XXX-20','id':'20'}
        raise AssertionError((url,body,method))


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='wsflow I7 ');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.home=self.base/'work';self.home.mkdir()
        patch=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata')});patch.start();self.addCleanup(patch.stop)
        self.state=copy.deepcopy(STATE);self.save_state();self.provider=Provider()
        self.project={'id':'p','path':str(self.home),'alias':'project'}

    def save_state(self):put(self.home/'.workflow/state.json',json.dumps(self.state))
    def run_batch(self,**kw):return workflow.run(self.home,CREDS,send=self.provider,clock=lambda:WHEN,**kw)


class BatchTests(Home):
    def test_unsupported_business_year_and_vendor_timeout_keep_watermark(self):
        original=wfhome.state_path(self.home).read_bytes()
        result=workflow.run(self.home,CREDS,send=self.provider,clock=lambda:datetime.fromisoformat('2100-01-01T00:00:00+09:00'))
        self.assertEqual(result['status'],'failed');self.assertIn('2000',result['error']);self.assertEqual(wfhome.state_path(self.home).read_bytes(),original)
        with mock.patch.object(workflow,'_command',side_effect=subprocess.TimeoutExpired('aggregate',60)):
            self.assertEqual(self.run_batch()['status'],'failed')
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),original)

    def test_duplicate_search_rows_are_incomplete_unique_and_never_commit(self):
        real=self.provider
        def duplicated(url,*args):
            result=real(url,*args)
            if '/search/jql' in url:result['issues']*=2
            return result
        with mock.patch.object(self,'provider',duplicated):result=self.run_batch()
        self.assertEqual(result['status'],'failed');self.assertEqual(wfhome.read_state(self.home)['watermark'],STATE['watermark'])
        raw=json.loads(next(Path(result['run_dir']).glob('work/.workflow/raw/*_open.json')).read_bytes())
        self.assertEqual(len(raw['issues']),2);self.assertIn('duplicate_issue_key',raw['_provenance']['incomplete_reasons'])
        result=jiraapi.search(CREDS,BASE,'project=XXX',[],send=duplicated)
        self.assertEqual(len(result['issues']),1);self.assertTrue(result['_truncated'])

    def test_malformed_classification_fields_preserve_watermark_and_null_parent_is_explicit(self):
        for field,value in [('summary',None),('status',None),('status',{}),('issuetype',{}),('updated',None),('assignee','MISSING'),('parent','MISSING')]:
            with self.subTest(field=field,value=value):
                self.provider.open=[issue()]
                if value=='MISSING':self.provider.open[0]['fields'].pop(field)
                else:self.provider.open[0]['fields'][field]=value
                result=self.run_batch();self.assertEqual(result['status'],'failed',result)
                self.assertEqual(wfhome.read_state(self.home)['watermark'],STATE['watermark'])
        self.provider.open=[issue()];self.provider.open[0]['fields']['parent']=None
        self.assertEqual(self.run_batch()['status'],'succeeded')
        self.state['epic_keys']=[];self.save_state();self.provider.open=[issue()];self.provider.open[0]['fields'].pop('parent')
        self.assertEqual(self.run_batch()['status'],'succeeded')

    def test_corrupt_receipt_shapes_warn_without_hiding_committed_watermark(self):
        result=self.run_batch();self.assertEqual(result['status'],'succeeded',result)
        for value in ([],{'run_id':'bad','created_at':{}},{'run_id':5,'created_at':'x','status':'succeeded'}):
            put(self.home/'.workflow/batches/corrupt/receipt.json',json.dumps(value))
            status=workflow.status(self.home);self.assertEqual(status['watermark'],WHEN.isoformat());self.assertTrue(status['warnings'])
            self.assertEqual(status['latest']['run_id'],result['run_id'])

    def test_real_pipeline_preserves_memo_and_commits_capture_time_last(self):
        memo=wfhome.read_memo(self.home,'261018');wfhome.write_memo(self.home,'261018','\r\n- XXX-11: actual work\r\nPRIVATE\r\n',memo['sha'])
        before=wfhome.read_memo(self.home,'261018')['text'];result=self.run_batch()
        self.assertEqual(result['status'],'succeeded',result);state=wfhome.read_state(self.home)
        self.assertEqual(state['watermark'],WHEN.isoformat());self.assertEqual(state['last_batch']['run_id'],result['run_id'])
        self.assertEqual(wfhome.read_memo(self.home,'261018')['text'],before)
        self.assertEqual([s['stage'] for s in result['stages']],['prepare','collect','aggregate','render','checks','publish','commit'])
        self.assertTrue(Path(result['run_dir'],'checks.json').is_file())
        self.assertEqual(len(state['roots']),1);self.assertEqual(workflow.status(self.home)['latest']['status'],'succeeded')
        self.assertEqual([c[2] for c in self.provider.calls],['GET','POST','POST','POST'])
        records=calendar_query.query([self.project],'2026-10-18','2026-10-19')['days'][0]
        self.assertEqual(records['task_count'],1);self.assertEqual(records['tasks'][0]['records'][0]['text'],'actual work')

    def test_failure_at_every_stage_never_advances_watermark(self):
        original=wfhome.state_path(self.home).read_bytes()
        for phase in workflow.STAGES:
            with self.subTest(phase=phase):
                def fail(stage):
                    if stage==phase:raise OSError('injected '+phase)
                r=self.run_batch(checkpoint=fail);self.assertEqual(r['status'],'failed',r)
                self.assertEqual(wfhome.state_path(self.home).read_bytes(),original)
        self.assertEqual(self.run_batch()['status'],'succeeded')

    def test_truncated_or_failed_second_query_preserves_state_and_daily(self):
        memo=wfhome.read_memo(self.home,'261018');daily=Path(memo['path']).read_bytes();original=wfhome.state_path(self.home).read_bytes()
        self.provider.truncated=True
        with mock.patch.object(jiraapi,'MAX_PAGES',2):r=self.run_batch()
        self.assertEqual(r['status'],'failed');self.assertIn('완전',r['error'])
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),original);self.assertEqual(Path(memo['path']).read_bytes(),daily)
        self.provider.truncated=False;real=self.provider
        def partial(url,*args):
            if '/search/jql' in url and len(real.calls)>=2:raise jiraapi.JiraError('network')
            return real(url,*args)
        with mock.patch.object(self,'provider',partial):self.assertEqual(self.run_batch()['status'],'failed')
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),original)

    def test_invariant_rejection_keeps_watermark_and_user_original(self):
        self.provider.open=[issue('OTHER-1')];before=wfhome.state_path(self.home).read_bytes()
        result=self.run_batch();self.assertEqual(result['status'],'failed');self.assertEqual(result['failed_stage'],'checks')
        self.assertEqual(wfhome.state_path(self.home).read_bytes(),before)
        checks=json.loads(Path(result['run_dir'],'checks.json').read_text(encoding='utf-8'))
        self.assertTrue(any('INV-5' in v for v in checks['violations']))

    def test_external_memo_or_state_edit_is_preserved(self):
        memo=wfhome.read_memo(self.home,'261018');before=wfhome.state_path(self.home).read_bytes()
        def edit(stage):
            if stage=='publish':put(Path(memo['path']),Path(memo['path']).read_text(encoding='utf-8').replace('MEMO:START -->','MEMO:START -->\nEXTERNAL'))
        self.assertEqual(self.run_batch(checkpoint=edit)['status'],'failed')
        self.assertIn('EXTERNAL',Path(memo['path']).read_text(encoding='utf-8'));self.assertEqual(wfhome.state_path(self.home).read_bytes(),before)
        def edit_state(stage):
            if stage=='commit':
                self.state['custom_user_value']='external';self.save_state()
        self.assertEqual(self.run_batch(checkpoint=edit_state)['status'],'failed');self.assertEqual(wfhome.read_state(self.home)['custom_user_value'],'external')
        self.assertEqual(wfhome.read_state(self.home)['watermark'],STATE['watermark'])

    def test_crash_before_commit_is_interrupted_and_can_rerun(self):
        def stop(stage):
            if stage=='commit':raise KeyboardInterrupt('process interruption')
        with self.assertRaises(KeyboardInterrupt):
            result=self.run_batch(checkpoint=stop)
            self.assertEqual(result.get('failed_stage'),'commit',result)
        self.assertEqual(wfhome.read_state(self.home)['watermark'],STATE['watermark'])
        self.assertEqual(workflow.status(self.home)['latest']['status'],'running_or_interrupted')
        self.assertEqual(self.run_batch()['status'],'succeeded')

    def test_lost_commit_receipt_is_resolved_from_state_marker(self):
        real=fileio.atomic_write
        def lost(path,data,*args,**kw):
            real(path,data,*args,**kw)
            if Path(path)==wfhome.state_path(self.home) and b'last_batch' in data:raise OSError('response lost after durable replace')
        with mock.patch.object(fileio,'atomic_write',lost):result=self.run_batch()
        self.assertEqual(result['status'],'succeeded');self.assertEqual(workflow.status(self.home)['latest']['status'],'succeeded')

    def test_invalid_schema_mode_timezone_and_backwards_clock_stop_before_commit(self):
        before=copy.deepcopy(self.state)
        for changes in ({'schema_version':99},{'sprint_mode':'future'},{'watermark':'2099-01-01T00:00:00+09:00'}):
            with self.subTest(changes=changes):
                self.state={**before,**changes};self.save_state();raw=wfhome.state_path(self.home).read_bytes()
                self.assertEqual(self.run_batch()['status'],'failed');self.assertEqual(wfhome.state_path(self.home).read_bytes(),raw)
        self.state=before;self.save_state();original=self.provider
        def bad_zone(url,*args):return {'accountId':'me','timeZone':'missing/zone'} if url.endswith('/myself') else original(url,*args)
        with mock.patch.object(self,'provider',bad_zone):self.assertEqual(self.run_batch()['status'],'failed')

    def test_empty_success_is_complete_and_planned_tasks_are_not_performed_records(self):
        self.provider.open=[];self.provider.changes=[];r=self.run_batch();self.assertEqual(r['status'],'succeeded')
        self.assertEqual(calendar_query.query([self.project],'2026-10-18','2026-10-19')['days'][0]['task_count'],0)
        self.assertEqual(workflow.overview([self.project],'2026-10-18')['projects'][0]['signals']['new'],0)

    def test_calendar_cursor_and_unrelated_user_files_are_untouched(self):
        from wsflow import google_calendar
        mirror=google_calendar.MirrorStore(self.base/'registry/projects.json');key=operations.encoded(['account','mirror','cal'])
        mirror.put({key:{'cursor':'GOOGLE CURSOR PRIVATE','items':{},'complete':True}});original=mirror.path.read_bytes();put(self.home/'USER.txt','USER')
        self.assertEqual(self.run_batch()['status'],'succeeded');self.assertEqual(mirror.get_value(key)['cursor'],'GOOGLE CURSOR PRIVATE');self.assertEqual(mirror.path.read_bytes(),original)
        self.assertEqual((self.home/'USER.txt').read_bytes(),b'USER')

    def test_status_overview_get_are_readonly_and_use_same_date_source(self):
        m=wfhome.read_memo(self.home,'261018');wfhome.write_memo(self.home,'261018','- XXX-11: recorded\ncommon\n',m['sha'])
        self.run_batch();before={str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        view=workflow.overview([self.project,{**self.project,'id':'alias'}],'2026-10-18')
        self.assertEqual(view['calendar']['snapshot_id'],calendar_query.query([self.project,{**self.project,'id':'alias'}],'2026-10-18','2026-10-19')['snapshot_id'])
        self.assertEqual(view['projects'][0]['date_records']['task_count'],1);self.assertTrue(view['projects'][0]['complete'])
        workflow.status(self.home);self.assertEqual(before,{str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_config_or_aggregate_change_invalidates_previous_success_and_keeps_unknown(self):
        self.assertEqual(self.run_batch()['status'],'succeeded')
        self.state=wfhome.read_state(self.home);self.state['title_include']=['changed'];self.save_state()
        view=workflow.overview([self.project],'2026-10-18')['projects'][0]
        self.assertIsNone(view['signals']);self.assertFalse(view['complete'])
        self.state['title_include']=[];self.save_state();put(self.home/'.workflow/agg/261018.json','{}')
        view=workflow.overview([self.project],'2026-10-18')['projects'][0];self.assertFalse(view['complete']);self.assertIsNone(view['signals'])

    def test_unreadable_later_receipt_never_reuses_current_success_badge(self):
        result=self.run_batch();self.assertEqual(result['status'],'succeeded',result)
        put(self.home/'.workflow/batches/unreadable/receipt.json','{bad receipt')
        view=workflow.overview([self.project],'2026-10-18')['projects'][0]
        self.assertFalse(view['complete']);self.assertTrue(any('작업 기록' in w for w in view['warnings']))

    def test_malformed_legacy_state_never_hides_other_dashboard_projects(self):
        other=self.base/'other';put(other/'.workflow/state.json',json.dumps(STATE))
        projects=[self.project,{'id':'good','path':str(other),'alias':'good'}]
        for value in ([1,2],{**STATE,'roots':{'legacy':1}},{**STATE,'roots':{'legacy':{'children':['invalid']}}}):
            put(wfhome.state_path(self.home),json.dumps(value))
            result=workflow.overview(projects,'2026-10-18')
            self.assertEqual([p['id'] for p in result['projects']],['p','good']);self.assertFalse(result['projects'][0]['complete']);self.assertTrue(result['projects'][0]['warnings'])

    def test_previous_memo_with_no_changes_is_suspected_not_confirmed_missing_close(self):
        m=wfhome.read_memo(self.home,'261017');wfhome.write_memo(self.home,'261017','- XXX-11: yesterday work',m['sha'])
        self.provider.changes=[];result=self.run_batch();self.assertEqual(result['status'],'succeeded',result);r=workflow.overview([self.project],'2026-10-18')['projects'][0]
        self.assertEqual(r['missing_close'],'suspected');self.assertTrue(any('누락 의심' in w for w in r['warnings']))

    def test_vendor_mismatch_missing_fields_duplicate_markers_and_invalid_external_json_fail_closed(self):
        with mock.patch.object(workflow,'vendor_status',return_value={'identical':False}):
            self.assertEqual(self.run_batch()['status'],'failed')
        self.provider.open[0]['fields'].pop('status');self.assertEqual(self.run_batch()['failed_stage'],'collect')
        self.provider.open=[issue()];m=wfhome.read_memo(self.home,'261018');put(Path(m['path']),wfhome.MEMO_START*2+wfhome.MEMO_END)
        self.assertEqual(self.run_batch()['failed_stage'],'prepare');put(Path(m['path']),wfhome.SKELETON.format(ymd='261018'))
        def corrupt(stage):
            if stage=='commit':put(wfhome.state_path(self.home),'{external invalid JSON')
        self.assertEqual(self.run_batch(checkpoint=corrupt)['status'],'failed');self.assertEqual(wfhome.state_path(self.home).read_bytes(),b'{external invalid JSON')

    def test_reinit_preserves_batch_marker_user_metadata_and_watermark(self):
        from wsflow import wfinit
        self.run_batch();old=wfhome.read_state(self.home);old['user_metadata']={'x':1};put(wfhome.state_path(self.home),json.dumps(old))
        wfinit.write_state(self.home,wfinit.build_state(old,project_key='XXX'))
        current=wfhome.read_state(self.home)
        for key in ('roots','watermark','last_success','last_batch','user_metadata'):self.assertEqual(current[key],old[key])

    def test_transient_windows_receipt_sharing_failure_has_a_bounded_retry(self):
        real=fileio.atomic_write;calls=[]
        def transient(path,data,*args,**kw):
            calls.append(path)
            if len(calls)<3:raise PermissionError('sharing violation')
            return real(path,data,*args,**kw)
        path=self.base/'receipt.json'
        with mock.patch.object(fileio,'atomic_write',transient):workflow._write(path,{'ok':True})
        self.assertEqual(len(calls),3);self.assertEqual(json.loads(path.read_bytes()),{'ok':True})
        calls.clear()
        with mock.patch.object(fileio,'atomic_write',side_effect=PermissionError('persistent')) as write:
            with self.assertRaises(PermissionError):workflow._write(path,{'ok':False})
        self.assertEqual(write.call_count,5);self.assertEqual(json.loads(path.read_bytes()),{'ok':True})

    def test_jira_account_timezone_controls_day_and_converts_watermark_query(self):
        self.state['watermark']='2026-10-17T21:00:00+00:00';self.save_state();original=self.provider
        def account(url,*args):return {'accountId':'me','timeZone':'America/New_York'} if url.endswith('/myself') else original(url,*args)
        with mock.patch.object(self,'provider',account):result=self.run_batch()
        self.assertEqual(result['status'],'succeeded',result);self.assertEqual(result['date'],'2026-10-17')
        self.assertEqual(wfhome.read_state(self.home)['watermark'],'2026-10-17T21:30:00-04:00')
        change=next(body['jql'] for url,body,_ in original.calls if body and 'updated >=' in body.get('jql',''))
        self.assertIn('2026-10-17 16:58',change)

    def test_concurrent_batches_serialize_without_original_memo_loss(self):
        memo=wfhome.read_memo(self.home,'261018');wfhome.write_memo(self.home,'261018','THREAD PRIVATE',memo['sha']);original=wfhome.read_memo(self.home,'261018')['text']
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.run_batch(),range(2)))
        self.assertEqual([r['status'] for r in results],['succeeded','succeeded'],results)
        self.assertNotEqual(results[0]['run_id'],results[1]['run_id']);self.assertEqual(wfhome.read_memo(self.home,'261018')['text'],original)
        self.assertIn(wfhome.read_state(self.home)['last_batch']['run_id'],[r['run_id'] for r in results])


class ReadCreateTests(Home):
    def test_no_csc_uses_explicit_epic_outer_scope_and_no_title_jql(self):
        state={**self.state,'csc_value':'','epic_keys':['XXX-10','XXX-20'],'title_include':['PRIVATE FILTER']}
        queries=jiraapi.build_queries(state)
        self.assertEqual(len(queries),2)
        for query in queries:
            self.assertIn('parent in (XXX-10,XXX-20)',query['jql']);self.assertNotIn('PRIVATE FILTER',query['jql'])

    def test_non_epic_missing_type_and_wrong_parent_key_never_issue_a_creation_context(self):
        for key,kind in [('XXX-10',{'hierarchyLevel':0}),('XXX-10',{'hierarchyLevel':-1,'subtask':True}),('XXX-10',{'hierarchyLevel':True}),('XXX-10',{}),('XXX-999',{'hierarchyLevel':1})]:
            def bad_parent(url,*args):
                return {'key':key,'fields':{'summary':'wrong','issuetype':kind}} if '/issue/XXX-10?' in url else self.provider(url,*args)
            with self.subTest(key=key,kind=kind),self.assertRaises(jiraapi.JiraError):
                jiraapi.creation_context(self.state,CREDS,'XXX-10',send=bad_parent)

    def test_foreign_csc_prefixes_share_the_epic_sequence(self):
        self.state['title_prefix']='[AAA]';self.save_state()
        context={'account_id':'me','epic_key':'XXX-10','epic_name':'Epic','complete':True,
                 'epic_summaries':['[BBB] --025-- done other scope','[AAA] [긴급] --027-- hidden']}
        p=writeplans.preview_new(self.project,self.home,self.state,{'summary':'new'},creation=context)
        self.assertEqual(p['title'],'[AAA] --028-- new')

    def test_unrecognized_existing_sequence_format_is_an_explicit_support_error(self):
        for title in ('other arbitrary prefix --025-- task','--1000-- beyond supported range'):
            context={'account_id':'me','epic_key':'XXX-10','epic_name':'Epic','complete':True,'epic_summaries':[title]}
            with self.subTest(title=title),self.assertRaises(operations.StateConflict):
                writeplans.preview_new(self.project,self.home,self.state,{'summary':'new'},creation=context)

    def test_terminal_marker_types_and_contradictions_are_rejected_but_token_absence_is_supported(self):
        for marker in ({'isLast':None},{'isLast':'false'},{'isLast':0},{'isLast':True,'nextPageToken':'more'}):
            with self.subTest(marker=marker),self.assertRaises(jiraapi.JiraError):
                jiraapi.search(CREDS,BASE,'project=XXX',[],send=lambda *args:{'issues':[],**marker})
        self.assertFalse(jiraapi.search(CREDS,BASE,'project=XXX',[],send=lambda *args:{'issues':[]})['_truncated'])

    def test_visibility_requires_assignee_in_the_requested_response_fields(self):
        self.provider.created['fields']['assignee']={'accountId':'me'}
        self.assertEqual(jiraapi.visibility(self.state,CREDS,'XXX-20','me',self.provider)['state'],'visible')
        with mock.patch.object(jiraapi,'FIELDS_BASE',[f for f in jiraapi.FIELDS_BASE if f!='assignee']):
            self.assertNotEqual(jiraapi.visibility(self.state,CREDS,'XXX-20','me',self.provider)['state'],'visible')
        self.provider.created['fields']['assignee']={'accountId':'someone-else'}
        self.assertEqual(jiraapi.visibility(self.state,CREDS,'XXX-20','me',self.provider)['state'],'not_visible')
    def test_live_read_exposes_truncation_and_does_not_disguise_zero(self):
        self.provider.truncated=True
        with mock.patch.object(jiraapi,'MAX_PAGES',2):r=wfhome.live_tasks(self.state,CREDS,self.provider)
        self.assertFalse(r['complete']);self.assertTrue(r['warnings']);self.assertTrue(any(q['truncated'] for q in r['queries']))
        self.assertEqual(wfhome.read_state(self.home)['watermark'],STATE['watermark'])

    def test_epic_title_context_is_complete_all_assignees_and_statuses(self):
        c=jiraapi.creation_context(self.state,CREDS,'XXX-10',send=self.provider)
        self.assertEqual(c['account_id'],'me');self.assertEqual(c['epic_summaries'],['--009-- done'])
        jql=next(body['jql'] for url,body,_ in self.provider.calls if body and '/search/jql' in url)
        for forbidden in ('currentUser','statusCategory','CSC','summary'):self.assertNotIn(forbidden,jql)
        self.provider.truncated=True
        with mock.patch.object(jiraapi,'MAX_PAGES',1),self.assertRaises(jiraapi.JiraError):jiraapi.creation_context(self.state,CREDS,'XXX-10',send=self.provider)

    def test_new_plan_assigns_self_and_rechecks_epic_sequence_before_write(self):
        with mock.patch.object(jiraapi,'_send',self.provider),mock.patch.object(newtask.jirawrite,'_send',self.provider):
            context=jiraapi.creation_context(self.state,CREDS,'XXX-10')
            p=writeplans.preview_new(self.project,self.home,self.state,{'summary':'new task','epic_key':'XXX-10'},creation=context)
            self.assertEqual(p['payload']['fields']['assignee'],{'accountId':'me'});self.assertIn('--010--',p['title'])
            self.provider.titles.append(issue('XXX-19','--010-- raced'))
            with self.assertRaises(operations.StateConflict):writeplans.apply_new(self.project,self.home,p['plan_id'],p['operation_id'],CREDS,[])
            self.assertFalse(any(m=='POST' and u.endswith('/issue') for u,_,m in self.provider.calls))

    def test_pending_local_plans_reserve_unique_sequence_and_uncertain_keeps_reservation(self):
        with mock.patch.object(jiraapi,'_send',self.provider):
            c=jiraapi.creation_context(self.state,CREDS,'XXX-10')
            a=writeplans.preview_new(self.project,self.home,self.state,{'summary':'a','epic_key':'XXX-10'},creation=c)
            b=writeplans.preview_new(self.project,self.home,self.state,{'summary':'b','epic_key':'XXX-10'},creation=c)
            self.assertIn('--010--',a['title']);self.assertIn('--011--',b['title'])

    def test_created_visibility_is_explicit_and_requery_failure_never_recreates(self):
        with mock.patch.object(jiraapi,'_send',self.provider),mock.patch.object(newtask.jirawrite,'_send',self.provider):
            c=jiraapi.creation_context(self.state,CREDS,'XXX-10')
            p=writeplans.preview_new(self.project,self.home,self.state,{'summary':'a','epic_key':'XXX-10'},creation=c)
            self.provider.visible=False
            r=writeplans.apply_new(self.project,self.home,p['plan_id'],p['operation_id'],CREDS,[])
            self.assertEqual(r['visibility']['state'],'not_visible');self.assertTrue(r['visibility']['warnings'])
            again=writeplans.apply_new(self.project,self.home,p['plan_id'],p['operation_id'],{},[])
            self.assertEqual(again['key'],r['key']);self.assertEqual(sum(m=='POST' and u.endswith('/issue') for u,_,m in self.provider.calls),1)

    def test_credentials_never_fall_back_to_a_different_named_site(self):
        path=self.base/'credentials.json';put(path,json.dumps({'sites':{'https://one.atlassian.net':{'email':'one','api_token':'secret'}}}))
        with mock.patch.dict(os.environ,{'JIRA_EMAIL':'','JIRA_API_TOKEN':''}),mock.patch.object(auth,'cred_path',return_value=path):
            with self.assertRaises(auth.CredentialError):auth.load('https://two.atlassian.net')
            self.assertEqual(auth.load('https://one.atlassian.net')['email'],'one')

    def test_pagination_last_page_false_without_token_and_repeated_token_are_incomplete(self):
        for response in ({'issues':[],'isLast':False},{'issues':[],'nextPageToken':'repeat'}):
            with self.subTest(response=response):
                result=jiraapi.search(CREDS,BASE,'project=XXX',[],send=lambda *args:response)
                self.assertTrue(result['_truncated']);self.assertLessEqual(result['_pages'],2)

    def test_missing_scope_fields_and_unsupported_mode_are_never_complete_live_reads(self):
        self.provider.open[0]['fields'].pop('status')
        self.assertFalse(wfhome.live_tasks(self.state,CREDS,self.provider)['complete'])
        self.provider.open=[issue()];self.state['sprint_mode']='future'
        self.assertFalse(wfhome.live_tasks(self.state,CREDS,self.provider)['complete'])

    def test_selfcheck_cannot_mark_a_truncated_search_healthy(self):
        from wsflow import selfcheck
        self.provider.truncated=True;messages=[]
        with mock.patch.object(auth,'load',return_value=CREDS),mock.patch.object(jiraapi,'_send',self.provider),mock.patch.object(jiraapi,'MAX_PAGES',1):
            self.assertEqual(selfcheck.run(self.home,out=messages.append),1)
        self.assertTrue(any('불완전' in m for m in messages))

    def test_prefix_sequence_includes_hidden_done_and_self_assignee_overrides_site_default(self):
        self.state['title_prefix']='[SCH]';self.save_state();self.provider.titles=[issue(title='[SCH] --025-- hidden',done=True,assignee='other')]
        with mock.patch.object(jiraapi,'_send',self.provider),mock.patch.object(newtask.jirawrite,'_send',self.provider):
            c=jiraapi.creation_context(self.state,CREDS,'XXX-10')
            p=writeplans.preview_new(self.project,self.home,self.state,{'summary':'new','epic_key':'XXX-10'},creation=c)
            self.assertEqual(p['title'],'[SCH] --026-- new');self.assertEqual(p['payload']['fields']['assignee']['accountId'],'me')
            r=writeplans.apply_new(self.project,self.home,p['plan_id'],p['operation_id'],CREDS,[]);self.assertEqual(r['visibility']['state'],'visible')
            sent=next(body['fields'] for url,body,method in self.provider.calls if method=='POST' and url.endswith('/issue'))
            self.assertEqual(sent['assignee'],{'accountId':'me'})

    def test_current_account_change_or_incomplete_scope_blocks_creation(self):
        c=jiraapi.creation_context(self.state,CREDS,'XXX-10',send=self.provider)
        with self.assertRaises(operations.StateConflict):writeplans.preview_new(self.project,self.home,self.state,{'summary':'new'},{'complete':False,'known_keys':[],'tasks':[]},creation=c)
        p=writeplans.preview_new(self.project,self.home,self.state,{'summary':'new'},creation=c)
        with mock.patch.object(jiraapi,'creation_context',return_value={**c,'account_id':'other'}),self.assertRaises(operations.StateConflict):
            writeplans.apply_new(self.project,self.home,p['plan_id'],p['operation_id'],CREDS,[])


class HttpTests(Home):
    def setUp(self):
        super().setUp();from wsflow.server import serve
        registry=self.base/'registry/projects.json';put(registry,json.dumps({'roots':[],'projects':[self.project]}))
        self.httpd=serve(registry,'127.0.0.1',0);self.addCleanup(self.httpd.server_close);self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever,daemon=True).start()
        for patch in (mock.patch.object(auth,'load',return_value=CREDS),mock.patch.object(jiraapi,'_send',self.provider),mock.patch.object(newtask.jirawrite,'_send',self.provider)):
            patch.start();self.addCleanup(patch.stop)

    def api(self,path,body=None,status=200,headers=None):
        req=urllib.request.Request(f'http://127.0.0.1:{self.httpd.server_address[1]}'+path,data=json.dumps(body).encode() if body is not None else None,
                                   headers=headers if headers is not None else {'X-Auth-Token':self.httpd.wsflow_token,'Content-Type':'application/json'})
        try:response=urllib.request.urlopen(req,timeout=15)
        except urllib.error.HTTPError as exc:response=exc
        with response:
            text=response.read().decode();self.assertEqual(response.status,status,text);return json.loads(text)

    def test_batch_routes_guard_inputs_status_and_readonly_overview(self):
        route='/api/projects/p/jira/batch/run';self.api(route,{},403,headers={})
        for body in ({'date':'2026-10-01'},{'path':'outside'},{'watermark':'2099-01-01'}):self.api(route,body,400)
        real=workflow.run
        with mock.patch.object(workflow,'run',lambda home,creds:real(home,creds,clock=lambda:WHEN)):
            result=self.api(route,{})
        self.assertEqual(result['status'],'succeeded');self.assertEqual(self.api('/api/projects/p/jira/batch')['latest']['run_id'],result['run_id'])
        before={str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertTrue(self.api('/api/v1/workflow/status?date=2026-10-18')['projects'][0]['complete'])
        self.assertIn('workflow',self.api('/api/v1/context/dashboard'));self.assertEqual(before,{str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_offline_preview_remains_available_but_cannot_create(self):
        self.state['epic_keys']=[];self.save_state()
        with mock.patch.object(auth,'load',side_effect=auth.CredentialError('offline')):
            p=self.api('/api/projects/p/jira/new/plan',{'summary':'offline'})
        self.assertFalse(p['ready_to_create']);self.api('/api/projects/p/jira/new/create',{'plan_id':p['plan_id'],'operation_id':p['operation_id']},409)
        self.assertFalse(any(method=='POST' and url.endswith('/issue') for url,_,method in self.provider.calls))

    def test_partial_read_invalidates_previous_trusted_scope(self):
        self.api('/api/projects/p/jira/tasks');self.provider.truncated=True
        with mock.patch.object(jiraapi,'MAX_PAGES',1):r=self.api('/api/projects/p/jira/tasks')
        self.assertFalse(r['complete']);self.api('/api/projects/p/jira/close/plan',{'ymd':'261018'},409)

    def test_git_routes_accept_no_custom_options(self):
        repo=make_repo(self.base,'git');project=self.api('/api/projects',{'path':str(repo)})
        for action in ('fetch','pull'):self.api('/api/projects/'+project['id']+'/'+action,{'force':True},400)


class GitSyncTests(Home):
    def setUp(self):
        super().setUp();self.remote=make_bare(self.base);self.a=make_repo(self.base,'A')
        git(self.a,'remote','add','origin',str(self.remote));git(self.a,'push','-u','origin','main');git(self.base,'clone',str(self.remote),str(self.base/'B'));self.b=self.base/'B'

    def remote_commit(self):
        put(self.b/'remote.txt','from B');git(self.b,'add','-A');git(self.b,'commit','-m','remote');git(self.b,'push')

    def test_real_fetch_ff_only_and_timestamp_survive_restart(self):
        self.remote_commit();self.assertEqual(gitops.status(self.a)['behind'],0)
        r=gitops.fetch(self.a);self.assertTrue(r['ok']);self.assertTrue(gitops.sync_info(self.a)['last_fetch_at']);self.assertEqual(gitops.status(self.a)['behind'],1)
        p=gitops.pull_ff(self.a);self.assertTrue(p['ok'],p);self.assertIn('--ff-only',p['command']);self.assertEqual((self.a/'remote.txt').read_bytes(),b'from B')

    def test_dirty_diverged_detached_and_missing_upstream_never_destroy_files(self):
        put(self.a/'user.txt','PRIVATE');before=git(self.a,'rev-parse','HEAD').stdout
        self.assertEqual(gitops.pull_ff(self.a)['reason'],'dirty');self.assertEqual((self.a/'user.txt').read_bytes(),b'PRIVATE')
        git(self.a,'add','-A');git(self.a,'commit','-m','local');self.remote_commit()
        self.assertEqual(gitops.pull_ff(self.a)['reason'],'non_ff');self.assertEqual(git(self.a,'rev-parse','HEAD').stdout,git(self.a,'rev-parse','main').stdout)
        self.assertEqual((self.a/'user.txt').read_bytes(),b'PRIVATE');git(self.a,'checkout','--detach')
        self.assertEqual(gitops.pull_ff(self.a)['reason'],'detached');git(self.a,'checkout','main');git(self.a,'branch','--unset-upstream')
        self.assertEqual(gitops.pull_ff(self.a)['reason'],'no_upstream')

    def test_failed_fetch_does_not_replace_last_successful_timestamp(self):
        self.assertTrue(gitops.fetch(self.a)['ok']);before=gitops.sync_info(self.a)['last_fetch_at']
        git(self.a,'remote','set-url','origin',str(self.base/'missing.git'));self.assertFalse(gitops.fetch(self.a)['ok'])
        self.assertEqual(gitops.sync_info(self.a)['last_fetch_at'],before)

    def test_actual_conflict_and_fetch_timeout_preserve_head_and_user_work(self):
        git(self.a,'checkout','-b','side');put(self.a/'f0.txt','side');git(self.a,'add','-A');git(self.a,'commit','-m','side')
        git(self.a,'checkout','main');put(self.a/'f0.txt','main');git(self.a,'add','-A');git(self.a,'commit','-m','main')
        with self.assertRaises(subprocess.CalledProcessError):git(self.a,'merge','side')
        before=(self.a/'f0.txt').read_bytes();head=git(self.a,'rev-parse','HEAD').stdout
        self.assertEqual(gitops.pull_ff(self.a)['reason'],'conflict');self.assertEqual((self.a/'f0.txt').read_bytes(),before);self.assertEqual(git(self.a,'rev-parse','HEAD').stdout,head)
        real=gitops.run_git
        def timeout(repo,*args,**kw):
            if args[0]=='fetch':raise gitops.GitError('timeout',124)
            return real(repo,*args,**kw)
        with mock.patch.object(gitops,'run_git',timeout):self.assertFalse(gitops.fetch(self.a)['ok'])
        self.assertIsNone(gitops.sync_info(self.a)['last_fetch_at']);self.assertEqual((self.a/'f0.txt').read_bytes(),before)

    def test_status_and_pipeline_subprocesses_do_not_inherit_server_input(self):
        real=subprocess.run;seen=[];timeouts=[]
        def capture(*args,**kw):
            seen.append(kw.get('stdin'))
            if any(str(v).endswith('aggregate.py') for v in args[0]):timeouts.append(kw.get('timeout'))
            return real(*args,**kw)
        with mock.patch.object(subprocess,'run',capture):
            gitops.status(self.a);self.assertEqual(self.run_batch()['status'],'succeeded');newtask.confirm_title(self.home,'new task')
        self.assertTrue(seen);self.assertTrue(all(v==subprocess.DEVNULL for v in seen));self.assertEqual(timeouts,[60])

    def test_fetch_without_remote_is_not_a_successful_network_observation(self):
        git(self.a,'remote','remove','origin');result=gitops.fetch(self.a)
        self.assertFalse(result['ok']);self.assertEqual(result['reason'],'no_remote');self.assertIsNone(gitops.sync_info(self.a)['last_fetch_at'])


if __name__=='__main__':unittest.main()
