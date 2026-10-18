// Actual local server+HTML+SQLite; only artificial HTTP delay/drop injected.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT || 'output/playwright/i2-'+Date.now());fs.mkdirSync(out,{recursive:true});
const cases=[],errors=[],requests=[];let held=false,release,seen,dropApply=false,holdApply=false,releaseApply,seenApply;
async function main(){
  const server=spawn(process.env.PYTHON||'python',['-X','utf8','tests/i2_fixture_server.py'],{stdio:['pipe','pipe','pipe']});
  let stderr='';server.stderr.on('data',d=>stderr+=d);
  const config=await new Promise((resolve,reject)=>{let text='';server.stdout.on('data',d=>{text+=d;const line=text.split('\n')[0];try{resolve(JSON.parse(line));}catch{}});server.on('exit',code=>reject(Error('fixture server '+code+' '+stderr)));setTimeout(()=>reject(Error('fixture startup timeout '+stderr)),15000).unref();});
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
  const page=await browser.newPage({viewport:{width:1560,height:1120}});page.setDefaultTimeout(12000);
  await page.clock.setFixedTime(new Date('2026-10-17T03:00:00Z'));
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());
    assert.equal(url.origin,config.url,'external network is forbidden');
    if(url.pathname.startsWith('/api/'))requests.push({method:req.method(),path:url.pathname,query:url.search,body:req.postDataJSON()});
    if(url.pathname==='/api/v2/calendar/range'&&held){held=false;const response=await route.fetch();seen();await new Promise(r=>release=r);return route.fulfill({response});}
    if(url.pathname==='/api/v2/calendar/apply'&&(dropApply||holdApply)){
      const response=await route.fetch();if(dropApply){dropApply=false;return route.abort('failed');}
      holdApply=false;seenApply();await new Promise(r=>releaseApply=r);return route.fulfill({response});
    }
    return route.continue();
  });
  async function snapshot(name){fs.writeFileSync(path.join(out,'P016-'+name+'.yml'),await page.locator('body').ariaSnapshot());}
  async function ready(){await page.waitForFunction(()=>document.querySelector('#cStatus')?.textContent.includes('저장된 기록을 확인했습니다.'));}
  async function api(method,tail,body){const r=await page.request.fetch(config.url+'/api/v2/calendar/'+tail,{method,headers:{'X-Auth-Token':config.token},data:body});assert.ok(r.ok(),await r.text());return r.json();}
  async function save(){await page.locator('#cSave').click();await page.locator('#cApply').waitFor();await page.locator('#cApply').click();}
  try{
    await page.goto(config.url+'/?t='+config.token);await page.getByRole('heading',{name:'대시보드',exact:false}).waitFor();await snapshot('dashboard');
    await page.locator('#btnCalendar').click();await ready();await snapshot('month');
    const cell=page.locator('[data-date="2026-10-17"]');
    assert.equal(await cell.locator('.cal-event').count(),3);assert.ok((await cell.innerText()).includes('일정 +2'));
    assert.ok((await cell.innerText()).includes('업무 3 · 메모 2'));
    for(const hidden of ['TASK PRIVATE','NOTE PRIVATE','COMMON PRIVATE','EVENT DESCRIPTION PRIVATE'])assert.ok(!(await cell.innerText()).includes(hidden));
    assert.ok((await page.locator('#cDetail').innerText()).includes('ARCHIVED TASK PRIVATE'));
    assert.ok((await page.locator('#cDetail').innerText()).includes('NOTE PRIVATE SECOND'));cases.push('month cells contain event preview and counts only; same-day detail retains all records');
    await page.locator('#cMode').selectOption('week');await ready();assert.equal(await page.locator('[data-date]').count(),7);
    assert.equal(await page.locator('[aria-selected=true][data-date]').getAttribute('data-date'),'2026-10-17');
    await page.locator('[data-date="2026-10-18"]').click();assert.ok((await page.locator('#cDetail').innerText()).startsWith('2026-10-18'));
    await page.locator('#cMode').selectOption('month');await ready();assert.equal(await page.locator('[aria-selected=true][data-date]').getAttribute('data-date'),'2026-10-18');cases.push('month/week preserve selection and date click opens matching right detail');
    await page.locator('#cProject').selectOption('a');await ready();await page.locator('[data-date="2026-10-17"]').click();
    assert.ok((await page.locator('[data-date="2026-10-17"]').innerText()).includes('업무 2 · 메모 2'));
    await page.locator('#cSource').selectOption('legacy_daily');await ready();assert.equal(await page.locator('.cal-event').count(),0);
    assert.ok((await page.locator('[data-date="2026-10-17"]').innerText()).includes('메모 1'));cases.push('project and source filters agree with detail and counts');
    await page.locator('#cSource').selectOption('');await ready();
    held=true;const captured=new Promise(r=>seen=r);await page.locator('#cNext').click();await captured;
    await page.locator('#cPrev').click();await ready();const oldResponse=page.waitForResponse(r=>r.url().includes('/api/v2/calendar/range')&&r.url().includes('2026-10-26'));release();await oldResponse;
    assert.equal(await page.locator('#cPeriod').innerText(),'2026-10');cases.push('late prior range response cannot replace current calendar');
    await page.locator('#cAddNote').click();await page.locator('[data-field=body]').fill('DRAFT SURVIVES DATE');await page.locator('#cClose').click();
    await page.locator('[data-date="2026-10-18"]').click();await page.locator('[data-draft]').click();assert.equal(await page.locator('[data-field=body]').inputValue(),'DRAFT SURVIVES DATE');
    assert.equal(await page.locator('[data-field=date]').inputValue(),'2026-10-17');cases.push('unsaved note survives close and date navigation with original date');
    dropApply=true;await save();await page.waitForFunction(()=>document.querySelector('#cSave')?.textContent.includes('같은 작업'));
    await page.locator('#cSave').click();await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();
    let result=await api('GET','range?start=2026-10-17&end=2026-10-18');assert.equal(result.days[0].memos.filter(m=>m.body==='DRAFT SURVIVES DATE').length,1);
    const applied=requests.filter(r=>r.path.endsWith('/calendar/apply')).slice(-2);assert.deepEqual(applied[0].body,applied[1].body);cases.push('lost local response retries same operation and creates one note');
    await page.locator('[data-date="2026-10-17"]').click();await page.locator('#cAddEvent').click();await page.locator('[data-field=title]').fill('새 로컬 일정');await save();
    await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();
    assert.ok((await page.locator('#cDetail').innerText()).includes('새 로컬 일정'));cases.push('local event creation updates current day and count');
    await page.locator('#cAddNote').click();await page.locator('[data-field=body]').fill('FIRST VERSION');await page.locator('#cSave').click();
    holdApply=true;const appliedSeen=new Promise(r=>seenApply=r);await page.locator('#cApply').click();await appliedSeen;
    await page.locator('[data-field=body]').fill('NEW INPUT DURING SAVE');releaseApply();
    await page.waitForFunction(()=>document.querySelector('#cEditorStatus')?.textContent.includes('미저장 초안'));
    assert.equal(await page.locator('[data-field=body]').inputValue(),'NEW INPUT DURING SAVE');await save();
    await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();
    result=await api('GET','range?start=2026-10-17&end=2026-10-18');assert.equal(result.days[0].memos.filter(m=>m.body==='NEW INPUT DURING SAVE').length,1);
    assert.equal(result.days[0].memos.filter(m=>m.body==='FIRST VERSION').length,0);cases.push('typing during save remains draft and updates created identity without duplicate');
    const record=result.days[0].memos.find(m=>m.body==='NEW INPUT DURING SAVE');await page.locator(`[data-edit="${record.id}"]`).click();await page.locator('[data-field=body]').fill('LOCAL CONFLICT DRAFT');
    const plan=await api('POST','plan',{action:'update',kind:'note',id:record.id,expected_revision:record.revision,payload:{date:record.date,body:'EXTERNAL SAVED',project_ids:record.project_ids}});
    await api('POST','apply',{plan_id:plan.plan_id,operation_id:plan.operation_id});await page.locator('#cSave').click();await page.locator('#cRebase').waitFor();
    assert.equal(await page.locator('[data-field=body]').inputValue(),'LOCAL CONFLICT DRAFT');await page.locator('#cEditorStatus summary').click();assert.ok((await page.locator('#cEditorStatus').innerText()).includes('EXTERNAL SAVED'));
    await page.locator('#cRebase').click();await save();await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();cases.push('stale edit preserves both draft and current record before explicit rebase');
    await page.locator(`[data-edit="${record.id}"]`).click();await page.locator('#cDelete').click();await page.locator('#cApply').click();await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();
    assert.equal(await page.locator(`[data-edit="${record.id}"]`).count(),0);cases.push('explicit local deletion removes note from summary and detail');
    await page.locator('[aria-selected=true][data-date]').focus();await page.keyboard.press('ArrowRight');assert.equal(await page.locator('[aria-selected=true][data-date]').getAttribute('data-date'),'2026-10-18');
    await snapshot('desktop');await page.screenshot({path:path.join(out,'P016-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});const grid=await page.locator('#cGrid').boundingBox(),detail=await page.locator('#cDetail').boundingBox();
    assert.ok(detail.y>=grid.y+grid.height);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await snapshot('narrow');await page.screenshot({path:path.join(out,'P016-narrow.png'),fullPage:true});cases.push('keyboard date selection and narrow layout keep detail below grid without page overflow');
    await page.locator('#cDetail').screenshot({path:path.join(out,'P016-narrow-detail.png')});
    assert.deepEqual(errors,[]);
    const sha=file=>require('crypto').createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    fs.writeFileSync(path.join(out,'P016-evidence.json'),JSON.stringify({at:new Date().toISOString(),source:'actual local HTTP server, HTML and SQLite, synthetic records only',source_sha256:sha('static/index.html'),backend_hashes:Object.fromEntries(fs.readdirSync('wsflow').filter(f=>f.endsWith('.py')).map(f=>['wsflow/'+f,sha('wsflow/'+f)])),cases,errors,requests},null,2));
    console.log(JSON.stringify({passed:cases.length,cases,errors}));
  }catch(e){await page.screenshot({path:path.join(out,'P016-failure.png'),fullPage:true});await snapshot('failure');throw e;}
  finally{await browser.close();server.stdin.end('stop\n');await new Promise(resolve=>{const timer=setTimeout(()=>{server.kill();resolve();},5000);server.on('exit',()=>{clearTimeout(timer);resolve();});});}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
