// Real app/SQLite/DPAPI/loopback, synthetic Google boundary; no real account traffic.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT||'output/playwright/i4-'+Date.now());fs.mkdirSync(out,{recursive:true});
async function main(){
  const {fixture,bounded}=require('./i4_fixture_client.cjs');
  const client=fixture();let browser,page;
  const cases=[],errors=[];let dropApply=false;
  try{
  const config=await client.start();
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
  const context=await browser.newContext({viewport:{width:1560,height:1120}});page=await context.newPage();
  page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));await page.clock.setFixedTime(new Date('2026-10-18T03:00:00Z'));
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin===config.url){
      if(dropApply&&url.pathname.endsWith('/google/write/apply')){dropApply=false;await route.fetch();return route.abort('failed');}
      return route.continue();
    }
    if(url.origin==='https://accounts.google.com'){
      assert.equal(url.searchParams.get('code_challenge_method'),'S256');
      const callback=new URL(url.searchParams.get('redirect_uri'));callback.searchParams.set('state',url.searchParams.get('state'));
      callback.searchParams.set('code',url.searchParams.get('scope').includes('auth/calendar.events ')||url.searchParams.get('scope').endsWith('auth/calendar.events')?'write':'read');
      return route.fulfill({status:302,headers:{location:callback.toString()}});
    }
    assert.equal(url.hostname,'127.0.0.1','external traffic forbidden');assert.equal(url.pathname,'/oauth/callback');return route.continue();
  });
  const stats=()=>client.request('stats');
  const control=data=>client.request('control',data);
  async function ready(){await page.locator('#cStatus').filter({hasText:'저장된 기록을 확인했습니다.'}).waitFor();}
  async function connect(button){
    await page.locator(button).click();const promise=context.waitForEvent('page');await page.getByText('Google 브라우저 승인 열기 (3분 이내)').click();const popup=await promise;
    await popup.getByText('Google 연결 완료.',{exact:false}).waitFor();await popup.close();
    await page.waitForFunction(()=>!document.querySelector('#gAuthorization a'));
  }
  async function add(title,google=true){
    await page.locator('#cAddEvent').click();await page.locator('[data-field=title]').fill(title);await page.locator('[data-field=body]').fill('EVENT PRIVATE BODY');
    if(google)await page.locator('#cDestination').selectOption('cal');
    await page.locator('#cSave').click();await page.locator('#cApply').waitFor();await page.locator('#cApply').click();await page.locator('#dlg').waitFor({state:'hidden'});await page.locator('#cDetail .cal-entry').filter({hasText:title}).waitFor();await ready();
  }
  const detail=title=>page.locator('#cDetail .cal-entry').filter({has:page.locator('b').filter({hasText:title})});
  const link=title=>page.locator('#gOutbox .cal-entry').filter({has:page.locator('b').filter({hasText:title})});
  async function saveEdit(title,newTitle){await detail(title).locator('[data-edit]').click();await page.locator('[data-field=title]').fill(newTitle);await page.locator('#cSave').click();await page.locator('#cApply').click();await page.locator('#dlg').waitFor({state:'hidden'});await detail(newTitle).waitFor();await ready();}
    await page.goto(config.url+'/?t='+config.token);await page.locator('#btnCalendar').click();await ready();await page.locator('#gPanel summary').click();await page.locator('#gConnect').waitFor();
    await connect('#gConnect');await page.locator('#gWrite').waitFor();assert.equal((await stats()).writes.length,0);
    await page.locator('[data-gcal="0"]').check();await page.locator('#gSelect').click();await ready();
    await connect('#gWrite');await page.locator('[data-gcal="0"]').waitFor();assert.equal(await page.locator('#gWrite').count(),0);cases.push('explicit write OAuth upgrade preserves read connection and selected calendar');
    await add('direct Google');assert.equal((await stats()).events.length,1);assert.equal(await page.locator('#cDetail .cal-entry').count(),1);assert.ok((await link('direct Google').innerText()).includes('동기화됨'));assert.ok(!(await detail('direct Google').innerText()).includes('로컬에만 저장'));
    assert.ok(!(await page.locator('[data-date="2026-10-18"]').innerText()).includes('EVENT PRIVATE BODY'));cases.push('new event target Google creates one remote identity and one display without private body in cell');
    await saveEdit('direct Google','local updated');assert.equal((await stats()).events[0].title,'local updated');assert.equal((await stats()).writes.at(-1).method,'PATCH');assert.ok((await stats()).writes.at(-1).if_match);cases.push('linked local edit uses conditional PATCH and keeps one remote event');
    const remote=(await stats()).events[0].id;await control({edit_id:remote,summary:'remote updated'});await page.locator('#gSync').click();await detail('remote updated').waitFor();cases.push('Google change pulls into linked local revision without duplicate');
    await add('existing local',false);const before=(await stats()).writes.length;
    await detail('existing local').locator('[data-gpublish]').click();await page.locator('#gPublishPreview').click();await page.locator('#gApprove').waitFor();dropApply=true;await page.locator('#gApprove').click();await page.locator('#gPending').waitFor();
    await page.locator('#gPending').click();await page.locator('#gPending').waitFor({state:'hidden'});assert.equal((await stats()).writes.length,before+1);assert.equal((await stats()).events.length,2);cases.push('lost app approval reply retries the same plan and remote write occurs once');
    await control({drop:'POST'});await add('lost remote');await link('lost remote').locator('[data-greconcile]').waitFor();assert.equal(await link('lost remote').locator('[data-gsend]').count(),0);const count=(await stats()).writes.length;await control({});
    await link('lost remote').locator('[data-greconcile]').click();await page.waitForFunction(()=>!document.querySelector('[data-greconcile]'));assert.equal((await stats()).writes.length,count);assert.equal((await stats()).events.length,3);cases.push('remote response loss stays uncertain and GET reconcile confirms without second insert');
    await control({fail:503});await detail('existing local').locator('[data-edit]').click();await page.locator('[data-field=title]').fill('offline local');await page.locator('#cSave').click();await page.locator('#cApply').click();
    await detail('offline local').waitFor();await link('offline local').locator('[data-gsend]').waitFor();assert.equal((await stats()).writes.length,count);
    await control({});await link('offline local').locator('[data-gsend]').click();await page.waitForFunction(()=>!document.querySelector('[data-gsend]'));await ready();
    assert.ok((await stats()).events.some(e=>e.title==='offline local'));cases.push('offline edit remains local with visible durable error and explicit retry sends the same operation');
    await control({edit_id:remote,summary:'remote conflict'});await saveEdit('remote updated','local conflict');await link('local conflict').getByText('로컬 내용',{exact:true}).waitFor();
    assert.ok((await link('local conflict').innerText()).includes('remote conflict'));assert.equal((await stats()).events.find(e=>e.id===remote).title,'remote conflict');
    await link('local conflict').locator('[data-gresolve][data-choice=remote]').click();await page.locator('#gApprove').click();await detail('remote conflict').waitFor();cases.push('both changed preserves both bodies and explicit Google adoption resolves conflict');
    await detail('remote conflict').locator('[data-edit]').click();await page.locator('#cDelete').click();await page.locator('#cApply').waitFor();assert.ok((await page.locator('#cPreview').innerText()).includes('Google 원본은 유지'));await page.locator('#cApply').click();await page.locator('#dlg').waitFor({state:'hidden'});await ready();
    assert.ok((await stats()).events.some(e=>e.id===remote));await link('remote conflict').locator('[data-gshow]').waitFor();cases.push('local hide suppresses mapped remote display while preserving Google original');
    await link('remote conflict').locator('[data-gshow]').click();await page.locator('#gApprove').click();await detail('remote conflict').waitFor();
    await link('remote conflict').locator('[data-gdelete]').click();await page.locator('#gApprove').click();await page.waitForFunction(()=>document.querySelector('#gOutbox')?.textContent.includes('화면에서 숨김'));
    assert.ok(!(await stats()).events.some(e=>e.id===remote));assert.equal((await stats()).writes.at(-1).method,'DELETE');assert.ok((await stats()).writes.at(-1).if_match);cases.push('explicit Google deletion uses separate approval and If-Match');
    await page.locator('#cAddNote').click();await page.locator('[data-field=body]').fill('NOTE PRIVATE NEVER UPLOAD');assert.equal(await page.locator('#cDestination').count(),0);await page.locator('#cClose').click();cases.push('note editor offers no Google destination and draft remains local');
    await page.setViewportSize({width:800,height:1100});await page.screenshot({path:path.join(out,'narrow.png'),fullPage:true});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    fs.writeFileSync(path.join(out,'final.yml'),await page.locator('body').ariaSnapshot());assert.deepEqual(errors,[]);
    const evidence={passed:cases.length,cases,errors,live_google:'not_run',source_sha256:crypto.createHash('sha256').update(fs.readFileSync('static/index.html')).digest('hex'),provider:(await stats()),fixture_controls:client.controls};
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify({passed:cases.length,cases,errors}));
  }catch(e){
    fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({error:e.stack,cases,errors,controls:client.controls,fixture_stderr:client.stderr},null,2));
    if(page)fs.writeFileSync(path.join(out,'failure.yml'),await page.locator('body').ariaSnapshot().catch(()=>''));
    throw e;
  }finally{
    try{if(browser)await bounded(browser.close(),'browser close',5000);}
    finally{await client.close();}
    if(client.stderr)console.error(client.stderr);
  }
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
