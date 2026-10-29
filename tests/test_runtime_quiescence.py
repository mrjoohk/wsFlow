"""C3-1 host-mediated admission, drain and owned lifecycle acceptance."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from types import SimpleNamespace

from wsflow import wfhome
from wsflow.module_host import ModuleHost, ModuleError
from wsflow.module_services import ModuleServices
from wsflow.runtime_activity import ActivityUnavailable
from wsflow.server import serve
from wsflow.store import Store


class RuntimeFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='wsflow-c3-host-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.registry=self.base/'registry/projects.json';self.registry.parent.mkdir()
        self.project=self.base/'project';self.project.mkdir()
        (self.project/'.workflow').mkdir()
        (self.project/'.workflow/state.json').write_text(json.dumps({'schema_version':4,'project_key':'SYN','roots':{}}))
        self.registry.write_text(json.dumps({'roots':[],'projects':[{'id':'p','path':str(self.project),'alias':'synthetic'}]}))
        env=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata'),
            'WSFLOW_GOOGLE_CLIENT_FILE':str(self.base/'no-client.json')})
        env.start();self.addCleanup(env.stop)
        self.provider=mock.Mock(side_effect=AssertionError('real provider forbidden'))
        self.host=ModuleHost(self.registry)
        self.services=ModuleServices(Store(self.registry),self.host,self.provider)
        self.host.bind_services(self.services);self.host.start_backends()
        self.addCleanup(self.host.close)
        self.addCleanup(self.provider.assert_not_called)

    def held_activity(self):
        entered,release=threading.Event(),threading.Event()
        errors=[]
        def work():
            try:
                with self.services.activity():
                    entered.set()
                    if not release.wait(5):raise AssertionError('fixture release timed out')
                    with self.services.activity():pass
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=work,daemon=True);thread.start()
        self.assertTrue(entered.wait(2))
        def finish():
            release.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertFalse(errors,errors)
        self.addCleanup(finish)
        return release,thread

class RuntimeHostTests(RuntimeFixture, unittest.TestCase):
    def test_stop_order_and_shared_remaining_budget(self):
        observations=[]
        expected=['workspace-operations','work-calendar']
        ticks=iter((10.0,10.0,10.125))
        def stop(ident):
            def invoke(timeout):
                observations.append((ident,timeout))
                return True
            return invoke
        with mock.patch('wsflow.module_host.time',SimpleNamespace(monotonic=lambda:next(ticks))), \
                mock.patch.object(self.host.backends[expected[0]],'stop',side_effect=stop(expected[0])), \
                mock.patch.object(self.host.backends[expected[1]],'stop',side_effect=stop(expected[1])):
            self.assertTrue(self.host.quiesce(0,.2)['stop_verified'])
        self.assertEqual([ident for ident,_ in observations],expected)
        self.assertAlmostEqual(observations[0][1],.2)
        self.assertAlmostEqual(observations[1][1],.075)

    def test_runtime_observation_cannot_mix_open_admission_with_verified_stop(self):
        captured,release,control_entered=threading.Event(),threading.Event(),threading.Event()
        completed=threading.Event();outcome={}
        original_snapshot=self.host._activity.snapshot
        original_state=self.host._runtime_state
        def observed_snapshot():
            value=original_snapshot()
            if threading.current_thread() is observer:
                captured.set()
                if not release.wait(3):raise AssertionError('observation release timed out')
            return value
        def state(*args,**kwargs):
            if threading.current_thread() is controller:control_entered.set()
            return original_state(*args,**kwargs)
        def observe():
            try:outcome['observed']=self.host.runtime_snapshot()
            except BaseException as error:outcome['observer_error']=error
        def control():
            try:outcome['stopped']=self.host.quiesce(2,2)
            except BaseException as error:outcome['controller_error']=error
            finally:completed.set()
        observer=threading.Thread(target=observe,daemon=True)
        controller=threading.Thread(target=control,daemon=True)
        try:
            with mock.patch.object(self.host._activity,'snapshot',side_effect=observed_snapshot), \
                    mock.patch.object(self.host,'_runtime_state',side_effect=state):
                observer.start();self.assertTrue(captured.wait(2))
                controller.start();self.assertTrue(control_entered.wait(2))
                # Permit the previously broken transition to finish while the
                # observation is held. A serialized observation blocks it here.
                completed.wait(.3)
                release.set();observer.join(3);controller.join(3)
        finally:
            release.set();observer.join(3)
            if controller.ident is not None:controller.join(3)
        self.assertFalse(observer.is_alive());self.assertFalse(controller.is_alive())
        self.assertNotIn('observer_error',outcome);self.assertNotIn('controller_error',outcome)
        value=outcome['observed']
        self.assertFalse(value['accepting'] and value['stop_verified'],value)
        if value['stop_verified']:self.assertEqual(value['phase'],'stopped')
        self.assertTrue(outcome['stopped']['stop_verified'])

    def test_ownership_changed_by_admitted_work_is_rechecked_after_drain(self):
        backend=self.host.backends['work-calendar']
        original_runtime=backend.google
        self.addCleanup(original_runtime.close)
        borrowed=mock.Mock()
        preflight_done=threading.Event();outcome={}
        original=self.host._quiescence_participants
        def preflight():
            result=original()
            preflight_done.set()
            return result
        def control():
            try:outcome['value']=self.host.quiesce(2,2)
            except BaseException as error:outcome['error']=error
        thread=threading.Thread(target=control,daemon=True)
        with mock.patch.object(self.host,'_quiescence_participants',side_effect=preflight):
            with self.services.activity():
                thread.start()
                self.assertTrue(preflight_done.wait(2))
                backend.use_runtime(borrowed)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get('error'),ModuleError,outcome)
        self.assertNotIn('value',outcome)
        self.assertFalse(self.host.runtime_snapshot()['stop_verified'])
        self.assertFalse(self.host.runtime_snapshot()['accepting'])
        self.assertEqual(self.host.runtime_snapshot()['stop_results'],{})
        self.assertTrue(original_runtime.refresh_thread.is_alive())
        borrowed.close.assert_not_called()

    def test_owned_runtime_quiesces_without_promoting_snapshot(self):
        before=self.registry.read_bytes()
        worker=self.host.backends['work-calendar'].google.refresh_thread
        result=self.host.quiesce(drain_timeout=2,stop_timeout=2)
        self.assertEqual(result['phase'],'stopped')
        self.assertTrue(result['stop_verified'])
        self.assertFalse(result['snapshot_ready'])
        self.assertFalse(result['accepting'])
        self.assertFalse(worker.is_alive())
        self.assertEqual(before,self.registry.read_bytes())
        with self.assertRaises(ModuleError):self.host.resume_activity()
        with self.assertRaises(ActivityUnavailable):
            with self.services.activity():pass

    def test_timeout_starts_no_stop_and_existing_nested_work_finishes(self):
        release,thread=self.held_activity()
        work=self.host.backends['work-calendar']
        with mock.patch.object(work,'stop',wraps=work.stop) as stopped:
            result=self.host.quiesce(drain_timeout=0,stop_timeout=0)
            self.assertEqual(result['phase'],'draining')
            self.assertEqual(result['in_flight'],1)
            self.assertFalse(result['stop_verified']);stopped.assert_not_called()
            with self.assertRaises(ActivityUnavailable):
                with self.services.activity():pass
            release.set();thread.join(3);self.assertFalse(thread.is_alive())
            self.assertEqual(self.host.runtime_snapshot()['in_flight'],0)
            self.assertTrue(self.host.quiesce(2,2)['stop_verified'])

    def test_pre_stop_timeout_can_be_explicitly_resumed(self):
        release,thread=self.held_activity()
        self.host.quiesce(0,0)
        self.assertTrue(self.host.resume_activity()['accepting'])
        with self.services.activity():pass
        release.set();thread.join(3);self.assertFalse(thread.is_alive())

    def test_control_from_an_admitted_thread_leaves_runtime_open(self):
        with self.services.activity():
            with self.assertRaises(ActivityUnavailable):self.host.quiesce(0,0)
            self.assertTrue(self.host.runtime_snapshot()['accepting'])
            self.assertEqual(self.host.runtime_snapshot()['in_flight'],1)
        self.assertEqual(self.host.runtime_snapshot()['in_flight'],0)

    def test_failed_stop_keeps_generation_closed_and_can_be_rechecked(self):
        work=self.host.backends['work-calendar']
        with mock.patch.object(work,'stop',return_value=False):
            result=self.host.quiesce(2,0)
        self.assertEqual(result['phase'],'stop_failed')
        self.assertFalse(result['stop_verified'])
        self.assertIn('work-calendar',self.host.started_backends)
        with self.assertRaises(ModuleError):self.host.resume_activity()
        self.assertTrue(self.host.quiesce(2,2)['stop_verified'])
        self.assertNotIn('work-calendar',self.host.started_backends)

    def test_stop_exception_is_not_completion_and_retry_clears_stop_error(self):
        work=self.host.backends['work-calendar']
        with mock.patch.object(work,'stop',side_effect=RuntimeError('synthetic stop failure')):
            result=self.host.quiesce(2,0)
        self.assertFalse(result['stop_verified'])
        self.assertIn('stop failed',self.host.errors['work-calendar'])
        self.assertTrue(self.host.quiesce(2,2)['stop_verified'])
        self.assertNotIn('work-calendar',self.host.errors)

    def test_implicit_stop_result_is_not_strict_true(self):
        work=self.host.backends['work-calendar']
        with mock.patch.object(work,'stop',return_value=None):
            self.assertFalse(self.host.quiesce(2,0)['stop_verified'])
        self.assertIn('work-calendar',self.host.started_backends)

    def test_caller_owned_runtime_is_rejected_before_pause_or_stop(self):
        work=self.host.backends['work-calendar']
        work.google.close()
        caller=mock.Mock()
        work.use_runtime(caller)
        with self.assertRaises(ModuleError):self.host.quiesce(0,0)
        self.assertTrue(self.host.runtime_snapshot()['accepting'])
        caller.close.assert_not_called()

    def test_unverified_package_digest_is_rejected_before_pause(self):
        self.host._active['modules']['work-calendar']['package_digest']='not-the-current-bundled-digest'
        work=self.host.backends['work-calendar']
        with mock.patch.object(work,'stop',wraps=work.stop) as stopped:
            with self.assertRaises(ModuleError):self.host.quiesce(0,0)
            stopped.assert_not_called()
        self.assertTrue(self.host.runtime_snapshot()['accepting'])

    def test_invalid_timeouts_do_not_change_admission(self):
        for bad in (True,-1,31,float('nan'),float('inf'),'1'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):self.host.quiesce(bad,0)
                with self.assertRaises(ValueError):self.host.quiesce(0,bad)
                self.assertTrue(self.host.runtime_snapshot()['accepting'])

    def test_direct_configuration_writes_are_blocked_during_pause(self):
        release,thread=self.held_activity()
        self.host.quiesce(0,0)
        with self.assertRaises(ActivityUnavailable):
            self.host.plan({'action':'disable','module_id':'work-calendar','expected_revision':0})
        with self.assertRaises(ActivityUnavailable):self.host.apply('not-issued')
        self.assertFalse(self.host.state_path.exists())
        release.set();thread.join(3);self.assertFalse(thread.is_alive())

    def test_close_timeout_defers_cleanup(self):
        release,thread=self.held_activity()
        work=self.host.backends['work-calendar']
        with mock.patch.object(work,'stop',wraps=work.stop) as stopped:
            self.assertFalse(self.host.close(drain_timeout=0));stopped.assert_not_called()
        release.set();thread.join(3);self.assertFalse(thread.is_alive())
        self.assertTrue(self.host.close())

    def test_concurrent_control_is_rejected_without_deadlock(self):
        release,thread=self.held_activity()
        results=[]
        controller=threading.Thread(target=lambda:results.append(self.host.quiesce(3,2)),daemon=True)
        controller.start()
        try:
            deadline=time.monotonic()+2
            while self.host.runtime_snapshot()['accepting'] and time.monotonic()<deadline:
                time.sleep(.001)
            self.assertFalse(self.host.runtime_snapshot()['accepting'])
            with self.assertRaises(ModuleError):self.host.resume_activity()
            with self.assertRaises(ModuleError):self.host.quiesce(0,0)
        finally:
            release.set();thread.join(3);controller.join(4)
        self.assertFalse(controller.is_alive());self.assertFalse(thread.is_alive())
        self.assertEqual(len(results),1)
        self.assertTrue(results[0]['stop_verified'])

    def test_active_extension_is_unsupported_before_any_stop(self):
        folder=self.base/'erp';folder.mkdir()
        marker=self.base/'erp-stopped'
        manifest={'schema_version':1,'id':'erp-local','name':'synthetic ERP','version':'1.0.0',
            'host_api':1,'data_schema_version':1,'capabilities':['erp'],'requires':[],
            'entrypoint':'backend.py:create_module'}
        (folder/'module.json').write_text(json.dumps(manifest))
        (folder/'backend.py').write_text("from pathlib import Path\nclass Module:\n"
            " def handle(self,*args): return {'synthetic':True}\n"
            f" def stop(self): Path({str(marker)!r}).write_text('stopped'); return True\n"
            "def create_module(context): return Module()\n")
        plan=self.host.plan({'action':'install','module_id':'erp-local','expected_revision':0,
                             'source_path':str(folder),'trusted':True})
        self.host.apply(plan['plan_id'])
        active=ModuleHost(self.registry)
        services=ModuleServices(Store(self.registry),active,self.provider)
        active.bind_services(services);active.start_backends();self.addCleanup(active.close)
        work=active.backends['work-calendar']
        with mock.patch.object(work,'stop',wraps=work.stop) as stopped:
            with self.assertRaises(ModuleError):active.quiesce(0,0)
            stopped.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertTrue(active.runtime_snapshot()['accepting'])


class RuntimeHTTPTests(RuntimeFixture, unittest.TestCase):
    def request(self,path,method='GET',body=None):
        data=json.dumps(body).encode() if body is not None else None
        request=urllib.request.Request(self.url+path,data=data,method=method,
            headers={'X-Auth-Token':'c3-test','Content-Type':'application/json'})
        try:response=urllib.request.urlopen(request,timeout=5)
        except urllib.error.HTTPError as response:
            code=response.code;raw=response.read();response.close()
        else:
            with response:code=response.status;raw=response.read()
        return code,json.loads(raw)

    def test_real_http_get_writer_and_registry_are_blocked_while_control_reads_remain(self):
        server=serve(self.registry,port=0,token='c3-test',google_transport=self.provider)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.url=f'http://127.0.0.1:{server.server_port}'
        entered,release=threading.Event(),threading.Event();responses=[]
        original=wfhome.read_memo
        def blocked(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('memo fixture timeout')
            return original(*args,**kwargs)
        reader=None
        try:
            with mock.patch.object(wfhome,'read_memo',side_effect=blocked):
                reader=threading.Thread(target=lambda:responses.append(self.request('/api/projects/p/jira/memo?ymd=261029')),daemon=True)
                reader.start();self.assertTrue(entered.wait(2))
                before=self.registry.read_bytes()
                state=server.module_host.quiesce(0,0)
                self.assertEqual(state['in_flight'],1)
                code,value=self.request('/api/projects/p/jira/memo?ymd=261030')
                self.assertEqual(code,503,value)
                self.assertFalse((self.project/'daily/261030.md').exists())
                self.assertEqual(self.request('/api/projects','POST',{'path':str(self.project)})[0],503)
                self.assertEqual(self.request('/api/v1/modules/plan','POST',{'action':'disable','expected_revision':0,'module_id':'work-calendar'})[0],503)
                with mock.patch('wsflow.server.pickdir.pick_folder',return_value='synthetic-folder') as folder, \
                        mock.patch('wsflow.server.pickdir.pick_files',return_value=['synthetic-file']) as files:
                    self.assertEqual(self.request('/api/pick-folder','POST',{})[0],503)
                    self.assertEqual(self.request('/api/pick-files','POST',{})[0],503)
                    folder.assert_not_called();files.assert_not_called()
                self.assertEqual(self.registry.read_bytes(),before)
                self.assertEqual(self.request('/api/config')[0],200)
                code,status=self.request('/api/v1/modules')
                self.assertEqual(code,200)
                self.assertFalse(status['runtime_activity']['accepting'])
                self.assertFalse(status['runtime_activity']['snapshot_ready'])
                for path in ('/','/api/v1/modules/work-calendar/assets/ui.js'):
                    request=urllib.request.Request(self.url+path,headers={'X-Auth-Token':'c3-test'})
                    with urllib.request.urlopen(request,timeout=3) as response:
                        self.assertEqual(response.status,200);self.assertTrue(response.read())
                release.set();reader.join(3);self.assertFalse(reader.is_alive())
                self.assertEqual(responses[0][0],200,responses)
                self.assertTrue((self.project/'daily/261029.md').exists())
                self.assertTrue(server.module_host.quiesce(2,2)['stop_verified'])
        finally:
            release.set()
            if reader is not None:reader.join(3)
            server.shutdown();server.server_close();thread.join(3)
            self.assertFalse(thread.is_alive())
