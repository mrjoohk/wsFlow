// Terminal Playwright harness: actual UI, synthetic providers, all network intercepted.
// PLAYWRIGHT_MODULE and PLAYWRIGHT_CHROMIUM select an installed local runtime.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const out = path.resolve(process.env.WSFLOW_BROWSER_OUTPUT || 'output/playwright/i1-' + Date.now()); fs.mkdirSync(out, {recursive:true});
const html = fs.readFileSync('static/index.html', 'utf8');
const requests = [], cases = [], errors = [], snapshots = [];
const memos = new Map(); let holdPut = null, releasePut, putSeen, holdRead = null, releaseRead, readSeen;
let badSchema = false, pending = [], legacy = [], holdNew = null, releaseNew, newSeen, failNew = false;
const doc = (id, ymd) => `${id}/${ymd}`;
function memo(id, ymd) {
  const key = doc(id, ymd);
  if (!memos.has(key)) memos.set(key, {ymd,document_id:key,sha:'sha-0',text:`* XXX-99:  hidden original  \n- XXX-1: ${id}/${ymd}\ncommon`});
  return {...memos.get(key)};
}
async function main() {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM});
  const page = await browser.newPage({viewport:{width:1440,height:1080}});
  await page.clock.setFixedTime(new Date('2026-10-18T03:00:00Z'));
  page.setDefaultTimeout(10000);page.on('pageerror', e=>errors.push(e.message));
  await page.route('**/*', async route => {
    const req=route.request(), url=new URL(req.url());
    if (url.origin !== 'http://wsflow.test') throw Error('unexpected external network '+url.origin);
    if (!url.pathname.startsWith('/api/')) return route.fulfill({contentType:'text/html',body:html});
    const body=req.postDataJSON(), method=req.method(), p=url.pathname;
    requests.push({method,path:p,body,query:url.search});let value={},status=200;
    const project = /\/projects\/([^/]+)/.exec(p)?.[1];
    if(p==='/api/config')value={roots:[]};
    else if(p==='/api/projects')value={projects:['a','b'].map(id=>({id,alias:id==='a'?'Alpha':'Beta',path:`C:/synthetic/${id}`,exists:true,git:'none'}))};
    else if(p==='/api/jira/status')value={configured:true,source:'synthetic'};
    else if(p.endsWith('/rules'))value={git:'none',remotes:[],ledger:{exists:false},worklog:{},drift:[],markers:{},locks:[]};
    else if(p.endsWith('/jira'))value={enabled:true,wf_home:`C:/synthetic/${project}`,scope:{project_key:'XXX',epic_keys:[]},schema_ok:!badSchema,gitignore_ok:true,reason:badSchema?'지원하지 않는 schema':''};
    else if(p.endsWith('/tasks'))value={tasks:[{key:'XXX-1',summary:'synthetic task',status:'In Progress'}],counts:{tasks:1},known_keys:['XXX-1']};
    else if(p.endsWith('/memo')) {
      const ymd=url.searchParams.get('ymd') || body.ymd;
      if(method==='GET') { value=memo(project,ymd); if(holdRead===ymd){holdRead=null;readSeen();await new Promise(r=>releaseRead=r);} }
      else { assert.equal(body.document_id,doc(project,ymd));value={...memo(project,ymd),text:body.text,sha:`sha-${requests.length}`};memos.set(doc(project,ymd),value);
        if(holdPut){holdPut=false;putSeen();await new Promise(r=>releasePut=r);} }
    }
    else if(p.endsWith('/close/plan'))value={plan_id:'P-close',memo_sha:memo(project,body.ymd).sha,items:[{operation_id:'A-close',target:'XXX-1',payload:{text:'synthetic comment'},lint:[]}],unknown_keys:[],deferred:[],pending:[]};
    else if(p.endsWith('/close/apply')) {pending=[{operation_id:'A-close',kind:'comment',target:'XXX-1',status:'uncertain'}];value={done:[],failed:[{target:'XXX-1',operation_id:'A-close',status:'uncertain',error:'response lost',retry_allowed:false}]};}
    else if(p.endsWith('/new/plan')) {value={plan_id:'P-new',operation_id:'A-new',title:body.summary,payload:{fields:{summary:body.summary}},warnings:[]};const failed=failNew;failNew=false;if(holdNew){holdNew=false;newSeen();await new Promise(r=>releaseNew=r);}if(failed){status=422;value={error:'old preview failed'};}}
    else if(p.endsWith('/operations'))value={operations:pending,legacy_pending:legacy};
    else if(p.endsWith('/repair') || p.endsWith('/reconcile')) {pending=[];value={status:'succeeded'};}
    else if (p==='/api/v1/context/dashboard'&&method==='GET') value={schema_version:1,projects:[],calendar:{state:'unknown',warnings:['I1 synthetic context scope']}};
    else throw Error('Unhandled synthetic route '+method+' '+p);
    await route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
  });
  async function snapshot(name){snapshots.push(name);fs.writeFileSync(path.join(out,`P015-${name}.yml`),await page.locator('body').ariaSnapshot());}
  async function choose(id){await page.locator(`#list [data-id="${id}"] [data-tab="jira"]`).click();await page.waitForFunction(id=>JSON.parse(document.querySelector('#jBody')?.dataset.doc || '[]')[0]===id && document.querySelector('textarea[data-key]'),id);}
  async function date(ymd){await page.locator('#jYmd').fill(ymd);await page.locator('#jYmd').press('Tab');await page.waitForFunction(y=>JSON.parse(document.querySelector('#jBody')?.dataset.doc || '[]')[2]===y && document.querySelector('textarea[data-key]'),ymd);}
  async function saved(){await page.waitForFunction(()=>document.querySelector('#jSaveState')?.textContent.includes('저장됨'));}
  try {
    await page.goto('http://wsflow.test/?t=synthetic');await page.locator('#list .card').first().waitFor();
    await snapshot('dashboard');await choose('a');await date('261017');await snapshot('memo');
    const task=()=>page.locator('textarea[data-key="XXX-1"]');
    await task().fill('visible edit');await page.locator('#jSave').click();await saved();
    assert.ok(memo('a','261017').text.includes('* XXX-99:  hidden original  '));cases.push('hidden task original preserved by visible edit');
    await date('261018');assert.equal(await task().inputValue(),'a/261018');
    await choose('b');await date('261017');assert.equal(await task().inputValue(),'b/261017');
    await choose('a');await date('261017');assert.equal(await task().inputValue(),'visible edit');cases.push('project and date documents separated');
    holdPut=true;const seenPut=new Promise(r=>putSeen=r);
    await task().fill('first save');await page.locator('#jSave').click();await seenPut;
    await task().fill('typed during save');releasePut();
    await page.waitForFunction(()=>!document.querySelector('#jSave').disabled);
    assert.equal(await task().inputValue(),'typed during save');assert.ok((await page.locator('#jSaveState').innerText()).includes('저장되지 않음'));
    assert.ok(memo('a','261017').text.includes('first save'));await page.locator('#jSave').click();await saved();
    assert.ok(memo('a','261017').text.includes('typed during save'));cases.push('typing during PUT remains dirty then saves against returned revision');
    holdRead='261019';const seenRead=new Promise(r=>readSeen=r);
    await page.locator('#jYmd').fill('261019');await page.locator('#jYmd').press('Tab');await seenRead;
    await date('261020');assert.equal(await task().inputValue(),'a/261020');releaseRead();
    await page.waitForFunction(()=>document.querySelector('textarea[data-key]')?.value==='a/261020');
    await date('261019');assert.equal(await task().inputValue(),'a/261019');cases.push('late prior day GET cannot replace visible day');
    holdNew=true;const seenNew=new Promise(r=>newSeen=r);
    await page.locator('#nSum').fill('old title');await page.locator('#nPlan').click();await seenNew;
    await page.locator('#nSum').fill('new title');releaseNew();
    await page.waitForFunction(()=>document.querySelector('#nBox')?.textContent.includes('다시 확인'));
    assert.equal(await page.locator('#nGo').count(),0);cases.push('late new task preview invalidated by changed input');
    holdNew=true;failNew=true;const failedNew=new Promise(r=>newSeen=r);
    await page.locator('#nPlan').click();await failedNew;await page.locator('#nSum').fill('latest title');releaseNew();
    await page.waitForFunction(()=>document.querySelector('#nBox')?.textContent.includes('다시 확인'));
    assert.ok(!(await page.locator('#nBox').innerText()).includes('old preview failed'));cases.push('late failed preview does not replace newer form state');
    await page.locator('#jPlan').click();await page.locator('.jPick').check();await snapshot('approval');
    await page.locator('#jApply').click();await page.locator('[data-reconcile]').waitFor();
    const applied=requests.find(r=>r.path.endsWith('/close/apply')).body;
    assert.deepEqual(Object.keys(applied).sort(),['ack_lint','operation_ids','plan_id','ymd']);assert.deepEqual(applied.operation_ids,['A-close']);
    assert.equal(await page.locator('#jRetry').count(),0);cases.push('approval uses server IDs and uncertain response offers no resend');
    await snapshot('uncertain');await page.locator('[data-remote]').fill('900');await page.locator('[data-reconcile]').click();
    await page.waitForFunction(()=>!document.querySelector('[data-reconcile]'));cases.push('uncertain remote correlation recovery control submitted');
    pending=[{operation_id:'A-known',kind:'create_issue',target:'XXX',status:'succeeded',local_recorded:false,receipt_exported:true}];
    await page.locator('#jLoad').click();await page.locator('[data-repair]').waitFor();await page.locator('[data-repair]').click();
    await page.waitForFunction(()=>!document.querySelector('[data-repair]'));cases.push('known success offers local-only repair');
    legacy=[{id:'A-old',target:'XXX-1',kind:'comment',status:'legacy_uncertain'}];
    await page.locator('#jLoad').click();await page.getByRole('heading',{name:'구형 승인 — 수동 확인 필요'}).waitFor();
    assert.equal(await page.locator('[data-reconcile],[data-repair]').count(),0);cases.push('legacy pending approvals have separate manual review guidance');
    badSchema=true;await choose('b');
    for(const id of ['jSave','jPlan','nPlan'])assert.equal(await page.locator('#'+id).isDisabled(),true);
    cases.push('unsupported schema disables business write controls');
    assert.deepEqual(errors,[]);await snapshot('schema');await page.screenshot({path:path.join(out,'P015-final.png'),fullPage:true});
    fs.writeFileSync(path.join(out,'P015-evidence.json'),JSON.stringify({at:new Date().toISOString(),source:'actual static/index.html, mocked HTTP only',source_sha256:require('crypto').createHash('sha256').update(html).digest('hex'),cases,requests,errors,snapshots},null,2));
    console.log(JSON.stringify({passed:cases.length,cases,errors}));
  } catch(e){await page.screenshot({path:path.join(out,'P015-failure.png'),fullPage:true});await snapshot('failure');throw e;}
  finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
