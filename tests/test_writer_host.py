"""C3-2A real host boundaries; a cooperative lease never grants a snapshot."""
from contextlib import contextmanager
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_runtime_quiescence import RuntimeFixture
from wsflow.module_host import ModuleError, ModuleHost
from wsflow.module_services import ModuleServices
from wsflow.runtime_activity import ActivityGate, ActivityUnavailable
from wsflow.server import serve
from wsflow.store import Store


class GateAroundTests(unittest.TestCase):
    def test_context_covers_root_and_nested_completion_once(self):
        seen=[]
        @contextmanager
        def around():
            seen.append('enter')
            try:yield
            finally:seen.append('exit')
        gate=ActivityGate(around=around)
        with gate():
            with gate():
                seen.append('effect')
                self.assertEqual(gate.snapshot()['in_flight'],1)
            self.assertEqual(seen,['enter','effect'])
        self.assertEqual(seen,['enter','effect','exit'])
        self.assertEqual(gate.snapshot()['in_flight'],0)

    def test_failed_external_entry_releases_gate_count(self):
        @contextmanager
        def denied():
            raise ActivityUnavailable('writer_domain_busy')
            yield
        gate=ActivityGate(around=denied)
        with self.assertRaises(ActivityUnavailable):
            with gate():self.fail('effect admitted')
        self.assertFalse(gate.in_activity())
        self.assertEqual(gate.snapshot(),{'accepting':True,'in_flight':0})
        self.assertTrue(gate.pause(0))

    def test_invalid_around_factory_is_rejected(self):
        for value in (False,1,[],object()):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):ActivityGate(around=value)


class WriterHostTests(RuntimeFixture,unittest.TestCase):
    def state(self):
        return json.loads((self.base/'userdata/writer-admission/state.json').read_bytes())

    def test_read_activity_does_not_write_admission_state(self):
        def files():return {str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        before=files()
        with self.services.activity():
            self.services.summary(self.services.project('p'))
        self.assertEqual(files(),before)

    def test_writing_promotes_outer_host_root_until_after_return(self):
        with self.services.activity():
            with self.services.writing():
                begun=self.state()
                self.assertIsNotNone(begun['active'])
            self.assertEqual(self.state(),begun)
            with self.services.writing():self.assertEqual(self.state(),begun)
        ended=self.state()
        self.assertIsNone(ended['active'])
        self.assertEqual(ended['epoch'],begun['epoch']+1)

    def test_verified_stop_is_required_and_lease_is_lexical(self):
        before=self.host.runtime_snapshot()
        with self.assertRaises(ModuleError):
            with self.host.writer_barrier(0):self.fail('running host barrier')
        self.assertEqual(self.host.runtime_snapshot(),before)
        worker=self.host.backends['work-calendar'].google.refresh_thread
        self.assertTrue(self.host.quiesce(2,2)['stop_verified'])
        self.assertFalse(worker.is_alive())
        with self.host.writer_barrier(0) as lease:
            view=lease.view()
            for name in ('snapshot_ready','activation_ready','coverage_complete'):
                self.assertIs(view[name],False)
            self.assertEqual(view['scope'],'cooperative_writer_domain')
        with self.assertRaises(ActivityUnavailable):lease.view()

    def test_raw_domain_reentrant_control_does_not_change_host(self):
        from wsflow.writer_admission import default_domain
        before=self.host.runtime_snapshot()
        with default_domain().writing():
            with self.assertRaises(ActivityUnavailable):self.host.quiesce(0,0)
            self.assertFalse(self.host.close(0))
            with self.assertRaises(ActivityUnavailable):
                with self.host.writer_barrier(0):self.fail('reentrant barrier')
        self.assertEqual(self.host.runtime_snapshot(),before)

    def test_raw_barrier_reentrant_control_does_not_change_host(self):
        from wsflow.writer_admission import default_domain
        before=self.host.runtime_snapshot()
        with default_domain().barrier() as lease:
            with self.assertRaises(ActivityUnavailable):self.host.quiesce(0,0)
            self.assertFalse(self.host.close(0))
            with self.assertRaises(ActivityUnavailable):
                with self.host.writer_barrier(0):self.fail('reentrant barrier')
            self.assertFalse(lease.view()['snapshot_ready'])
        self.assertEqual(self.host.runtime_snapshot(),before)

    def test_barrier_holds_host_control_and_releases_it_after_exception(self):
        self.host.quiesce(2,2)
        with self.assertRaisesRegex(ValueError,'fixture'):
            with self.host.writer_barrier(0):
                errors=[]
                def control():
                    try:self.host.quiesce(0,0)
                    except BaseException as exc:errors.append(exc)
                thread=threading.Thread(target=control,daemon=True);thread.start();thread.join(2)
                self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1)
                self.assertIsInstance(errors[0],ModuleError)
                raise ValueError('fixture')
        self.assertTrue(self.host.quiesce(2,2)['stop_verified'])

    def test_builtin_non_get_dispatch_promotes_before_route_effect(self):
        for ident,module in (('work-calendar','wsflow.modules.work_calendar.backend'),
                             ('workspace-operations','wsflow.modules.workspace_operations.backend')):
            with self.subTest(module=ident):
                seen=[]
                def route(*args):
                    seen.append(self.state()['active'])
                    marker=self.base/(ident+'.effect');marker.write_text('completed')
                    return marker.read_text()
                with mock.patch(module+'.Routes.dispatch',side_effect=route):
                    result=self.host.backends[ident].dispatch(SimpleNamespace(),'POST','/synthetic',{}, {})
                self.assertEqual(result,'completed');self.assertEqual(len(seen),1)
                self.assertIsNotNone(seen[0]);self.assertIsNone(self.state()['active'])

    def test_module_plan_and_apply_are_writers(self):
        seen=[]
        from wsflow import fileio
        original=fileio.atomic_write
        def write(path,raw,*args,**kwargs):
            if str(path).startswith(str(self.host.root)):
                seen.append(self.state()['active'])
            return original(path,raw,*args,**kwargs)
        with mock.patch.object(fileio,'atomic_write',side_effect=write):
            plan=self.host.plan({'action':'disable','module_id':'work-calendar','expected_revision':0})
            self.host.apply(plan['plan_id'])
        self.assertTrue(seen);self.assertTrue(all(item is not None for item in seen))
        self.assertIsNone(self.state()['active'])

    def test_legacy_services_writing_context_is_optional(self):
        services=ModuleServices(Store(self.registry),SimpleNamespace())
        with services.writing():pass

    def test_installed_extension_handle_promotes_even_get_before_actual_file_effect(self):
        folder=self.base/'extension';folder.mkdir()
        (folder/'module.json').write_text(json.dumps({'schema_version':1,'id':'erp-writer',
            'name':'synthetic local writer','version':'1.0.0','host_api':1,'data_schema_version':1,
            'capabilities':['erp'],'requires':[],'entrypoint':'backend.py:create_module'}))
        (folder/'backend.py').write_text('import json\nfrom pathlib import Path\nclass Module:\n'
            ' def handle(self,method,rest,query,body):\n'
            '  active=json.loads(Path(body["state"]).read_bytes())["active"]\n'
            '  Path(body["effect"]).write_text("effect")\n'
            '  return {"active":active}\n'
            ' def stop(self): return True\n'
            'def create_module(context): return Module()\n')
        plan=self.host.plan({'action':'install','module_id':'erp-writer','expected_revision':0,
                             'trusted':True,'source_path':str(folder)})
        self.host.apply(plan['plan_id']);self.assertTrue(self.host.close())
        active=ModuleHost(self.registry)
        services=ModuleServices(Store(self.registry),active,self.provider)
        active.bind_services(services);active.start_backends();self.addCleanup(active.close)
        marker=self.base/'extension-effect'
        result=active.extension_request('GET','erp-writer',['run'],{},
            {'state':str(self.base/'userdata/writer-admission/state.json'),'effect':str(marker)})
        self.assertEqual(marker.read_text(),'effect');self.assertIsNotNone(result['active'])
        self.assertIsNone(self.state()['active'])


class WriterHTTPTests(RuntimeFixture,unittest.TestCase):
    def request(self,url,path,method='GET',body=None):
        data=None if body is None else json.dumps(body).encode()
        request=urllib.request.Request(url+path,data=data,method=method,
            headers={'X-Auth-Token':'writer-test','Content-Type':'application/json'})
        try:response=urllib.request.urlopen(request,timeout=9)
        except urllib.error.HTTPError as response:
            code=response.code;raw=response.read();response.close()
        else:
            with response:code=response.status;raw=response.read()
        return code,raw

    def test_peer_host_barrier_refuses_real_http_memo_and_registry_before_effect(self):
        server=serve(self.registry,port=0,token='writer-test',google_transport=self.provider)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            self.host.quiesce(2,2)
            before=self.registry.read_bytes()
            with self.host.writer_barrier(1) as lease:
                # This core boundary has no memo/registry lower-layer hook that
                # could hide a missing host-domain admission bridge.
                with mock.patch('wsflow.server.pickdir.pick_folder',return_value='synthetic') as picker:
                    code,raw=self.request(url,'/api/pick-folder','POST',{})
                    self.assertEqual(code,503,raw);picker.assert_not_called()
                code,raw=self.request(url,'/api/projects/p/jira/memo?ymd=261030')
                self.assertEqual(code,503,raw)
                self.assertFalse((self.project/'daily/261030.md').exists())
                code,raw=self.request(url,'/api/projects','POST',{'path':str(self.project)})
                self.assertEqual(code,503,raw);self.assertEqual(self.registry.read_bytes(),before)
                for path in ('/api/config','/api/v1/modules','/','/api/v1/modules/work-calendar/assets/ui.js'):
                    code,raw=self.request(url,path)
                    self.assertEqual(code,200,(path,raw));self.assertTrue(raw)
                self.assertFalse(lease.view()['snapshot_ready'])
            code,raw=self.request(url,'/api/projects/p/jira/memo?ymd=261030')
            self.assertEqual(code,200,raw);self.assertTrue((self.project/'daily/261030.md').is_file())
            self.assertEqual(self.registry.read_bytes(),before)
        finally:
            server.shutdown();server.server_close();thread.join(3)
            self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
