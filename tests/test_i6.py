"""I6 public contracts: static files and submitted observations stay distinct."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest import mock

from wsflow import context_projection as projection, operations, session_prep
from wsflow.store import Store


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(value.encode('utf-8') if isinstance(value,str) else value)


def files(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.root=self.base/'workspace with spaces';self.root.mkdir()
        patch=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata')});patch.start();self.addCleanup(patch.stop)
        self.registry=Store(self.base/'projects.json');self.registry.add_root(str(self.root))
        self.a=self.registry.create_project(str(self.root),'Alpha');self.b=self.registry.create_project(str(self.root),'Beta')
        self.home=Path(self.a['path']);self.other=Path(self.b['path'])
        put(self.root/'GLOBAL_RULES.md','COMMON');put(self.home/'PROJECT_RULES.md','PROJECT')
        put(self.root/'skills/alpha/SKILL.md','---\nname: alpha\ndescription: fixture\n---\nRead [ref](references/a.md).')
        put(self.root/'skills/alpha/references/a.md','SUPPORT')
        put(self.other/'PRIVATE.md','OTHER PROJECT PRIVATE')
        p=projection.Service(self.registry)
        for kind in ('root_init','project_init'):
            plan=p.preview(self.a['id'],{'kind':kind,'backend':'managed-copy','selected_skills':['common:alpha'] if kind=='project_init' else []})
            self.assertEqual(plan['blockers'],[]);self.assertEqual(p.apply(self.a['id'],plan['plan_id'],plan['operation_id'])['status'],'succeeded')
        self.service=session_prep.Service(self.registry)

    def prepare(self,**extra):
        return self.service.prepare(self.a['id'],{'tool':'codex','version':'test-1','surface':'cli',**extra})

    def document(self,packet,**extra):
        return {'schema_version':1,'origin':'agent_report','session_ref':'session-1','captured_at':operations.now(),
                'snapshot_id':packet['snapshot_id'],'profile_id':packet['profile']['profile_id'],'cwd':packet['cwd'],
                'observed_sources':[{'id':s['id'],'sha256':s['sha256']} for s in packet['expected_sources']],
                'observed_skills':[{'id':s['id'],'tree_hash':s['tree_hash']} for s in packet['expected_skills']],
                'notes':'plain data',**extra}

    def observe(self,packet,kind='agent_reported',document=None,**extra):
        evidence=self.service.upload(json.dumps(document or self.document(packet)))
        return self.service.observe(self.a['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':kind,**extra})

    def status(self,tool='codex'):
        return self.service.status(self.a['id'])['tools'][tool]

    def test_prepare_is_user_readonly_scoped_static_and_tool_specific(self):
        before=files(self.root);packet=self.prepare();self.assertTrue(packet['ready']);self.assertEqual(packet['cwd'],str(self.home))
        self.assertEqual(packet['recognition'],'unknown');self.assertEqual(packet['observation_kind'],'static_only')
        self.assertEqual(packet['profile']['tool'],'codex');self.assertEqual(packet['profile']['discovery_path'],'.agents/skills')
        self.assertIn('AGENTS.md',packet['startup_text']);self.assertNotIn('OTHER PROJECT PRIVATE',json.dumps(packet))
        self.assertEqual(files(self.root),before);self.assertFalse(packet['scope_complete'])
        claude=self.prepare(tool='claude',surface='desktop');self.assertEqual(claude['profile']['discovery_path'],'.claude/skills');self.assertIn('CLAUDE.md',claude['startup_text'])

    def test_legacy_status_get_never_assigns_uid_or_creates_database(self):
        before=files(self.root);status=self.service.status(self.b['id']);self.assertFalse(status['tools']['codex']['files']['ready'])
        self.assertEqual(status['tools']['codex']['recognition'],'unknown');self.assertEqual(files(self.root),before)
        self.assertFalse(self.service.ops.path.exists());self.assertFalse((self.other/'.wsflow').exists())

    def test_prepare_rejects_missing_or_extra_profile_fields(self):
        for extra in ({'version':''},{'tool':'shell'},{'surface':'other'},{'cwd':str(self.other)},{'host':'spoof'}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.prepare(**extra)

    def test_support_copy_or_entrypoint_mismatch_blocks_new_preparation(self):
        put(self.home/'.agents/skills/alpha/references/a.md','USER EDIT');packet=self.prepare()
        self.assertFalse(packet['ready']);self.assertIsNone(packet['packet_id']);self.assertEqual(packet['recognition'],'unknown')
        self.assertTrue(any('projection' in b for b in packet['blockers']))

    def test_agent_report_is_never_tool_observation(self):
        packet=self.prepare();receipt=self.observe(packet);status=self.status()
        self.assertEqual(receipt['evidence_kind'],'agent_reported');self.assertTrue(receipt['verification_scope']['selected_scope_complete'])
        self.assertEqual(status['recognition'],'agent_reported');self.assertFalse(status['independently_verified'])
        self.assertEqual(status['latest_receipt']['trust'],'user_submitted_unattested')

    def test_tool_output_requires_schema_origin_and_explicit_attestation(self):
        packet=self.prepare()
        with self.assertRaises(ValueError):self.observe(packet,'tool_observed',attest_tool_output=True)
        doc=self.document(packet,origin='tool_output')
        with self.assertRaises(ValueError):self.observe(packet,'tool_observed',document=doc)
        receipt=self.observe(packet,'tool_observed',document=doc,attest_tool_output=True)
        self.assertEqual(receipt['recognition'],'tool_observed');self.assertFalse(receipt['independently_verified']);self.assertEqual(self.status()['recognition'],'tool_observed')

    def test_partial_observation_does_not_confirm_selected_scope(self):
        packet=self.prepare();receipt=self.observe(packet,'tool_observed',document=self.document(packet,origin='tool_output',observed_skills=[]),attest_tool_output=True)
        self.assertFalse(receipt['verification_scope']['selected_scope_complete']);self.assertEqual(receipt['recognition'],'partial')
        self.assertEqual(self.status()['recognition'],'partial')

    def test_unknown_and_static_do_not_claim_runtime_recognition(self):
        packet=self.prepare()
        for kind in ('unknown','static_only'):
            doc=self.document(packet,origin=kind,observed_sources=[],observed_skills=[])
            receipt=self.observe(packet,kind,document=doc)
            self.assertEqual(receipt['recognition'],'unknown')

    def test_wrong_cwd_snapshot_profile_source_skill_or_time_is_rejected(self):
        packet=self.prepare();doc=self.document(packet)
        wrongs=[{'cwd':str(self.other)},{'snapshot_id':'wrong'},{'profile_id':'wrong'},
                {'observed_sources':[{'id':doc['observed_sources'][0]['id'],'sha256':'0'*64}]},
                {'observed_skills':[{'id':'common:not-selected','tree_hash':'0'*64}]},
                {'captured_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()},
                {'captured_at':'2020-01-01T00:00:00'},{'observed_sources':doc['observed_sources']*2}]
        for wrong in wrongs:
            with self.subTest(wrong=wrong),self.assertRaises(ValueError):self.observe(packet,document={**doc,**wrong})

    def test_receipt_replay_reload_and_sql_immutability(self):
        packet=self.prepare();evidence=self.service.upload(json.dumps(self.document(packet)))
        body={'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'}
        first=self.service.observe(self.a['id'],body);second=session_prep.Service(Store(self.registry.file)).observe(self.a['id'],body)
        self.assertEqual(first['receipt_id'],second['receipt_id']);self.assertEqual(len(self.status()['receipts']),1)
        self.assertEqual(first['profile']['tool'],'codex');self.assertEqual(first['cwd'],packet['cwd']);self.assertEqual(first['snapshot_id'],packet['snapshot_id'])
        self.assertEqual(first['observed_skill_ids'],['common:alpha']);self.assertTrue(Path(first['evidence_path']).is_relative_to(self.service.evidence_dir))
        with self.service.ops.connection(True) as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute('UPDATE plans SET snapshot=? WHERE id=?',('{}',packet['packet_id']))
            with self.assertRaises(sqlite3.IntegrityError):db.execute('UPDATE operations SET intent=? WHERE id=?',('{}',first['receipt_id']))

    def test_common_change_stales_old_receipt_without_mutating_history(self):
        packet=self.prepare();receipt=self.observe(packet);before=self.service.ops.get(receipt['receipt_id'])
        put(self.root/'GLOBAL_RULES.md','UPDATED COMMON');status=self.status()
        self.assertEqual(status['recognition'],'unknown');self.assertIn('context_changed',status['latest_receipt']['stale_reasons'])
        self.assertEqual(self.service.ops.get(receipt['receipt_id']),before)

    def test_support_source_change_stales_packet_and_reports_impacted_projects(self):
        packet=self.prepare();self.observe(packet);put(self.root/'skills/alpha/references/a.md','NEW SUPPORT')
        status=self.status();self.assertFalse(status['files']['ready']);self.assertEqual(status['recognition'],'unknown')
        dashboard=self.service.dashboard();self.assertEqual(dashboard['projects'][0]['impact_project_ids'],[self.a['id'],self.b['id']])

    def test_version_host_and_new_preparation_invalidate_previous_receipt(self):
        packet=self.prepare();self.observe(packet);self.prepare(version='test-2')
        receipt=self.status()['receipts'][0];self.assertIn('profile_changed',receipt['stale_reasons']);self.assertIn('newer_preparation',receipt['stale_reasons'])
        self.assertEqual(self.status()['recognition'],'unknown')
        with mock.patch.object(session_prep,'runtime',return_value={'host':'other','os':'other'}):
            self.assertIn('runtime_changed',self.status()['packet']['stale_reasons'])

    def test_new_packet_is_unknown_even_when_same_files_and_tool(self):
        packet=self.prepare();self.observe(packet,'tool_observed',document=self.document(packet,origin='tool_output'),attest_tool_output=True)
        new=self.prepare();self.assertNotEqual(packet['packet_id'],new['packet_id']);self.assertEqual(self.status()['recognition'],'unknown')

    def test_evidence_changes_or_disappears_never_reuses_positive_badge(self):
        packet=self.prepare();evidence=self.service.upload(json.dumps(self.document(packet,origin='tool_output')))
        self.service.observe(self.a['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'tool_observed','attest_tool_output':True})
        path=self.service.evidence_dir/(evidence['evidence_id']+'.txt');put(path,'CHANGED')
        self.assertIn('evidence_changed',self.status()['latest_receipt']['stale_reasons']);self.assertEqual(self.status()['recognition'],'unknown')
        path.unlink();self.assertEqual(self.status()['recognition'],'unknown')

    def test_untrusted_evidence_is_bounded_data_and_arbitrary_paths_rejected(self):
        packet=self.prepare();payload='__import__("os").system("echo EXECUTED")'
        evidence=self.service.upload(payload)
        with self.assertRaises(ValueError):self.service.observe(self.a['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'tool_observed','attest_tool_output':True})
        with self.assertRaises(ValueError):self.service.upload('x'*(256*1024+1))
        with self.assertRaises(ValueError):self.service.observe(self.a['id'],{'packet_id':packet['packet_id'],'evidence_id':'../../credentials.json','evidence_kind':'unknown'})
        receipt=self.observe(packet,document=self.document(packet,notes='<img src=x onerror="window.PWNED=1">'))
        self.assertIn('<img',receipt['notes']);self.assertFalse((self.root/'EXECUTED').exists())

    @unittest.skipUnless(os.name=='nt','Windows evidence junction boundary')
    def test_evidence_link_escape_is_rejected(self):
        packet=self.prepare();evidence=self.service.upload(json.dumps(self.document(packet)))
        old=self.service.evidence_dir;renamed=old.with_name(old.name+'-saved');old.rename(renamed)
        subprocess.run(['cmd','/c','mklink','/J',str(old),str(self.other)],check=True,capture_output=True)
        self.addCleanup(old.rmdir)
        with self.assertRaises((ValueError,operations.StateConflict)):
            self.service.observe(self.a['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'})

    def test_other_project_cannot_submit_for_packet(self):
        packet=self.prepare();evidence=self.service.upload(json.dumps(self.document(packet)))
        with self.assertRaises(operations.StateConflict):self.service.observe(self.b['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'})

    def test_dashboard_is_readonly_and_calendar_warning_is_separate(self):
        packet=self.prepare();self.observe(packet);before=files(self.root)
        result=self.service.dashboard(calendar={'state':'attention','warnings':['Google 확인 필요'],'outbox_counts':{'uncertain':1}})
        self.assertEqual(result['calendar']['state'],'attention');self.assertEqual(len(result['projects']),2)
        self.assertEqual(result['projects'][0]['tools']['codex']['recognition'],'agent_reported');self.assertEqual(files(self.root),before)

    def test_manifest_tamper_or_checkout_binding_invalidates_preparation(self):
        self.prepare();put(self.home/'.wsflow/manifest.json','{}')
        status=self.status();self.assertFalse(status['files']['ready']);self.assertEqual(status['recognition'],'unknown')

    def test_http_additive_routes_keep_guards_and_do_not_accept_paths(self):
        from wsflow.server import serve
        httpd=serve(self.registry.file,'127.0.0.1',0);self.addCleanup(httpd.server_close);self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(httpd.server_address[1])
        def request(path,body=None,auth=True,origin=None):
            headers={'Content-Type':'application/json',**({'X-Auth-Token':httpd.wsflow_token} if auth else {}),**({'Origin':origin} if origin else {})}
            req=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
            try:
                with urllib.request.urlopen(req) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as r:
                with r:return r.code,json.load(r)
        endpoint='/api/v1/projects/'+self.a['id']+'/session/'
        self.assertEqual(request(endpoint+'status',auth=False)[0],403)
        self.assertEqual(request(endpoint+'prepare',{'tool':'codex','version':'1','surface':'cli'},origin='https://evil.invalid')[0],403)
        code,packet=request(endpoint+'prepare',{'tool':'codex','version':'1','surface':'cli'});self.assertEqual(code,200);self.assertTrue(packet['ready'])
        self.assertEqual(request(endpoint+'evidence',{'path':str(self.root/'GLOBAL_RULES.md')})[0],400)
        code,evidence=request(endpoint+'evidence',{'content':json.dumps(self.document(packet))});self.assertEqual(code,200)
        code,receipt=request(endpoint+'observations',{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'});self.assertEqual(code,200);self.assertEqual(receipt['evidence_kind'],'agent_reported')
        self.assertEqual(request(endpoint+'status')[0],200);self.assertEqual(request('/api/v1/context/dashboard')[0],200)
        self.assertEqual(request('/api/v1/projects/'+self.a['id']+'/context')[0],200)

    def test_support_directory_and_selected_skill_changes_invalidate_old_scope(self):
        packet=self.prepare();self.observe(packet)
        context=projection.Service(self.registry)
        plan=context.preview(self.a['id'],{'kind':'project_init','backend':'managed-copy','selected_skills':[]})
        self.assertEqual(plan['blockers'],[]);context.apply(self.a['id'],plan['plan_id'],plan['operation_id'])
        self.assertEqual(self.status()['recognition'],'unknown');self.assertIn('context_changed',self.status()['latest_receipt']['stale_reasons'])
        self.assertEqual(self.status()['files']['expected_skills'],[])

    def test_user_entrypoint_area_changes_invalidate_even_when_block_preserved(self):
        packet=self.prepare();self.observe(packet);entry=self.home/'AGENTS.md';put(entry,entry.read_bytes()+b'\nUSER NEW POLICY\n')
        status=self.status();self.assertTrue(status['files']['ready']);self.assertEqual(status['recognition'],'unknown')

    def test_claude_additional_rules_are_scoped_and_changes_go_stale(self):
        rule=self.home/'.claude/rules/ui.md';put(rule,'RULE A')
        packet=self.prepare(tool='claude');self.assertTrue(any(s['id']=='additional:.claude/rules/ui.md' for s in packet['expected_sources']))
        self.observe(packet);put(rule,'RULE B');self.assertEqual(self.status('claude')['recognition'],'unknown')

    def test_checkout_move_root_rebinding_and_duplicate_uid_block(self):
        packet=self.prepare();self.observe(packet)
        put(self.other/'.wsflow/project.json',(self.home/'.wsflow/project.json').read_bytes())
        self.assertIn('duplicate_project_uid',self.status()['files']['blockers'])
        (self.other/'.wsflow/project.json').unlink();profile=self.home/'.wsflow/project.json';value=json.loads(profile.read_bytes());value['root_uid']='changed';put(profile,json.dumps(value))
        self.assertIn('root_uid_mismatch',self.status()['files']['blockers']);self.assertEqual(self.status()['recognition'],'unknown')

    def test_database_failure_and_untrusted_schema_never_make_observation(self):
        before=files(self.root)
        with mock.patch.object(self.service.ops,'save_plan',side_effect=OSError('injected DB failure')):
            with self.assertRaises(OSError):self.prepare()
        self.assertEqual(files(self.root),before);self.assertEqual(self.status()['recognition'],'unknown')
        packet=self.prepare()
        for document in ({**self.document(packet),'schema_version':True},[],{'command':'execute me'}):
            evidence=self.service.upload(json.dumps(document))
            with self.assertRaises(ValueError):self.service.observe(self.a['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'})

    def test_concurrent_receipt_submission_is_idempotent(self):
        packet=self.prepare();evidence=self.service.upload(json.dumps(self.document(packet)));body={'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'}
        out=[]
        def submit():out.append(session_prep.Service(self.registry).observe(self.a['id'],body)['receipt_id'])
        first=threading.Thread(target=submit);second=threading.Thread(target=submit);first.start();second.start();first.join();second.join()
        self.assertEqual(len(out),2);self.assertEqual(out[0],out[1]);self.assertEqual(len(self.status()['receipts']),1)

    def test_userdata_inside_project_is_rejected(self):
        with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.home/'user-data')}):
            with self.assertRaises(operations.StateConflict):session_prep.Service(self.registry).prepare(self.a['id'],{'tool':'codex','version':'1','surface':'cli'})
        self.assertFalse((self.home/'user-data').exists())

    @unittest.skipUnless(os.name=='nt','Windows prepared source junction boundary')
    def test_source_link_outside_checkout_invalidates_existing_receipt(self):
        packet=self.prepare();self.observe(packet);support=self.home/'.agents/skills/alpha/references'
        renamed=support.with_name('references-saved');support.rename(renamed)
        subprocess.run(['cmd','/c','mklink','/J',str(support),str(self.other)],check=True,capture_output=True)
        self.addCleanup(support.rmdir);self.assertFalse(self.status()['files']['ready']);self.assertEqual(self.status()['recognition'],'unknown')

    def test_cwd_slash_normalization_keeps_physical_checkout_identity(self):
        packet=self.prepare();doc=self.document(packet,cwd=packet['cwd'].replace('\\','/'))
        self.assertEqual(self.observe(packet,document=doc)['recognition'],'agent_reported')

    def test_one_missing_project_does_not_hide_other_dashboard_rows(self):
        self.prepare();(self.other/'PRIVATE.md').unlink();self.other.rmdir()
        dashboard=self.service.dashboard();self.assertEqual(len(dashboard['projects']),2)
        self.assertTrue(dashboard['projects'][0]['tools']['codex']['files']['ready']);self.assertFalse(dashboard['projects'][1]['tools']['codex']['files']['ready'])

    def test_tampered_selection_cannot_adopt_unmanaged_preparation(self):
        self.prepare();profile=self.home/'.wsflow/project.json';value=json.loads(profile.read_bytes());value['selected_skills']=[];put(profile,json.dumps(value))
        state=self.status();self.assertFalse(state['files']['ready']);self.assertIn('managed_profile_changed:project:profile',state['files']['blockers'])
        self.assertIsNone(self.prepare()['packet_id'])

    def test_real_worktree_descriptor_change_invalidates_receipt(self):
        for args in (['init'],['-c','user.name=fixture','-c','user.email=fixture@invalid','commit','--allow-empty','-m','fixture']):
            subprocess.run(['git','-C',str(self.home),*args],check=True,capture_output=True)
        worktree=self.root/'separate worktree';subprocess.run(['git','-C',str(self.home),'worktree','add','--detach',str(worktree)],check=True,capture_output=True)
        pr=self.registry.add_project(str(worktree));context=projection.Service(self.registry)
        plan=context.preview(pr['id'],{'kind':'project_init','backend':'managed-copy','selected_skills':['common:alpha']})
        self.assertEqual(plan['blockers'],[]);context.apply(pr['id'],plan['plan_id'],plan['operation_id'])
        packet=self.service.prepare(pr['id'],{'tool':'codex','version':'1','surface':'cli'});self.assertTrue(packet['ready'])
        evidence=self.service.upload(json.dumps(self.document(packet)))
        self.service.observe(pr['id'],{'packet_id':packet['packet_id'],'evidence_id':evidence['evidence_id'],'evidence_kind':'agent_reported'})
        (worktree/'.git').unlink();put(worktree/'.git','gitdir: '+str(self.home/'.git'))
        state=self.service.status(pr['id'])['tools']['codex'];self.assertEqual(state['recognition'],'unknown');self.assertIn('context_changed',state['latest_receipt']['stale_reasons'])

    def test_instructions_in_json_notes_are_never_executed(self):
        packet=self.prepare();marker=self.home/'EXECUTED.txt'
        instruction="__import__('pathlib').Path("+repr(str(marker))+").write_text('EXECUTED')"
        receipt=self.observe(packet,document=self.document(packet,notes=instruction))
        self.assertEqual(receipt['notes'],instruction);self.assertFalse(marker.exists())


if __name__=='__main__':unittest.main()
