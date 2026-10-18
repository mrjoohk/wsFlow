"""I1 acceptance. Temporary data and fake providers; never live Jira."""
import concurrent.futures
import json
import os
import tempfile
import threading
import subprocess
import sys
import multiprocessing
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest import mock

from wsflow import operations, wfhome, jirawrite, newtask
from test_jirawrite import Recorder, CREDS, BASE
from wsflow.server import serve
from wsflow import writeplans
from wsflow import fileio


STATE = {'schema_version':4, 'project_key':'XXX', 'watermark':'2026-10-01T00:00:00+09:00',
         'roots':{}, 'epic_keys':[], 'csc_value':None, 'title_include':[], 'title_exclude':[],
         'issue_type_map':{'site_url':BASE, 'task_types':['Task']}}


class I1Home(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.home=self.base/'work';self.home.mkdir()
        self.env=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'user-data')})
        self.env.start();self.addCleanup(self.env.stop)
        (self.home/'.workflow').mkdir()
        (self.home/'.workflow/state.json').write_text(json.dumps(STATE),encoding='utf-8')


class MemoSafetyTests(I1Home):
    def test_same_version_concurrent_saves_have_one_winner_and_keep_outer_text(self):
        m=wfhome.read_memo(self.home,'261017')
        barrier=threading.Barrier(2)
        def save(text):
            barrier.wait()
            try:return wfhome.write_memo(self.home,'261017',text,m['sha'])
            except wfhome.MemoConflict:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(save,['first','second']))
        self.assertEqual(results.count('conflict'),1)
        self.assertIn('# 261017',(self.home/'daily/261017.md').read_text(encoding='utf-8'))

    def test_backup_names_never_collide_and_failed_replace_keeps_original(self):
        m=wfhome.read_memo(self.home,'261017');backups=[]
        for text in ['a','b','c']:
            r=wfhome.write_memo(self.home,'261017',text,m['sha']);backups.append(r['backup'])
            m=wfhome.read_memo(self.home,'261017')
        self.assertEqual(len(set(backups)),3)
        before=(self.home/'daily/261017.md').read_bytes()
        with mock.patch('wsflow.fileio.os.replace',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):wfhome.write_memo(self.home,'261017','must not replace',m['sha'])
        self.assertEqual((self.home/'daily/261017.md').read_bytes(),before)

    def test_actual_dates_and_document_identity_are_checked(self):
        for value in ['../state','260230','20261017','000000']:
            with self.subTest(value=value),self.assertRaises(ValueError):wfhome.read_memo(self.home,value)
        a=wfhome.read_memo(self.home,'261017');b=wfhome.read_memo(self.home,'261018')
        self.assertNotEqual(a['document_id'],b['document_id'])
        with self.assertRaises(wfhome.MemoConflict):
            wfhome.write_memo(self.home,'261018','wrong day',b['sha'],document_id=a['document_id'])

    def test_external_edit_detected_before_replace_keeps_external_file(self):
        memo=wfhome.read_memo(self.home,'261017')
        original=fileio.atomic_write
        def outside_edit(path,data,before_replace=None):
            body=Path(path).read_text(encoding='utf-8')
            Path(path).write_text(body.replace(wfhome.MEMO_START,wfhome.MEMO_START+'\nexternal change'),encoding='utf-8')
            return original(path,data,before_replace)
        with mock.patch.object(fileio,'atomic_write',outside_edit):
            with self.assertRaises(wfhome.MemoConflict):wfhome.write_memo(self.home,'261017','UI edit',memo['sha'])
        self.assertIn('external change',(self.home/'daily/261017.md').read_text(encoding='utf-8'))


class WriteSafetyTests(I1Home):
    def test_uuid_separates_identical_new_approvals(self):
        payload={'text':'same intended new comment'}
        a=jirawrite.approve(self.home,'comment','XXX-1',payload)
        jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,Recorder())
        b=jirawrite.approve(self.home,'comment','XXX-1',payload)
        self.assertNotEqual(a['id'],b['id'])
        send=Recorder();jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send)
        self.assertEqual(len(send.calls),1)

    def test_concurrent_execution_of_one_operation_sends_once(self):
        payload={'text':'work'};a=jirawrite.approve(self.home,'comment','XXX-1',payload)
        send=Recorder();barrier=threading.Barrier(2)
        def run(_):
            barrier.wait()
            try:return jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
            except jirawrite.JiraError as e:return str(e)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:list(pool.map(run,range(2)))
        self.assertEqual(len(send.calls),1)

    def test_lost_reply_survives_restart_and_new_approval_cannot_bypass(self):
        payload={'text':'work'};a=jirawrite.approve(self.home,'comment','XXX-1',payload)
        calls=[]
        def lose(*args):calls.append(args);raise TimeoutError('response lost after remote success')
        with self.assertRaises(jirawrite.UncertainWrite):
            jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,lose,operation_id=a['id'])
        self.assertEqual(operations.Store(self.home).get(a['id'])['status'],'uncertain')
        with self.assertRaises(jirawrite.UncertainWrite):jirawrite.approve(self.home,'comment','XXX-1',payload)
        with self.assertRaises(jirawrite.UncertainWrite):
            jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,lose,operation_id=a['id'])
        self.assertEqual(len(calls),1)

    def test_result_log_failure_recovers_without_remote_resend(self):
        payload={'text':'work'};a=jirawrite.approve(self.home,'comment','XXX-1',payload);send=Recorder()
        original=jirawrite._append
        def append(path,record):
            if record['event']=='pushed':raise OSError('disk receipt failed')
            return original(path,record)
        with mock.patch.object(jirawrite,'_append',append):
            with self.assertRaises(jirawrite.UncertainWrite):
                jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
        jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
        self.assertEqual(len(send.calls),1)

    def test_result_commit_failure_retains_claim_and_never_resends(self):
        payload={'text':'work'};a=jirawrite.approve(self.home,'comment','XXX-1',payload);send=Recorder()
        with mock.patch.object(operations.Store,'finish',side_effect=OSError('DB unavailable after response')):
            with self.assertRaises(jirawrite.UncertainWrite):
                jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
        self.assertEqual(operations.Store(self.home).get(a['id'])['status'],'executing')
        with self.assertRaises(jirawrite.UncertainWrite):
            jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
        self.assertEqual(len(send.calls),1)
        self.assertEqual(send.calls[0]['body']['properties'][0]['value']['operation_id'],a['id'])

    def test_claim_and_audit_failure_rolls_back_together_and_plan_is_immutable(self):
        store=operations.Store(self.home)
        plan=store.save_plan({'kind':'test'},[{'kind':'comment','target':'XXX-1','payload':{'text':'work'}}])
        ident=plan['items'][0]['operation_id'];store.approve(ident)
        with mock.patch.object(operations.Store,'_event',side_effect=OSError('audit full')):
            with self.assertRaises(OSError):store.claim(ident)
        self.assertEqual(store.get(ident)['status'],'approved')
        import sqlite3
        for sql in ["UPDATE plans SET snapshot='{}'", "UPDATE operations SET intent='{}'",'DELETE FROM events']:
            with self.assertRaises(sqlite3.IntegrityError),store.connection(True) as db:db.execute(sql)
        store.claim(ident);self.assertEqual(store.get(ident)['status'],'executing')

    def test_create_receipt_has_vendor_approval_and_replay_does_not_duplicate_root(self):
        intent=newtask.plan(self.home,STATE,summary='new task')
        a=jirawrite.approve(self.home,intent['kind'],intent['target'],intent['payload']);send=Recorder()
        first=newtask.create(self.home,CREDS,BASE,intent,send,operation_id=a['id'])
        newtask.create(self.home,CREDS,BASE,intent,send,operation_id=a['id'])
        roots=wfhome.read_state(self.home)['roots']
        self.assertEqual(len(roots),1)
        root=next(iter(roots.values()))
        self.assertEqual(root['approval_id'],a['id']);self.assertTrue(root['approved_at'])
        self.assertEqual(root['jira_key'],first['key']);self.assertEqual(len(send.calls),1)

    def test_legacy_approved_only_record_is_not_blindly_reapproved(self):
        payload={'text':'old uncertain'}
        jirawrite._append(jirawrite.approvals_path(self.home), {'event':'approved','id':'A-legacy',
            'kind':'comment','target':'XXX-1','payload_hash':jirawrite.payload_hash('comment','XXX-1',payload),'approved_at':'2026-10-16T00:00:00+09:00'})
        with self.assertRaises(jirawrite.UncertainWrite):jirawrite.approve(self.home,'comment','XXX-1',payload)

    def test_interrupted_claim_never_resends_and_positive_reconciliation_is_required(self):
        payload={'text':'work'};a=jirawrite.approve(self.home,'comment','XXX-1',payload)
        code='from wsflow.operations import Store; import sys; Store(sys.argv[1]).claim(sys.argv[2]); sys.exit(23)'
        r=subprocess.run([sys.executable,'-c',code,str(self.home),a['id']],capture_output=True)
        self.assertEqual(r.returncode,23)
        send=Recorder()
        with self.assertRaises(jirawrite.UncertainWrite):
            jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
        self.assertEqual(send.calls,[])
        wrong=Recorder([{'id':'9','body':jirawrite.adf('different')}])
        with self.assertRaises(operations.StateConflict):jirawrite.reconcile(self.home,CREDS,BASE,a['id'],'9',wrong)
        same_body_wrong_op=Recorder([{'id':'9','body':jirawrite.adf('work')},{'value':{'operation_id':'another'}}])
        with self.assertRaises(operations.StateConflict):jirawrite.reconcile(self.home,CREDS,BASE,a['id'],'9',same_body_wrong_op)
        positive=Recorder([{'id':'9','body':jirawrite.adf('work')},
                           {'value':jirawrite.remote_receipt(operations.Store(self.home).get(a['id']))}])
        jirawrite.reconcile(self.home,CREDS,BASE,a['id'],'9',positive)
        jirawrite.execute(self.home,CREDS,BASE,'comment','XXX-1',payload,send,operation_id=a['id'])
        self.assertEqual(send.calls,[])
        self.assertEqual([c['method'] for c in positive.calls],['GET','GET'])

    def test_unknown_operation_database_schema_fails_without_modification(self):
        store=operations.Store(self.home);store.create({'kind':'comment'})
        import sqlite3
        db=sqlite3.connect(store.path)
        try:db.execute('PRAGMA user_version=99');db.commit()
        finally:db.close()
        before=store.path.read_bytes()
        with self.assertRaises(operations.StateConflict):store.list()
        with self.assertRaises(operations.StateConflict):store.create({'kind':'comment'})
        self.assertEqual(store.path.read_bytes(),before)


def claim_worker(home, ident, queue):
    try:operations.Store(home).claim(ident);queue.put('claimed')
    except operations.StateConflict:queue.put('blocked')


def lock_worker(home, queue):
    try:
        with fileio.locked(home,timeout=.15):queue.put('acquired')
    except fileio.LockBusy:queue.put('busy')


class ProcessClaimTests(I1Home):
    def test_actual_processes_have_one_durable_claim(self):
        payload={'text':'work'};a=jirawrite.approve(self.home,'comment','XXX-1',payload)
        ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
        workers=[ctx.Process(target=claim_worker,args=(str(self.home),a['id'],queue)) for _ in range(4)]
        for worker in workers:worker.start()
        results=[queue.get(timeout=20) for _ in workers]
        for worker in workers:worker.join(timeout=20);self.assertEqual(worker.exitcode,0)
        self.assertEqual(results.count('claimed'),1)
        queue.close();queue.join_thread()

    def test_os_lock_excludes_another_process(self):
        ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
        with fileio.locked(self.home):
            worker=ctx.Process(target=lock_worker,args=(str(self.home),queue));worker.start()
            self.assertEqual(queue.get(timeout=20),'busy')
            worker.join(timeout=20);self.assertEqual(worker.exitcode,0)
        queue.close();queue.join_thread()


class PlanHttpTests(I1Home):
    def setUp(self):
        super().setUp()
        self.project={'id':'p','path':str(self.home),'alias':'p','tags':[]}
        registry=self.base/'projects.json'
        registry.write_text(json.dumps({'roots':[],'projects':[self.project,{'id':'other','path':str(self.base),'wf_home':str(self.home)}]}),encoding='utf-8')
        self.httpd=serve(registry,'127.0.0.1',0)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.httpd.server_close);self.addCleanup(self.httpd.shutdown)
        self.view={'known_keys':['XXX-1'],'tasks':[{'key':'XXX-1','summary':'task','parent_key':None}],
                   'counts':{'tasks':1},'orphans':[]}
        for patcher in [mock.patch('wsflow.auth.load',return_value=CREDS),mock.patch('wsflow.wfhome.live_tasks',return_value=self.view)]:
            patcher.start();self.addCleanup(patcher.stop)
        self.send=Recorder();patcher=mock.patch.object(jirawrite,'_send',self.send)
        patcher.start();self.addCleanup(patcher.stop)
        # I7 adds read-only identity and post-create visibility checks at REST boundary.
        def jira_read(url,headers,payload,method):
            if url.endswith('/myself'):return {'accountId':'synthetic-me','timeZone':'Asia/Seoul'}
            if '/search/jql' in url:return {'issues':[],'isLast':True}
            return {'key':'XXX-9','fields':{}}
        patcher=mock.patch('wsflow.jiraapi._send',jira_read);patcher.start();self.addCleanup(patcher.stop)

    def api(self,method,tail,body=None,status=200,project='p'):
        url=f'http://127.0.0.1:{self.httpd.server_address[1]}/api/projects/{project}/jira/'+tail
        data=json.dumps(body).encode('utf-8') if body is not None else None
        request=urllib.request.Request(url,data=data,method=method,headers={'X-Auth-Token':self.httpd.wsflow_token,'Content-Type':'application/json'})
        try:r=urllib.request.urlopen(request,timeout=15)
        except urllib.error.HTTPError as e:r=e
        with r:
            raw=r.read().decode('utf-8');self.assertEqual(r.status,status,raw)
            return json.loads(raw)

    def memo(self,text='- XXX-1: work'):
        m=self.api('GET','memo?ymd=261017')
        return self.api('PUT','memo',{'ymd':'261017','sha':m['sha'],'text':text,'document_id':m['document_id']})

    def close_plan(self,text='- XXX-1: work'):
        self.memo(text);self.api('GET','tasks')
        return self.api('POST','close/plan',{'ymd':'261017','known_keys':['XXX-999']})

    def close_apply(self,plan,**extra):
        return {'plan_id':plan['plan_id'],'ymd':'261017','operation_ids':[i['operation_id'] for i in plan['items']],**extra}

    def test_raw_payload_and_client_known_keys_cannot_approve_writes(self):
        self.api('POST','new/create',{'intent':{'kind':'comment','target':'XXX-999','payload':{'text':'forged'}}},409)
        self.api('POST','close/apply',{'ymd':'261017','items':[{'kind':'comment','target':'XXX-999','payload':{'text':'forged'}}]},409)
        plan=self.close_plan('- XXX-1: allowed\n- XXX-999: forbidden')
        self.assertEqual([i['target'] for i in plan['items']],['XXX-1'])
        self.assertEqual([i['target'] for i in plan['unknown_keys']],['XXX-999'])
        self.api('POST','close/apply',self.close_apply(plan,items=[]),409)
        self.assertEqual(self.send.calls,[])
        self.assertFalse(jirawrite.approvals_path(self.home).exists())

    def test_stale_memo_wrong_date_wrong_project_and_foreign_operation_are_rejected(self):
        plan=self.close_plan()
        request=self.close_apply(plan)
        self.api('POST','close/apply',{**request,'ymd':'261018'},409)
        self.api('POST','close/apply',request,409,project='other')
        self.api('POST','close/apply',{**request,'operation_ids':['A-not-in-plan']},409)
        self.memo('- XXX-1: changed')
        self.api('POST','close/apply',request,409)
        self.assertEqual(self.send.calls,[])

    def test_lint_is_rechecked_and_explicitly_acknowledged_and_replay_is_cached(self):
        plan=self.close_plan('- XXX-1: changed rd/private.py')
        self.assertTrue(plan['items'][0]['lint'])
        self.api('POST','close/apply',self.close_apply(plan),409)
        self.assertFalse(jirawrite.approvals_path(self.home).exists())
        request=self.close_apply(plan,ack_lint=True)
        self.assertEqual(len(self.api('POST','close/apply',request)['done']),1)
        self.api('POST','close/apply',request)
        self.assertEqual(len(self.send.calls),1)

    def test_changed_scope_and_expired_plan_have_no_effect(self):
        plan=self.close_plan()
        self.view['known_keys']=[]
        self.api('POST','close/apply',self.close_apply(plan),409)
        self.view['known_keys']=['XXX-1']
        with mock.patch('wsflow.writeplans.time.time',return_value=plan['expires_at']+1):
            self.api('POST','close/apply',self.close_apply(plan),409)
        self.assertEqual(self.send.calls,[])

    def test_document_token_prevents_equal_hash_wrong_day_save(self):
        a=self.api('GET','memo?ymd=261017');b=self.api('GET','memo?ymd=261018')
        self.api('PUT','memo',{'ymd':'261018','sha':b['sha'],'text':'wrong day','document_id':a['document_id']},409)
        self.assertEqual(self.api('GET','memo?ymd=261018')['text'],b['text'])

    def test_schema_blocks_business_writes_and_repair_init_remains_available(self):
        memo=self.api('GET','memo?ymd=261017')
        value=dict(STATE,schema_version=3)
        (self.home/'.workflow/state.json').write_text(json.dumps(value),encoding='utf-8')
        self.api('PUT','memo',{'ymd':'261017','sha':memo['sha'],'document_id':memo['document_id'],'text':'must not write'},409)
        self.assertNotIn('must not write',(self.home/'daily/261017.md').read_text(encoding='utf-8'))
        for route,body in [('memo',{'ymd':'261017'}),('close/plan',{'ymd':'261017'}),('close/apply',{}),('new/plan',{'summary':'task'}),('new/create',{})]:
            self.api('PUT' if route=='memo' else 'POST',route,body,409)
        self.assertEqual(self.send.calls,[])
        self.api('POST','init/apply',{'cfg':{'project_key':'XXX','issue_type_map':{'site_url':BASE}}})
        self.assertEqual(wfhome.read_state(self.home)['schema_version'],4)

    def test_new_plan_payload_and_state_are_bound_and_create_is_idempotent(self):
        plan=self.api('POST','new/plan',{'summary':'new task'})
        request={'plan_id':plan['plan_id'],'operation_id':plan['operation_id']}
        self.api('POST','new/create',{**request,'intent':{'kind':'comment'}},409)
        changed=wfhome.read_state(self.home);changed['title_include']=['^required']
        (self.home/'.workflow/state.json').write_text(json.dumps(changed),encoding='utf-8')
        self.api('POST','new/create',request,409)
        self.assertEqual(self.send.calls,[])
        (self.home/'.workflow/state.json').write_text(json.dumps(STATE),encoding='utf-8')
        first=self.api('POST','new/create',request)
        self.api('POST','new/create',request)
        self.assertEqual(first['key'],'XXX-9');self.assertEqual(len(self.send.calls),1)
        self.assertEqual(len(wfhome.read_state(self.home)['roots']),1)

    def test_changed_source_version_is_rejected_even_if_rebuilt_payload_would_match(self):
        plan=self.api('POST','new/plan',{'summary':'new task'})
        changed=wfhome.read_state(self.home);changed['title_exclude']=['^unrelated-policy-change']
        (self.home/'.workflow/state.json').write_text(json.dumps(changed),encoding='utf-8')
        self.api('POST','new/create',{'plan_id':plan['plan_id'],'operation_id':plan['operation_id']},409)
        self.assertEqual(self.send.calls,[])

    def test_changed_vendor_policy_version_rejects_unchanged_payload(self):
        plan=self.api('POST','new/plan',{'summary':'new task'})
        with mock.patch.object(writeplans,'policy_hash',return_value='changed-vendor-policy'):
            self.api('POST','new/create',{'plan_id':plan['plan_id'],'operation_id':plan['operation_id']},409)
        self.assertEqual(self.send.calls,[])

    def test_known_success_local_failure_repair_requires_no_credentials_or_remote_write(self):
        plan=self.api('POST','new/plan',{'summary':'new task'})
        request={'plan_id':plan['plan_id'],'operation_id':plan['operation_id']}
        with mock.patch.object(newtask,'record',side_effect=OSError('disk full')):
            self.api('POST','new/create',request,409)
        op=self.api('GET','operations')['operations'][0]
        self.assertEqual(op['status'],'succeeded');self.assertFalse(op['local_recorded'])
        original=wfhome.read_state;reads=[]
        def change_site_after_read(home):
            current=original(home);reads.append(current)
            if len(reads)==1:
                changed={**current,'issue_type_map':{**current['issue_type_map'],'site_url':'https://changed.atlassian.net'}}
                wfhome.state_path(home).write_text(json.dumps(changed),encoding='utf-8')
            return current
        with mock.patch.object(wfhome,'read_state',change_site_after_read):
            self.api('POST','operations/'+op['operation_id']+'/repair',{},409)
        self.assertFalse(operations.Store(self.home).get(op['operation_id'])['local_recorded'])
        wfhome.state_path(self.home).write_text(json.dumps(STATE),encoding='utf-8')
        with mock.patch('wsflow.auth.load',side_effect=AssertionError('must not need credentials')):
            self.api('POST','operations/'+op['operation_id']+'/repair',{},409,project='other')
            self.api('POST','operations/'+op['operation_id']+'/repair',{})
            self.api('POST','operations/'+op['operation_id']+'/repair',{})
        self.assertEqual(len(self.send.calls),1)
        self.assertEqual(len(wfhome.read_state(self.home)['roots']),1)
        self.assertTrue(self.api('GET','operations')['operations'][0]['local_recorded'])


class VendorCompatibilityTests(I1Home):
    def test_real_loop_checks_accepts_receipts_and_rejects_removed_approval(self):
        intent=newtask.plan(self.home,STATE,summary='main task')
        approval=jirawrite.approve(self.home,intent['kind'],intent['target'],intent['payload'])
        newtask.create(self.home,CREDS,BASE,intent,Recorder(),operation_id=approval['id'])
        value=wfhome.read_state(self.home)
        parent=next(iter(value['roots'].values()))['jira_key']
        child=newtask.plan(self.home,value,summary='child task',kind='subtask',parent_key=parent)
        approval2=jirawrite.approve(self.home,child['kind'],child['target'],child['payload'])
        newtask.create(self.home,CREDS,BASE,child,Recorder([{'key':'XXX-10','id':'10'}]),operation_id=approval2['id'])
        daily=wfhome.read_memo(self.home,'261017')['path']
        agg=self.home/'.workflow/agg.json'
        agg.write_text(json.dumps({'scope':{'project_key':'XXX','csc_field':None,'csc_value':None},'epics':[],'all_issue_keys':[]}),encoding='utf-8')
        def run(name):
            evidence=self.home/'.workflow'/name
            r=subprocess.run([sys.executable,str(wfhome.VENDOR/'loop_checks.py'),'--daily',daily,'--agg',str(agg),'--state',str(wfhome.state_path(self.home)),'--evidence',str(evidence)],capture_output=True,encoding='utf-8')
            return r.returncode,json.loads(evidence.read_text(encoding='utf-8'))
        normal_code,normal=run('normal.json')
        value=wfhome.read_state(self.home)
        for root in value['roots'].values():
            root.pop('approved_at',None)
            for child in root['children']:child.pop('approved_at',None)
        wfhome.state_path(self.home).write_text(json.dumps(value),encoding='utf-8')
        negative_code,negative=run('negative.json')
        from uuid import uuid4
        evidence_dir=Path(os.environ.get('WSFLOW_TEST_EVIDENCE_DIR') or ('logs/test_evidence/reruns/'+uuid4().hex))
        out=evidence_dir/'i1_vendor_evidence.json'
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps({'normal':normal,'approval_removed':negative,'normal_exit':normal_code,'negative_exit':negative_code},ensure_ascii=False,indent=2),encoding='utf-8')
        self.assertEqual(normal_code,0,normal)
        self.assertEqual(negative_code,1,negative)
        self.assertTrue(any('INV-3' in row for row in negative['violations']))
