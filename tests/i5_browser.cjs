// Actual HTML/HTTP/files/SQLite in a temporary ROOT. No real account or CLI session.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('child_process'),fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const out=path.resolve(process.env.WSFLOW_BROWSER_OUTPUT||'output/playwright/i5-'+Date.now());fs.mkdirSync(out,{recursive:true});
async function main(){
  const child=spawn('python',['-X','utf8','tests/i5_fixture_server.py'],{stdio:['pipe','pipe','pipe']});let stderr='';child.stderr.on('data',d=>stderr+=d);
  const config=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',d=>{text+=d;try{resolve(JSON.parse(text.split('\n')[0]));}catch{}});child.on('exit',code=>reject(Error('fixture '+code+' '+stderr)));setTimeout(()=>reject(Error('startup '+stderr)),15000).unref();});
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
  const page=await browser.newPage({viewport:{width:1460,height:1100}}),cases=[],errors=[];let drop=false,delay=null;
  page.setDefaultTimeout(14000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    assert.equal(new URL(route.request().url()).origin,config.url,'external requests forbidden');
    if(drop && route.request().url().endsWith('/apply')){drop=false;await route.fetch();return route.abort('failed');}
    if(delay && route.request().url().endsWith('/context/plan')){const response=await route.fetch();const release=delay;delay=null;await release;return route.fulfill({response});}
    return route.continue();
  });
  const file=(...parts)=>path.join(config.home,...parts),root=(...parts)=>path.join(config.root,...parts);
  const read=(...parts)=>fs.readFileSync(file(...parts),'utf8');
  const open=async()=>{await page.locator('#ctxOpen').click();await page.locator('#ctxPreview').waitFor();};
  const options=async()=>{await page.locator('#ctxBackend').selectOption('managed-copy');await page.locator('#ctxAdopt').check();};
  const close=async()=>{await page.locator('#ctxClose').click();await page.locator('#dlg').waitFor({state:'hidden'});};
  async function preview(){await page.locator('#ctxPreview').click();await page.locator('#ctxPlan').getByText(/변경 \d+개 항목/).waitFor();}
  async function applied(){await page.locator('#ctxApply').click();await page.locator('#ctxBackend').waitFor();await page.waitForFunction(()=>document.querySelector('#ctxBackend')?.value==='');}
  try{
    await page.goto(config.url+'/?t='+config.token);await page.locator(`[data-id="${config.pid}"]`).first().click();await page.getByRole('button',{name:'규칙',exact:true}).click();await page.locator('#ctxOpen').waitFor();
    await open();assert.ok((await page.locator('#ctxObservation').innerText()).includes('미검증'));await page.locator('#ctxPreview').click();await page.getByText(/차단:.*backend_not_supported/).waitFor();assert.ok(await page.locator('#ctxApply').isDisabled());cases.push('explicit backend required and tool recognition remains unknown');
    await options();await page.locator('#ctxRootPreview').click();await page.locator('#ctxPlan').getByText('root_init',{exact:true}).waitFor();assert.ok((await page.locator('#ctxPlan').innerText()).includes(config.other));assert.ok(!fs.existsSync(root('.wsflow')));cases.push('ROOT preview is read-only and lists both impacted projects');
    await applied();assert.equal(fs.readFileSync(root('GLOBAL_RULES.md'),'utf8'),'COMMON USER RULES\r\n');assert.ok(!fs.existsSync(file('.wsflow')));cases.push('ROOT apply preserves common policy and does not prepare projects');
    await options();await page.locator('[data-ctx-skill="0"]').check();await preview();await page.locator('#ctxPlan details').filter({has:page.locator('summary').filter({hasText:'AGENTS.md'})}).locator('summary').click();assert.ok((await page.locator('#ctxPlan').innerText()).includes('USER HEADER'));await page.locator('#ctxAdopt').uncheck();assert.ok(await page.locator('#ctxApply').isDisabled());await page.locator('#ctxAdopt').check();await preview();cases.push('user-content diff is reviewable and changing selection invalidates plan');
    let release;delay=new Promise(r=>release=r);await page.locator('#ctxPreview').click();await page.waitForTimeout(250);await page.locator('[data-ctx-skill="1"]').check();release();await page.waitForTimeout(350);assert.ok(await page.locator('#ctxApply').isDisabled());await page.locator('[data-ctx-skill="1"]').uncheck();await preview();cases.push('late preview cannot re-enable approval after selection changed');
    drop=true;await page.locator('#ctxApply').click();await page.locator('#ctxRetry').waitFor();assert.ok(fs.existsSync(file('.wsflow','project.json')));const uid=JSON.parse(read('.wsflow','project.json')).project_uid;
    await page.locator('#ctxRetry').click();await page.waitForFunction(()=>document.querySelector('#ctxBackend')?.value==='');assert.equal(JSON.parse(read('.wsflow','project.json')).project_uid,uid);assert.equal(read('AGENTS.md').split('WSFLOW:BEGIN').length-1,1);cases.push('lost approval response retries same plan once without duplicate managed block or UID');
    assert.ok(read('AGENTS.md').startsWith('USER HEADER\r\nNO NEWLINE'));assert.equal(read('.agents','skills','alpha','references','data.md'),'SUPPORT alpha');assert.equal(read('.claude','skills','alpha','references','data.md'),'SUPPORT alpha');assert.ok(!fs.existsSync(file('.agents','skills','unselected')));assert.equal(read('.claude','skills','legacy','SKILL.md'),'LEGACY USER SKILL');assert.equal(read('PROJECT_RULES.md'),'PROJECT USER POLICY');assert.equal(read('.workflow','state.json'),'{"watermark":"keep","roots":{"x":1}}');cases.push('only selected full packages exposed while entrypoint/project/Jira/legacy bytes remain');
    await options();await preview();fs.appendFileSync(root('skills','alpha','references','data.md'),' LATE');await page.locator('#ctxApply').click();await page.locator('#ctxStatus').filter({hasText:'stale-plan'}).waitFor();assert.equal(read('.agents','skills','alpha','references','data.md'),'SUPPORT alpha');await close();cases.push('source changed after preview causes zero new user-file effects');
    await open();await options();await preview();await applied();assert.equal(read('.agents','skills','alpha','references','data.md'),'SUPPORT alpha LATE');cases.push('fresh reviewed plan updates tracked package from changed canonical');
    fs.appendFileSync(file('AGENTS.md'),'\nUSER AFTER BLOCK\n');await options();await preview();await applied();assert.ok(read('AGENTS.md').endsWith('USER AFTER BLOCK\n'));cases.push('reinitialization preserves post-init user edits outside managed block');
    fs.appendFileSync(file('.agents','skills','alpha','references','data.md'),' USER EDIT');await options();await preview();assert.ok((await page.locator('#ctxPlan').innerText()).includes('managed_package_changed'));assert.ok(await page.locator('#ctxApply').isDisabled());assert.ok(read('.agents','skills','alpha','references','data.md').endsWith('USER EDIT'));cases.push('user edits inside managed package block overwrite and remain intact');
    await page.setViewportSize({width:800,height:1100});await page.screenshot({path:path.join(out,'narrow.png'),fullPage:true});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    fs.writeFileSync(path.join(out,'final.yml'),await page.locator('body').ariaSnapshot());assert.deepEqual(errors,[]);
    const backend_hashes=Object.fromEntries(['context_projection.py','context_skills.py','deploy.py','server.py'].map(f=>[f,crypto.createHash('sha256').update(fs.readFileSync(path.join('wsflow',f))).digest('hex')]));
    const evidence={passed:cases.length,cases,errors,source_sha256:crypto.createHash('sha256').update(fs.readFileSync('static/index.html')).digest('hex'),backend_hashes,actual_tool_recognition:'unknown',external_requests:0};fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));
  }catch(e){fs.writeFileSync(path.join(out,'failure.yml'),await page.locator('body').ariaSnapshot());throw e;}finally{await browser.close();child.stdin.end('stop\n');await new Promise(resolve=>child.on('exit',resolve));if(stderr)console.error(stderr);}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
