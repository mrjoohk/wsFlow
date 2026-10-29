"""I5 public contracts: temporary real files, registry, SQLite and HTTP."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import urllib.request
import urllib.error
from unittest import mock

from wsflow import context_projection as ctx, deploy, operations
from wsflow.store import Store


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)


def files(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


class ContextSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'work space';self.root.mkdir()
        self.patch=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata')});self.patch.start();self.addCleanup(self.patch.stop)
        self.store=Store(self.base/'projects.json');self.store.add_root(str(self.root))
        self.a=self.store.create_project(str(self.root),'project A');self.b=self.store.create_project(str(self.root),'project B')
        self.home=Path(self.a['path']);self.other=Path(self.b['path']);self.service=ctx.Service(self.store)
        put(self.root/'GLOBAL_RULES.md','COMMON USER RULES\r\n')
        put(self.root/'skills/alpha/SKILL.md','---\nname: alpha\ndescription: sample\n---\nRead [support](references/a.md).')
        put(self.root/'skills/alpha/references/a.md','SUPPORT');put(self.root/'skills/alpha/scripts/a.py','raise RuntimeError("NEVER EXECUTE")')
        put(self.other/'PRIVATE.md','OTHER PROJECT PRIVATE')
        put(self.home/'.workflow/state.json','{"watermark":"keep","roots":{"x":1}}')

    def plan(self,**body):
        return self.service.preview(self.a['id'],{'kind':'project_init','backend':'managed-copy','selected_skills':['common:alpha'],'adopt_existing':True,**body})

    def apply(self,plan,recover=False):
        self.assertEqual(plan['blockers'],[],plan['blockers'])
        return self.service.apply(self.a['id'],plan['plan_id'],plan['operation_id'],recover=recover)

    def root_init(self):
        return self.apply(self.plan(kind='root_init',selected_skills=[]))

    def initialized(self):
        self.root_init();return self.apply(self.plan())

    def test_preview_is_readonly_and_empty_create_stays_empty(self):
        before=files(self.root);p=self.plan(kind='root_init',selected_skills=[])
        self.assertEqual(files(self.root),before);self.assertEqual(p['impact_project_ids'],[self.a['id'],self.b['id']])
        new=self.store.create_project(str(self.root),'empty');self.assertEqual(list(Path(new['path']).iterdir()),[])

    def test_root_separate_scope_and_explicit_backend(self):
        p=self.plan();self.assertIn('root_not_initialized',p['blockers'])
        p=self.plan(kind='root_init',backend='junction');self.assertIn('backend_not_supported',p['blockers'])
        self.assertFalse((self.root/'.wsflow').exists())

    def test_full_selected_package_user_files_and_replay_preserved(self):
        original=b'USER HEADER\r\nno final newline';put(self.home/'AGENTS.md',original)
        put(self.home/'PROJECT_RULES.md','LOCAL USER POLICY');put(self.home/'.codex/skills/legacy/SKILL.md','LEGACY')
        old=files(self.root);self.initialized();uid=json.loads((self.home/'.wsflow/project.json').read_bytes())['project_uid']
        self.assertTrue((self.home/'AGENTS.md').read_bytes().startswith(original))
        for name in ('.agents','.claude'):
            self.assertEqual((self.home/name/'skills/alpha/references/a.md').read_text(),'SUPPORT')
            self.assertIn('NEVER EXECUTE',(self.home/name/'skills/alpha/scripts/a.py').read_text())
        p=self.plan();r=self.apply(p);self.assertEqual(r['status'],'succeeded');self.assertEqual(self.apply(p),r)
        self.assertEqual(json.loads((self.home/'.wsflow/project.json').read_bytes())['project_uid'],uid)
        for rel,data in old.items():
            if rel!='project A/AGENTS.md':self.assertEqual((self.root/rel).read_bytes(),data,rel)
        self.assertFalse((self.other/'.wsflow').exists())

    def test_unmanaged_adoption_requires_explicit_choice(self):
        self.root_init();put(self.home/'AGENTS.md','USER')
        p=self.plan(adopt_existing=False);self.assertIn('adoption_required:AGENTS.md',p['blockers'])
        self.assertEqual((self.home/'AGENTS.md').read_text(),'USER')

    def test_name_case_collision_blocks_without_merging(self):
        self.root_init();put(self.home/'skills/ALPHA/SKILL.md','different')
        self.assertIn('skill_name_collision:alpha',self.plan()['blockers'])

    def test_existing_exposure_even_identical_is_not_silently_claimed(self):
        self.root_init();put(self.home/'.agents/skills/alpha/SKILL.md',(self.root/'skills/alpha/SKILL.md').read_bytes())
        self.assertIn('unmanaged_exposure:.agents/skills/alpha',self.plan()['blockers'])

    def test_support_file_source_stale_zero_effect(self):
        self.root_init();p=self.plan();before=files(self.root);put(self.root/'skills/alpha/references/a.md','CHANGED')
        expected=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p)
        self.assertEqual(files(self.root),expected);self.assertNotEqual(before,expected)

    def test_new_target_or_registry_change_stale_zero_effect(self):
        self.root_init();p=self.plan();put(self.home/'CLAUDE.md','LATE USER')
        before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p)
        self.assertEqual(files(self.root),before)
        p=self.plan();self.store.update_project(self.b['id'],alias='changed');before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p)
        self.assertEqual(files(self.root),before)

    def test_outside_user_block_edit_preserved_inside_edit_blocked(self):
        self.initialized();entry=self.home/'AGENTS.md';put(entry,entry.read_bytes()+b'\nAFTER USER\n');self.apply(self.plan())
        self.assertTrue(entry.read_bytes().endswith(b'AFTER USER\n'))
        put(entry,entry.read_bytes().replace(b'Read',b'Changed',1));before=files(self.root)
        self.assertIn('managed_block_changed:AGENTS.md',self.plan()['blockers']);self.assertEqual(files(self.root),before)

    def test_managed_copy_user_edit_and_extra_file_block(self):
        self.initialized();put(self.home/'.agents/skills/alpha/references/a.md','USER EDIT')
        self.assertIn('managed_file_changed:.agents/skills/alpha/references/a.md',self.plan()['blockers'])
        put(self.home/'.claude/skills/alpha/user.txt','EXTRA')
        self.assertIn('managed_package_changed:.claude/skills/alpha',self.plan()['blockers'])

    def test_deselect_removes_only_owned_bytes_not_canonical_or_legacy(self):
        self.initialized();put(self.home/'.codex/skills/legacy/SKILL.md','KEEP')
        self.apply(self.plan(selected_skills=[]))
        self.assertFalse((self.home/'.agents/skills/alpha/SKILL.md').exists())
        self.assertTrue((self.root/'skills/alpha/SKILL.md').exists());self.assertEqual((self.home/'.codex/skills/legacy/SKILL.md').read_text(),'KEEP')

    def test_partial_failure_recovers_and_post_edit_refused(self):
        self.root_init();p=self.plan();real=ctx.fileio.atomic_write;count=0
        def fail(path,data,**kwargs):
            nonlocal count
            if not Path(path).is_relative_to(self.root):return real(path,data,**kwargs)
            count+=1
            if count==3:raise OSError('injected write failure')
            return real(path,data,**kwargs)
        with mock.patch.object(ctx.fileio,'atomic_write',side_effect=fail):r=self.apply(p)
        self.assertEqual(count,3)
        self.assertEqual(r['status'],'uncertain')
        with self.assertRaises(operations.StateConflict):self.apply(p)
        service=ctx.Service(self.store);r=service.apply(self.a['id'],p['plan_id'],p['operation_id'],recover=True)
        self.assertEqual(r['status'],'succeeded');self.assertTrue((self.home/'.wsflow/project.json').exists())

    def test_recovery_preserves_after_write_user_edit(self):
        self.root_init();p=self.plan();real=ctx.fileio.atomic_write;count=0
        def fail(path,data,**kwargs):
            nonlocal count
            if not Path(path).is_relative_to(self.root):return real(path,data,**kwargs)
            count+=1
            if count==2:raise OSError('injected')
            return real(path,data,**kwargs)
        with mock.patch.object(ctx.fileio,'atomic_write',side_effect=fail):self.assertEqual(self.apply(p)['status'],'uncertain')
        self.assertEqual(count,2)
        first=Path(p['actions'][0]['path']);put(first,'LATER USER');before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p,recover=True)
        self.assertEqual(files(self.root),before)

    def test_parallel_approvals_have_one_effect(self):
        self.root_init();p=self.plan();out=[]
        def run():
            try:out.append(ctx.Service(self.store).apply(self.a['id'],p['plan_id'],p['operation_id'])['status'])
            except operations.StateConflict:out.append('conflict')
        a=threading.Thread(target=run);b=threading.Thread(target=run);a.start();b.start();a.join();b.join()
        self.assertIn('succeeded',out);self.assertEqual(out.count('succeeded')+out.count('conflict'),2)
        self.assertEqual((self.home/'AGENTS.md').read_bytes().count(b'<!-- WSFLOW:BEGIN v1 -->'),1)

    def test_managed_deploy_is_blocked_but_normal_documents_work(self):
        self.initialized();put(self.base/'AGENTS.md','REPLACE')
        r=deploy.apply([str(self.base/'AGENTS.md')],[self.a],'',lambda _:None)
        self.assertEqual(r['copied_total'],0);self.assertTrue(r['results'][0]['errors'])
        put(self.base/'normal.txt','NORMAL');r=deploy.apply([str(self.base/'normal.txt')],[self.a],'',lambda _:None)
        self.assertEqual(r['copied_total'],1)

    def test_manifest_tamper_is_not_new_ownership(self):
        self.initialized();p=self.home/'.wsflow/manifest.json';value=json.loads(p.read_bytes());value['files']={};put(p,json.dumps(value))
        self.assertIn('manifest_changed',self.plan()['blockers'])

    def test_duplicate_uid_checkout_move_and_root_rebind_block(self):
        self.initialized();put(self.other/'.wsflow/project.json',(self.home/'.wsflow/project.json').read_bytes())
        self.assertIn('duplicate_project_uid',self.plan()['blockers'])
        (self.other/'.wsflow/project.json').unlink();profile=self.home/'.wsflow/project.json';data=json.loads(profile.read_bytes());data['checkout_path']=str(self.other);put(profile,json.dumps(data))
        self.assertIn('checkout_binding_changed',self.plan()['blockers'])

    def test_external_dependency_path_is_blocked(self):
        self.root_init();put(self.root/'skills/alpha/SKILL.md','Read [outside](../../outside.md).')
        self.assertTrue(any('package_dependency' in x for x in self.plan()['blockers']))

    def test_unknown_skill_and_payload_shape_rejected(self):
        self.root_init();self.assertIn('selected_skill_missing:common:missing',self.plan(selected_skills=['common:missing'])['blockers'])
        with self.assertRaises(ValueError):self.plan(selected_skills='common:alpha')

    def test_empty_support_directories_binary_files_and_claude_space_import(self):
        (self.root/'skills/alpha/assets/empty').mkdir(parents=True)
        put(self.root/'skills/alpha/assets/image.bin',bytes(range(256)))
        nested=self.store.create_project(str(self.root),'Nested Folder')
        self.root_init()
        p=self.service.preview(nested['id'],{'backend':'managed-copy','selected_skills':['common:alpha']})
        self.assertEqual(p['blockers'],[])
        self.service.apply(nested['id'],p['plan_id'],p['operation_id'])
        home=Path(nested['path']);self.assertTrue((home/'.agents/skills/alpha/assets/empty').is_dir())
        self.assertEqual((home/'.claude/skills/alpha/assets/image.bin').read_bytes(),bytes(range(256)))
        self.assertIn(b'@../GLOBAL_RULES.md',(home/'CLAUDE.md').read_bytes())
        self.assertEqual(self.service.preview(nested['id'],{'backend':'managed-copy','selected_skills':['common:alpha']})['blockers'],[])

    def test_root_does_not_record_project_private_catalog(self):
        put(self.home/'skills/private/SKILL.md','PRIVATE')
        self.root_init();p=json.loads((self.root/'.wsflow/workspace.json').read_bytes())
        self.assertEqual([s['skill_id'] for s in p['catalog']],['common:alpha'])

    def test_source_changes_mid_apply_stop_and_preserve_user_edit(self):
        self.root_init();p=self.plan();real=ctx.fileio.atomic_write;count=0
        def changed(path,data,**kwargs):
            nonlocal count
            result=real(path,data,**kwargs)
            if not Path(path).is_relative_to(self.root):return result
            count+=1
            if count==1:put(self.root/'skills/alpha/references/a.md','LATE SOURCE USER EDIT')
            return result
        with mock.patch.object(ctx.fileio,'atomic_write',side_effect=changed):r=self.apply(p)
        self.assertEqual(r['status'],'uncertain');self.assertEqual((self.root/'skills/alpha/references/a.md').read_text(),'LATE SOURCE USER EDIT')
        before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p,recover=True)
        self.assertEqual(files(self.root),before)

    def test_plan_expiry_wrong_operation_and_wrong_project_zero_effect(self):
        self.root_init();p=self.plan();before=files(self.root)
        with mock.patch.object(ctx.time,'time',return_value=p['expires_at']+1):
            with self.assertRaises(operations.StateConflict):self.apply(p)
        with self.assertRaises(operations.StateConflict):self.service.apply(self.b['id'],p['plan_id'],p['operation_id'])
        q=self.plan()
        with self.assertRaises(operations.StateConflict):self.service.apply(self.a['id'],p['plan_id'],q['operation_id'])
        self.assertEqual(files(self.root),before)

    def test_override_common_shadow_and_root_uid_clone_block(self):
        self.root_init();p=self.plan();put(self.home/'AGENTS.override.md','OVERRIDE');before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p)
        self.assertEqual(files(self.root),before);self.assertIn('entrypoint_override',self.plan()['blockers'])
        (self.home/'AGENTS.override.md').unlink();put(self.home/'GLOBAL_RULES.md','OTHER COMMON')
        self.assertIn('common_rules_shadowed',self.plan()['blockers'])
        second=self.base/'second root';second.mkdir();self.store.add_root(str(second))
        put(second/'.wsflow/workspace.json',(self.root/'.wsflow/workspace.json').read_bytes())
        self.assertIn('duplicate_root_uid',self.plan()['blockers'])

    def test_readonly_target_permission_is_preflight_zero_effect(self):
        self.initialized();p=self.plan(selected_skills=[]);target=self.home/'.agents/skills/alpha/SKILL.md'
        target.chmod(0o444);self.addCleanup(target.chmod,0o666)
        before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.apply(p)
        self.assertEqual(files(self.root),before)

    @unittest.skipUnless(os.name=='nt','Windows junction boundary')
    def test_junction_escape_and_alias_deploy_are_blocked(self):
        self.initialized();alias=self.home/'alias'
        subprocess.run(['cmd','/c','mklink','/J',str(alias),str(self.home/'.agents/skills')],check=True,capture_output=True)
        self.addCleanup(alias.rmdir)
        put(self.base/'SKILL.md','OVERWRITE')
        r=deploy.apply([str(self.base/'SKILL.md')],[self.a],'alias/alpha',lambda _:None)
        self.assertEqual(r['copied_total'],0);self.assertTrue(r['results'][0]['errors'])
        put(self.home/'skills/local/SKILL.md','LOCAL')
        escape=self.home/'skills/escape'
        subprocess.run(['cmd','/c','mklink','/J',str(escape),str(self.other)],check=True,capture_output=True)
        self.addCleanup(escape.rmdir)
        self.assertTrue(any('sibling_skill_source' in b or 'linked_path' in b for b in self.plan()['blockers']))

    def test_real_git_worktree_is_bound_and_support_package_is_complete(self):
        repo=self.root/'repo';repo.mkdir()
        for args in (['init'],['-c','user.name=probe','-c','user.email=p@invalid','commit','--allow-empty','-m','fixture']):
            subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
        checkout=self.root/'worktree checkout'
        subprocess.run(['git','-C',str(repo),'worktree','add','--detach',str(checkout)],check=True,capture_output=True)
        pr=self.store.add_project(str(checkout));self.root_init()
        p=self.service.preview(pr['id'],{'backend':'managed-copy','selected_skills':['common:alpha']})
        self.assertEqual(p['blockers'],[]);self.service.apply(pr['id'],p['plan_id'],p['operation_id'])
        self.assertEqual((checkout/'.agents/skills/alpha/references/a.md').read_text(),'SUPPORT')
        p=self.service.preview(pr['id'],{'backend':'managed-copy','selected_skills':[]});(checkout/'.git').unlink();put(checkout/'.git','gitdir: '+str(repo/'.git'))
        before=files(self.root)
        with self.assertRaises(operations.StateConflict):self.service.apply(pr['id'],p['plan_id'],p['operation_id'])
        self.assertEqual(files(self.root),before)

    def test_http_guard_payload_scope_and_replay(self):
        from wsflow.server import serve
        httpd=serve(self.store.file,'127.0.0.1',0);self.addCleanup(httpd.server_close);self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        url='http://127.0.0.1:'+str(httpd.server_address[1]);token=httpd.wsflow_token
        def request(path,body=None,auth=True,headers=None):
            h={'Content-Type':'application/json',**({'X-Auth-Token':token} if auth else {}),**(headers or {})}
            req=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
            try:
                with urllib.request.urlopen(req) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as r:
                with r:return r.code,json.load(r)
        endpoint='/api/v1/projects/'+self.a['id']+'/context/plan'
        body={'kind':'root_init','backend':'managed-copy','selected_skills':[],'adopt_existing':True}
        self.assertEqual(request(endpoint,body)[0],400)
        self.assertEqual(request('/api/v1/roots/context/plan',{**body,'project_id':self.a['id']},auth=False)[0],403)
        self.assertEqual(request('/api/v1/roots/context/plan',{**body,'project_id':self.a['id']},headers={'Origin':'https://evil.invalid'})[0],403)
        status,p=request('/api/v1/roots/context/plan',{**body,'project_id':self.a['id']});self.assertEqual(status,200)
        endpoint='/api/v1/context/operations/'+p['operation_id']+'/apply';approval={'plan_id':p['plan_id'],'project_id':self.a['id']}
        self.assertEqual(request(endpoint,{**approval,'actions':[]})[0],409)
        status,result=request(endpoint,approval);self.assertEqual(status,200);self.assertEqual(result['status'],'succeeded')
        self.assertEqual(request(endpoint,approval)[1],result)
        status,op=request('/api/v1/context/operations/'+p['operation_id']+'?project_id='+self.a['id']);self.assertEqual(status,200);self.assertEqual(op['status'],'succeeded')
        self.assertEqual(request('/api/v1/projects/'+self.a['id']+'/context')[0],200)

    def test_opened_handle_boundary_failure_writes_no_instruction_bytes(self):
        self.root_init();p=self.plan();before=files(self.root)
        with mock.patch.object(ctx,'validate_staging_handle',side_effect=ValueError('injected opened-handle escape')):
            r=self.apply(p)
        self.assertEqual(r['status'],'uncertain');self.assertEqual(files(self.root),before)
        self.assertEqual(self.apply(p,recover=True)['status'],'succeeded')

    def test_handle_cannot_target_other_project(self):
        external=self.other/'staged.tmp'
        with external.open('w+b') as stream:
            with self.assertRaises(ValueError):ctx.validate_staging_handle(stream.fileno(),self.home)
            self.assertEqual(external.read_bytes(),b'')

    def test_actual_process_concurrency_and_result_reload(self):
        self.root_init();p=self.plan()
        script='import sys,json;from wsflow.context_projection import Service;from wsflow.store import Store;s=Service(Store(sys.argv[1]));print(json.dumps(s.apply(*sys.argv[2:])))'
        args=['python','-X','utf8','-c',script,str(self.store.file),self.a['id'],p['plan_id'],p['operation_id']]
        first=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        second=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for process in (first,second):
            stdout,stderr=process.communicate(timeout=30);self.assertEqual(process.returncode,0,stderr);self.assertEqual(json.loads(stdout)['status'],'succeeded')
        self.assertEqual((self.home/'AGENTS.md').read_bytes().count(ctx.BEGIN),1)
        reloaded=ctx.Service(Store(self.store.file));self.assertEqual(reloaded.status(self.a['id'],p['operation_id'])['status'],'succeeded')

    def test_common_edit_impact_is_reported_and_rules_never_overwritten(self):
        self.initialized();put(self.root/'GLOBAL_RULES.md','UPDATED COMMON USER')
        setup=self.service.setup(self.a['id']);self.assertEqual(setup['impact_project_ids'],[self.a['id'],self.b['id']])
        self.assertTrue(any(d['kind']=='common_source_changed' for d in setup['diagnostics']))
        self.apply(self.plan(kind='root_init',selected_skills=[]));self.apply(self.plan())
        self.assertEqual((self.root/'GLOBAL_RULES.md').read_text(),'UPDATED COMMON USER')

    def test_response_lost_after_last_write_recovers_without_rewrite(self):
        self.root_init();p=self.plan();original=self.service.ops.finish
        def finish(ident,status,**kwargs):
            if status=='succeeded':raise OSError('injected completion DB loss')
            return original(ident,status,**kwargs)
        with mock.patch.object(self.service.ops,'finish',side_effect=finish):self.assertEqual(self.apply(p)['status'],'uncertain')
        before={p:p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(self.apply(p,recover=True)['status'],'succeeded')
        self.assertEqual({p:p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()},before)

    def test_existing_unbound_profiles_are_explicitly_adopted_with_unknown_fields(self):
        put(self.root/'.wsflow/workspace.json',json.dumps({'schema_version':1,'root_uid':'root-existing','custom':'keep'}))
        put(self.home/'.wsflow/project.json',json.dumps({'schema_version':1,'root_uid':'root-existing','project_uid':'project-existing','custom':'keep','policy_exceptions':[{'id':'EX-1','reason':'keep'}]}))
        self.root_init();self.apply(self.plan())
        profile=json.loads((self.home/'.wsflow/project.json').read_bytes())
        self.assertEqual(profile['project_uid'],'project-existing');self.assertEqual(profile['custom'],'keep');self.assertEqual(profile['policy_exceptions'],[{'id':'EX-1','reason':'keep'}])

    def test_duplicate_original_diagnostics_and_legacy_collision_are_explicit(self):
        self.root_init();put(self.home/'.codex/skills/alpha/SKILL.md','DIFFERENT ORIGINAL')
        diagnostics=self.service.setup(self.a['id'])['diagnostics']
        self.assertTrue(any(d['kind']=='package_comparison' and '내용 다름' in d['policy'] for d in diagnostics))
        self.assertIn('legacy_skill_collision:.codex/skills/alpha',self.plan()['blockers'])
        self.assertEqual((self.home/'.codex/skills/alpha/SKILL.md').read_text(),'DIFFERENT ORIGINAL')

    def test_new_root_rules_stub_becomes_user_owned_and_never_replaced(self):
        (self.root/'GLOBAL_RULES.md').unlink();self.root_init();put(self.root/'GLOBAL_RULES.md','USER COMMON AFTER INIT')
        self.root_init();self.assertEqual((self.root/'GLOBAL_RULES.md').read_text(),'USER COMMON AFTER INIT')

    @unittest.skipUnless(os.name=='nt','Windows parent junction precondition')
    def test_parent_swap_after_preview_has_zero_user_effects(self):
        self.root_init();p=self.plan();alias=self.home/'.agents'
        subprocess.run(['cmd','/c','mklink','/J',str(alias),str(self.other)],check=True,capture_output=True)
        self.addCleanup(alias.rmdir);before=files(self.other)
        with self.assertRaises(operations.StateConflict):self.apply(p)
        self.assertEqual(files(self.other),before);self.assertFalse((self.home/'.wsflow/project.json').exists())

    def test_support_link_cycle_is_blocked_without_execution(self):
        self.root_init()
        if os.name!='nt':self.skipTest('Windows junction cycle fixture')
        cycle=self.root/'skills/alpha/cycle'
        subprocess.run(['cmd','/c','mklink','/J',str(cycle),str(self.root/'skills/alpha')],check=True,capture_output=True)
        self.addCleanup(cycle.rmdir)
        self.assertTrue(any('linked_path' in b for b in self.plan()['blockers']))

    def test_malformed_managed_markers_are_not_replaced(self):
        self.root_init();put(self.home/'AGENTS.md','USER\n<!-- WSFLOW:BEGIN v1 -->\nBROKEN')
        before=files(self.root);self.assertTrue(any('unowned_managed_markers' in b for b in self.plan()['blockers']))
        self.assertEqual(files(self.root),before)

    def test_legacy_package_parent_directory_preserved_on_initialization(self):
        put(self.home/'.claude/skills/existing/SKILL.md','USER EXISTING')
        self.initialized();self.assertEqual((self.home/'.claude/skills/existing/SKILL.md').read_text(),'USER EXISTING')


if __name__=='__main__':unittest.main()
