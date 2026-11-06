"""UF-ST-02: actual module facade validates tool targets before host promotion."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
from http.server import ThreadingHTTPServer
import urllib.error
import urllib.request
import unittest
from unittest import mock

from wsflow import scanner, writer_admission
from wsflow.module_host import ModuleHost
from wsflow.module_services import ApiError, ModuleServices
from wsflow.modules.workspace_operations.backend import create_module
from wsflow.operations import StateConflict
from wsflow.store import Store
from wsflow.server import make_handler


class ScannerHostWriterTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='wsflow-scanner-host-');self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        self.project=self.base/'project';self.project.mkdir();self.registry=self.base/'registry.json'
        self.registry.write_text(json.dumps(dict(roots=[],projects=[dict(id='p',path=str(self.project),alias='P',tags=[])])),encoding='utf8')
        patch=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata')});patch.start();self.addCleanup(patch.stop)

    def host(self):
        host=ModuleHost(self.registry);services=ModuleServices(Store(self.registry),host)
        backend=create_module({'services':services});request=SimpleNamespace(_project=services.project)
        return host,backend,request

    def test_host_invalid_userdata_rejects_before_control_files_or_spawn(self):
        for where in ('same','inside'):
            for index,path in enumerate(('/api/projects/p/rules/run','/api//projects/p/rules/run/')):
                self.project=self.base/('project-'+where+'-'+str(index));self.project.mkdir()
                self.registry.write_text(json.dumps(dict(roots=[],projects=[dict(id='p',path=str(self.project),alias='P',tags=[])])),encoding='utf8')
                data=self.project if where=='same' else self.project/'userdata'
                with self.subTest(where=where,path=path),mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(data)}):
                    _,backend,request=self.host()
                    with mock.patch.object(scanner.subprocess,'run') as calls,self.assertRaises(StateConflict):backend.dispatch(request,'POST',path,{},dict(tool='graph_checks'))
                    self.assertEqual(0,calls.call_count);self.assertEqual([],list(self.project.rglob('*')))

    def test_host_actual_tool_joins_one_root_and_preserves_result(self):
        script=self.project/'tools/graph_checks.py';script.parent.mkdir()
        script.write_text("import json,os\nfrom pathlib import Path\np=Path.cwd();data=Path(os.environ['WSFLOW_DATA_DIR'])\n(p/'observed.json').write_text((data/'writer-admission/state.json').read_text(),encoding='utf8')\nprint('host 검사')\n",encoding='utf8')
        host,backend,request=self.host();domain=host._writer_domain
        with domain.barrier(0):pass
        result=backend.dispatch(request,'POST','/api/projects/p/rules/run',{},dict(tool='graph_checks'))
        self.assertEqual(dict(ok=True,returncode=0,stdout='host 검사\n',stderr='',command='python '+str(script)),result)
        during=json.loads((self.project/'observed.json').read_text());after=domain.observe()
        self.assertEqual(1,during['epoch']);self.assertIsNotNone(during['active']);self.assertEqual(2,after['epoch']);self.assertIsNone(after['active'])
        self.assertEqual(during['domain_id'],after['domain_id']);self.assertEqual(0,host.runtime_snapshot()['in_flight'])

    def test_host_barrier_refuses_tool_before_spawn_and_keeps_project(self):
        script=self.project/'tools/graph_checks.py';script.parent.mkdir();script.write_text("raise AssertionError('must not launch')",encoding='utf8')
        host,backend,request=self.host();before=script.read_bytes()
        with host._writer_domain.barrier(0),mock.patch.object(scanner.subprocess,'run') as calls:
            with self.assertRaises(writer_admission.WriterUnavailable):backend.dispatch(request,'POST','/api/projects/p/rules/run',{},dict(tool='graph_checks'))
            self.assertEqual(0,calls.call_count)
        self.assertEqual(before,script.read_bytes());self.assertEqual(0,host._writer_domain.observe()['epoch'])

    def test_unknown_project_refuses_before_writer_promotion(self):
        host,backend,request=self.host();domain=host._writer_domain
        with domain.barrier(0):pass
        before=domain.state_path.read_bytes()
        with mock.patch.object(scanner.subprocess,'run') as calls,self.assertRaises(ApiError) as caught:backend.dispatch(request,'POST','/api/projects/absent/rules/run',{},dict(tool='graph_checks'))
        self.assertEqual(404,caught.exception.status);self.assertEqual(0,calls.call_count);self.assertEqual(before,domain.state_path.read_bytes());self.assertEqual([],list(self.project.rglob('*')))

    def test_loopback_http_preflight_precedes_outer_host_activity(self):
        for where in ('same','inside'):
            for index,path in enumerate(('/api/projects/p/rules/run','/api//projects/p/rules/run/')):
                self.project=self.base/('http-'+where+'-'+str(index));self.project.mkdir()
                self.registry.write_text(json.dumps(dict(roots=[],projects=[dict(id='p',path=str(self.project),alias='P',tags=[])])),encoding='utf8')
                data=self.project if where=='same' else self.project/'userdata'
                with self.subTest(where=where,path=path),mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(data)}):
                    host,backend,request=self.host()
                    # Bind the actual owned workspace backend without starting unrelated workers.
                    services=backend.services;host.services=services;host.backends['workspace-operations']=backend;host.started_backends.add('workspace-operations')
                    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(services.store,'scanner-test',modules=host))
                    # Handler binding is an earlier host activity; preserve its existing lock.
                    before={str(p.relative_to(self.project)):p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
                    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    try:
                        req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,data=b'{"tool":"graph_checks"}',method='POST',headers={'Content-Type':'application/json','X-Auth-Token':'scanner-test'})
                        with mock.patch.object(scanner.subprocess,'run') as calls,mock.patch.object(writer_admission.fileio,'locked',wraps=writer_admission.fileio.locked) as controls:
                            try:response=urllib.request.urlopen(req,timeout=5)
                            except urllib.error.HTTPError as error:response=error
                            with response:status=response.status;body=json.loads(response.read())
                            self.assertEqual(409,status);self.assertIn('작업 DB는 WF_HOME 밖',body['error']);self.assertEqual(0,calls.call_count)
                            self.assertEqual(0,controls.call_count)
                        self.assertEqual(before,{str(p.relative_to(self.project)):p.read_bytes() for p in self.project.rglob('*') if p.is_file()});self.assertEqual(0,host.runtime_snapshot()['in_flight'])
                    finally:
                        server.shutdown();server.server_close();thread.join(5);self.assertFalse(thread.is_alive())

    def test_loopback_valid_tool_joins_one_root_and_preserves_real_effect(self):
        script=self.project/'tools/graph_checks.py';script.parent.mkdir()
        script.write_text("import json,os\nfrom pathlib import Path\np=Path.cwd();data=Path(os.environ['WSFLOW_DATA_DIR'])\n(p/'http_effect.json').write_text((data/'writer-admission/state.json').read_text(),encoding='utf8')\nprint('HTTP 검사')\n",encoding='utf8')
        host,backend,_=self.host();services=backend.services;host.services=services;host.backends['workspace-operations']=backend;host.started_backends.add('workspace-operations')
        with host._writer_domain.barrier(0):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(services.store,'scanner-test',modules=host));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/projects/p/rules/run',data=b'{"tool":"graph_checks"}',method='POST',headers={'Content-Type':'application/json','X-Auth-Token':'scanner-test'})
            with urllib.request.urlopen(req,timeout=5) as response:self.assertEqual(200,response.status);result=json.loads(response.read())
            self.assertEqual(dict(ok=True,returncode=0,stdout='HTTP 검사\n',stderr='',command='python '+str(script)),result)
            during=json.loads((self.project/'http_effect.json').read_text());after=host._writer_domain.observe()
            self.assertEqual(1,during['epoch']);self.assertIsNotNone(during['active']);self.assertEqual(2,after['epoch']);self.assertIsNone(after['active']);self.assertEqual(0,host.runtime_snapshot()['in_flight'])
        finally:
            server.shutdown();server.server_close();thread.join(5);self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
