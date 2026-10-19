// Actual child processes and production fixture protocol; bounded by Python owner.
const assert=require('assert/strict');
const {fixture,bounded}=require('./i4_fixture_client.cjs');
const mode=process.argv[2];
function owned(client){
  // Independent owner deadline also closes descendants when a mutated deadline
  // no longer rejects. A failed mutant must not leave its fixture running.
  const timer=setTimeout(()=>client.child.kill(),10000);
  client.child.once('close',()=>clearTimeout(timer));
  return client;
}
async function main(){
  if(mode==='actual'){
    const client=owned(fixture());
    try{
      const config=await client.start();assert.equal(config.protocol,1);assert.match(config.url,/^http:\/\/127\.0\.0\.1:/);
      for(let n=0;n<50;n++)assert.deepEqual(await client.request('control',n%2?{fail:503}:{}),{applied:true});
      assert.deepEqual(await client.request('stats'),{writes:[],events:[]});
      assert.equal(new Set(client.controls.map(c=>c.id)).size,51);
      assert.equal(client.controls.every(c=>c.acknowledged),true);
    }finally{await client.close();}
    assert.notEqual(client.child.exitCode,null);return;
  }
  if(mode==='provider_rejection'){
    const client=owned(fixture());
    try{await client.start();await assert.rejects(client.request('control',{edit_id:'missing',summary:'invalid edit'}),/missing/);}
    finally{await client.close();}
    assert.ok(client.child.exitCode!==null||client.child.signalCode!==null);return;
  }
  if(mode==='bounds'){
    await assert.rejects(bounded(new Promise(()=>{}),'controlled',20),/controlled timeout/);
    assert.equal(await bounded(Promise.resolve(7),'immediate',100),7);return;
  }
  if(mode==='startup_timeout'||mode==='shutdown_timeout'){
    const program=mode==='startup_timeout'?'setInterval(()=>{},1000)':`
      const lines=require('readline').createInterface({input:process.stdin});setInterval(()=>{},1000);
      console.log(JSON.stringify({protocol:1}));
      lines.on('line',line=>{const r=JSON.parse(line);console.log(JSON.stringify({id:r.id,ok:true,result:{stopping:true}}));});`;
    const client=owned(fixture(process.execPath,['-e',program],1000,{startupTimeout:mode==='startup_timeout'?100:5000,shutdownTimeout:100,killTimeout:2000}));
    if(mode==='startup_timeout'){
      try{await assert.rejects(client.start(),/fixture startup timeout/);}finally{await client.close();}
    }else{
      await client.start();await assert.rejects(client.close(),/fixture shutdown timeout/);
    }
    assert.ok(client.child.exitCode!==null||client.child.signalCode!==null);return;
  }
  const program=mode==='startup'?'process.exit(3)':`
    const readline=require('readline').createInterface({input:process.stdin});
    console.log(JSON.stringify({protocol:1}));
    readline.on('line',line=>{const r=JSON.parse(line);
      if('${mode}'==='timeout')return;
      if('${mode}'==='malformed'){console.log('broken JSON');return;}
      console.log(JSON.stringify({id:'${mode}'==='wrong_id'?'wrong':r.id,ok:r.action==='stats'||'${mode}'!=='rejected',error:'controlled rejection',result:7}));
    });
  `;
  const client=owned(fixture(process.execPath,['-e',program],100));
  try{
    if(mode==='startup'){await assert.rejects(client.start(),/fixture exit/);return;}
    await client.start();
    const expected={wrong_id:/unexpected reply/,malformed:/JSON|Unexpected/,rejected:/controlled rejection/,timeout:/fixture control timeout/}[mode];
    const begin=Date.now();await assert.rejects(client.request('control'),expected);
    assert.ok(Date.now()-begin<2000,'command rejection is bounded');
    await assert.rejects(client.request('stats'),expected);assert.equal(client.controls.length,0);
  }finally{await client.close();}
  assert.ok(client.child.exitCode!==null||client.child.signalCode!==null);
}
main().then(()=>console.log(JSON.stringify({mode,status:'PASS'}))).catch(e=>{console.error(e.stack);process.exitCode=1;});
