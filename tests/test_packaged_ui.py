"""Package-owned views and active-generation loading, without a provider or server."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'static/index.html'
PACKAGES = {
    'work-calendar': ROOT / 'wsflow/modules/work_calendar/ui.js',
    'workspace-operations': ROOT / 'wsflow/modules/workspace_operations/ui.js',
}
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'node unavailable')
class PackagedUITests(unittest.TestCase):
    def node(self, source):
        with tempfile.TemporaryDirectory(prefix='wsflow-packaged-ui-') as folder:
            script = Path(folder) / 'test.js'
            script.write_text(source, encoding='utf-8')
            result = subprocess.run([NODE, str(script)], text=True, encoding='utf-8',
                                    capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_business_views_live_in_their_package_not_the_core_shell(self):
        shell = INDEX.read_text(encoding='utf-8')
        work = PACKAGES['work-calendar'].read_text(encoding='utf-8')
        workspace = PACKAGES['workspace-operations'].read_text(encoding='utf-8')
        for function in ('renderCalendar', 'renderJira', 'drawJira', 'openCalendarEditor'):
            self.assertNotRegex(shell, r'function\s+' + function + r'\(')
            self.assertRegex(work, r'function\s+' + function + r'\(')
        for function in ('renderStatus', 'renderRules', 'sessionDialog', 'deployDialog'):
            self.assertNotRegex(shell, r'function\s+' + function + r'\(')
            self.assertRegex(workspace, r'function\s+' + function + r'\(')
        self.assertIn('<script id="ui-pure">', shell)
        self.assertIn('<script id="calendar-pure">', shell)

    def test_every_package_bundle_parses(self):
        for path in PACKAGES.values():
            result = subprocess.run([NODE, '--check', str(path)], capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, path.as_posix() + '\n' + result.stderr)

    def test_failed_builtin_ui_keeps_backend_state_and_management_truthful(self):
        self.failed_ui_case('const =', '모듈 화면을 읽을 수 없습니다')

    def test_failed_on_ready_retracts_views_and_restores_partial_header_bindings(self):
        self.failed_ui_case('''window.wsflowUI.register('workspace-operations', {
          tabs:[{id:'broken-tab',label:'Broken',render(){}}],
          onReady(){document.querySelector('#btnAddRoot').onclick=()=>{throw Error('partial add handler must not run');};
            document.querySelector('#btnModules').onclick=()=>{throw Error('partial manager handler must not run');};
            throw Error('fixture onReady failure');}
        });''', 'fixture onReady failure')

    def failed_ui_case(self, package_source, expected_message):
        shell = INDEX.read_text(encoding='utf-8')
        source = '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', shell, re.S))
        source = source.replace('loadAll().catch(e => e.status === 403 ? showBlocked(e) : fail(e));', '')
        self.node(r'''
const assert=require('assert/strict'),vm=require('vm'),elements=new Map();
const element=selector=>{
  if(!elements.has(selector))elements.set(selector,{value:'',innerHTML:'',textContent:'',dataset:{},addEventListener(){},classList:{add(){},remove(){},toggle(){}},querySelectorAll(){return[];},closest(){return this;}});
  return elements.get(selector);
};
const record={id:'workspace-operations',name:'워크스페이스 운영',builtin:true,active:true,active_version:'1.0.0',version:'1.0.0',enabled:true,installed:true,active_ui:{entry:'broken.js',mode:'classic'},config_schema:{properties:{}},settings:{},error:null};
const snapshot={revision:0,active_revision:0,restart_required:false,modules:[record],catalog:[]};
let context;
const sandbox={URL,URLSearchParams,console,setTimeout,clearTimeout,setInterval,clearInterval,
  location:{search:'?t=fixture-token'},navigator:{},addEventListener(){},
  async fetch(url){assert.equal(new URL(url,'http://fixture.test').pathname,'/api/v1/modules');return{ok:true,status:200,json:async()=>snapshot};},
  document:{querySelector:element,querySelectorAll(selector){return selector.startsWith('header')?[element('#btnAddRoot'),element('#btnModules')]:[];},addEventListener(){},createElement(){return{dataset:{}};},head:{append(script){
    Promise.resolve().then(()=>{try{vm.runInContext(PACKAGE_SOURCE,context);script.onload();}catch(error){script.onerror(error);}});
  }}}
};
sandbox.window=sandbox;context=vm.createContext(sandbox);vm.runInContext(SOURCE,context);
vm.runInContext('state.modules='+JSON.stringify(snapshot),context);
const originalAddRoot=element('#btnAddRoot').onclick, originalManager=element('#btnModules').onclick;
(async()=>{
  await vm.runInContext('loadBuiltinUI()',context);
  assert.equal(vm.runInContext('state.modules.modules[0].active',context),true,'a UI parse failure must not pretend the backend was disabled');
  assert.equal(vm.runInContext('window.wsflowUI.get("workspace-operations")',context),undefined);
  assert.ok(vm.runInContext('state.moduleUIErrors["workspace-operations"]',context).includes(EXPECTED_MESSAGE));
  assert.equal(element('#btnAddRoot').onclick,originalAddRoot,'a partially assigned business action must be restored');
  assert.equal(element('#btnModules').onclick,originalManager,'the core management action must survive failed initialization');
  assert.equal(element('#menuAdd').hidden,true);
  assert.equal(vm.runInContext('window.wsflowUI.tabs().length',context),0);
  await vm.runInContext('renderDashboard()',context);
  assert.ok(element('#content').innerHTML.includes('워크스페이스 운영 화면 미준비'),'active backend with broken UI must show UI not ready, rather than module inactive');
  assert.ok(!element('#content').innerHTML.includes('워크스페이스 운영 모듈 비활성'));
  await vm.runInContext('renderModules()',context);
  assert.ok(element('#content').innerHTML.includes(EXPECTED_MESSAGE));
  assert.equal(vm.runInContext('state.modules.modules[0].active',context),true);
})().catch(error=>{console.error(error);process.exit(1);});
'''.replace('PACKAGE_SOURCE', json.dumps(package_source)).replace('EXPECTED_MESSAGE', json.dumps(expected_message)).replace('SOURCE', json.dumps(source)))

    def test_active_package_loader_contributes_views_without_loading_inactive_code(self):
        shell = INDEX.read_text(encoding='utf-8')
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', shell, re.S)
        source = '\n'.join(scripts)
        startup = 'loadAll().catch(e => e.status === 403 ? showBlocked(e) : fail(e));'
        self.assertEqual(source.count(startup), 1)
        source = source.replace(startup, '')
        bundles = {name: path.read_text(encoding='utf-8') for name, path in PACKAGES.items()}
        fixture = r'''
const assert=require('assert/strict'),vm=require('vm');
const source=SOURCE, bundles=BUNDLES;
async function exercise(active,reverse=false){
  const elements=new Map(),loads=[],unload=[];
  const element=selector=>{
    if(!elements.has(selector))elements.set(selector,{value:'',innerHTML:'',textContent:'',dataset:{},
      addEventListener(){},classList:{add(){},remove(){},toggle(){}},querySelectorAll(){return[];},closest(){return this;}});
    return elements.get(selector);
  };
  let context,records;
  const sandbox={URL,URLSearchParams,console,setTimeout,clearTimeout,setInterval,clearInterval,
    location:{search:'?t=fixture-token'},navigator:{},
    addEventListener(type,callback){if(type==='beforeunload')unload.push(callback);},
    async fetch(url){
      const path=new URL(url,'http://fixture.test').pathname;
      let payload;
      if(path==='/api/v1/modules')payload={modules:records};
      else if(path==='/api/config')payload={roots:[]};
      else if(path==='/api/projects'){
        assert.equal(vm.runInContext('window.wsflowUI.entries().length',context),active.length,'core must await active package views before reading/rendering projects');
        payload={projects:[]};
      }else throw Error('unexpected package API call '+path);
      return {ok:true,status:200,json:async()=>payload};
    },
    document:{querySelector:element,querySelectorAll(){return[];},addEventListener(){},
      createElement(){return{dataset:{}};},head:{append(script){
        loads.push(script.src);
        Promise.resolve().then(()=>{
          try{const id=decodeURIComponent(script.src.split('/')[4]);
            vm.runInContext(bundles[id],context,{filename:id+'/ui.js'});script.onload();
          }catch(error){script.onerror(error);}
        });
      }}}
  };
  sandbox.window=sandbox;context=vm.createContext(sandbox);vm.runInContext(source,context);
  const ids=Object.keys(bundles);if(reverse)ids.reverse();
  records=ids.map(id=>({id,builtin:true,active:active.includes(id),active_version:'1.0.0',
    active_ui:{entry:'ui.js',mode:'classic'}}));
  vm.runInContext('state.modules='+JSON.stringify({modules:records}),context);
  await Promise.all([vm.runInContext('loadAll()',context),vm.runInContext('loadAll()',context)]);
  assert.deepEqual(loads.map(url=>decodeURIComponent(url.split('/')[4])).sort(),active.slice().sort());
  assert.ok(loads.every(url=>url.includes('?t=fixture-token')));
  assert.equal(vm.runInContext('Object.keys(state.moduleUIErrors).length',context),0);
  const tabs=Array.from(vm.runInContext('window.wsflowUI.tabs().map(tab=>tab.id)',context));
  assert.deepEqual(tabs,[...(active.includes('workspace-operations')?['status','log','remotes','rules']:[]),...(active.includes('work-calendar')?['jira']:[])]);
  assert.equal(vm.runInContext('typeof renderJira',context),active.includes('work-calendar')?'function':'undefined');
  assert.equal(vm.runInContext('typeof renderStatus',context),active.includes('workspace-operations')?'function':'undefined');
  assert.equal(unload.length,active.includes('work-calendar')?2:0);
  // A second status refresh does not reload const declarations or UI state.
  await vm.runInContext('loadBuiltinUI()',context);assert.equal(loads.length,active.length);
}
(async()=>{for(const active of [[],['work-calendar'],['workspace-operations'],Object.keys(bundles)])await exercise(active);await exercise(Object.keys(bundles),true);})().catch(error=>{console.error(error);process.exit(1);});
'''.replace('SOURCE', json.dumps(source)).replace('BUNDLES', json.dumps(bundles))
        self.node(fixture)
