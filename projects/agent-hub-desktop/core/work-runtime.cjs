'use strict';
const crypto=require('node:crypto');
const {HubError}=require('./remote.cjs');
const observer=require('./git-observer.cjs');
const experience=require('./experience-cache.cjs');
const {WorkStore,sameOwner}=require('./work-store.cjs');
const retryable=new Set(['NETWORK_UNAVAILABLE','SERVICE_BUSY','UNAVAILABLE','CLIENT_UPDATE_REQUIRED','UPDATE_CHECK_PENDING']);
const uuid=value=>typeof value==='string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
class WorkRuntime{
  constructor(controller){this.c=controller;this.store=new WorkStore(controller.directory,controller.vault.protector);this.operations=new Map();}
  owner(){const p=this.c.profile;return p?.user?.id && p.deviceId?{user_id:p.user.id,device_id:p.deviceId,origin:p.origin}:null;}
  runs(){const p=this.c.profile;if(!p)throw new HubError('LOGIN_REQUIRED');return p.workRuns??={};}
  run(binding,args){
    const r=this.runs()[args.run_id];
    if(!r || r.binding_id!==binding.id || r.project_id!==binding.projectId || r.task_id!==args.task_id ||
      r.lease_epoch!==args.lease_epoch || r.workspace!==binding.workspace || !sameOwner(r.owner,this.owner()))
      throw new HubError('WORK_CONTEXT_REQUIRED','请先准备并确认本执行实例的规则。');
    return r;
  }
  serialize(key,action){
    const promise=(this.operations.get(key) || Promise.resolve()).catch(()=>{}).then(action);
    this.operations.set(key,promise);
    return promise.finally(()=>{if(this.operations.get(key)===promise)this.operations.delete(key);});
  }
  start(){if(this.timer)return;this.timer=setInterval(()=>this.tick().catch(()=>{}),15000);this.timer.unref?.();}
  stop(){clearInterval(this.timer);this.timer=null;}
  async tick(){
    if(this.activeTick)return this.activeTick;
    const job=this.cycle();this.activeTick=job;
    try{return await job;}finally{if(this.activeTick===job)this.activeTick=null;}
  }
  async drain(){
    this.stop();await this.activeTick?.catch(()=>{});
    await Promise.allSettled([...this.operations.values()]);await this.store.pending;
  }
  async cycle(){
    const c=this.c,p=c.profile,remote=c.remote;
    if(c.closed || c.draining || !['active','offline','reauth_required'].includes(c.connection) || !this.owner())return;
      for(const r of Object.values(p.workRuns || {})){
        if(c.profile!==p || c.remote!==remote || c.connection==='signed_out')return;
        const b=p.bindings.find(b=>b.id===r.binding_id);
        if(!b || r.stopped || !r.acknowledged)continue;
        if(!r.next_observe || r.next_observe<=Date.now()){
          try{await this.observe(b,r);}catch(error){r.observation_error=error.code || 'WORK_OBSERVATION_FAILED';}
          r.next_observe=Date.now()+60000;
        }
        if(c.profile!==p || c.remote!==remote || c.connection==='signed_out')return;
        if(c.connection!=='active' || b.status!=='active' || Date.now()-r.last_activity>600000 || r.next_heartbeat>Date.now())continue;
        r.heartbeat_operation??=crypto.randomUUID();
        try{
          await c.invokeProjectRemote(b,'hub_heartbeat_task',{task_id:r.task_id,run_id:r.run_id,lease_epoch:r.lease_epoch,operation_id:r.heartbeat_operation});
          c.ensureCurrent(p,remote);r.next_heartbeat=Date.now()+120000;delete r.heartbeat_operation;
        }catch(error){if(!retryable.has(error.code)){r.stopped=error.code;experience.clear(r);}r.next_heartbeat=Date.now()+30000;}
      }
      if(c.profile===p)await c.persist();
      if(c.connection!=='active' || c.profile!==p || c.remote!==remote)return;
      for(const record of await this.store.list(this.owner())){
        if((record.state!=='pending' && !(record.state==='shared' && !record.code_receipt && record.backup_state==='pending' &&
            (!record.next_backup || record.next_backup<=Date.now()))) || c.profile!==p || c.remote!==remote)continue;
        const b=p.bindings.find(b=>b.id===record.binding_id && b.status==='active');
        if(b)await this.serialize(record.operation_id,async()=>{
          const fresh=await this.store.read(record.operation_id);
          if(fresh && ['pending','shared'].includes(fresh.state))return this.deliver(b,fresh);
        });
      }
  }
  async prepare(binding,args){
    const c=this.c,p=c.profile,remote=c.remote;
    for(const other of Object.values(this.runs()).filter(r=>r.workspace===binding.workspace && r.run_id!==args.run_id && !r.stopped)){
      const {task}=await c.invokeProjectRemote(binding,'hub_get_task',{task_id:other.task_id});
      c.ensureCurrent(p,remote);
      if(task.active_run_id===other.run_id && Date.parse(task.lease_until)>Date.now())
        throw new HubError('WORKSPACE_IN_USE','这个目录已有其他有效执行实例。请用独立任务 worktree，再准备当前运行。');
      other.stopped='lease_inactive';experience.clear(other);
    }
    if(c.repositories){
      const {task}=await c.invokeProjectRemote(binding,'hub_get_task',{task_id:args.task_id});
      await c.repositories.verifyTaskBinding(binding,task);c.ensureCurrent(p,remote);
    }
    const previous=this.runs()[args.run_id];
    if(previous)this.run(binding,args);
    const baseline=previous?{workspace:previous.workspace,base_commit:previous.base_commit}:
      await observer.initialize(binding.workspace,{ignoreUntracked:await this.managedPaths(binding)});
    if(baseline.workspace!==binding.workspace)throw new HubError('WORKSPACE_CHANGED');
    const context=await c.invokeProjectRemote(binding,'hub_prepare_work',args);
    c.ensureCurrent(p,remote);
    this.runs()[args.run_id]={binding_id:binding.id,project_id:binding.projectId,repository_id:binding.repositoryId || null,task_id:args.task_id,run_id:args.run_id,
      lease_epoch:args.lease_epoch,owner:this.owner(),...baseline,context_id:context.id,context_hash:context.content_hash,
      scope:context.payload.task.scope,policy:context.payload.policy,acknowledged:false,last_activity:Date.now(),next_heartbeat:0,
      memory:context.payload.memory || null,memory_required:context.payload.schema_version>=2,
      role:context.payload.role || null,role_required:context.payload.schema_version>=3,
      instance_id:context.payload.execution?.instance_id || null,harness:context.payload.execution?.harness || null,
      experience_units:previous?.experience_units || 0,
      ...Object.fromEntries(Object.entries(previous || {}).filter(([key])=>key.startsWith('last_record_') || ['last_evidence_hash','last_worklog_id'].includes(key))),
      execution_mode:context.payload.task.execution_mode};
    await c.persist();this.start();return {...context,baseline_commit:baseline.base_commit,local_write_isolation:false,
      local_memory:{storage:'os-encrypted-vault',scope:'account-device-repository-run',context_id:context.id,
        present:!!context.payload.memory}};
  }
  async validateMemory(binding,r){
    if(!r.memory_required)return null;
    if(!r.memory || r.role_required && !r.role)throw new HubError('WORK_CONTEXT_REQUIRED','本机执行职责或记忆已失效，请重新准备并确认。');
    const c=this.c,p=c.profile,remote=c.remote;
    if(c.connection!=='active' || binding.status!=='active')throw new HubError('NETWORK_UNAVAILABLE','联网核验来源权限后再加载执行记忆。');
    try{
      const result=await c.invokeProjectRemote(binding,'hub_validate_work_context',{
        task_id:r.task_id,run_id:r.run_id,lease_epoch:r.lease_epoch,context_id:r.context_id});
      c.ensureCurrent(p,remote);
      if(result.content_hash!==r.context_hash)throw new HubError('WORK_CONTEXT_STALE');
      if(this.runs()[r.run_id]!==r || r.stopped)throw new HubError('WORK_CONTEXT_STALE');
      const known=experience.versions(r);
      if(known.length){
        const id=r.experience_validation_session??=crypto.randomUUID();
        const checked=await c.invokeProjectRemote(binding,'hub_recall_experience',{
          task_id:r.task_id,run_id:r.run_id,lease_epoch:r.lease_epoch,context_id:r.context_id,memory_session_id:id,
          known,validate_only:true,max_tokens:4096,consumed_units:r.experience_units || 0},{internal:true});
        c.ensureCurrent(p,remote);
        if(this.runs()[r.run_id]!==r || r.stopped || !r.memory)throw new HubError('WORK_CONTEXT_STALE');
        experience.accept(r,checked,id,{validation:true});
      }
      return result;
    }catch(error){
      if(!retryable.has(error.code)){experience.clear(r);await c.persist();}
      throw error;
    }
  }
  async managedPaths(binding){
    return this.c.closeouts?require('./host-install.cjs').ownedProjectPaths(this.c.closeouts.options(binding)):[];
  }
  async capture(binding,r){
    return observer.capture(binding.workspace,r.base_commit,r.scope,r.policy,{ignoreUntracked:await this.managedPaths(binding)});
  }
  async loadMemory(binding,args){
    const r=this.run(binding,args);
    if(!r.acknowledged)throw new HubError('WORK_CONTEXT_ACK_REQUIRED');
    const status=await this.validateMemory(binding,r);
    if(!r.memory)throw new HubError('WORK_CONTEXT_REQUIRED','请升级服务器并重新准备共享记忆。');
    if(args.memory_session_id){
      const s=experience.session(r,args.memory_session_id);
      if(args.known_content_hash && args.known_content_hash!==r.context_hash)throw new HubError('WORK_CONTEXT_STALE');
      if(args.known_content_hash===r.context_hash && s.base_delivered===r.context_hash)
        return {context_id:r.context_id,content_hash:r.context_hash,memory:null,role:null,unchanged:true,
          fresh_source_authorization:true,receipt_meaning:'caller-reported-current-context; not proof of understanding'};
      s.base_delivered=r.context_hash;await this.c.persist();
    }
    return {context_id:r.context_id,content_hash:r.context_hash,memory:r.memory,role:r.role || null,
      current_status:status?.memory,storage:'os-encrypted-vault',fresh_source_authorization:true};
  }
  recallExperience(binding,args){
    return this.serialize('experience:'+args.run_id,async()=>{
      const r=this.run(binding,args),c=this.c,p=c.profile,remote=c.remote;
      if(!r.acknowledged)throw new HubError('WORK_CONTEXT_ACK_REQUIRED');
      if(args.context_id!==r.context_id)throw new HubError('WORK_CONTEXT_STALE');
      await this.validateMemory(binding,r);c.ensureCurrent(p,remote);
      const s=experience.session(r,args.memory_session_id);
      if(args.after_receipt){
        if(s.pending?.receipt!==args.after_receipt)throw new HubError('MEMORY_RECEIPT_MISMATCH');
        const known=new Map(s.known.map(v=>[v.kind+':'+v.id,v]));
        for(const v of s.pending.versions)known.set(v.kind+':'+v.id,v);
        s.known=[...known.values()];delete s.pending;
      }
      const data=await c.invokeProjectRemote(binding,'hub_recall_experience',{
        ...args,known:s.known,consumed_units:r.experience_units || 0},{internal:true});
      c.ensureCurrent(p,remote);
      if(this.runs()[args.run_id]!==r || r.stopped || !r.memory)throw new HubError('WORK_CONTEXT_STALE');
      try{experience.accept(r,data,args.memory_session_id);}catch(error){await c.persist();throw error;}
      await c.persist();return data;
    });
  }
  async acknowledge(binding,args){
    const r=this.run(binding,args),p=this.c.profile,remote=this.c.remote;
    if(r.context_id!==args.context_id || r.context_hash!==args.content_hash)throw new HubError('WORK_CONTEXT_STALE');
    const result=await this.c.invokeProjectRemote(binding,'hub_acknowledge_work',args);
    this.c.ensureCurrent(p,remote);r.acknowledged=true;r.last_activity=Date.now();await this.c.persist();this.start();return result;
  }
  async check(binding,args){
    const r=this.run(binding,args),p=this.c.profile,remote=this.c.remote;
    if(!r.acknowledged)throw new HubError('WORK_CONTEXT_ACK_REQUIRED');
    if(this.c.connection!=='active')throw new HubError('NETWORK_UNAVAILABLE','离线时只保全工作，恢复连接并核验后再开始受管修改。');
    const checkedContext=await this.validateMemory(binding,r);
    if(checkedContext?.usage_budget_open===false)throw new HubError('USAGE_BUDGET_EXCEEDED','项目已达到所设的上报用量额度，请先保全工作并联系负责人。日志与交接仍可上传。');
    const policy=await this.c.invokeProjectRemote(binding,'hub_get_policy',{});
    this.c.ensureCurrent(p,remote);
    if(policy.content_hash!==r.policy.content_hash)throw new HubError('WORK_CONTEXT_STALE','规则已更新，请重新准备并确认。');
    const {task}=await this.c.invokeProjectRemote(binding,'hub_get_task',{task_id:r.task_id});
    this.c.ensureCurrent(p,remote);
    if(task.status!=='in_progress' || task.active_run_id!==r.run_id || task.lease_epoch!==r.lease_epoch)
      throw new HubError('TASK_LEASE_STALE');
    if(!task.lease_until || Date.parse(task.lease_until)<=Date.now())throw new HubError('TASK_LEASE_EXPIRED');
    if(task.execution_mode!==r.execution_mode || observer.digest(task.scope)!==observer.digest(r.scope))throw new HubError('WORK_CONTEXT_STALE');
    if(args.proposed_paths){
      if(!Array.isArray(args.proposed_paths) || args.proposed_paths.length>100)throw new HubError('INVALID_REQUEST');
      const rejected=observer.violations(args.proposed_paths,r.scope,r.policy);
      if(rejected.length)throw new HubError(rejected[0].code,'拟修改路径被任务范围或保护规则拒绝。',{violations:rejected});
    }
    const snapshot=await this.capture(binding,r);
    this.c.turns?.update(r,snapshot.evidence_hash,snapshot.evidence.files.length);
    this.c.ensureCurrent(p,remote);
    r.last_activity=Date.now();await this.c.persist();
    return {can_continue:true,evidence:snapshot.evidence,evidence_hash:snapshot.evidence_hash,
      checked_proposed_paths:args.proposed_paths || [],local_write_isolation:false};
  }
  async observe(binding,r){
    if(r.execution_mode!=='observed')return;
    r.automatic_checkpoint_id??=crypto.randomUUID();
    return this.serialize(r.automatic_checkpoint_id,async()=>{
    const snapshot=await this.capture(binding,r);
    this.c.turns?.update(r,snapshot.evidence_hash,snapshot.evidence.files.length);
    if(snapshot.evidence_hash===r.observed_hash || !snapshot.evidence.files.length && !r.observed_hash)return;
    const record={schema:1,operation_id:r.automatic_checkpoint_id,owner:r.owner,binding_id:binding.id,
      project_id:binding.projectId,repository_id:binding.repositoryId || null,task_id:r.task_id,run_id:r.run_id,lease_epoch:r.lease_epoch,workspace:binding.workspace,
      created_at:new Date().toISOString(),state:'local_only',body:{summary:'后台观察检查点（尚未填写本轮工作说明）',
        notes:'自动保存实际变更，不代表测试或任务完成。'},snapshot};
    if(this.c.backups){
      const previous=await this.store.read(record.operation_id);
      if(previous?.snapshot.baseline)snapshot.baseline=previous.snapshot.baseline;
      else try{await this.c.backups.prepare(binding,snapshot);}
      catch(error){record.backup_error=error.code || 'WORK_ARCHIVE_INVALID';}
    }
    await this.store.write(record);r.observed_hash=snapshot.evidence_hash;r.observation_error=null;r.last_activity=Date.now();
    this.c.emitState();
    });
  }
  async save(binding,args){
    if(!uuid(args.operation_id))throw new HubError('INVALID_REQUEST');
    return this.serialize(args.operation_id,async()=>{
      const existing=await this.store.read(args.operation_id);
      const request_hash=observer.digest(args);
      if(existing){
        if(!sameOwner(existing.owner,this.owner()) || existing.binding_id!==binding.id || existing.request_hash!==request_hash)
          throw new HubError('IDEMPOTENCY_PAYLOAD_CHANGED');
        return this.deliver(binding,existing);
      }
      const r=this.run(binding,args);
      const profile=this.c.profile,owner={...r.owner};
      if(!r.acknowledged)throw new HubError('WORK_CONTEXT_ACK_REQUIRED');
      if(r.execution_mode!=='observed')return this.c.invokeProjectRemote(binding,'hub_write_worklog',args);
      if(typeof args.summary!=='string' || !args.summary.trim() || typeof args.notes!=='string' || !args.notes.trim() ||
         typeof args.tests!=='string' || !args.tests.trim() || typeof args.next_steps!=='string' || !args.next_steps.trim())
        throw new HubError('INVALID_REQUEST');
      const snapshot=await this.capture(binding,r);
      const turn=this.c.turns?.forRun(r);
      if(args.turn_id && turn && args.turn_id!==turn.turn_id)throw new HubError('WORK_TURN_MISMATCH');
      const paths=snapshot.evidence.files.map(f=>f.path);
      if(args.changed_paths && observer.canonical([...args.changed_paths].sort())!==observer.canonical([...paths].sort()))
        throw new HubError('WORK_CHANGE_PATHS_MISMATCH','申报路径与实际 Git 改动不一致，未上传。');
      const {task_id,operation_id,...body}=args;
      if(turn)body.turn_id=turn.turn_id;
      delete body.evidence_hash;
      Object.assign(body,{changed_paths:paths,context_id:r.context_id,change_evidence:snapshot.evidence,git_commit:snapshot.evidence.head_commit});
      if(Buffer.byteLength(JSON.stringify(body))>65536)throw new HubError('WORK_RECORD_LIMIT','日志和文件清单超过 64 KiB，请缩短日志。');
      const record={schema:1,operation_id,request_hash,owner,binding_id:binding.id,project_id:binding.projectId,repository_id:binding.repositoryId || null,
        task_id,run_id:r.run_id,lease_epoch:r.lease_epoch,workspace:binding.workspace,created_at:new Date().toISOString(),state:'pending',body,snapshot};
      if(this.c.backups){
        record.backup_state='pending';
        try{await this.c.backups.prepare(binding,snapshot);}
        catch(error){record.backup_state='blocked';record.backup_error=error.code || 'WORK_ARCHIVE_INVALID';}
      }
      await this.store.write(record,{create:true});
      if(this.c.profile!==profile || this.c.connection==='signed_out')return this.receipt(record);
      r.last_record_id=record.operation_id;r.last_record_hash=snapshot.evidence_hash;
      r.last_record_turn_id=body.turn_id || null;r.last_record_state='pending';
      r.last_activity=Date.now();await this.c.persist();
      return this.deliver(binding,record);
    });
  }
  async deliver(binding,record){
    const profile=this.c.profile;
    if(!sameOwner(record.owner,this.owner()) || record.binding_id!==binding.id)throw new HubError('WORK_RECORD_NOT_ACCESSIBLE');
    if(record.state==='blocked' || this.c.connection!=='active' || this.c.draining || binding.status==='blocked')return this.receipt(record);
    if(record.state!=='shared')try{
      const result=await this.c.invokeProjectRemote(binding,'hub_write_worklog',{task_id:record.task_id,operation_id:record.operation_id,...record.body},{internal:true});
      record.state='shared';record.server_receipt=result;record.last_error=null;
      const r=profile?.workRuns?.[record.run_id];
      if(r && this.c.profile===profile && sameOwner(record.owner,this.owner())){r.last_worklog_id=result.id;r.last_evidence_hash=record.snapshot.evidence_hash;await this.c.persist();}
    }catch(error){record.last_error=error.code || 'UPLOAD_FAILED';if(!retryable.has(error.code))record.state='blocked';}
    if(record.state==='shared' && !record.code_receipt && this.c.backups){
      try{await this.c.backups.deliver(binding,record);}
      catch(error){record.backup_error=error.code || 'WORK_ARCHIVE_UPLOAD_FAILED';
        record.backup_state=retryable.has(error.code) || ['SERVICE_BUSY','WORK_ARCHIVE_UPLOAD_TIMEOUT','INVALID_RESPONSE'].includes(error.code)?'pending':'blocked';
        record.next_backup=Date.now()+30000;}
    }
    await this.store.write(record);
    const run=this.c.profile===profile && profile?.workRuns?.[record.run_id];
    if(run && run.last_record_id===record.operation_id){run.last_record_state=record.state;
      this.c.turns?.update(run,record.snapshot.evidence_hash,record.snapshot.evidence.files.length);await this.c.persist();}
    this.c.emitState();return this.receipt(record);
  }
  receipt(record){
    const log=record.server_receipt;
    return {local_checkpoint_id:record.operation_id,state:record.state,server_confirmed:record.state==='shared',
    server_receipt:log?{id:log.id,receipt_hash:log.receipt_hash,created_at:log.created_at,evidence_hash:log.evidence_hash}:null,
    evidence_hash:record.snapshot.evidence_hash,local_code_backup:true,
    server_code_backup:!!record.code_receipt,code_receipt:record.code_receipt || null,backup_state:record.backup_state || 'local_only',
    backup_error:record.backup_error || null,recovered_from:record.recovered_from || null,
    files:record.snapshot.evidence.files.length,last_error:record.last_error || null,can_remove:record.state==='shared' && !!record.exported_to,
    message:record.code_receipt?'日志和完整代码工作树已保存在服务器，可按权限交接；未包含 Git 历史。':
      record.state==='shared'?'日志已共享，代码备份尚未确认；本机检查点继续保留。':'本机加密检查点已保留，尚未确认共享；过期或撤权记录等待限定收尾通道。'};}
  async list(binding){return {items:(await this.store.list(this.owner())).filter(r=>!binding || r.binding_id===binding.id).map(r=>({
    ...this.receipt(r),binding_id:r.binding_id,project_id:r.project_id,task_id:r.task_id,run_id:r.run_id,summary:r.body.summary,created_at:r.created_at})),
    unreadable_records:this.store.readErrors?.length || 0};}
  async retry(id){
    if(!uuid(id))throw new HubError('INVALID_REQUEST');
    return this.serialize(id,async()=>{
      const record=await this.store.read(id);
      if(!record || !sameOwner(record.owner,this.owner()))throw new HubError('WORK_RECORD_NOT_ACCESSIBLE');
      if(this.c.connection!=='active')throw new HubError('NETWORK_UNAVAILABLE');
      const binding=this.c.profile.bindings.find(b=>b.id===record.binding_id && b.status==='active');
      if(!binding)throw new HubError('PROJECT_ACCESS_REVOKED');
      if(record.state==='local_only')throw new HubError('WORK_LOG_REQUIRED','后台观察尚无本轮工作说明，请先通过 MCP 保存工作日志。');
      if(record.state==='blocked')throw new HubError(record.last_error || 'WORK_RECORD_BLOCKED','原日志已被权限或租约拦截，请保留原记录并处理收尾。');
      return this.deliver(binding,record);
    });
  }
  async submit(binding,args){
    const r=this.run(binding,args),p=this.c.profile,remote=this.c.remote;
    if(!r.acknowledged || r.last_worklog_id!==args.worklog_id)throw new HubError('TASK_LATEST_CHECKPOINT_REQUIRED');
    const snapshot=await this.capture(binding,r);
    this.c.ensureCurrent(p,remote);
    if(snapshot.evidence_hash!==r.last_evidence_hash)throw new HubError('WORK_EVIDENCE_CHANGED','检查点之后代码已变化，请先保存本轮日志。');
    if(this.c.backups && binding.repositoryId){
      const rows=await this.store.list(this.owner());
      if(!rows.some(row=>row.server_receipt?.id===args.worklog_id && row.code_receipt?.evidence_hash===snapshot.evidence_hash))
        throw new HubError('WORK_REMOTE_BACKUP_REQUIRED');
    }
    const result=await this.c.invokeProjectRemote(binding,'hub_submit_task',{...args,evidence_hash:snapshot.evidence_hash},{internal:true});
    r.stopped='submitted';experience.clear(r);await this.c.persist();return result;
  }
  async export(id,destination){
    return this.serialize(id,async()=>{
    const record=await this.store.read(id);
    if(!record || !sameOwner(record.owner,this.owner()))throw new HubError('WORK_RECORD_NOT_ACCESSIBLE');
    const result=await this.store.export(record,destination);
    record.exported_to=result.directory;record.exported_at=new Date().toISOString();
    await this.store.write(record);return result;
    });
  }
  async removeExported(id){
    return this.serialize(id,async()=>{
      const record=await this.store.read(id);
      if(!record || !sameOwner(record.owner,this.owner()))throw new HubError('WORK_RECORD_NOT_ACCESSIBLE');
      return this.store.removeExported(record);
    });
  }
}
module.exports={WorkRuntime};
