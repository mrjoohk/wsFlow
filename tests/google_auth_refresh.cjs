// Deterministic VM contract: execute the real UI/action/poll; fixture only DOM/API/time.
const assert=require('assert/strict'),fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(__dirname,'../wsflow/modules/work_calendar/ui.js'),'utf8');
const CASES=['blocks_selection_through_status_refresh_and_calendar','restores_selection_admission_after_each_failure','poll_preserves_existing_action_owner_and_visibility','pending_polls_restore_controls_and_continue_requests'];
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
async function bounded(promise,label){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(label+' exceeded 2000 ms')),2000);})]);}finally{clearTimeout(timer);}}
function checkboxMarkup(markup){
  return [...markup.matchAll(/<input\b[^>]*\bdata-gcal="(\d+)"[^>]*>/g)].map(match=>({dataset:{gcal:match[1]},checked:/\bchecked\b/.test(match[0]),disabled:/\bdisabled\b/.test(match[0])}));
}
function assertRenderedEnabled(markup,label,selectionExpected=false){
  const buttons=[...markup.matchAll(/<button\b[^>]*>/g)].map(match=>match[0]);
  const connect=buttons.find(tag=>tag.includes('id="gConnect"'));
  assert.ok(connect,label+': rendered connect control must exist');
  assert.doesNotMatch(connect,/\bdisabled\b/,label+': rendered connect control must be enabled before the next action');
  const select=buttons.find(tag=>tag.includes('id="gSelect"'));
  if(selectionExpected)assert.ok(select,label+': rendered selection control must exist');
  if(select)assert.doesNotMatch(select,/\bdisabled\b/,label+': rendered selection control must be enabled before the next action');
  const checks=checkboxMarkup(markup);
  if(selectionExpected)assert.equal(checks.length,1,label+': fixture calendar checkbox must be rendered');
  for(const checkbox of checks)assert.equal(checkbox.disabled,false,label+': rendered calendar checkbox must be enabled before the next action');
}
function fixture({pending=[]}={}){
  const requests=[],renders=[],timers=new Map(),cleared=[],nodes=new Map();let timerId=0,hold=null,selected=[],checkboxes=[],pendingState=false;const pendingReplies=[...pending];
  function node(id){return {id,innerHTML:'',onclick:null,replaceChildren(){this.children=[];},append(value){(this.children??=[]).push(value);},querySelectorAll(){return [];}};}
  for(const id of ['calendarRoot','gAuthorization','gControls','gOutbox','gConnect','gDisconnect','gSync','gWrite','gSelect'])nodes.set('#'+id,node(id));
  Object.defineProperty(nodes.get('#gControls'),'innerHTML',{get(){return renders.at(-1)||'';},set(value){renders.push(value);checkboxes=checkboxMarkup(value);}});
  nodes.get('#gControls').querySelectorAll=selector=>selector==='[data-gcal]:checked'?checkboxes.filter(box=>box.checked):[];
  const status=()=>({connected:!pendingState,configured:true,account:pendingState?null:{id:'fixture',email:'fixture@example.test'},selected_calendar_ids:[...selected],pending:pendingState,can_write:false,last_sync:null,complete:true});
  async function api(method,url,body){
    // Model the real API JSON request boundary for values constructed inside the VM.
    if(body!==undefined)body=JSON.parse(JSON.stringify(body));
    requests.push({method,url,body});const gate=hold;
    if(gate&&gate.match(method,url)){hold=null;gate.entered.resolve();await gate.release.promise;}
    if(url.endsWith('/connect'))return {authorization_url:'https://accounts.google.com/fixture'};
    if(url.endsWith('/select')){selected=[...body.calendar_ids];return status();}
    if(url.endsWith('/status')){if(pendingReplies.length)pendingState=pendingReplies.shift();return status();}
    if(url.endsWith('/calendars'))return {calendars:[{id:'cal',summary:'Team',accessRole:'owner',can_read:true,can_write:false}]};
    if(url.endsWith('/outbox'))return {links:[],operations:[]};
    if(url.endsWith('/sync'))return {complete:true};
    if(url.startsWith('/api/v2/calendar/range?'))return {days:[],complete:true};
    throw Error('Unexpected fixture API '+method+' '+url);
  }
  const context=vm.createContext({console,URLSearchParams,api,esc:value=>String(value??''),state:{view:'calendar'},
    $:selector=>nodes.get(selector)||null,document:{createElement:()=>node('a')},
    window:{addEventListener(){},wsflowUI:{register(){}}},setInterval(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},clearInterval(id){cleared.push(id);},
    calendarRange:()=>({start:'2026-10-01',end:'2026-11-01'}),calendarCurrent:()=>true});
  vm.runInContext(source,context,{filename:'wsflow/modules/work_calendar/ui.js'});
  // Drawing the calendar grid is a DOM dependency; retain actual loadCalendar/API flow.
  vm.runInContext("drawCalendar=()=>{};CAL.selected='2026-10-18';globalThis.contract={action:googleAction,state:()=>({busy:GCAL.busy,googleError:GCAL.error,calendarError:CAL.error,pending:GCAL.status?.pending})};",context);
  return {requests,renders,cleared,action:context.contract.action,state:context.contract.state,
    async start(){await bounded(this.action('connect'),'connect');assert.equal(timers.size,1);assert.equal([...timers.values()][0].ms,2000);},
    poll:()=>[...timers.values()][0].fn(),invisible(){context.state.view='other';},
    hold(stage){const entered=deferred(),release=deferred();const match=stage==='status'?(m,u)=>m==='GET'&&u.endsWith('/status'):stage==='refresh'?(m,u)=>u.endsWith('/calendars'):stage==='calendar'?(m,u)=>u.startsWith('/api/v2/calendar/range?'):(m,u)=>m==='POST'&&u.endsWith('/select');hold={match,entered,release};return {entered:entered.promise,release};},
    selectUsingRegisteredHandler(){
      assertRenderedEnabled(renders.at(-1),'registered selection handler',true);
      const checkbox=checkboxes.find(box=>box.dataset.gcal==='0');assert.ok(checkbox,'calendar checkbox input must exist');checkbox.checked=true;
      const handler=nodes.get('#gSelect').onclick;assert.equal(typeof handler,'function','actual UI must register the selection handler');return handler();
    },
    statusRequests:()=>requests.filter(r=>r.method==='GET'&&r.url.endsWith('/status')).length,
    selections:()=>requests.filter(r=>r.method==='POST'&&r.url.endsWith('/select'))};
}
async function heldRefreshes(){
  for(const stage of ['status','refresh','calendar']){
    const f=fixture();await f.start();const gate=f.hold(stage),poll=f.poll();let selection;
    try{await bounded(gate.entered,stage+' entry');selection=f.action('select',{calendar_ids:['cal']});assert.equal(f.selections().length,0,stage+': select transport must remain blocked during OAuth refresh');assert.match(f.renders.at(-1),/id="gConnect" disabled/,stage+': controls must be disabled');}
    finally{gate.release.resolve();await bounded(Promise.all([poll,selection]),stage+' release');}
    assertRenderedEnabled(f.renders.at(-1),stage+' completed',true);
    await bounded(f.selectUsingRegisteredHandler(),stage+' subsequent selection');assert.equal(f.selections().length,1,stage+': select transport must reopen after OAuth refresh');assert.deepEqual(f.selections()[0].body.calendar_ids,['cal']);
  }
}
async function failedRefreshes(){
  for(const stage of ['status','refresh','calendar']){
    const f=fixture();await f.start();const gate=f.hold(stage),poll=f.poll(),message=stage+' controlled failure';let selection;
    try{await bounded(gate.entered,stage+' failure entry');selection=f.action('select',{calendar_ids:['cal']});assert.equal(f.selections().length,0,stage+': select transport must remain blocked before failed refresh settles');}
    finally{gate.release.reject(Error(message));await bounded(Promise.all([poll,selection]),stage+' failure release');}
    assert.equal(stage==='calendar'?f.state().calendarError:f.state().googleError,message,stage+': real UI error path must execute');
    assertRenderedEnabled(f.renders.at(-1),stage+' failed refresh completed');
    await bounded(f.action('select',{calendar_ids:['cal']}),stage+' recovered selection');assert.equal(f.selections().length,1,stage+': failure cleanup must restore select transport');
  }
}
async function existingOwner(){
  const f=fixture();await f.start();const gate=f.hold('select'),owner=f.action('select',{calendar_ids:['cal']});
  try{await bounded(gate.entered,'owner entry');const requestCount=f.requests.length,renderCount=f.renders.length;await bounded(f.poll(),'poll with owner');assert.equal(f.requests.length,requestCount,'poll must not start work while a user action owns busy');assert.equal(f.renders.length,renderCount,'poll must not redraw the active owner');await bounded(f.action('select',{calendar_ids:[]}),'selection with owner');assert.equal(f.selections().length,1,'poll must not release another action owner');}
  finally{gate.release.resolve();await bounded(owner,'owner release');}
  await bounded(f.action('select',{calendar_ids:['cal']}),'selection after owner');assert.equal(f.selections().length,2,'original owner must still finish and reopen admission');
  f.invisible();const count=f.requests.length,draws=f.renders.length,cancelled=f.cleared.length;await bounded(f.poll(),'hidden poll');assert.equal(f.requests.length,count,'hidden calendar poll must not send requests');assert.equal(f.renders.length,draws,'hidden calendar poll must not redraw');assert.equal(f.cleared.length,cancelled+1,'hidden calendar must cancel its poll');
}
async function pendingPolls(){
  const expectedPending=[true,true,false],expectedRequests=[1,2,4],snapshots=[];
  const f=fixture({pending:expectedPending});await f.start();const cancelled=f.cleared.length;
  for(let index=0;index<expectedPending.length;index++){
    await bounded(f.poll(),'pending tick '+(index+1));
    assert.equal(f.statusRequests(),expectedRequests[index],'pending tick '+(index+1)+': actual status requests must continue');
    snapshots.push({...f.state(),markup:f.renders.at(-1),cancelled:f.cleared.length});
  }
  // Capture each tick before the next tick can redraw; inspect before any selection action.
  for(const [index,snapshot] of snapshots.entries()){
    assert.equal(snapshot.busy,false,'pending tick '+(index+1)+': callback must release admission');
    assert.equal(snapshot.pending,expectedPending[index],'pending status sequence must reach completion');
    assert.equal(snapshot.cancelled,cancelled+(expectedPending[index]?0:1),'poll must remain scheduled until authorization completes');
    assertRenderedEnabled(snapshot.markup,'pending tick '+(index+1),!expectedPending[index]);
  }
  assert.equal(f.selections().length,0,'polling itself must not submit a selection');
  await bounded(f.selectUsingRegisteredHandler(),'selection after pending completion');
  assert.equal(f.selections().length,1,'completed authorization must admit the registered selection handler');
  assert.deepEqual(f.selections()[0].body.calendar_ids,['cal'],'registered handler must submit the checked calendar after pending completion');
}
(async()=>{const passed=[];for(const [name,run] of CASES.map((name,index)=>[name,[heldRefreshes,failedRefreshes,existingOwner,pendingPolls][index]])){await run();passed.push(name);}console.log(JSON.stringify({scope:'actual UI in Node VM with DOM/API fixtures; not a browser',passed:passed.length,cases:passed}));})().catch(error=>{console.error(error.stack);process.exitCode=1;});
