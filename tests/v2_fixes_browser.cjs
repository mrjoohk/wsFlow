const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT||'output/playwright/v2-fixes-'+Date.now());fs.mkdirSync(out,{recursive:true});
async function bounded(p,ms=20000){let timer;try{return await Promise.race([p,new Promise((_,r)=>timer=setTimeout(()=>r(Error('timeout')),ms))]);}finally{clearTimeout(timer);}}
async function main(){
  const child=spawn('python',['-X','utf8','tests/v2_fixes_fixture_server.py'],{stdio:['pipe','pipe','pipe']});let stderr='',browser;
  child.stderr.on('data',d=>stderr+=d);const cases=[],errors=[];
  try{
    const c=await bounded(new Promise((resolve,reject)=>{let text='';child.stdout.on('data',d=>{text+=d;try{resolve(JSON.parse(text.split('\n')[0]));}catch{}});child.on('error',reject);child.on('exit',()=>reject(Error(stderr)));}));
    const sp=path.join(c.home,'.workflow/state.json'),original=fs.readFileSync(sp),user=fs.readFileSync(path.join(c.home,'USER.txt')),memo=fs.readFileSync(path.join(c.home,'daily/261018.md'));
    browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});const page=await browser.newPage();page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));let lose=true,applyIds=[];
    await page.route('**/*',async route=>{const req=route.request(),url=new URL(req.url());assert.equal(url.origin,c.url,'external traffic prohibited');
      if(url.pathname.endsWith('/migration/apply')){applyIds.push(JSON.parse(req.postData()));const r=await route.fetch();if(lose){lose=false;return route.abort('failed');}return route.fulfill({response:r});}return route.continue();});
    await page.goto(c.url+'/?t='+c.token);await page.locator(`#list [data-id="${c.pid}"]`).click();await page.locator('.tabs [data-tab="jira"]').click();
    await page.locator('#jMigratePlan').waitFor();assert.equal(await page.locator('#jBatch').isDisabled(),true);cases.push('schema3 exposes preservation transition and retains batch gate');
    await page.locator('#jMigratePlan').click();await page.locator('#jMigrateApply').waitFor();assert.deepEqual(fs.readFileSync(sp),original);cases.push('server preview leaves original state bytes intact');
    await page.locator('#jMigrateApply').click();await page.locator('#jMigrateStatus').filter({hasText:'응답을 확인하지 못했습니다'}).waitFor();
    const backups=fs.readdirSync(path.join(c.home,'.workflow/migrations'));assert.equal(backups.length,1);assert.deepEqual(fs.readFileSync(path.join(c.home,'.workflow/migrations',backups[0],'state.before.json')),original);
    const after=JSON.parse(fs.readFileSync(sp,'utf8')),before=JSON.parse(original.toString('utf8').replace(/^\uFEFF/,''));assert.deepEqual(after,{...before,schema_version:4});assert.deepEqual(fs.readFileSync(path.join(c.home,'USER.txt')),user);assert.deepEqual(fs.readFileSync(path.join(c.home,'daily/261018.md')),memo);cases.push('lost response follows schema-only replacement with byte backup and user files preserved');
    await page.locator('#jMigrateCheck').click();await page.locator('#jMigrateStatus').filter({hasText:'전환된 설정을 확인'}).waitFor();cases.push('result check proves local target and original backup after response loss');
    await page.locator('#jMigrateApply').click();await page.locator('#jMigratePlan').waitFor({state:'detached'});assert.deepEqual(applyIds[0],applyIds[1]);assert.equal(await page.locator('#jBatch').isEnabled(),true);assert.equal(fs.readdirSync(path.join(c.home,'.workflow/migrations')).length,1);cases.push('same operation retry reuses result and refreshes schema4 UI');
    assert.deepEqual(errors,[]);await page.screenshot({path:path.join(out,'schema4.png'),fullPage:true});
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify({cases,errors,remote_business_writes:0,provider:'synthetic reads; actual local server/UI/files',browser:browser.version()},null,2));console.log(JSON.stringify({cases:cases.length,errors:errors.length,out}));
  }catch(e){fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({error:e.stack,stderr,cases,errors},null,2));throw e;}
  finally{if(browser)await browser.close();if(child.exitCode===null){const exited=new Promise(r=>child.once('exit',r));child.stdin.end('stop\n');try{await bounded(exited,8000);}catch(e){child.kill();throw e;}}}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
