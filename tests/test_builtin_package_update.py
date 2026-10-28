"""Real package installation, isolated versions and legacy HTTP compatibility."""
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from unittest import mock

from wsflow.module_host import ModuleError, ModuleHost, PACKAGE_NAMES
from wsflow.server import serve


ROOT = Path(__file__).resolve().parents[1]


class BuiltinPackageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow-p055-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base/'project'; self.project.mkdir()
        self.registry = self.base/'registry/projects.json'; self.registry.parent.mkdir()
        self.registry.write_text(json.dumps({'roots':[], 'projects':[{'id':'p','path':str(self.project),'alias':'p','tags':[]}]}), encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR':str(self.base/'userdata'), 'PYTHONDONTWRITEBYTECODE':'1'})
        patch.start(); self.addCleanup(patch.stop)
        self.provider = mock.Mock(side_effect=AssertionError('provider network forbidden'))

    def package(self, ident, version='1.1.0'):
        dest = self.base/'sources'/ident/version
        shutil.copytree(ROOT/'wsflow/modules'/PACKAGE_NAMES[ident], dest,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        path = dest/'module.json'; manifest=json.loads(path.read_text(encoding='utf-8'))
        manifest['version']=version; path.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
        return dest

    @contextmanager
    def running(self):
        server=serve(self.registry,port=0,token='package-test',google_transport=self.provider)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.server=server
        try:
            yield server
        finally:
            server.shutdown();server.server_close();thread.join(5)
            self.provider.assert_not_called()

    def get(self, path, status=200):
        return self.request(path, status=status)

    def request(self, path, method='GET', body=None, status=200):
        data=None if body is None else json.dumps(body).encode('utf-8')
        req=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,
                                   data=data,method=method,
                                   headers={'X-Auth-Token':'package-test','Content-Type':'application/json'})
        try:
            response=urllib.request.urlopen(req,timeout=10)
        except urllib.error.HTTPError as response:
            data=response.read();code=response.code;response.close()
        else:
            with response:
                data=response.read();code=response.status
        self.assertEqual(code,status,data)
        return json.loads(data) if path.find('/assets/') < 0 else data

    def update(self, host, ident, folder):
        plan=host.plan({'action':'update','module_id':ident,'expected_revision':host.snapshot()['revision'],
                        'source_path':str(folder),'trusted':True})
        host.apply(plan['plan_id'])
        return plan

    def test_core_import_does_not_import_business_implementations(self):
        code="import sys; import wsflow.server; assert not any(n.startswith(('wsflow.modules.work_calendar','wsflow.modules.workspace_operations')) for n in sys.modules)"
        result=subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_bad_shipped_manifest_is_isolated_and_a_trusted_update_can_repair_it(self):
        from wsflow import module_host
        original = module_host.inspect_package
        broken = ROOT/'wsflow/modules/work_calendar'
        source = self.package('work-calendar')
        def inspect(path, builtin_id=None):
            if Path(path).resolve()==broken.resolve():
                raise ModuleError('fixture malformed shipped manifest')
            return original(path,builtin_id=builtin_id)
        with mock.patch.object(module_host,'inspect_package',inspect):
            with self.running() as server:
                self.assertFalse(server.module_host.active('work-calendar'))
                self.assertTrue(server.module_host.active('workspace-operations'))
                state=self.get('/api/v1/modules')
                self.assertIn('fixture malformed',next(r for r in state['modules'] if r['id']=='work-calendar')['error'])
                self.get('/api/config')
                self.update(server.module_host,'work-calendar',source)
            with self.running() as server:
                self.assertTrue(server.module_host.active('work-calendar'))
                self.assertEqual(self.get('/api/v1/modules/work-calendar/api/package')['version'],'1.1.0')

    def test_p054_state_is_adopted_without_writing_or_reenabling_removed_module(self):
        host=ModuleHost(self.registry)
        legacy={}
        for row in host.snapshot()['modules']:
            legacy[row['id']]={k:copy.deepcopy(row[k]) for k in ('id','name','version','builtin','installed','enabled','settings','capabilities','requires','data_schema_version','host_api','config_schema')}
            legacy[row['id']]['ui']={}
        legacy['work-calendar'].update(installed=False,enabled=False)
        state={'schema_version':1,'revision':7,'modules':legacy,'applied_plans':[]}
        host.root.mkdir(parents=True);raw=json.dumps(state).encode();host.state_path.write_bytes(raw)
        adopted=ModuleHost(self.registry)
        self.assertFalse(adopted.active('work-calendar'))
        self.assertTrue(adopted.active('workspace-operations'))
        self.assertEqual(adopted.snapshot()['revision'],7)
        self.assertEqual(adopted.state_path.read_bytes(),raw)
        self.assertEqual(adopted.snapshot()['modules'][1]['active_ui']['mode'],'classic')

    def test_work_calendar_backend_and_ui_update_without_workspace_change(self):
        folder=self.package('work-calendar')
        p=folder/'backend.py';text=p.read_text(encoding='utf-8')
        old="return workflow.overview(self.services.store.snapshot()['projects'], day)"
        self.assertEqual(text.count(old),1)
        p.write_text(text.replace(old,"return {**workflow.overview(self.services.store.snapshot()['projects'], day), 'package_marker':'work-1.1'}"),encoding='utf-8')
        ui=folder/'ui.js';ui.write_text(ui.read_text(encoding='utf-8')+'\n// work-package-version-1.1\n',encoding='utf-8')
        registry_before=self.registry.read_bytes();sentinel=self.project/'keep.txt';sentinel.write_bytes(b'user-business-bytes')
        with self.running() as server:
            before={r['id']:r for r in server.module_host.snapshot()['modules']}
            self.update(server.module_host,'work-calendar',folder)
            self.assertNotIn('package_marker',self.get('/api/v1/workflow/status?date=2026-10-28'))
            self.assertNotIn(b'work-package-version-1.1',self.get('/api/v1/modules/work-calendar/assets/ui.js'))
        with self.running() as server:
            self.assertEqual(self.get('/api/v1/workflow/status?date=2026-10-28')['package_marker'],'work-1.1')
            self.assertIn(b'work-package-version-1.1',self.get('/api/v1/modules/work-calendar/assets/ui.js'))
            after={r['id']:r for r in server.module_host.snapshot()['modules']}
            self.assertEqual(after['work-calendar']['active_version'],'1.1.0')
            self.assertEqual(after['workspace-operations']['active_version'],before['workspace-operations']['active_version'])
            self.assertEqual(after['workspace-operations']['package_digest'],before['workspace-operations']['package_digest'])
        self.assertEqual(self.registry.read_bytes(),registry_before)
        self.assertEqual(sentinel.read_bytes(),b'user-business-bytes')

    def test_workspace_backend_updates_without_work_calendar_code_change(self):
        folder=self.package('workspace-operations')
        p=folder/'backend.py';text=p.read_text(encoding='utf-8')
        old='return Routes(None, self.services)._summary(project)'
        self.assertEqual(text.count(old),1)
        p.write_text(text.replace(old,"return {**Routes(None, self.services)._summary(project), 'package_marker':'workspace-1.1'}"),encoding='utf-8')
        with self.running() as server:
            before={r['id']:r for r in server.module_host.snapshot()['modules']}
            self.update(server.module_host,'workspace-operations',folder)
        with self.running() as server:
            row=self.get('/api/projects')['projects'][0]
            self.assertEqual(row['package_marker'],'workspace-1.1')
            after={r['id']:r for r in server.module_host.snapshot()['modules']}
            self.assertEqual(after['work-calendar']['package_digest'],before['work-calendar']['package_digest'])
            self.assertEqual(after['work-calendar']['active_version'],'1.0.0')

    def test_work_calendar_update_uses_its_updated_workflow_domain(self):
        folder=self.package('work-calendar')
        path=folder/'workflow.py'
        path.write_text(path.read_text(encoding='utf-8')+"\n_original_overview=overview\ndef overview(projects, day):\n    return {**_original_overview(projects, day), 'package_domain':'work-domain-1.1'}\n",encoding='utf-8')
        endpoint='/api/v1/workflow/status?date=2026-10-28'
        with self.running() as server:
            self.update(server.module_host,'work-calendar',folder)
            self.assertNotIn('package_domain',self.get(endpoint))
        with self.running():
            self.assertEqual(self.get(endpoint)['package_domain'],'work-domain-1.1')

    def test_workspace_update_uses_its_updated_gitops_domain(self):
        folder=self.package('workspace-operations')
        path=folder/'gitops.py'
        path.write_text(path.read_text(encoding='utf-8')+"\ndef repo_kind(repo):\n    return 'workspace-domain-1.1'\n",encoding='utf-8')
        with self.running() as server:
            self.update(server.module_host,'workspace-operations',folder)
            self.assertNotEqual(self.get('/api/projects')['projects'][0]['git'],'workspace-domain-1.1')
        with self.running():
            self.assertEqual(self.get('/api/projects')['projects'][0]['git'],'workspace-domain-1.1')

    def test_memo_save_uses_the_updated_package_adapter_in_common_writes(self):
        from wsflow import wfhome
        folder=self.package('work-calendar')
        path=folder/'wfhome.py'
        path.write_text(path.read_text(encoding='utf-8')+"\n_original_write_memo=write_memo\ndef write_memo(*args, **kwargs):\n    return {**_original_write_memo(*args, **kwargs), 'package_domain':'memo-domain-1.1'}\n",encoding='utf-8')
        state=wfhome.state_path(self.project);state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'schema_version':wfhome.SCHEMA_VERSION}),encoding='utf-8')
        daily=wfhome.daily_path(self.project,'261028');daily.parent.mkdir(parents=True)
        daily.write_text(wfhome.SKELETON.format(ymd='261028'),encoding='utf-8')
        registry_before=self.registry.read_bytes()
        with self.running() as server:
            self.update(server.module_host,'work-calendar',folder)
        with self.running():
            opened=self.get('/api/projects/p/jira/memo?ymd=261028')
            result=self.request('/api/projects/p/jira/memo','PUT',
                {'ymd':'261028','text':'updated adapter memo','sha':opened['sha'],
                 'document_id':opened['document_id']})
            self.assertEqual(result['package_domain'],'memo-domain-1.1')
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual(self.get('/api/projects/p/jira/memo?ymd=261028')['text'].strip(),'updated adapter memo')
        self.assertEqual(self.registry.read_bytes(),registry_before)

    def test_occupied_server_port_does_not_start_any_business_module(self):
        from wsflow.modules.work_calendar.backend import Service as WorkCalendar
        from wsflow.modules.workspace_operations.backend import Backend as WorkspaceOperations
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0));occupied.listen()
            with mock.patch.object(WorkCalendar,'start') as work_start, \
                 mock.patch.object(WorkspaceOperations,'start') as workspace_start:
                with self.assertRaises(OSError):
                    serve(self.registry,port=occupied.getsockname()[1],token='package-test',
                          google_transport=self.provider)
                work_start.assert_not_called()
                workspace_start.assert_not_called()
        self.provider.assert_not_called()

    def test_builtin_schema_change_and_cross_id_update_are_rejected(self):
        host=ModuleHost(self.registry);folder=self.package('work-calendar')
        p=folder/'module.json';manifest=json.loads(p.read_text(encoding='utf-8'));manifest['data_schema_version']=2
        p.write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaises(ModuleError):self.update(host,'work-calendar',folder)
        with self.assertRaises(ModuleError):self.update(host,'workspace-operations',folder)
        self.assertFalse(host.state_path.exists())

    def test_bundled_versions_are_pinned_for_restore_after_update(self):
        host=ModuleHost(self.registry);folder=self.package('work-calendar')
        self.update(host,'work-calendar',folder)
        history=json.loads((host.root/'history/0.json').read_text(encoding='utf-8'))
        for row in history['modules'].values():
            self.assertEqual(row['source_kind'],'managed')
            self.assertTrue((host.root/row['package_path']/'module.json').is_file())
        plan=host.plan({'action':'restore','target_revision':0,'expected_revision':1});host.apply(plan['plan_id'])
        with self.running() as server:
            versions={r['id']:r['active_version'] for r in server.module_host.snapshot()['modules']}
            self.assertEqual(versions,{'work-calendar':'1.0.0','workspace-operations':'1.0.0'})


if __name__=='__main__':
    unittest.main()
