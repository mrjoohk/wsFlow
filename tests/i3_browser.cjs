// Actual HTML/server/SQLite/DPAPI and loopback callback, fake Google transport.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT||'output/playwright/i3-'+Date.now());fs.mkdirSync(out,{recursive:true});
async function main(){
  const child=spawn('python',['-X','utf8','tests/i3_fixture_server.py'],{stdio:['pipe','pipe','pipe']});let stderr='';child.stderr.on('data',d=>stderr+=d);
  const config=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',d=>{text+=d;try{resolve(JSON.parse(text.split('\n')[0]));}catch{}});child.on('exit',code=>reject(Error('fixture '+code+' '+stderr)));setTimeout(()=>reject(Error('fixture startup '+stderr)),15000).unref();});
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
  const context=await browser.newContext({viewport:{width:1560,height:1120}}),page=await context.newPage(),cases=[],errors=[];
  page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));await page.clock.setFixedTime(new Date('2026-10-18T03:00:00Z'));
  let heldBootstrap=false,releaseBootstrap;
  const bootstrapGate=new Promise(resolve=>releaseBootstrap=resolve);
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin===config.url){
      if(!heldBootstrap&&url.pathname==='/api/v1/modules'){heldBootstrap=true;await bootstrapGate;}
      return route.continue();
    }
    if(url.origin==='https://accounts.google.com'){
      assert.equal(url.searchParams.get('code_challenge_method'),'S256');
      const callback=new URL(url.searchParams.get('redirect_uri'));callback.searchParams.set('state',url.searchParams.get('state'));callback.searchParams.set('code','synthetic-code');
      return route.fulfill({status:302,headers:{location:callback.toString()}});
    }
    assert.equal(url.hostname,'127.0.0.1','external traffic forbidden');assert.equal(url.pathname,'/oauth/callback');return route.continue();
  });
  try {
    const metadata=page.waitForRequest(request=>new URL(request.url()).pathname==='/api/v1/modules');
    await page.goto(config.url+'/?t='+config.token,{waitUntil:'domcontentloaded'});await metadata;
    for(const selector of ['#btnCalendar','#menuAdd','#menuTools','#rootSel'])
      assert.equal(await page.locator(selector).isVisible(),false,'business navigation waits for module readiness: '+selector);
    assert.equal(await page.locator('#btnModules').isVisible(),true);
    releaseBootstrap();
    await page.locator('#btnCalendar').click();await page.locator('#cStatus').filter({hasText:'저장된 기록을 확인했습니다.'}).waitFor();
    await page.locator('#gPanel summary').click();await page.getByText('Google 미연결',{exact:true}).waitFor();cases.push('unconnected local calendar remains usable');
    await page.locator('#gConnect').click();const popupPromise=context.waitForEvent('page');await page.getByText('Google 브라우저 승인 열기 (3분 이내)').click();const popup=await popupPromise;
    await popup.getByText('Google 연결 완료.',{exact:false}).waitFor();await page.locator('#gDisconnect').waitFor();await popup.close();cases.push('browser approval uses S256 and actual one-time loopback callback; UI observes connection');
    assert.equal(await page.locator('[data-gcal="1"]').isDisabled(),true);
    await page.locator('[data-gcal="0"]').check();await page.locator('#gSelect').click();await page.locator('#cDetail').getByText('Google 읽기 전용',{exact:true}).waitFor();
    const cell=page.locator('[data-date="2026-10-18"]');assert.ok((await cell.innerText()).includes('Google 일정'));assert.ok(!(await cell.innerText()).includes('GOOGLE PRIVATE BODY'));
    assert.equal(await page.locator('#cDetail [data-edit]').count(),0);assert.equal(await page.locator('#cDetail img').count(),0);assert.ok((await page.locator('#cDetail').innerText()).includes('GOOGLE PRIVATE BODY'));
    assert.equal(await page.locator('#cDetail .cal-entry').count(),1);cases.push('selected readable calendar appears with origin and role; canceled occurrence hidden; body escaped; remote edit absent');
    await page.locator('#cSource').selectOption('local');await page.locator('#cStatus').filter({hasText:'저장된 기록을 확인했습니다.'}).waitFor();assert.equal(await page.locator('#cDetail .cal-entry').count(),0);
    await page.locator('#cSource').selectOption('google');await page.locator('#cDetail .cal-entry').waitFor();cases.push('source filter and same-day counts/detail agree');
    await page.locator('#cAddNote').click();await page.locator('[data-field=body]').fill('LOCAL DRAFT SURVIVES GOOGLE');await page.locator('#cClose').click();
    fs.writeFileSync(config.control,JSON.stringify({fail:503}));await page.locator('#gSync').click();await page.locator('#cStatus').filter({hasText:'일부 기록'}).waitFor();
    assert.ok((await cell.getAttribute('aria-label')).includes('일정 ?'));assert.ok((await page.locator('#cDetail').innerText()).includes('GOOGLE PRIVATE BODY'));
    await page.locator('#cDrafts button').click();assert.equal(await page.locator('[data-field=body]').inputValue(),'LOCAL DRAFT SURVIVES GOOGLE');await page.locator('#cClose').click();cases.push('offline sync retains cached body, count becomes unknown and local draft survives');
    fs.writeFileSync(config.control,'{}');await page.locator('#gSync').click();await page.locator('#cStatus').filter({hasText:'저장된 기록을 확인했습니다.'}).waitFor();cases.push('manual recovery restores complete snapshot and last-sync status');
    fs.writeFileSync(path.join(out,'connected.yml'),await page.locator('body').ariaSnapshot());await page.screenshot({path:path.join(out,'connected.png'),fullPage:true});
    await page.locator('#gDisconnect').click();await page.getByText('Google 미연결',{exact:true}).waitFor();await page.locator('#cStatus').filter({hasText:'저장된 기록을 확인했습니다.'}).waitFor();assert.equal(await page.locator('#cDetail .cal-entry').count(),0);cases.push('disconnect hides account events while local drafts remain');
    assert.deepEqual(errors,[]);
    const evidence={passed:cases.length,cases,errors,live_google:'not_run',source_sha256:crypto.createHash('sha256').update(fs.readFileSync('static/index.html')).digest('hex')};
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));
  } finally {releaseBootstrap();await browser.close();child.stdin.end('stop\n');await new Promise(resolve=>child.on('exit',resolve));if(stderr)console.error(stderr);}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
