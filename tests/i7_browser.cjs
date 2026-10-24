// Actual server/Git/vendor/UI, synthetic Jira. Local requests only.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn,spawnSync}=require('child_process'),fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT||'output/playwright/i7-'+Date.now());fs.mkdirSync(out,{recursive:true});
async function bounded(promise,label,ms=12000){
  let timer;
  try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(label+' timeout')),ms);})]);}
  finally{clearTimeout(timer);}
}
async function stopFixture(child){
  if(child.exitCode!==null||child.signalCode!==null)return;
  const exited=new Promise(resolve=>child.once('exit',resolve));child.stdin.end('stop\n');
  try{await bounded(exited,'fixture graceful exit',8000);}
  catch(e){child.kill();await bounded(exited,'fixture killed exit',3000);throw e;}
}
async function main(){
  const child=spawn('python',['-X','utf8','tests/i7_fixture_server.py'],{stdio:['pipe','pipe','pipe']});let stderr='';child.stderr.on('data',d=>stderr+=d);
  const cases=[],errors=[],requests=[];let browser,page,held=null,loseBatch=false,releaseDate=null,holdDate=false,holdStatus=null,loseGitReply=true,gitApplyCount=0;
  try{
  const config=await bounded(new Promise((resolve,reject)=>{let text='';child.stdout.on('data',d=>{text+=d;try{resolve(JSON.parse(text.split('\n')[0]));}catch{}});child.on('error',reject);child.on('exit',code=>reject(Error('fixture '+code+' '+stderr)));}),'fixture startup',20000);
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});page=await browser.newPage({viewport:{width:1480,height:1100}});
  const append=cases.push.bind(cases);cases.push=(...items)=>{const result=append(...items);fs.writeFileSync(path.join(out,'progress.json'),JSON.stringify({cases,errors},null,2));return result;};
  page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{errors.push('unexpected dialog '+d.message());d.dismiss();});
  const git=(cwd,...args)=>{const cp=spawnSync('git',args,{cwd,encoding:'utf8',timeout:15000,env:{...process.env,GIT_AUTHOR_NAME:'fixture',GIT_AUTHOR_EMAIL:'t@x',GIT_COMMITTER_NAME:'fixture',GIT_COMMITTER_EMAIL:'t@x'}});assert.equal(cp.status,0,cp.stderr);return cp.stdout.trim();};
  const control=mode=>fs.writeFileSync(config.control,JSON.stringify({mode}));const stateBytes=()=>fs.readFileSync(path.join(config.home,'.workflow','state.json'));
  page.on('request',r=>requests.push({url:r.url(),method:r.method(),body:r.postDataJSON()}));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());assert.equal(url.origin,config.url,'external traffic forbidden');
    if(url.pathname===`/api/v1/projects/${config.pid}/writes/git/apply`){
      gitApplyCount++;const response=await route.fetch({timeout:90000});if(loseGitReply){loseGitReply=false;return route.abort('failed');}return route.fulfill({response});
    }
    if(url.pathname===`/api/projects/${config.pid}/status`&&holdStatus){const pending=holdStatus;holdStatus=null;const response=await route.fetch();pending.seen();await bounded(pending.gate,'status route gate');return route.fulfill({response});}
    if(url.pathname.endsWith('/batch/run')){
      const response=await route.fetch();if(held){const gate=held;held=null;await bounded(gate,'batch route gate');}
      if(loseBatch){loseBatch=false;return route.abort('failed');}return route.fulfill({response});
    }
    if(url.pathname==='/api/v1/workflow/status'&&holdDate&&url.searchParams.get('date')==='2026-10-17'){
      holdDate=false;const response=await route.fetch();await bounded(new Promise(r=>releaseDate=r),'date route gate');return route.fulfill({response});
    }
    return route.continue();
  });
  const selectJira=async()=>{await page.locator(`#list [data-id="${config.pid}"]`).click();await page.locator('.tabs [data-tab="jira"]').click();await page.locator('#jRest').waitFor();};
    const originalState=stateBytes(),originalUser=fs.readFileSync(path.join(config.home,'USER.txt'));
    await page.goto(config.url+'/?t='+config.token);await page.locator('#workflowDashboard').waitFor();
    await page.locator('#wfDate').fill('2026-10-18');await page.locator('#wfDate').dispatchEvent('change');await page.locator('#wfRows').filter({hasText:'수행 업무 1'}).waitFor();
    assert.ok((await page.locator('#wfRows').innerText()).includes('최신 완전 배치 미확인'));assert.ok((await page.locator('#wfRows').innerText()).includes('신규 ?'));assert.deepEqual(stateBytes(),originalState);cases.push('overview distinguishes unknown batch from zero and reads saved task records without writes');
    assert.equal(await page.locator('#workflowDashboard img').count(),0);cases.push('project and source text remain escaped in whole status');
    await page.locator(`[data-workflow-calendar="${config.pid}"]`).click();await page.locator('#cDetail').filter({hasText:'ACTUAL PRIVATE WORK'}).waitFor();cases.push('whole status opens the same date and project in Calendar detail');
    await page.locator(`#list [data-id="${config.pid}"]`).click();await page.locator('.tabs [data-tab="status"]').click();await page.locator('#btnFetch').click();await page.locator('#out').filter({hasText:'결과 확인 필요'}).waitFor({timeout:60000});await page.locator('#btnFetch').click();await page.locator('#out').filter({hasText:'성공'}).waitFor({timeout:60000});assert.equal(gitApplyCount,1);
    assert.ok(!(await page.locator('#gitFetchTime').innerText()).includes('fetch: 미확인'));assert.notEqual(git(config.home,'rev-parse','HEAD'),git(config.home,'rev-parse','origin/main'));cases.push('explicit fetch displays its successful time and refreshes tracking refs');
    await page.locator('#btnPull').click();await page.locator('#out').filter({hasText:'--ff-only'}).waitFor({timeout:60000});assert.equal(fs.readFileSync(path.join(config.home,'remote.txt'),'utf8'),'from other');cases.push('ff-only pull receives another checkout commit');
    fs.writeFileSync(path.join(config.home,'untracked-user.txt'),'DO NOT TOUCH');const beforeDirty=git(config.home,'rev-parse','HEAD');await page.locator('#btnPull').click();await page.locator('#out').filter({hasText:'미저장/미추적'}).waitFor({timeout:60000});assert.equal(git(config.home,'rev-parse','HEAD'),beforeDirty);assert.equal(fs.readFileSync(path.join(config.home,'untracked-user.txt'),'utf8'),'DO NOT TOUCH');cases.push('dirty pull stops with all user bytes and HEAD preserved');
    git(config.home,'add','untracked-user.txt');git(config.home,'commit','-m','local divergence');fs.writeFileSync(path.join(config.other,'remote.txt'),'second remote');git(config.other,'add','-A');git(config.other,'commit','-m','remote divergence');git(config.other,'push');
    const diverged=git(config.home,'rev-parse','HEAD');await page.locator('#btnPull').click();await page.locator('#out').filter({hasText:'분기됨'}).waitFor({timeout:60000});assert.equal(git(config.home,'rev-parse','HEAD'),diverged);cases.push('non-ff divergence is shown without merge or reset');
    let releaseStatus,seenStatus;const statusSeen=new Promise(r=>seenStatus=r);holdStatus={gate:new Promise(r=>releaseStatus=r),seen:seenStatus};await page.locator('.tabs [data-tab="status"]').click();await bounded(statusSeen,'status request observed');await selectJira();releaseStatus();await page.waitForTimeout(150);assert.deepEqual(errors,[]);cases.push('late Git response cannot bind handlers in a different project tab');
    control('truncated');await page.locator('#jLoad').click();await page.locator('#jIncomplete').waitFor();assert.deepEqual(stateBytes(),originalState);cases.push('partial Jira read shows an explicit incomplete warning');
    control('normal');await page.locator('#jLoad').click();await page.locator('#jRest').waitFor();await page.locator('#jRest').fill('UNSAVED BATCH DRAFT');const runsBefore=requests.filter(r=>r.url.endsWith('/batch/run')).length;
    await page.locator('#jBatch').click();await page.locator('#dlg button').filter({hasText:'취소'}).click();assert.equal(requests.filter(r=>r.url.endsWith('/batch/run')).length,runsBefore);assert.ok((await page.locator('#jRest').inputValue()).includes('UNSAVED'));cases.push('unsaved memo cancels batch without losing the draft or sending a run');
    await page.locator('#jSave').click();await page.locator('#jSaveState').filter({hasText:'저장됨'}).waitFor();const memo=fs.readFileSync(path.join(config.home,'daily','261018.md'),'utf8').split('<!-- MEMO:START -->')[1].split('<!-- MEMO:END -->')[0];
    let release;held=new Promise(r=>release=r);await page.locator('#jBatch').click();await page.waitForFunction(()=>document.querySelector('#jBatch')?.disabled);assert.ok(await page.locator('#jBatch').isDisabled());release();
    await page.locator('#jBatchResult').filter({hasText:'succeeded'}).waitFor();assert.equal(JSON.parse(stateBytes()).watermark,'2026-10-18T10:30:00+09:00');assert.equal(fs.readFileSync(path.join(config.home,'daily','261018.md'),'utf8').split('<!-- MEMO:START -->')[1].split('<!-- MEMO:END -->')[0],memo);cases.push('explicit batch shows all stages and preserves exact saved memo before watermark commit');
    control('invalid_scope');const wm=JSON.parse(stateBytes()).watermark;await page.locator('#jBatch').click();await page.locator('#jBatchResult').filter({hasText:'failed'}).waitFor();assert.ok((await page.locator('#jBatchResult').innerText()).includes('전진하지 않음'));assert.equal(JSON.parse(stateBytes()).watermark,wm);cases.push('invariant failure is visible and watermark remains unchanged');
    control('normal');loseBatch=true;await page.locator('#jBatch').click();await page.locator('#jBatchResult').filter({hasText:'응답을 확인하지 못했습니다'}).waitFor();await page.locator('#jBatchStatus').click();await page.locator('#jBatchResult').filter({hasText:'succeeded'}).waitFor();cases.push('lost batch response is recovered by reading the committed marker');
    const corrupt=path.join(config.home,'.workflow/batches/corrupt/receipt.json');fs.mkdirSync(path.dirname(corrupt),{recursive:true});fs.writeFileSync(corrupt,'[]');await page.locator('#jBatchStatus').click();await page.locator('#jBatchResult').filter({hasText:'배치 작업 기록을 읽지 못했습니다'}).waitFor();assert.ok((await page.locator('#jBatchResult').innerText()).includes('확정 watermark: 2026-10-18T10:30:00+09:00'));fs.unlinkSync(corrupt);cases.push('corrupt receipt shows a warning while retaining the committed watermark');
    await page.locator('#btnDash').click();await page.locator('#workflowDashboard').waitFor();holdDate=true;await page.locator('#wfDate').fill('2026-10-17');await page.locator('#wfDate').dispatchEvent('change');await page.waitForFunction(()=>document.querySelector('#wfRows').textContent.includes('상태 읽는 중'));
    await bounded((async()=>{while(!releaseDate)await page.waitForTimeout(20);})(),'date request observed');await page.locator('#wfDate').fill('2026-10-18');await page.locator('#wfDate').dispatchEvent('change');await page.locator('#wfRows').filter({hasText:'당일 완전 배치 확인'}).waitFor();releaseDate();await page.waitForTimeout(200);assert.ok((await page.locator('#wfRows').innerText()).includes('당일 완전 배치 확인'));cases.push('late whole-status date reply cannot replace the selected day');
    await selectJira();await page.locator('#nSum').fill('created task');await page.locator('#nPlan').click();await page.locator('#nGo').waitFor();control('invisible');await page.locator('#nGo').click();await page.locator('#jNew').filter({hasText:'가시성 not_visible'}).waitFor();assert.ok((await page.locator('#jNew').innerText()).includes('생성 성공'));cases.push('successful create and unconfirmed personal-list visibility stay distinct');
    await page.setViewportSize({width:800,height:1100});await page.locator('#btnDash').click();await page.locator('#workflowDashboard').waitFor();await page.screenshot({path:path.join(out,'narrow.png'),fullPage:true});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));cases.push('whole status works at a narrow viewport');
    fs.writeFileSync(path.join(out,'final.yml'),await page.locator('body').ariaSnapshot());assert.deepEqual(errors,[]);assert.deepEqual(fs.readFileSync(path.join(config.home,'USER.txt')),originalUser);
    const source_sha256=crypto.createHash('sha256').update(fs.readFileSync('static/index.html')).digest('hex');const backend_hashes=Object.fromEntries(['workflow.py','gitops.py','jiraapi.py','server.py','writeplans.py'].map(f=>[f,crypto.createHash('sha256').update(fs.readFileSync(path.join('wsflow',f))).digest('hex')]));
    const result={passed:cases.length,cases,errors,source_sha256,backend_hashes,external_requests:0,jira_provider:'synthetic',git_provider:'real local bare and two checkouts'};fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
  }catch(e){fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({error:e.stack,cases,errors,requests:requests.slice(-16)},null,2));if(page)fs.writeFileSync(path.join(out,'failure.yml'),await page.locator('body').ariaSnapshot().catch(()=>''));throw e;}
  finally{if(releaseDate)releaseDate();try{if(browser)await bounded(browser.close(),'browser close',5000);}finally{await stopFixture(child);}if(stderr)console.error(stderr);}
}
if(require.main===module)main().catch(e=>{console.error(e.stack);process.exitCode=1;});
module.exports={bounded};
