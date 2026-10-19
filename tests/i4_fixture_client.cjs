// Fixture-only JSONL channel. No shared control/stat files or browser imports.
const {spawn}=require('child_process'),{createInterface}=require('readline'),{randomUUID}=require('crypto');
async function bounded(promise,label,ms){
  let timer;
  try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(label+' timeout')),ms);})]);}
  finally{clearTimeout(timer);}
}
function fixture(command='python',args=['-X','utf8','tests/i4_fixture_server.py'],timeout=10000,
                 {startupTimeout=20000,shutdownTimeout=8000,killTimeout=3000}={}){
  const child=spawn(command,args,{stdio:['pipe','pipe','pipe']});
  const pending=new Map(),controls=[];let stderr='',failure,started=false,readyResolve,readyReject;
  const ready=new Promise((resolve,reject)=>{readyResolve=resolve;readyReject=reject;});
  const exited=new Promise(resolve=>child.once('close',resolve));
  function fail(error){failure=error;readyReject(error);for(const p of pending.values())p.reject(error);pending.clear();}
  child.stderr.on('data',d=>{stderr=(stderr+d).slice(-16000);});
  child.on('error',fail);child.stdin.on('error',fail);
  child.once('exit',(code,signal)=>fail(Error(`fixture exit ${code}/${signal}: ${stderr}`)));
  const lines=createInterface({input:child.stdout});
  lines.on('line',line=>{
    try{
      const reply=JSON.parse(line);
      if(!started){if(reply.protocol!==1)throw Error('fixture protocol');started=true;readyResolve(reply);return;}
      const p=pending.get(reply.id);if(!p)throw Error('fixture unexpected reply');
      pending.delete(reply.id);
      if(reply.ok!==true)p.reject(Error('fixture command rejected: '+reply.error));else p.resolve(reply.result);
    }catch(error){fail(error);}
  });
  return {child,controls,get stderr(){return stderr;},
    async start(){try{return await bounded(ready,'fixture startup',startupTimeout);}catch(e){fail(e);throw e;}},
    async request(action,data={}){
      if(failure)throw failure;
      const id=randomUUID();
      const response=new Promise((resolve,reject)=>pending.set(id,{resolve,reject}));
      child.stdin.write(JSON.stringify({id,action,data})+'\n');
      try{const result=await bounded(response,'fixture '+action,timeout);controls.push({id,action,acknowledged:true});return result;}
      catch(e){pending.delete(id);fail(e);throw e;}
    },
    async close(){
      try{
        if(failure){child.kill();await bounded(exited,'fixture failed exit',killTimeout);return;}
        if(child.exitCode===null&&child.signalCode===null){
          if(!failure)await this.request('stop');
          child.stdin.end();await bounded(exited,'fixture shutdown',shutdownTimeout);
        }
      }catch(error){child.kill();await bounded(exited,'fixture killed exit',killTimeout);throw error;}
      finally{lines.close();}
    }
  };
}
module.exports={fixture,bounded};
