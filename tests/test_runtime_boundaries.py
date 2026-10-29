"""Direct package/extension boundaries with real memo and installed-package effects."""
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from test_runtime_quiescence import RuntimeFixture
from wsflow import wfhome
from wsflow.module_host import ModuleHost
from wsflow.module_services import ModuleServices
from wsflow.runtime_activity import ActivityUnavailable
from wsflow.store import Store


def sql_bytes(base):
    return {str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*.sqlite3*') if p.is_file()}


class RuntimeBoundaryTests(RuntimeFixture,unittest.TestCase):
    def test_direct_calendar_dispatch_holds_actual_memo_effect_and_rejects_new_root(self):
        request=SimpleNamespace(_project=self.services.project)
        daily=self.project/'daily/261029.md';self.assertFalse(daily.exists())
        entered,release=threading.Event(),threading.Event();result={};original=wfhome.read_memo
        def blocked(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('memo release timed out')
            return original(*args,**kwargs)
        def dispatch():
            try:result['value']=self.host.backends['work-calendar'].dispatch(request,'GET','/api/projects/p/jira/memo',{'ymd':['261029']},{})
            except BaseException as exc:result['error']=repr(exc)
        thread=threading.Thread(target=dispatch,daemon=True)
        def finish():release.set();thread.join(4);self.assertFalse(thread.is_alive())
        self.addCleanup(finish)
        with mock.patch.object(wfhome,'read_memo',side_effect=blocked):
            thread.start();self.assertTrue(entered.wait(2))
            with mock.patch.object(self.host.backends['work-calendar'],'stop',wraps=self.host.backends['work-calendar'].stop) as stop:
                close_while_held=self.host.close(0);stop_before_release=stop.call_count
            release.set();thread.join(4)
        self.assertFalse(thread.is_alive());self.assertNotIn('error',result)
        raw=daily.read_bytes();start=b'<!-- MEMO:START -->';end=b'<!-- MEMO:END -->'
        memo=raw[raw.index(start)+len(start):raw.index(end)].decode('utf8')
        self.assertEqual(Path(result['value']['path']),daily)
        self.assertEqual(result['value']['text'],memo)
        self.assertEqual(result['value']['sha'],hashlib.sha256(memo.encode()).hexdigest())
        self.assertIn(b'261029',raw)
        # A second RuntimeFixture is a separate temp registry/WF_HOME, so the
        # admitted memo effect cannot satisfy the paused-case file oracle.
        paused=unittest.TestCase();RuntimeFixture.setUp(paused);self.addCleanup(paused.doCleanups)
        paused_daily=paused.project/'daily/261029.md';self.assertFalse(paused_daily.exists())
        self.assertTrue(paused.host._activity.pause(0));before_sql=sql_bytes(paused.base)
        rejected=False
        try:paused.host.backends['work-calendar'].dispatch(SimpleNamespace(_project=paused.services.project),'GET','/api/projects/p/jira/memo',{'ymd':['261029']},{})
        except ActivityUnavailable:rejected=True
        self.observation={'normal_memo_created':daily.is_file(),'normal_memo_sha256':hashlib.sha256(raw).hexdigest(),
            'normal_return_matches_actual_memo_bytes':True,'normal_close_while_actual_effect_held':close_while_held,'stop_before_effect_release':stop_before_release,
            'paused_rejected':rejected,'paused_memo_created':paused_daily.exists(),'paused_sql_before':before_sql,'paused_sql_after':sql_bytes(paused.base),
            'phases_use_independent_temp_fixtures':self.base!=paused.base}
        self.assertFalse(close_while_held);self.assertEqual(stop_before_release,0)
        self.assertTrue(rejected);self.assertFalse(paused_daily.exists());self.assertEqual(before_sql,sql_bytes(paused.base))

    def test_direct_installed_extension_holds_effect_through_close_and_rejects_new_root(self):
        folder=self.base/'erp-boundary';folder.mkdir();markers=self.base/'erp-markers';markers.mkdir()
        manifest={'schema_version':1,'id':'erp-local','name':'synthetic ERP','version':'1.0.0','host_api':1,
                  'data_schema_version':1,'capabilities':['erp'],'requires':[],'entrypoint':'backend.py:create_module'}
        (folder/'module.json').write_text(json.dumps(manifest),encoding='utf8')
        code="""from pathlib import Path
import threading
class Module:
 def __init__(self): self.entered=threading.Event(); self.release=threading.Event(); self.stop_calls=0
 def handle(self,method,rest,query,body):
  self.entered.set()
  if body.get('hold') and not self.release.wait(3): raise AssertionError('extension release timed out')
  name=body['name']; Path(MARKERS,name).write_text('effect:'+name,encoding='utf8'); return {'written':name}
 def stop(self):
  self.stop_calls+=1; Path(MARKERS,'stopped').write_text('stopped',encoding='utf8'); return True
def create_module(context): return Module()
""".replace('MARKERS',repr(str(markers)))
        (folder/'backend.py').write_text(code,encoding='utf8')
        plan=self.host.plan({'action':'install','module_id':'erp-local','expected_revision':0,'source_path':str(folder),'trusted':True})
        self.host.apply(plan['plan_id']);self.assertTrue(self.host.close())
        active=ModuleHost(self.registry);services=ModuleServices(Store(self.registry),active,self.provider)
        active.bind_services(services);active.start_backends();self.addCleanup(active.close)
        extension=active.extensions['erp-local'];result={};before_sql=sql_bytes(self.base)
        def handle():
            try:result['value']=active.extension_request('POST','erp-local',[],{},{'name':'admitted','hold':True})
            except BaseException as exc:result['error']=repr(exc)
        thread=threading.Thread(target=handle,daemon=True)
        def finish():extension.release.set();thread.join(4);self.assertFalse(thread.is_alive())
        self.addCleanup(finish);thread.start();self.assertTrue(extension.entered.wait(2))
        try:
            close_while_held=active.close(0);stop_before_release=extension.stop_calls
            rejected=False
            try:active.extension_request('POST','erp-local',[],{},{'name':'new-root'})
            except ActivityUnavailable:rejected=True
            new_marker=(markers/'new-root').exists()
        finally:extension.release.set();thread.join(4)
        self.assertFalse(thread.is_alive());self.assertNotIn('error',result)
        admitted=(markers/'admitted').read_text(encoding='utf8');closed=active.close(2)
        self.observation={'close_while_handle_held':close_while_held,'stop_before_handle_release':stop_before_release,
            'new_root_rejected':rejected,'new_root_marker_created':new_marker,'existing_handle_result':result.get('value'),
            'existing_marker_content':admitted,'close_after_release':closed,'stop_marker_content':(markers/'stopped').read_text(encoding='utf8'),
            'sql_before':before_sql,'sql_after':sql_bytes(self.base),'installed_extension_is_actual':True}
        self.assertFalse(close_while_held);self.assertEqual(stop_before_release,0)
        self.assertTrue(rejected);self.assertFalse(new_marker)
        self.assertEqual(result['value'],{'written':'admitted'});self.assertEqual(admitted,'effect:admitted')
        self.assertTrue(closed);self.assertEqual(before_sql,sql_bytes(self.base))


if __name__=='__main__':unittest.main()
