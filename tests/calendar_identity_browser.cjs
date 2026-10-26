// Actual local server/UI/SQLite; no external account and no policy changes.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT||'output/uid-'+Date.now());fs.mkdirSync(out,{recursive:true});
const cases=[],errors=[],requests=[];let server,browser,config,page;
async function api(method,endpoint,body,expected=200,token=true){
  const response=await page.request.fetch(config.url+'/api/v2/calendar/'+endpoint,{method,headers:token?{'X-Auth-Token':config.token}:{},data:body});
  const value=await response.json();assert.equal(response.status(),expected,JSON.stringify(value));return value;
}
async function ready(){await page.waitForFunction(()=>document.querySelector('#cStatus')?.textContent.includes('저장된 기록을 확인했습니다.'));}
async function main(){
  try{
    server=spawn(process.env.PYTHON||'python',['-B','-X','utf8','tests/calendar_identity_fixture.py'],{stdio:['pipe','pipe','pipe']});
    let stderr='';server.stderr.on('data',d=>stderr+=d);
    config=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('fixture timeout '+stderr)),15000);server.stdout.on('data',d=>{text+=d;try{const v=JSON.parse(text.split('\n')[0]);clearTimeout(timer);resolve(v);}catch{}});server.on('exit',code=>{clearTimeout(timer);reject(Error('fixture '+code+' '+stderr));});});
    browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});page=await browser.newPage({viewport:{width:1450,height:1000}});page.setDefaultTimeout(12000);
    await page.clock.setFixedTime(new Date('2026-10-17T03:00:00Z'));page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{const request=route.request(),url=new URL(request.url());assert.equal(url.origin,config.url);if(url.pathname.startsWith('/api/'))requests.push({method:request.method(),path:url.pathname,body:request.postDataJSON()});return route.continue();});
    await page.goto(config.url+'/?t='+config.token);await page.locator('#btnCalendar').click();await ready();
    assert.ok((await page.locator('#cDetail').innerText()).includes('HISTORICAL NOTE BODY'));
    assert.ok((await page.locator('#cDetail').innerText()).includes('소속 확인 불가'));
    assert.equal((await api('GET','range?start=2026-10-17&end=2026-10-18&source=local')).days[0].memo_count,1);
    cases.push('global view preserves historical note and explains unavailable association');
    await page.locator('#cProject').selectOption('p');await ready();
    assert.equal((await api('GET','range?start=2026-10-17&end=2026-10-18&source=local&project_id=p')).days[0].memo_count,0);
    assert.ok((await page.locator('#cStatus').innerText()).includes('프로젝트 필터에 자동 포함하지 않습니다'));
    assert.equal(await page.locator(`[data-edit="${config.record_id}"]`).count(),0);
    assert.ok(!(await page.locator('#cGrid').innerText()).includes('HISTORICAL NOTE BODY'));
    cases.push('new UID project excludes old note while date cells retain counts only');
    await api('POST','apply',config.old_plan,409);cases.push('HTTP rejects stale UID plan without creating a note');
    await api('POST','plan',{action:'create',kind:'note',payload:{date:'2026-10-17',body:'forged',project_ids:['p'],project_uid:'old-project'}},400);
    await api('POST','plan',{action:'update',kind:'note',id:config.record_id,expected_revision:1,rebind_project_ids:['p'],payload:{date:'2026-10-17',body:'forged',project_ids:['p']}},403,false);
    cases.push('HTTP refuses client UID injection and unapproved rebind without token');
    await page.locator('#cProject').selectOption('');await ready();await page.locator(`[data-edit="${config.record_id}"]`).click();
    await page.locator('[data-field=body]').fill('EDITED HISTORICAL BODY');await page.locator('#cSave').click();await page.locator('#cApply').click();await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();
    let value=await api('GET','range?start=2026-10-17&end=2026-10-18&source=local');let note=value.days[0].memos[0];
    assert.equal(note.body,'EDITED HISTORICAL BODY');assert.equal(note.project_references.p.project_uid,'old-project');assert.equal(note.revision,2);
    cases.push('ordinary body edit preserves the old UID association');
    await page.locator(`[data-edit="${config.record_id}"]`).click();await page.locator('[data-rebind=p]').check();await page.locator('#cSave').click();await page.locator('#cApply').waitFor();
    assert.ok((await page.locator('#cPreview').innerText()).includes('현재 프로젝트로 명시 재연결'));
    value=await api('GET','range?start=2026-10-17&end=2026-10-18&source=local&project_id=p');assert.equal(value.days[0].memo_count,0);
    cases.push('rebind preview leaves record untouched until explicit approval');
    await page.locator('#cApply').click();await page.waitForFunction(()=>!document.querySelector('#dlg').open);await ready();await page.locator('#cProject').selectOption('p');await ready();
    value=await api('GET','range?start=2026-10-17&end=2026-10-18&source=local&project_id=p');note=value.days[0].memos[0];
    assert.equal(value.days[0].memo_count,1);assert.equal(note.id,config.record_id);assert.equal(note.project_references.p.project_uid,'new-project');assert.equal(note.revision,3);
    assert.ok((await page.locator('#cDetail').innerText()).includes('EDITED HISTORICAL BODY'));cases.push('approved rebind updates one existing record and project detail');
    await page.screenshot({path:path.join(out,'uid-rebound.png'),fullPage:true});assert.deepEqual(errors,[]);
    const sha=p=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
    const backend_hashes=Object.fromEntries(fs.readdirSync('wsflow').filter(n=>n.endsWith('.py')).map(n=>['wsflow/'+n,sha('wsflow/'+n)]));
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify({status:'PASS',cases,errors,requests,source_sha256:sha('static/index.html'),backend_hashes,browser:browser.version(),provider:'synthetic identity replacement; actual local server/UI/SQLite',remote_business_writes:0},null,2));
    console.log(JSON.stringify({passed:cases.length,cases,errors}));
  }catch(error){fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({status:'FAIL',error:String(error),cases,errors},null,2));throw error;}
  finally{if(browser)await browser.close();if(server){server.stdin.end('\n');await new Promise(resolve=>{server.once('exit',resolve);setTimeout(()=>{server.kill();resolve();},7000).unref();});}}
}
const timeout=setTimeout(()=>{console.error('UID browser timeout');if(server)server.kill();process.exit(124);},90000);
main().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>clearTimeout(timeout));
