"""Common write acceptance using real temporary Git, bare remotes and WF_HOME."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import git, make_bare, make_repo
from wsflow import common_writes, fileio, gitops, operations, wfhome
from wsflow.server import serve
from wsflow.store import Store


class CommonFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='wsflow common writes ');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.repo=make_repo(self.base)
        self.env=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata')})
        self.env.start();self.addCleanup(self.env.stop)
        self.project={'id':'p','path':str(self.repo),'alias':'test'}
        (self.repo/'.git/info/exclude').write_text('.workflow/\ndaily/\n')
        (self.repo/'.workflow').mkdir()
        (self.repo/'.workflow/state.json').write_text(json.dumps({'schema_version':4,'project_key':'XXX','watermark':'old','roots':{}}))
        self.gs=common_writes.Service(self.project,'git')
        self.ms=common_writes.Service(self.project,'memo',self.repo)

    def plan_memo(self,text='saved',day='261024'):
        m=wfhome.read_memo(self.repo,day)
        return self.ms.preview('save',dict(ymd=day,text=text,sha=m['sha'],document_id=m['document_id']))

    def apply(self,service,p):return service.apply(p['plan_id'],p['operation_id'])


class CommonWrites(CommonFixture):
    def test_preview_is_read_only_in_git_and_daily(self):
        m=wfhome.read_memo(self.repo,'261024');daily=self.repo/'daily/261024.md'
        before={str(p):p.read_bytes() for p in [daily,self.repo/'.git/index',self.repo/'.git/HEAD']}
        self.gs.preview('fetch',{});p=self.plan_memo()
        self.assertTrue(p['plan_id']);self.assertNotEqual(p['plan_id'],p['operation_id'])
        self.assertEqual(before,{name:Path(name).read_bytes() for name in before})
        self.assertEqual(wfhome.read_memo(self.repo,'261024')['sha'],m['sha'])

    def test_git_commit_same_id_is_one_effect(self):
        (self.repo/'f0.txt').write_text('updated')
        p=self.gs.preview('commit',dict(message='한글 common commit',add_all=True))
        a=self.apply(self.gs,p);b=self.apply(self.gs,p)
        self.assertEqual(a['hash'],b['hash']);self.assertTrue(b['reused'])
        self.assertEqual(git(self.repo,'rev-list','--count','HEAD').stdout.strip(),'2')
        self.assertEqual(self.gs.status(p['operation_id'])['status'],'succeeded')
        with self.assertRaises(gitops.GitError):self.gs.legacy('commit',dict(message='no changes',add_all=True))
        self.assertEqual(self.gs.list()[-1]['status'],'failed')

    def test_concurrent_git_claim_is_single_commit(self):
        (self.repo/'f0.txt').write_text('updated');p=self.gs.preview('commit',dict(message='once',add_all=True))
        gate=threading.Barrier(2)
        def run(_):
            gate.wait()
            try:return self.apply(self.gs,p)
            except fileio.LockBusy:return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        hashes=[r['hash'] for r in results if r is not None]
        self.assertTrue(hashes);self.assertEqual(len(set(hashes)),1)
        self.assertEqual(self.apply(self.gs,p)['hash'],hashes[0])
        self.assertEqual(git(self.repo,'rev-list','--count','HEAD').stdout.strip(),'2')

    def test_stale_content_index_config_and_ref_are_refused(self):
        for change in ('content','index','config','ref'):
            with self.subTest(change=change):
                p=self.gs.preview('add',{'paths':['f0.txt']})
                if change=='content':(self.repo/'f0.txt').write_text('changed '+change)
                elif change=='index':git(self.repo,'add','f0.txt')
                elif change=='config':git(self.repo,'config','user.name','changed')
                else:git(self.repo,'branch','extra')
                with self.assertRaises(operations.StateConflict):self.apply(self.gs,p)

    def test_unknown_fields_options_and_outside_paths_are_refused(self):
        for action,payload in [('commit',{'message':'x','add_all':True,'extra':1}),('add',{'paths':['../outside']}),
                               ('add',{'paths':['--all']}),('push',{'remote':'--all'}),('remove_everything',{})]:
            with self.subTest(action=action,payload=payload),self.assertRaises(ValueError):self.gs.preview(action,payload)

    def test_expiry_foreign_project_and_wrong_operation_are_refused(self):
        p=self.gs.preview('fetch',{});q=self.gs.preview('fetch',{})
        with self.assertRaises(operations.StateConflict):self.gs.apply(p['plan_id'],q['operation_id'])
        other=common_writes.Service({**self.project,'id':'foreign'},'git')
        with self.assertRaises(operations.StateConflict):self.apply(other,p)
        with mock.patch.object(common_writes.time,'time',return_value=p['expires_at']+1):
            with self.assertRaises(operations.StateConflict):self.apply(self.gs,p)

    def test_git_receipt_repairs_result_record_loss(self):
        (self.repo/'f0.txt').write_text('changed');p=self.gs.preview('commit',dict(message='receipt',add_all=True))
        original=operations.Store.finish
        def lose(store,ident,status,*a,**kw):
            if status=='succeeded':raise OSError('DB record lost')
            return original(store,ident,status,*a,**kw)
        with mock.patch.object(operations.Store,'finish',lose),self.assertRaises(OSError):self.apply(self.gs,p)
        self.assertEqual(self.gs.status(p['operation_id'])['status'],'uncertain')
        r=common_writes.Service(self.project,'git').check(p['operation_id'])
        self.assertEqual(r['status'],'succeeded');self.assertTrue(r['reconciled'])
        self.assertEqual(git(self.repo,'rev-list','--count','HEAD').stdout.strip(),'2')

    def test_unproven_git_effect_never_replays_or_accepts_new_plan(self):
        (self.repo/'f0.txt').write_text('changed');p=self.gs.preview('commit',dict(message='lost',add_all=True))
        original=self.gs._execute
        def lose(plan):original(plan);raise TimeoutError('native success, receipt lost')
        with mock.patch.object(self.gs,'_execute',lose),self.assertRaises(TimeoutError):self.apply(self.gs,p)
        self.assertEqual(self.gs.check(p['operation_id'])['status'],'uncertain')
        with self.assertRaises(operations.StateConflict):self.apply(self.gs,p)
        with self.assertRaises(operations.StateConflict):self.gs.preview('commit',dict(message='new',add_all=True))
        self.assertEqual(git(self.repo,'rev-list','--count','HEAD').stdout.strip(),'2')

    def test_push_lost_receipt_is_reconciled_by_exact_remote_read(self):
        bare=make_bare(self.base);gitops.remote_add(self.repo,'origin',str(bare))
        p=self.gs.preview('push',dict(remote='origin',branch='main',set_upstream=True))
        original=self.gs._execute
        def lose(plan):original(plan);raise TimeoutError('remote succeeded')
        with mock.patch.object(self.gs,'_execute',lose),self.assertRaises(TimeoutError):self.apply(self.gs,p)
        with mock.patch.object(gitops,'push',side_effect=AssertionError('no resend')):
            r=self.gs.check(p['operation_id'])
        self.assertEqual(r['status'],'succeeded');self.assertTrue(r['reconciled'])
        self.assertEqual(git(bare,'rev-parse','refs/heads/main').stdout.strip(),git(self.repo,'rev-parse','HEAD').stdout.strip())

    def test_memo_same_id_is_one_write_and_backup(self):
        p=self.plan_memo('first');original=(self.repo/'daily/261024.md').read_bytes()
        a=self.apply(self.ms,p);b=self.apply(self.ms,p)
        self.assertEqual(a['sha'],b['sha']);self.assertTrue(b['reused'])
        self.assertEqual(len(list((self.repo/'.workflow/memo_bak').glob('*.md'))),1)
        self.assertEqual(Path(self.ms.status(p['operation_id'])['before_backup']).read_bytes(),original)

    def test_memo_effect_before_receipt_is_recovered_without_resave(self):
        p=self.plan_memo('survived');original=self.ms._execute
        def lose(plan):original(plan);raise TimeoutError('saved but reply lost')
        with mock.patch.object(self.ms,'_execute',lose),self.assertRaises(TimeoutError):self.apply(self.ms,p)
        with mock.patch.object(wfhome,'write_memo',side_effect=AssertionError('no second save')):
            r=self.ms.check(p['operation_id'])
        self.assertEqual(r['status'],'succeeded');self.assertIn('survived',r['result']['text'])

    def test_memo_stale_date_identity_and_schema_keep_original(self):
        p=self.plan_memo();daily=self.repo/'daily/261024.md';original=daily.read_bytes()
        m=wfhome.read_memo(self.repo,'261025')
        with self.assertRaises(wfhome.MemoConflict):
            self.ms.preview('save',dict(ymd='261024',text='bad day',sha=m['sha'],document_id=m['document_id']))
        valid=wfhome.read_memo(self.repo,'261024')
        with self.assertRaises(wfhome.MemoConflict):
            self.ms.preview('save',dict(ymd='261024',text='bad sha',sha='stale',document_id=valid['document_id']))
        daily.write_bytes(original+b'outside tail')
        with self.assertRaises(operations.StateConflict):self.apply(self.ms,p)
        self.assertEqual(daily.read_bytes(),original+b'outside tail')
        (self.repo/'.workflow/state.json').write_text('{"schema_version":3}')
        with self.assertRaises(operations.StateConflict):self.plan_memo()

    def test_memo_completed_result_does_not_hide_late_edit(self):
        p=self.plan_memo('saved');self.apply(self.ms,p)
        m=wfhome.read_memo(self.repo,'261024');wfhome.write_memo(self.repo,'261024','external',m['sha'])
        with self.assertRaises(wfhome.MemoConflict):self.apply(self.ms,p)
        self.assertIn('external',wfhome.read_memo(self.repo,'261024')['text'])

    def test_failed_original_backup_produces_no_effect(self):
        p=self.plan_memo();before=(self.repo/'daily/261024.md').read_bytes()
        real=common_writes.fileio.atomic_write
        def fail(path,*a,**kw):
            if Path(path).name=='before.bin':raise OSError('backup unavailable')
            return real(path,*a,**kw)
        with mock.patch.object(common_writes.fileio,'atomic_write',fail),self.assertRaises(OSError):self.apply(self.ms,p)
        self.assertEqual((self.repo/'daily/261024.md').read_bytes(),before)
        self.assertEqual(self.ms.status(p['operation_id'])['status'],'failed')

    def test_two_memo_plans_have_one_current_version(self):
        p=self.plan_memo('one');q=self.plan_memo('two');self.apply(self.ms,p)
        with self.assertRaises(wfhome.MemoConflict):self.apply(self.ms,q)
        self.assertIn('one',wfhome.read_memo(self.repo,'261024')['text'])

    def test_status_is_read_only_and_list_keeps_pending_id(self):
        p=self.plan_memo();store=operations.Store(self.repo);before=store.path.read_bytes()
        self.assertEqual(self.ms.status(p['operation_id'])['status'],'planned')
        self.assertEqual(self.ms.list()[0]['operation_id'],p['operation_id'])
        self.assertEqual(store.path.read_bytes(),before)

    def test_corrupted_or_foreign_receipt_cannot_complete_operation(self):
        for fault in ('checksum','foreign'):
            with self.subTest(fault=fault):
                p=self.plan_memo(fault,day='261024' if fault=='checksum' else '261025')
                original=operations.Store.finish
                def lose(store,ident,status,*a,**kw):
                    if status=='succeeded':raise OSError('result loss')
                    return original(store,ident,status,*a,**kw)
                with mock.patch.object(operations.Store,'finish',lose),self.assertRaises(OSError):self.apply(self.ms,p)
                receipt,_=self.ms._paths(p['operation_id']);value=json.loads(receipt.read_text())
                if fault=='checksum':value['result']['text']='corrupted'
                else:
                    value['operation_id']='A-foreign';value.pop('sha256');value['sha256']=operations.digest(value)
                receipt.write_text(json.dumps(value))
                with self.assertRaises(operations.StateConflict):self.ms.check(p['operation_id'])
                self.assertEqual(self.ms.status(p['operation_id'])['status'],'uncertain')

    def test_result_check_never_recreates_deleted_daily(self):
        p=self.plan_memo();self.apply(self.ms,p);path=self.repo/'daily/261024.md';path.unlink()
        with self.assertRaises(operations.StateConflict):self.ms.check(p['operation_id'])
        self.assertFalse(path.exists())

    def test_unproven_memo_with_late_outer_edit_stays_uncertain(self):
        p=self.plan_memo('effect');original=self.ms._execute
        def lose(plan):original(plan);raise TimeoutError('receipt lost')
        with mock.patch.object(self.ms,'_execute',lose),self.assertRaises(TimeoutError):self.apply(self.ms,p)
        path=self.repo/'daily/261024.md';path.write_bytes(path.read_bytes()+b'OUTSIDE EDIT')
        self.assertEqual(self.ms.check(p['operation_id'])['status'],'uncertain')
        self.assertTrue(path.read_bytes().endswith(b'OUTSIDE EDIT'))

    def lost_push(self):
        bare=make_bare(self.base);gitops.remote_add(self.repo,'origin',str(bare))
        p=self.gs.preview('push',dict(remote='origin',branch='main',set_upstream=True));original=self.gs._execute
        def lose(plan):original(plan);raise TimeoutError('remote effect, no receipt')
        with mock.patch.object(self.gs,'_execute',lose),self.assertRaises(TimeoutError):self.apply(self.gs,p)
        return p,bare

    def test_changed_push_url_cannot_confirm_other_remote(self):
        p,bare=self.lost_push();other=make_bare(self.base,'other.git')
        git(self.repo,'push',str(other),'refs/heads/main:refs/heads/main')
        gitops.remote_set_url(self.repo,'origin',str(other))
        with mock.patch.object(gitops,'push',side_effect=AssertionError('no resend')):
            self.assertEqual(self.gs.check(p['operation_id'])['status'],'uncertain')

    def test_wrong_push_object_never_confirms_success(self):
        p,bare=self.lost_push();git(bare,'update-ref','-d','refs/heads/main')
        with mock.patch.object(gitops,'push',side_effect=AssertionError('no resend')):
            self.assertEqual(self.gs.check(p['operation_id'])['status'],'uncertain')

    def test_missing_upstream_remains_partial_after_remote_success(self):
        p,bare=self.lost_push();git(self.repo,'config','--unset','branch.main.remote')
        self.assertEqual(self.gs.check(p['operation_id'])['status'],'uncertain')

    def test_partial_multi_url_push_never_becomes_success(self):
        bare=make_bare(self.base);gitops.remote_add(self.repo,'origin',str(bare))
        git(self.repo,'config','--add','remote.origin.pushurl',str(bare))
        git(self.repo,'config','--add','remote.origin.pushurl',str(self.base/'missing.git'))
        p=self.gs.preview('push',dict(branch='main',set_upstream=True))
        self.assertEqual(self.apply(self.gs,p)['status'],'uncertain')
        self.assertEqual(self.gs.check(p['operation_id'])['status'],'uncertain')
        self.assertEqual(git(bare,'rev-parse','refs/heads/main').stdout.strip(),git(self.repo,'rev-parse','HEAD').stdout.strip())

    def test_tag_only_is_refused_and_branch_tag_collision_uses_exact_branch(self):
        bare=make_bare(self.base);gitops.remote_add(self.repo,'origin',str(bare));git(self.repo,'tag','v1')
        with self.assertRaises(gitops.GitError):self.gs.preview('push',dict(branch='v1'))
        git(self.repo,'tag','main');git(self.repo,'tag','-a','annotated','-m','not approved')
        git(self.repo,'config','push.followTags','true')
        p=self.gs.preview('push',dict(branch='main'));self.assertTrue(self.apply(self.gs,p)['ok'])
        refs=git(bare,'show-ref').stdout
        self.assertIn('refs/heads/main',refs);self.assertNotIn('refs/tags/',refs)

    def test_unexecuted_check_or_stale_rejection_allows_new_plan(self):
        p=self.plan_memo('draft');path=self.repo/'daily/261024.md';before=path.read_bytes()
        state=self.repo/'.workflow/state.json';state.write_text(state.read_text().replace('old','new'))
        with self.assertRaises(operations.StateConflict):self.apply(self.ms,p)
        self.assertEqual(self.ms.status(p['operation_id'])['status'],'failed')
        self.assertEqual(self.ms.check(p['operation_id'])['status'],'failed');self.assertEqual(path.read_bytes(),before)
        q=self.plan_memo('draft');self.assertEqual(self.ms.check(q['operation_id'])['status'],'failed')
        with self.assertRaises(operations.StateConflict):self.apply(self.ms,q)
        r=self.plan_memo('draft');self.assertEqual(self.apply(self.ms,r)['status'],'succeeded')

    def test_planning_latency_never_ages_a_fresh_lock_into_deletion(self):
        path=self.repo/'.git/index.lock';path.write_bytes(b'fresh');p=self.gs.preview('clean_locks',{})
        with mock.patch.object(common_writes.time,'time',return_value=p['expires_at']-1700),mock.patch.object(gitops,'_git_running',return_value=False):
            r=self.apply(self.gs,p)
        self.assertEqual(r['removed'],[]);self.assertIn(str(path),r['skipped']);self.assertEqual(path.read_bytes(),b'fresh')


class CommonHttp(CommonFixture):
    def setUp(self):
        super().setUp()
        registry=self.base/'registry';registry.mkdir();store=Store(registry/'projects.json')
        store.add_root(str(self.base));self.pid=store.add_project(str(self.repo))['id']
        self.http=serve(store.file,'127.0.0.1',0)
        thread=threading.Thread(target=self.http.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.url='http://127.0.0.1:'+str(self.http.server_address[1])

    def request(self,method,path,body=None,expected=200,token=True):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Auth-Token']=self.http.wsflow_token
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=30) as response:
                self.assertEqual(response.status,expected);return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            value=json.loads(exc.read());self.assertEqual(exc.code,expected,value);return value

    def test_api_plan_apply_status_and_guard(self):
        base=f'/api/v1/projects/{self.pid}/writes/git'
        self.request('POST',base+'/plan',{'action':'fetch','payload':{}},expected=403,token=False)
        (self.repo/'f0.txt').write_text('api edit')
        p=self.request('POST',base+'/plan',{'action':'commit','payload':{'message':'API','add_all':True}})
        ids={k:p[k] for k in ('plan_id','operation_id')}
        self.request('POST',base+'/apply',{**ids,'payload':{'message':'substituted'}},expected=400)
        a=self.request('POST',base+'/apply',ids);b=self.request('POST',base+'/apply',ids)
        self.assertEqual(a['hash'],b['hash']);self.assertTrue(b['reused'])
        status=self.request('GET',base+'/operations/'+p['operation_id']);self.assertEqual(status['status'],'succeeded')
        self.assertIn(p['operation_id'],[r['operation_id'] for r in self.request('GET',base+'/operations')['operations']])

    def test_api_memo_replay_and_legacy_conflict(self):
        self.request('GET',f'/api/projects/{self.pid}/jira/memo?ymd=261024')
        m=wfhome.read_memo(self.repo,'261024');payload=dict(ymd='261024',document_id=m['document_id'],sha=m['sha'],text='API memo')
        base=f'/api/v1/projects/{self.pid}/writes/memo'
        p=self.request('POST',base+'/plan',{'action':'save','payload':payload})
        ids={k:p[k] for k in ('plan_id','operation_id')};a=self.request('POST',base+'/apply',ids)
        self.assertEqual(a['document_id'],m['document_id']);self.assertTrue(self.request('POST',base+'/apply',ids)['reused'])
        self.request('PUT',f'/api/projects/{self.pid}/jira/memo',payload,expected=409)
        ops=self.request('GET',f'/api/projects/{self.pid}/jira/operations')
        self.assertEqual(ops['operations'],[]);self.assertEqual(ops['common_writes'][0]['operation_id'],p['operation_id'])


if __name__=='__main__':unittest.main()
