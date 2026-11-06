"""UF-ST-01: own project tools retain one complete cooperative writer root."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from wsflow import scanner, writer_admission
from wsflow.operations import StateConflict


SCRIPT = '''import json,os,sys,time
from pathlib import Path
p=Path.cwd();config=json.loads((p/'fixture_control.json').read_text(encoding='utf8'))
with (p/'tool_effects.jsonl').open('a',encoding='utf8') as f:
 f.write(json.dumps(dict(argv=sys.argv,cwd=str(p),encoding=os.environ.get('PYTHONIOENCODING')),ensure_ascii=False)+'\\n')
if config.get('hold'):
 (p/'child_ready').write_text(str(os.getpid()),encoding='ascii')
 deadline=time.monotonic()+12
 while not (p/'child_release').is_file():
  if time.monotonic()>=deadline:raise TimeoutError('owned fixture child deadline')
  time.sleep(.01)
(p/'child_finished').write_text('done',encoding='ascii')
print('검사 stdout');print('검사 stderr',file=sys.stderr)
sys.exit(config['returncode'])
'''


class ScannerWriterTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='wsflow-scanner-writer-')
        self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        env=mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'userdata')})
        env.start();self.addCleanup(env.stop)
        self.project=self.base/'project';self.project.mkdir()
        self.domain=writer_admission.default_domain()
        with self.domain.barrier(0):pass

    def script(self,tool='graph_checks',code=0,hold=False):
        path=self.project/('tools/graph_checks.py' if tool=='graph_checks' else 'tools/worklog/validate_worklog.py')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(SCRIPT,encoding='utf8')
        (self.project/'fixture_control.json').write_text(json.dumps(dict(returncode=code,hold=hold)),encoding='utf8')
        return path

    def effects(self):
        path=self.project/'tool_effects.jsonl'
        return [json.loads(x) for x in path.read_text(encoding='utf8').splitlines()] if path.exists() else []

    def completed(self,epoch,domain=None):
        domain=domain or self.domain;view=domain.observe()
        self.assertEqual(epoch+2,view['epoch']);self.assertIsNone(view['active'])
        with domain.barrier(0):pass

    def holding(self,epoch,thread,outcome):
        self.assertTrue(thread.is_alive());self.assertEqual({},outcome)
        self.assertEqual(epoch+1,self.domain.observe()['epoch'])
        with self.assertRaises(writer_admission.WriterUnavailable) as caught:
            with self.domain.barrier(0):pass
        self.assertEqual('busy',caught.exception.reason)

    @contextmanager
    def worker(self,call,patches=(),ready=None):
        entered,release,outcome=threading.Event(),threading.Event(),{}
        def pause():
            entered.set()
            if not release.wait(10):raise TimeoutError('owned scanner hold expired')
        def run():
            try:outcome['value']=call()
            except BaseException as error:outcome['error']=error
        with ExitStack() as stack:
            for patch in patches:stack.enter_context(patch(pause))
            thread=threading.Thread(target=run,daemon=True,name='owned-scanner')
            thread.start()
            try:
                if ready is None:self.assertTrue(entered.wait(8),outcome)
                else:
                    deadline=time.monotonic()+8
                    while not ready.is_file() and time.monotonic()<deadline and thread.is_alive():time.sleep(.01)
                    self.assertTrue(ready.is_file(),outcome)
                yield thread,outcome
            finally:
                release.set();(self.project/'child_release').write_bytes(b'release')
                thread.join(16);self.assertFalse(thread.is_alive())
        if 'error' in outcome:raise outcome['error']

    def test_barrier_refuses_both_tools_before_spawn_and_effect(self):
        for tool in ('graph_checks','validate_worklog'):
            self.script(tool)
            with self.subTest(tool=tool),self.domain.barrier(0),mock.patch.object(scanner.subprocess,'run',wraps=subprocess.run) as calls:
                with self.assertRaises(writer_admission.WriterUnavailable):scanner.run_tool(self.project,tool)
                self.assertEqual(0,calls.call_count);self.assertEqual([],self.effects())

    def test_invalid_userdata_precedes_admission_and_tool_effect(self):
        for where in ('same','inside'):
            for tool in ('graph_checks','validate_worklog'):
                project=self.base/(where+'-'+tool);project.mkdir()
                data=project if where=='same' else project/'userdata'
                with self.subTest(where=where,tool=tool),mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(data)}),mock.patch.object(scanner.subprocess,'run') as calls:
                    with self.assertRaises(StateConflict):scanner.run_tool(project,tool)
                    self.assertEqual(0,calls.call_count);self.assertEqual([],list(project.rglob('*')))

    def test_actual_tools_preserve_arguments_utf8_nonzero_and_effect_once(self):
        for tool in ('graph_checks','validate_worklog'):
            for code in (0,7):
                with self.subTest(tool=tool,code=code):
                    script=self.script(tool,code);before=len(self.effects());epoch=self.domain.observe()['epoch']
                    result=scanner.run_tool(project_path=self.project,tool=tool,timeout=9)
                    self.assertEqual(dict(ok=code==0,returncode=code,stdout='검사 stdout\n',stderr='검사 stderr\n',command=' '.join(['python',str(script),*(['logs/files.jsonl','logs/prompts.jsonl'] if tool=='validate_worklog' else [])])),result)
                    effects=self.effects();self.assertEqual(before+1,len(effects))
                    self.assertEqual([str(script),*(['logs/files.jsonl','logs/prompts.jsonl'] if tool=='validate_worklog' else [])],effects[-1]['argv'])
                    self.assertEqual(str(self.project),effects[-1]['cwd']);self.assertEqual('utf-8',effects[-1]['encoding']);self.completed(epoch)

    def test_live_direct_child_retains_root_until_output_collection(self):
        self.script(hold=True);epoch=self.domain.observe()['epoch'];children=[];original=subprocess.Popen
        def launch(*args,**kwargs):
            child=original(*args,**kwargs);children.append(child);return child
        with mock.patch.object(scanner.subprocess,'Popen',side_effect=launch):
            with self.worker(lambda:scanner.run_tool(self.project,'graph_checks'),ready=self.project/'child_ready') as (thread,outcome):
                self.holding(epoch,thread,outcome);self.assertEqual(1,len(children));self.assertIsNone(children[0].poll())
                self.assertEqual(1,len(self.effects()));self.assertFalse((self.project/'child_finished').exists())
        self.assertTrue(outcome['value']['ok']);self.assertEqual(0,children[0].returncode);self.assertEqual(0,children[0].poll());self.completed(epoch)

    def test_root_spans_each_result_field_after_actual_child_effect(self):
        self.script();original=subprocess.run
        for field in ('returncode','stdout','stderr'):
            with self.subTest(field=field):
                epoch=self.domain.observe()['epoch'];before=len(self.effects());sends=[]
                def patch(pause):
                    def run(*args,**kwargs):
                        cp=original(*args,**kwargs);sends.append(cp)
                        class Result:
                            def __getattr__(self,name):
                                if name==field:pause()
                                return getattr(cp,name)
                        return Result()
                    return mock.patch.object(scanner.subprocess,'run',side_effect=run)
                with self.worker(lambda:scanner.run_tool(self.project,'graph_checks'),[patch]) as (thread,outcome):
                    self.holding(epoch,thread,outcome);self.assertEqual(before+1,len(self.effects()));self.assertEqual(1,len(sends));self.assertTrue((self.project/'child_finished').is_file())
                self.assertTrue(outcome['value']['ok']);self.assertEqual('검사 stdout\n',outcome['value']['stdout']);self.assertEqual('검사 stderr\n',outcome['value']['stderr']);self.completed(epoch)

    def test_real_timeout_reaps_direct_child_before_timeout_result_and_no_retry(self):
        self.script(hold=True);epoch=self.domain.observe()['epoch'];children=[];original_run=subprocess.run;original_popen=subprocess.Popen
        def launch(*args,**kwargs):
            child=original_popen(*args,**kwargs);children.append(child);return child
        def patch(pause):
            def run(*args,**kwargs):
                try:return original_run(*args,**kwargs)
                except subprocess.TimeoutExpired:
                    self.assertEqual(1,len(children));self.assertIsNotNone(children[0].returncode);pause();raise
            return mock.patch.object(scanner.subprocess,'run',side_effect=run)
        with mock.patch.object(scanner.subprocess,'Popen',side_effect=launch):
            with self.worker(lambda:scanner.run_tool(self.project,'graph_checks',timeout=2),[patch]) as (thread,outcome):
                self.holding(epoch,thread,outcome);self.assertIsNotNone(children[0].poll());self.assertEqual(1,len(self.effects()));self.assertFalse((self.project/'child_finished').exists())
        self.assertEqual(dict(ok=False,error='시간 초과',returncode=124),outcome['value']);self.assertEqual(1,len(children));self.completed(epoch)

    def test_launch_errors_propagate_without_retry_and_complete_root(self):
        self.script();epoch=self.domain.observe()['epoch'];error=OSError('owned launch failure')
        with mock.patch.object(scanner.subprocess,'run',side_effect=error) as calls,self.assertRaises(OSError) as caught:scanner.run_tool(self.project,'graph_checks')
        self.assertIs(error,caught.exception);self.assertEqual(1,calls.call_count);self.assertEqual([],self.effects());self.completed(epoch)
        epoch=self.domain.observe()['epoch']
        with mock.patch.object(scanner.sys,'executable',str(self.base/'absent-python')),self.assertRaises(FileNotFoundError):scanner.run_tool(self.project,'graph_checks')
        self.assertEqual([],self.effects());self.completed(epoch)

    def test_nested_and_independent_calls_resolve_current_or_call_time_domain(self):
        self.script();epoch=self.domain.observe()['epoch'];alternate=self.base/'alternate'
        with self.domain.writing():
            active=self.domain.observe()
            with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(alternate)}):
                self.assertTrue(scanner.run_tool(self.project,'graph_checks')['ok']);self.assertEqual(active,self.domain.observe());self.assertEqual('missing',writer_admission.default_domain().observe()['status'])
        self.assertFalse(alternate.exists());self.completed(epoch)
        with mock.patch.dict(os.environ,{'WSFLOW_DATA_DIR':str(self.base/'independent')}):
            domain=writer_admission.default_domain();self.assertTrue(scanner.run_tool(self.project,'graph_checks')['ok']);self.completed(0,domain)

    def test_unknown_and_missing_return_existing_errors_without_fallback_or_spawn(self):
        for tool,error in [('unknown','알 수 없는 도구: unknown'),('graph_checks','tools\\graph_checks.py 없음' if os.name=='nt' else 'tools/graph_checks.py 없음')]:
            with self.subTest(tool=tool):
                epoch=self.domain.observe()['epoch']
                with mock.patch.object(scanner.subprocess,'run') as calls:result=scanner.run_tool(self.project,tool)
                self.assertEqual(0,calls.call_count);self.assertFalse(result['ok']);self.assertEqual(error,result['error'])
                self.assertEqual({'ok','error'} if tool=='unknown' else {'ok','error','returncode'},set(result));self.completed(epoch)

    def test_script_lookup_precedes_spawn_inside_same_root(self):
        script=self.script();epoch=self.domain.observe()['epoch'];original=Path.is_file
        def patch(pause):
            def exists(path):
                result=original(path)
                if path==script:pause()
                return result
            return mock.patch.object(Path,'is_file',exists)
        with self.worker(lambda:scanner.run_tool(self.project,'graph_checks'),[patch]) as (thread,outcome):self.holding(epoch,thread,outcome);self.assertEqual([],self.effects())
        self.assertTrue(outcome['value']['ok']);self.completed(epoch)

    def test_finish_storage_failure_preserves_unresolved_without_reexecution(self):
        self.script();original=writer_admission.WriterDomain._write;failures=[]
        def write(domain,state):
            if domain.control_dir==self.domain.control_dir and state['active'] is None and not failures:
                failures.append(True);raise writer_admission.WriterUnavailable('owned_finish_loss')
            return original(domain,state)
        with mock.patch.object(writer_admission.WriterDomain,'_write',write),self.assertRaises(writer_admission.WriterUnavailable) as caught:scanner.run_tool(self.project,'graph_checks')
        self.assertEqual('owned_finish_loss',caught.exception.reason);self.assertEqual(1,len(self.effects()));self.assertTrue((self.project/'child_finished').is_file())
        with self.assertRaises(writer_admission.WriterUnavailable) as refused:
            with self.domain.barrier(0):pass
        self.assertEqual('unresolved',refused.exception.reason);self.assertEqual(1,len(self.effects()))


if __name__=='__main__':unittest.main()
