'use strict';
const fs=require('node:fs/promises'),streamFs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {HubError}=require('./remote.cjs');
const hash=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const samePath=(a,b)=>process.platform==='win32'?a.toLowerCase()===b.toLowerCase():a===b;
async function fingerprint(file){
  if(typeof file!=='string' || !path.isAbsolute(file))throw new HubError('EXECUTABLE_NOT_SUPPORTED');
  const resolved=path.resolve(file),real=await fs.realpath(resolved),stat=await fs.lstat(resolved);
  if(!samePath(real,resolved) || !stat.isFile() || stat.isSymbolicLink() || stat.size>512*1024*1024 ||
    process.platform==='win32' && path.extname(real).toLowerCase()!=='.exe')throw new HubError('EXECUTABLE_NOT_SUPPORTED');
  const digest=crypto.createHash('sha256');for await(const part of streamFs.createReadStream(real))digest.update(part);
  return {file:real,sha256:digest.digest('hex'),size:stat.size};
}
function command(harness,workspace){
  if(harness==='codex')return ['exec','--cd',workspace,'--sandbox','workspace-write','-'];
  if(harness==='claude-code')return ['-p','--permission-mode','default'];
  throw new HubError('EXECUTION_ADAPTER_UNAVAILABLE','该开发工具尚无核验过的本机执行接口，可继续通过 MCP 手动接收待办。');
}
function environment(){
  // CLI uses the person's own normal login. Never pass Hub session or release signing secrets.
  const result={};for(const key of ['PATH','Path','PATHEXT','SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','HOMEDRIVE','HOMEPATH','LANG','LC_ALL','TERM']){
    if(process.env[key]!==undefined)result[key]=process.env[key];
  }
  return result;
}
class ExecutionRuntime{
  constructor(controller,{spawnProcess=spawn,intervalMs=5000}={}){this.c=controller;this.spawnProcess=spawnProcess;this.intervalMs=intervalMs;this.previews=new Map();this.active=new Map();this.starting=new Map();}
  workspaceKey(workspace){const value=path.resolve(workspace);return process.platform==='win32'?value.toLowerCase():value;}
  busy(workspace){const key=this.workspaceKey(workspace);return [...this.active.keys(),...this.starting.keys()].some(p=>this.workspaceKey(p)===key);}
  recover(profile=this.c.profile){
    if(!profile)return false;let changed=false;
    for(const record of profile.executionRecords || []){
      if(['running','starting','stopping'].includes(record.state) &&
        ![...this.active.values(),...this.starting.values()].some(j=>j.record?.id===record.id)){
        record.state='interrupted';record.reason='DESKTOP_RESTARTED_PROCESS_STOP_UNCONFIRMED';record.requires_process_check=true;
        record.receipt_payload={...record};record.receipt_shared=false;changed=true;
      }
    }
    return changed;
  }
  guard(profile,remote,binding){
    this.c.ensureCurrent(profile,remote);
    if(this.c.closed || this.c.draining || this.c.connection!=='active' || binding.status!=='active' || binding.closeout_id)
      throw new HubError('PROJECT_ACCESS_REVOKED');
  }
  async configure(harness,file,expectedHash){
    command(harness,'workspace');const p=this.c.profile,remote=this.c.remote;
    if(!p || this.c.connection!=='active')throw new HubError('LOGIN_REQUIRED');
    const value=await fingerprint(file);this.c.ensureCurrent(p,remote);
    if(expectedHash && value.sha256!==expectedHash)throw new HubError('EXECUTABLE_CHANGED');
    p.executionAdapters??={};p.executionAdapters[harness]={...value,configured_at:new Date().toISOString()};
    await this.c.persist();return {harness,...value};
  }
  async preview(binding,args){
    const p=this.c.profile,remote=this.c.remote;this.guard(p,remote,binding);
    // Inspect restored records before the first await; there is no startup window for a second CLI.
    if(this.recover(p))await this.c.persist();this.guard(p,remote,binding);
    const run=this.c.work.run(binding,args),adapter=p.executionAdapters?.[args.harness];
    if(!adapter)throw new HubError('EXECUTION_ADAPTER_UNAVAILABLE','请先在本机执行页面登记本人安装的原生 CLI 程序。');
    if(run.harness!==args.harness || run.stopped || run.execution_mode!=='observed')throw new HubError('EXECUTION_RUN_MISMATCH');
    if(this.busy(binding.workspace))throw new HubError('EXECUTION_WORKSPACE_BUSY');
    if((p.executionRecords || []).some(r=>r.requires_process_check &&
      (r.binding_id===binding.id || r.workspace_key===hash(this.workspaceKey(binding.workspace)))))
      throw new HubError('EXECUTION_PREVIOUS_STOP_UNCONFIRMED','上次桌面异常退出。请先在本机确认旧 CLI 已停止，再解除该记录的待核查状态。');
    const timeout=args.timeout_seconds ?? 600;
    if(!Number.isSafeInteger(timeout) || timeout<30 || timeout>1800)throw new HubError('INVALID_REQUEST');
    await this.c.work.check(binding,{task_id:run.task_id,run_id:run.run_id,lease_epoch:run.lease_epoch});this.guard(p,remote,binding);
    const hook=await this.c.hooks.invoke(binding,'hub_get_hook_status',{harness:args.harness});this.guard(p,remote,binding);
    if(!['observed','awaiting_host'].includes(hook.state))throw new HubError('EXECUTION_HOOKS_REQUIRED','请先安装并审查本项目的逐轮保护配置。');
    const executable=await fingerprint(adapter.file);this.guard(p,remote,binding);
    if(executable.sha256!==adapter.sha256)throw new HubError('EXECUTABLE_CHANGED','CLI 已更新，请在桌面端重新登记并核对程序。');
    const value={id:crypto.randomUUID(),binding_id:binding.id,task_id:run.task_id,run_id:run.run_id,lease_epoch:run.lease_epoch,
      instance_id:run.instance_id,harness:args.harness,workspace:binding.workspace,executable,args:command(args.harness,binding.workspace),
      timeout_seconds:timeout,context_hash:run.context_hash,policy_hash:run.policy.content_hash,expires_at:Date.now()+120000,
      user_id:p.user.id,device_id:p.deviceId,origin:p.origin,approved:false};
    value.preview_hash=hash(value);this.previews.set(value.id,{value,profile:p,remote});
    for(const [key,entry] of this.previews)if(entry.value.expires_at<Date.now())this.previews.delete(key);
    return this.publicPreview(value);
  }
  publicPreview(value){const {approved,...rest}=value;return {...rest,requires_desktop_approval:true,os_isolation_guarantee:false,
    coverage:'fixed CLI invocation and observed process group; normal host permissions remain in force'};}
  entry(id){
    const entry=this.previews.get(id);if(!entry || entry.value.expires_at<Date.now())throw new HubError('EXECUTION_APPROVAL_EXPIRED');
    this.guard(entry.profile,entry.remote,this.c.binding(entry.value.binding_id));return entry;
  }
  async approve(id,previewHash){
    // Only the desktop main process calls this method after its native confirmation dialog.
    const entry=this.entry(id);if(entry.value.preview_hash!==previewHash)throw new HubError('EXECUTION_PREVIEW_CHANGED');
    entry.value.approved=true;return {approval_id:id,expires_at:entry.value.expires_at};
  }
  async start(binding,id){
    const entry=this.entry(id),v=entry.value,p=entry.profile,remote=entry.remote;
    if(!v.approved || v.binding_id!==binding.id)throw new HubError('EXECUTION_DESKTOP_APPROVAL_REQUIRED');
    if(this.busy(v.workspace))throw new HubError('EXECUTION_WORKSPACE_BUSY');
    // Consume before awaiting anything, so two calls cannot share the single-use approval.
    this.previews.delete(id);
    const pending={binding,profile:p};pending.finished=new Promise(resolve=>{pending.resolve=resolve;});
    this.starting.set(v.workspace,pending);
    const check=()=>{this.guard(p,remote,binding);if(pending.cancelled)throw new HubError('EXECUTION_START_CANCELLED');};
    let record,executable;
    try{
    const r=this.c.work.run(binding,v);
    if(r.context_hash!==v.context_hash || r.policy.content_hash!==v.policy_hash)throw new HubError('EXECUTION_PREVIEW_CHANGED');
    await this.c.work.check(binding,v);check();
    executable=await fingerprint(v.executable.file);check();
    if(executable.sha256!==v.executable.sha256)throw new HubError('EXECUTABLE_CHANGED');
    record={id:v.id,binding_id:binding.id,task_id:v.task_id,run_id:v.run_id,harness:v.harness,state:'starting',
      workspace_key:hash(this.workspaceKey(v.workspace)),
      started_at:new Date().toISOString(),timeout_seconds:v.timeout_seconds,executable_hash:executable.sha256,
      stdout_bytes:0,stderr_bytes:0,os_isolation_guarantee:false};
    pending.record=record;
    p.executionRecords??=[];p.executionRecords.push(record);p.executionRecords=p.executionRecords.slice(-200);
    await this.c.persist();check();
    let child;
    try{child=this.spawnProcess(process.execPath,[path.join(__dirname,'execution-supervisor.cjs')],{cwd:v.workspace,
      env:{...environment(),ELECTRON_RUN_AS_NODE:'1'},shell:false,windowsHide:true,
      detached:process.platform!=='win32',stdio:['pipe','pipe','pipe','pipe']});}
    catch{throw new HubError('EXECUTION_SPAWN_FAILED');}
    const job={record,child,profile:p,remote,binding,value:v,stdout:crypto.createHash('sha256'),stderr:crypto.createHash('sha256')};
    this.active.set(v.workspace,job);record.state='running';record.pid=child.pid || null;
    job.finished=new Promise(resolve=>{job.resolve=resolve;});
    const capture=(name,chunk)=>{record[name+'_bytes']+=chunk.length;job[name].update(chunk);
      if(record.stdout_bytes+record.stderr_bytes>16*1024*1024)this.stop(record.id,'OUTPUT_LIMIT').catch(()=>{});};
    child.stdout.on('data',chunk=>capture('stdout',chunk));child.stderr.on('data',chunk=>capture('stderr',chunk));
    child.stdin.on('error',()=>{});
    child.once('error',()=>{record.reason='EXECUTION_SPAWN_FAILED';});
    child.once('close',(code,signal)=>this.finish(job,code,signal).catch(()=>{}));
    const prompt=`Use the Agent Hub MCP for this fixed project. Check the version first. Continue only task ${v.task_id}, `+
      `run ${v.run_id}, epoch ${v.lease_epoch}, instance ${v.instance_id}. Load and acknowledge the current rules and memory. `+
      `Bind this conversation using its actual hook session_key. Follow scope and protected paths. Log every changed turn through MCP. `+
      `Never claim another task, weaken host approval, or expand permissions. If authorization fails, stop and invoke the limited closeout. `+
      `At completion, save an actual checkpoint and submit for independent review; do not mark the business task complete yourself.\n`;
    child.stdin.write(JSON.stringify({type:'start',executable:executable.file,sha256:executable.sha256,args:v.args,cwd:v.workspace,prompt,
      timeout_seconds:v.timeout_seconds})+'\n');
    let controlBuffer='';
    child.stdio[3]?.on('data',chunk=>{
      controlBuffer+=chunk.toString('utf8');if(controlBuffer.length>8192){this.stop(record.id,'INVALID_SUPERVISOR_RECEIPT').catch(()=>{});return;}
      for(let at;(at=controlBuffer.indexOf('\n'))>=0;){const line=controlBuffer.slice(0,at);controlBuffer=controlBuffer.slice(at+1);
        try{const item=JSON.parse(line);if(item.event==='spawned' && item.executable_hash===executable.sha256){record.cli_spawn_observed=true;}
          if(item.event==='spawned' && Number.isSafeInteger(item.pid) && item.pid>0)job.cliPid=item.pid;
          if(item.event==='exited')job.cliExitObserved=true;
          if(item.event==='stop_requested')record.supervisor_stop_reason=item.reason;
          if(['spawn_failed','invalid_start'].includes(item.event))record.reason='EXECUTION_SPAWN_FAILED';}
        catch{this.stop(record.id,'INVALID_SUPERVISOR_RECEIPT').catch(()=>{});}
      }
    });
    job.pulse=setInterval(()=>{if(!child.stdin.destroyed)child.stdin.write('{"type":"pulse"}\n');},3000);job.pulse.unref?.();
    job.timer=setInterval(()=>this.monitor(job).catch(()=>{}),this.intervalMs);job.timer.unref?.();
    job.deadline=setTimeout(()=>this.stop(record.id,'TIMEOUT').catch(()=>{}),v.timeout_seconds*1000);job.deadline.unref?.();
    await this.c.persist();this.c.emitState();return {...record};
    }catch(error){
      if(record && record.state==='starting'){
        Object.assign(record,{state:pending.cancelled?'cancelled':'failed',reason:pending.cancelled || error.code || 'EXECUTION_START_FAILED',
          ended_at:new Date().toISOString()});
        record.receipt_payload={...record};record.receipt_shared=false;await this.c.persist().catch(()=>{});
      }
      throw error;
    }finally{if(this.starting.get(v.workspace)===pending)this.starting.delete(v.workspace);pending.resolve();}
  }
  async monitor(job){
    if(job.monitoring || job.stopping)return;
    job.monitoring=true;
    try{
      this.guard(job.profile,job.remote,job.binding);
      await this.c.work.check(job.binding,job.value);
      this.guard(job.profile,job.remote,job.binding);
    }catch(error){await this.stop(job.record.id,error.code || 'AUTHORIZATION_CHECK_FAILED');}
    finally{job.monitoring=false;}
  }
  async stop(id,reason='USER_STOP'){
    const job=[...this.active.values()].find(j=>j.record.id===id);if(!job)return {stopped:true,already_finished:true};
    if(job.stopping)return job.stopping;
    job.stopping=(async()=>{
      job.record.reason=reason;job.record.state='stopping';
      clearInterval(job.timer);clearInterval(job.pulse);clearTimeout(job.deadline);
      if(!job.child.stdin.destroyed)job.child.stdin.write('{"type":"stop"}\n');
      if(job.child.exitCode===null && job.child.signalCode===null){
        if(process.platform!=='win32' && job.child.pid){try{process.kill(-job.child.pid,'SIGTERM');}catch{job.child.kill('SIGTERM');}}
        // Supervisor escalates its separate CLI group after 1s. Do not kill it first.
        job.force=setTimeout(()=>{if(job.child.exitCode===null && job.child.signalCode===null){
          if(process.platform==='win32' && job.child.pid){
            const killer=spawn(path.join(process.env.SystemRoot || 'C:\\Windows','System32','taskkill.exe'),
              ['/PID',String(job.child.pid),'/T','/F'],{shell:false,windowsHide:true,stdio:'ignore'});
            killer.once('error',()=>job.child.kill('SIGKILL'));
          }else if(job.child.pid){
            if(job.cliPid && !job.cliExitObserved)try{process.kill(-job.cliPid,'SIGKILL');}catch{}
            try{process.kill(-job.child.pid,'SIGKILL');}catch{job.child.kill('SIGKILL');}
          }
        }},4000);job.force.unref?.();
      }
      await this.c.persist();this.c.emitState();
      return {stop_requested:true,coverage:'observed-process-tree; escaped independent daemons are outside this mechanism'};
    })();return job.stopping;
  }
  async finish(job,code,signal){
    clearInterval(job.timer);clearInterval(job.pulse);clearTimeout(job.deadline);clearTimeout(job.force);
    if(this.active.get(job.value.workspace)===job)this.active.delete(job.value.workspace);
    Object.assign(job.record,{state:job.record.reason?'stopped':code===0?'exited':'failed',exit_code:code,
      signal:signal || null,ended_at:new Date().toISOString(),stdout_hash:job.stdout.digest('hex'),stderr_hash:job.stderr.digest('hex')});
    if(job.record.cli_spawn_observed && !job.cliExitObserved){
      job.record.requires_process_check=true;job.record.reason='EXECUTION_PROCESS_STOP_UNCONFIRMED';
      if(process.platform!=='win32' && job.cliPid)try{process.kill(-job.cliPid,'SIGKILL');}catch{}
    }
    delete job.record.pid;job.child.stdin.destroy();
    job.record.receipt_payload={...job.record};job.record.receipt_shared=false;
    try{
      await this.c.persist();
      if(this.c.profile===job.profile && this.c.remote===job.remote)await this.share(job.record,job.profile);
    }catch{job.record.receipt_shared=false;await this.c.persist().catch(()=>{});}
    finally{this.c.emitState();job.resolve({...job.record});}
  }
  list(){return {adapters:Object.keys(this.c.profile?.executionAdapters || {}),items:this.c.profile?.executionRecords || [],
    coverage:'Codex and Claude Code native CLI; other harnesses receive manual MCP work offers'};}
  async haltAll(reason='DESKTOP_STOP'){
    clearInterval(this.receiptTimer);this.receiptTimer=null;
    this.previews.clear();const pending=[...this.starting.values()];pending.forEach(j=>{j.cancelled=reason;});
    await Promise.allSettled(pending.map(j=>j.finished));
    const jobs=[...this.active.values()];await Promise.allSettled(jobs.map(j=>this.stop(j.record.id,reason)));
    await Promise.allSettled(jobs.map(j=>j.finished));
  }
  async haltProject(projectId,reason){
    const pending=[...this.starting.values()].filter(j=>!projectId || j.binding.projectId===projectId);
    // May be called from work.check's failure handler while that same start is awaiting it.
    pending.forEach(j=>{j.cancelled=reason || 'PROJECT_ACCESS_REVOKED';});
    const jobs=[...this.active.values()].filter(j=>!projectId || j.binding.projectId===projectId);
    await Promise.allSettled(jobs.map(j=>this.stop(j.record.id,reason)));
    await Promise.allSettled(jobs.map(j=>j.finished));
  }
  async share(record,p){
    if(record.receipt_shared || !record.receipt_payload || this.c.profile!==p || this.c.closed || this.c.draining || this.c.connection!=='active')return;
    const binding=p.bindings?.find(b=>b.id===record.binding_id && b.status==='active');
    if(!binding)return;
    await this.c.invokeProjectRemote(binding,'hub_report_progress',{status:'checkpoint',task_reference:record.task_id,
      summary:'本机执行已结束 · '+record.receipt_payload.state,detail:JSON.stringify(record.receipt_payload),operation_id:record.id});
    record.receipt_shared=true;await this.c.persist();
  }
  watch(){
    // Recovery changes are synchronous, even before the first 30-second receipt retry.
    if(this.recover())this.c.persist().catch(()=>{});
    if(this.receiptTimer)return;
    this.receiptTimer=setInterval(()=>this.flush().catch(()=>{}),30000);this.receiptTimer.unref?.();
  }
  async flush(){
    if(this.flushing)return;this.flushing=true;
    try{
      const p=this.c.profile;if(!p)return;if(this.recover(p))await this.c.persist();
      for(const record of p.executionRecords || []){
        try{await this.share(record,p);}catch{}
      }
    }finally{this.flushing=false;}
  }
  async confirmStopped(id){
    const record=this.c.profile?.executionRecords?.find(r=>r.id===id && r.requires_process_check);
    if(!record)throw new HubError('EXECUTION_NOT_ACCESSIBLE');
    record.requires_process_check=false;record.manual_stop_confirmed_at=new Date().toISOString();
    await this.c.persist();this.c.emitState();return {confirmed_by_device_owner:true};
  }
}
module.exports={ExecutionRuntime,fingerprint,command,environment};
