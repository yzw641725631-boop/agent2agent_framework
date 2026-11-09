'use strict';
const crypto=require('node:crypto'),path=require('node:path');
const {HubError}=require('./remote.cjs');
const {digest}=require('./git-observer.cjs');
const events=['UserPromptSubmit','Stop','Interrupt'];
const samePath=(a,b)=>process.platform==='win32'?path.resolve(a).toLowerCase()===path.resolve(b).toLowerCase():path.resolve(a)===path.resolve(b);
function sanitize(input,event,harness){
  if(!input || typeof input!=='object' || Array.isArray(input) || !events.includes(event) ||
    !['codex','claude-code','cursor','qoder','codebuddy'].includes(harness) || harness!=='codex' && event==='Interrupt' ||
    input.hook_event_name!==undefined && input.hook_event_name!==event ||
    typeof input.session_id!=='string' || !input.session_id.length || input.session_id.length>200 || /[\x00-\x1f]/.test(input.session_id) ||
    typeof input.cwd!=='string' || input.cwd.length>4096 || !path.isAbsolute(input.cwd) ||
    input.turn_id!==undefined && (typeof input.turn_id!=='string' || input.turn_id.length>200))throw new HubError('INVALID_HOOK_INPUT');
  // Never forward prompt, transcript paths, tool arguments or assistant text.
  return {event,harness,session_id:input.session_id,cwd:input.cwd,
    ...(input.turn_id?{turn_ref:input.turn_id}:{}),stop_hook_active:input.stop_hook_active===true};
}
class TurnRuntime{
  constructor(controller){this.c=controller;}
  sessions(){const p=this.c.profile;if(!p)throw new HubError('LOGIN_REQUIRED');return p.workSessions??={};}
  start(){if(!this.timer){this.timer=setInterval(()=>this.tick().catch(()=>{}),15000);this.timer.unref?.();}}
  async drain(){clearInterval(this.timer);this.timer=null;await this.active?.catch(()=>{});}
  current(run){return Object.values(this.c.profile?.workSessions || {}).find(s=>s.run_id===run.run_id && s.binding_id===run.binding_id);}
  observe(run,hash){const s=this.current(run);if(s && s.start_hash && hash!==s.start_hash)s.dirty_seen=true;}
  update(run,hash,files){
    this.observe(run,hash);
    const s=this.current(run),changed=hash!==run.last_record_hash && (files>0 || !!run.last_record_hash);
    const missing=!!(changed || s?.dirty_seen && (run.last_record_turn_id!==s.turn_id || run.last_record_hash!==hash));
    return run.record_status={state:missing?'missing':run.last_record_state==='pending'?'pending':run.last_record_state==='blocked'?'blocked':'recorded',
      missing_record:missing,can_detect:true,evidence_hash:hash,files,turn_id:s?.turn_id || null,hook_bound:!!s,
      latest_checkpoint_id:run.last_record_id || null,server_confirmed:run.last_record_state==='shared',coverage:s?'bound-host-events':'git-polling-only'};
  }
  forRun(run){const s=this.current(run);return s?.turn_id && s.phase==='active'?s:null;}
  async bind(binding,args){
    const r=this.c.work.run(binding,args),p=this.c.profile,remote=this.c.remote;
    if(!r.acknowledged)throw new HubError('WORK_CONTEXT_ACK_REQUIRED');
    await this.c.work.validateMemory(binding,r);this.c.ensureCurrent(p,remote);
    const s=this.sessions()[args.session_key];
    if(!s || s.binding_id!==binding.id || s.harness!==r.harness)throw new HubError('WORK_SESSION_NOT_ACCESSIBLE');
    const previous=this.current(r);
    if(previous && previous!==s && previous.phase==='active' && Date.now()-previous.last_event_at<1800000)
      throw new HubError('WORK_SESSION_IN_USE','该运行已绑定另一个活动对话，请使用独立任务与目录。');
    if(s.run_id && s.run_id!==r.run_id)throw new HubError('WORK_SESSION_IN_USE');
    if(previous && previous!==s)delete previous.run_id;
    s.run_id=r.run_id;s.phase='active';s.last_event_at=Date.now();
    const snapshot=await this.c.work.capture(binding,r);this.c.ensureCurrent(p,remote);
    s.start_hash??=r.last_record_hash || (snapshot.evidence.files.length?null:snapshot.evidence_hash);
    if(snapshot.evidence.files.length && snapshot.evidence_hash!==r.last_record_hash)s.dirty_seen=true;
    await this.c.persist();return {session_key:args.session_key,turn_id:s.turn_id,bound:true,coverage:'host-events-and-git-observation'};
  }
  async status(binding,args){
    const r=this.c.work.run(binding,args),p=this.c.profile,remote=this.c.remote;
    if(r.execution_mode!=='observed')return {state:'declaration',can_detect:false,missing_record:false};
    const snapshot=await this.c.work.capture(binding,r);this.c.ensureCurrent(p,remote);
    this.update(r,snapshot.evidence_hash,snapshot.evidence.files.length);
    await this.c.persist();this.c.emitState();return r.record_status;
  }
  async hook(binding,input){
    // Validate again at the broker boundary. Only the fixed binding's directory
    // may be inspected, even if a caller has a valid local bridge secret.
    const v=sanitize({session_id:input.session_id,cwd:input.cwd,turn_id:input.turn_ref,stop_hook_active:input.stop_hook_active},input.event,input.harness);
    if(Object.keys(input).some(k=>!['event','harness','session_id','cwd','turn_ref','stop_hook_active'].includes(k)) || !samePath(v.cwd,binding.workspace))
      throw new HubError('WORK_SESSION_NOT_ACCESSIBLE');
    const c=this.c,p=c.profile,remote=c.remote;
    if(!p || !['active','offline','reauth_required'].includes(c.connection))throw new HubError('LOGIN_REQUIRED');
    const key=digest({binding_id:binding.id,harness:v.harness,session_id:v.session_id}),sessions=this.sessions();
    let s=sessions[key];
    if(!s){
      if(v.event!=='UserPromptSubmit')return {systemMessage:'Agent Hub：当前对话尚未绑定工作运行，逐轮记录检查未覆盖。'};
      if(Object.keys(sessions).length>=128){
        for(const [id,old] of Object.entries(sessions))if(old.phase!=='active' && !old.pending_interrupt && Date.now()-old.last_event_at>86400000)delete sessions[id];
        if(Object.keys(sessions).length>=128)throw new HubError('WORK_SESSION_LIMIT');
      }
      s=sessions[key]={binding_id:binding.id,harness:v.harness};
    }
    s.last_event_at=Date.now();
    const source=v.turn_ref?digest(v.turn_ref):null;
    if(v.event!=='UserPromptSubmit' && source && s.source_turn && source!==s.source_turn)
      return {systemMessage:'Agent Hub：忽略已过期的宿主事件，未改变当前对话的工作记录状态。'};
    if(v.event==='UserPromptSubmit'){
      if(!s.turn_id || !source || s.source_turn!==source){
        s.turn_id=crypto.randomUUID();s.source_turn=source;s.block_count=0;s.dirty_seen=false;delete s.start_hash;
      }
      s.phase='active';
      const r=p.workRuns?.[s.run_id];
      if(r && !r.stopped){
        try{const snapshot=await c.work.capture(binding,r);c.ensureCurrent(p,remote);s.start_hash=snapshot.evidence_hash;}
        catch(error){s.observation_error=error.code || 'WORK_OBSERVATION_FAILED';}
      }
      await c.persist();
      return {hookSpecificOutput:{hookEventName:v.event,additionalContext:`Agent Hub 本对话 session_key=${key}，本轮 turn_id=${s.turn_id}。`+
        (s.run_id?'本轮修改代码后用 hub_write_worklog 记录工作；Stop 将核对实际变更。':'认领并准备确认后，用 hub_bind_work_session 绑定本对话；绑定前逐轮检查未覆盖。')}};
    }
    if(!s.run_id){await c.persist();return {systemMessage:'Agent Hub：当前对话尚未绑定工作运行，逐轮检查未覆盖。'};}
    const r=p.workRuns?.[s.run_id];
    if(!r || r.binding_id!==binding.id)throw new HubError('WORK_CONTEXT_REQUIRED');
    if(v.event==='Interrupt'){
      s.pending_interrupt=true;s.phase='interrupted';await c.persist();this.start();
      return {queued:true,checkpoint_confirmed:false};
    }
    if(binding.status==='blocked' || binding.closeout_id || c.connection==='reauth_required' || r.stopped){
      s.phase='stopped';await c.persist();
      return {systemMessage:'Agent Hub：当前运行已停止或授权变化。停止常规开发，检查撤权收尾及本机检查点。'};
    }
    let status;
    try{status=await this.status(binding,{task_id:r.task_id,run_id:r.run_id,lease_epoch:r.lease_epoch});}
    catch(error){
      c.ensureCurrent(p,remote);s.observation_error=error.code || 'WORK_OBSERVATION_FAILED';await c.persist();
      return {systemMessage:'Agent Hub：本轮检查未完成（'+s.observation_error+'），请保留工作并查看本机检查点。'};
    }
    const block=status.missing_record && !v.stop_hook_active && !s.block_count && (!s.last_block_at || Date.now()-s.last_block_at>600000) && c.connection==='active';
    if(block){s.block_count=1;s.last_block_at=Date.now();await c.persist();return {decision:'block',reason:'本轮存在未记入工作日志的代码变更。请调用 hub_write_worklog，简要填写目标、实际验证、阻塞及下一步；不得编造测试结果。随后检查 hub_get_work_record_status。'};}
    s.phase='stopped';await c.persist();
    return status.missing_record?{systemMessage:'Agent Hub：工作日志仍有缺口，已保留提示；本轮不再重复唤醒模型。请补记。'}:
      ['pending','blocked'].includes(status.state)?{systemMessage:'Agent Hub：本轮日志已在本机保存，尚未确认共享，请在检查点中处理待传状态。'}:{};
  }
  tick(){if(this.active)return this.active;const job=this.cycle();this.active=job;return job.finally(()=>{if(this.active===job)this.active=null;});}
  async cycle(){
    const c=this.c,p=c.profile,remote=c.remote;
    if(!p || c.closed || c.draining || !['active','offline','reauth_required'].includes(c.connection))return;
    for(const s of Object.values(p.workSessions || {})){
      if(!s.pending_interrupt)continue;
      const r=p.workRuns?.[s.run_id],b=p.bindings.find(b=>b.id===s.binding_id);
      if(!r || !b)continue;
      try{await c.work.observe(b,r);c.ensureCurrent(p,remote);s.interrupt_checkpoint_at=new Date().toISOString();delete s.pending_interrupt;delete s.observation_error;}
      catch(error){if(c.profile!==p || c.remote!==remote)return;s.observation_error=error.code || 'WORK_OBSERVATION_FAILED';}
      await c.persist();c.emitState();
    }
  }
}
module.exports={TurnRuntime,sanitize};
