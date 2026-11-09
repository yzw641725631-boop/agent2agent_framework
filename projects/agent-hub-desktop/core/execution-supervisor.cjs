'use strict';
// Separate Node process: loss of the desktop pipe/heartbeat stops the owned CLI tree.
const {spawn}=require('node:child_process'),path=require('node:path'),readline=require('node:readline');
const {fingerprint,environment}=require('./execution-runtime.cjs');
let child,lastPulse=Date.now(),started=false,stopping=false,finished=false,limit,cleanup;
let closedResult,groupCleaned=process.platform==='win32';
function receipt(value){try{require('node:fs').writeSync(3,JSON.stringify(value)+'\n');}catch{}}
function signalGroup(signal){
  if(!child?.pid)return;
  // The CLI owns a separate process group. Its descendants can still be alive after its leader exits.
  if(process.platform!=='win32'){try{process.kill(-child.pid,signal);}catch(error){if(error.code!=='ESRCH')child.kill(signal);}}
  else if(child.exitCode===null && child.signalCode===null)child.kill(signal);
}
function finish(){
  if(finished || !closedResult || !groupCleaned)return;
  finished=true;clearTimeout(limit);clearInterval(watchdog);
  receipt({event:'exited',...closedResult});input.close();process.stdin.destroy();
  process.exitCode=stopping?1:closedResult.code??1;
}
function cleanGroup(){
  if(process.platform==='win32' || cleanup)return;
  signalGroup('SIGTERM');
  // Stay alive through escalation even if the group leader has already exited.
  cleanup=setTimeout(()=>{signalGroup('SIGKILL');groupCleaned=true;finish();},1000);
}
async function stop(reason){
  if(stopping)return;stopping=true;clearTimeout(limit);
  receipt({event:'stop_requested',reason});
  if(!child){process.exitCode=1;process.stdin.destroy();return;}
  if(child.exitCode===null && child.signalCode===null){
    if(process.platform==='win32'){
      const killer=spawn(path.join(process.env.SystemRoot || 'C:\\Windows','System32','taskkill.exe'),
        ['/PID',String(child.pid),'/T','/F'],{shell:false,windowsHide:true,stdio:'ignore'});
      killer.once('error',()=>child.kill('SIGKILL'));
    }
  }
  cleanGroup();
  if(process.platform==='win32')setTimeout(()=>signalGroup('SIGKILL'),1500).unref();
}
const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
input.on('line',async line=>{
  if(line.length>65536){await stop('INVALID_CONTROL');return;}
  try{
    const value=JSON.parse(line);
    if(started){if(value.type==='pulse')lastPulse=Date.now();else if(value.type==='stop')await stop('DESKTOP_STOP');return;}
    started=true;
    if(value.type!=='start' || !Array.isArray(value.args) || value.args.length>16 ||
      value.args.some(v=>typeof v!=='string' || v.length>4000) || !path.isAbsolute(value.cwd) ||
      !Number.isSafeInteger(value.timeout_seconds) || value.timeout_seconds<1 || value.timeout_seconds>1800 ||
      typeof value.prompt!=='string' || value.prompt.length>8000)throw Error('INVALID_CONTROL');
    const executable=await fingerprint(value.executable);
    if(executable.sha256!==value.sha256 || stopping)throw Error('EXECUTABLE_CHANGED');
    child=spawn(executable.file,value.args,{cwd:value.cwd,env:environment(),shell:false,windowsHide:true,
      detached:process.platform!=='win32',stdio:['pipe','pipe','pipe']});
    receipt({event:'spawned',pid:child.pid,executable_hash:executable.sha256});
    child.stdout.pipe(process.stdout);child.stderr.pipe(process.stderr);child.stdin.on('error',()=>{});
    child.stdin.end(value.prompt);
    child.once('error',()=>{receipt({event:'spawn_failed'});});
    child.once('exit',()=>cleanGroup());
    child.once('close',(code,signal)=>{closedResult={code,signal};
      // A failed spawn has no process group to clean.
      if(!child.pid)groupCleaned=true;
      finish();});
    limit=setTimeout(()=>stop('TIMEOUT').catch(()=>{}),value.timeout_seconds*1000);
  }catch{receipt({event:'invalid_start'});await stop('INVALID_START');process.exitCode=1;}
});
input.on('close',()=>{if(!finished)stop('DESKTOP_PIPE_CLOSED').catch(()=>{});});
process.on('SIGTERM',()=>stop('SUPERVISOR_TERMINATED').catch(()=>{}));
process.on('SIGINT',()=>stop('SUPERVISOR_INTERRUPTED').catch(()=>{}));
const watchdog=setInterval(()=>{if(Date.now()-lastPulse>15000)stop('DESKTOP_HEARTBEAT_LOST').catch(()=>{});},3000);
watchdog.unref();
process.stdout.on('error',()=>stop('DESKTOP_OUTPUT_CLOSED').catch(()=>{}));
process.stderr.on('error',()=>stop('DESKTOP_OUTPUT_CLOSED').catch(()=>{}));
