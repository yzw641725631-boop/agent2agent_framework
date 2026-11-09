'use strict';
const crypto=require('node:crypto'),fs=require('node:fs/promises'),path=require('node:path');
const {Remote,HubError}=require('./remote.cjs');
const observer=require('./git-observer.cjs'),codec=require('./backup-codec.cjs'),hostInstall=require('./host-install.cjs');
const {sameOwner}=require('./work-store.cjs');
const uuid=value=>typeof value==='string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
const hash=value=>typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
class CloseoutRuntime{
  constructor(controller){this.c=controller;this.operations=new Map();}
  start(){if(this.timer)return;this.timer=setInterval(()=>this.tick().catch(()=>{}),30000);this.timer.unref?.();}
  stop(){clearInterval(this.timer);clearTimeout(this.scheduled);this.timer=null;this.scheduled=null;}
  schedule(){if(this.scheduled || this.c.closed || this.c.draining)return;
    this.scheduled=setTimeout(()=>{this.scheduled=null;this.tick().catch(()=>{});},0);this.scheduled.unref?.();}
  async drain(){this.stop();this.remote?.close();await this.active?.catch(()=>{});}
  guard(profile){if(this.c.closed || this.c.draining || this.c.profile!==profile)throw new HubError('LOGIN_REQUIRED');}
  match(binding,job){const auth=binding.authorization;
    const generation=!auth || (auth.grant_id?job.scope.grant_ids?.includes(auth.grant_id):
      auth.user_epoch===job.scope.user_epoch && auth.device_epoch===job.scope.device_epoch);
    const version=job.scope.repository_versions?.[binding.repositoryId];
    return generation && (!version || !binding.repositoryVersion || binding.repositoryVersion<=version) && binding.projectId===job.project_id &&
      (job.scope.repository_ids===null || job.scope.repository_ids.includes(binding.repositoryId));}
  options(binding){return {workspace:binding.workspace,name:Object.keys(this.c.configuration(binding).mcpServers)[0],
    hookDirectory:path.join(this.c.directory,'hook-installations',binding.id),
    installed:binding.installed,receiptDirectories:['setup-projects','assembled-projects','task-worktrees','framework-deployments'].map(dir=>path.join(this.c.directory,dir,binding.id))};}
  publicJob(job){return {id:job.id,project_id:job.project_id,state:job.local_state || job.state,
    error:job.error || null,reason:job.reason,expires_at:job.expires_at,
    server_upload_confirmed:!!job.seal,cleanup:job.cleanup || null,
    normal_operations_allowed:false,required_action:'upload_work_then_uninstall',
    message:'必须停止本项目普通开发。桌面端将限定上传已有工作，核对服务器回执后卸载本项目受管框架。失败时保留工作、停止后续受管操作；不得自行删除工程。'};}
  status(binding){const jobs=Object.values(this.c.profile?.closeouts || {}).filter(job=>!binding || this.match(binding,job));
    const allowed=!jobs.length && this.c.connection==='active' && (!binding || binding.status==='active');
    return {items:jobs.map(job=>this.publicJob(job)),normal_operations_allowed:allowed,
      required_action:jobs.length?'upload_work_then_uninstall':allowed?'none':'reauthenticate_or_retry_connection',tokens_exposed:false};}
  async tick(){if(this.active)return this.active;
    const action=this.cycle();this.active=action;
    try{return await action;}finally{if(this.active===action)this.active=null;}}
  async cycle(){
    const c=this.c,p=c.profile,owner=c.work.owner();
    if(c.closed || c.draining || !p || !owner)return;
    const remote=new Remote(p,()=>c.persist(),c.transport);this.remote=remote;
    try{
      await remote.init();this.guard(p);
      let cursor=null;
      do{
        const result=await remote.raw('POST','/api/closeouts/discover'+(cursor?'?after='+cursor:''),{});
        this.guard(p);
        if(!Array.isArray(result.items))throw new HubError('INVALID_RESPONSE');
        for(const raw of result.items){
          if(!uuid(raw.id) || raw.user_id!==owner.user_id || raw.device_id!==owner.device_id || !uuid(raw.project_id) ||
              !raw.scope || !(raw.scope.repository_ids===null || Array.isArray(raw.scope.repository_ids) && raw.scope.repository_ids.every(uuid)) ||
              !Number.isFinite(Date.parse(raw.created_at)) || !Number.isFinite(Date.parse(raw.expires_at)) ||
              !/^co1_[a-f0-9]{64}$/.test(raw.upload_token || ''))throw new HubError('INVALID_RESPONSE');
          const previous=(p.closeouts??={})[raw.id];
          const job=p.closeouts[raw.id]={...previous,...raw};
          for(const b of [...p.bindings,...p.retiredBindings || []].filter(b=>this.match(b,job))){
            b.closeout_id=job.id;b.status=job.local_state==='complete'?'uninstalled':'blocked';b.errorCode='PROJECT_CLOSEOUT_REQUIRED';
            b.message=this.publicJob(job).message;
            for(const run of Object.values(p.workRuns || {}).filter(run=>run.binding_id===b.id)){run.stopped='revoked';delete run.memory;delete run.role;}
          }
          if(raw.scope.repository_ids===null)c.cancelWatch(raw.project_id);
        }
        cursor=result.next_cursor;
        if(cursor && !uuid(cursor))throw new HubError('INVALID_RESPONSE');
      }while(cursor);
      await c.persist();c.emitState();
      // No normal operation awaits this worker; wait for admitted operations to settle first.
      await Promise.allSettled([...c.projectOperations]);
      await c.work.activeTick?.catch(()=>{});await Promise.allSettled([...c.work.operations.values()]);
      for(const job of Object.values(p.closeouts || {})){
        this.guard(p);
        if(job.local_state==='complete')continue;
        try{await this.finish(p,remote,job,owner);}
        catch(error){job.error=error.code || 'CLOSEOUT_FAILED';job.local_state='blocked';
          if(c.profile!==p)throw error;
          if(c.profile===p){await c.persist();c.emitState();}}
      }
    }finally{remote.close();if(this.remote===remote)this.remote=null;}
  }
  async finish(p,remote,job,owner){
    const c=this.c;this.guard(p);
    if(job.report){await this.report(p,remote,job);return;}
    if(job.state==='complete')throw new HubError('CLOSEOUT_LOCAL_STATE_MISSING');
    if(Date.parse(job.expires_at)<=Date.now())throw new HubError('CLOSEOUT_EXPIRED');
    const bindings=[...p.bindings,...p.retiredBindings || []].filter(b=>this.match(b,job));
    job.local_state='uploading';job.error=null;await c.persist();c.emitState();
    await c.work.store.list(owner);
    if(c.work.store.readErrors.length)throw new HubError('WORK_STORE_UNAVAILABLE');
    const fresh=[];
    // A final observed snapshot covers saved edits since the last Agent worklog.
    for(const b of bindings){
      const runs=Object.values(p.workRuns || {}).filter(r=>r.binding_id===b.id && r.acknowledged && sameOwner(r.owner,owner))
        .sort((a,b)=>(b.last_activity || 0)-(a.last_activity || 0));
      if(!runs.length){
        if(b.repositoryId || b.initial_commit){
          const init=await observer.repository(b.workspace);
          if(!b.initial_commit || init.head!==b.initial_commit)throw new HubError('CLOSEOUT_UNREGISTERED_WORK');
          const empty=await observer.capture(b.workspace,b.initial_commit,{paths:['src']},{protected_paths:[]},
            {ignoreUntracked:await hostInstall.ownedProjectPaths(this.options(b))});
          if(empty.evidence.files.length)throw new HubError('CLOSEOUT_UNREGISTERED_WORK');
        }
        continue;
      }
      const r=runs[0];
      const ignored=await hostInstall.ownedProjectPaths(this.options(b));
      const snapshot=await observer.capture(b.workspace,r.base_commit,r.scope,r.policy,{ignoreUntracked:ignored});
      this.guard(p);
      const finals=job.final_records??={},previous=finals[b.id] && await c.work.store.read(finals[b.id]);
      let record=previous;
      if(!record || record.snapshot.evidence_hash!==snapshot.evidence_hash){
        const id=crypto.randomUUID();snapshot.baseline=await codec.baseline(b.workspace,r.base_commit);
        record={schema:1,operation_id:id,owner,binding_id:b.id,repository_id:b.repositoryId || null,
          project_id:b.projectId,task_id:r.task_id,run_id:r.run_id,lease_epoch:r.lease_epoch,workspace:b.workspace,
          created_at:new Date().toISOString(),state:'blocked',backup_state:'pending',snapshot,
          body:{context_id:r.context_id,summary:'撤权自动收尾检查点',notes:'自动保存撤权时的实际工作树，人工任务说明见此前工作日志。',
            tests:'未因撤权自动运行测试；此记录不代表验证通过。',next_steps:'由获准接手者读取工作日志并审查代码后继续。',outcome:'blocked'}};
        await c.work.store.write(record,{create:true});this.guard(p);finals[b.id]=id;await c.persist();
      }
      fresh.push({binding:b,run:r,evidence_hash:snapshot.evidence_hash,ignoreUntracked:ignored,record});
    }
    const all=await c.work.store.list(owner);if(c.work.store.readErrors.length)throw new HubError('WORK_STORE_UNAVAILABLE');
    // Server log timestamps reflect admission order. Upload the final observed
    // worktree last so the default newest-first handoff view selects that code.
    const finalIds=new Set(fresh.map(item=>item.record.operation_id));
    all.sort((a,b)=>Number(finalIds.has(a.operation_id))-Number(finalIds.has(b.operation_id)) ||
      a.created_at.localeCompare(b.created_at) || a.operation_id.localeCompare(b.operation_id));
    for(const item of all){
      if(item.recovered_from || item.project_id!==job.project_id)continue;
      const b=[...p.bindings,...p.retiredBindings || []].find(b=>b.id===item.binding_id);
      if(b && !this.match(b,job))continue;
      const repo=item.repository_id || b?.repositoryId || p.workRuns?.[item.run_id]?.repository_id;
      if(job.scope.repository_ids!==null && !job.scope.repository_ids.includes(repo)){
        if(!repo && !item.code_receipt)throw new HubError('CLOSEOUT_UNREGISTERED_WORK');
        continue;
      }
      if(item.code_receipt && !item.closeout_receipts?.[job.id])continue;
      const record=await c.work.store.read(item.operation_id);this.guard(p);
      await this.upload(p,remote,job,record,b);
    }
    const result=await remote.raw('POST',`/api/closeouts/${job.id}/seal`,
      {worklog_ids:Object.values(job.worklog_ids || {})},job.upload_token);
    this.guard(p);
    if(!uuid(result.id) || result.job_id!==job.id || !hash(result.receipt_hash) || result.server_code_backup!==true ||
        result.checkpoints!==Object.keys(job.worklog_ids || {}).length)throw new HubError('CLOSEOUT_RECEIPT_MISMATCH');
    job.seal=result;job.local_state='uninstalling';await c.persist();c.emitState();
    // Refuse cleanup if an editor changed work during the network round trip.
    for(const item of fresh){
      const again=await observer.capture(item.binding.workspace,item.run.base_commit,item.run.scope,item.run.policy,
        {ignoreUntracked:item.ignoreUntracked});
      if(again.evidence_hash!==item.evidence_hash)throw new HubError('WORKTREE_CHANGED');
    }
    const cleanup={removed_files:0,removed_entries:0,conflicts:0,retained_shared_components:true};
    for(const b of bindings){
      this.guard(p);
      const result=await hostInstall.uninstallProject({...this.options(b),guard:()=>this.guard(p)});
      cleanup.removed_files+=result.removed_files;cleanup.removed_entries+=result.removed_entries;cleanup.conflicts+=result.conflicts.length;
      // The broker keeps a tombstone for live MCP processes; no normal operations can resume.
      const descriptor=c.descriptorPath(b),before=await hostInstall.snapshot(descriptor);
      if(before){
        const data=JSON.parse(before.toString('utf8'));
        if(data.binding!==b.id || data.secret!==b.secret)throw new HubError('CONFIGURATION_CONFLICT');
        this.guard(p);await fs.unlink(descriptor);cleanup.removed_files++;
      }
      b.status=result.state==='complete'?'uninstalled':'blocked';
    }
    job.cleanup=cleanup;
    job.report={seal_id:job.seal.id,receipt_hash:job.seal.receipt_hash,result:cleanup.conflicts?'conflicts':'complete',
      removed_files:cleanup.removed_files,conflicts:cleanup.conflicts,retained_shared_components:true};
    job.local_state='report_pending';await c.persist();c.emitState();
    await this.report(p,remote,job);
  }
  async upload(p,remote,job,record,binding){
    const c=this.c;this.guard(p);
    if(!sameOwner(record.owner,c.work.owner()))throw new HubError('WORK_RECORD_NOT_ACCESSIBLE');
    const receipts=record.closeout_receipts??={},previous=receipts[job.id];
    if(previous?.code){(job.worklog_ids??={})[record.operation_id]=previous.log.id;return;}
    const body={operation_id:record.operation_id,task_id:record.task_id,run_id:record.run_id,lease_epoch:record.lease_epoch,
      context_id:record.body.context_id || p.workRuns?.[record.run_id]?.context_id,
      summary:record.body.summary,notes:record.body.notes,tests:record.body.tests || '后台检查点，未提交人工验证结果。',
      next_steps:record.body.next_steps || '交由获准接手者审查后继续。',outcome:record.body.outcome || 'blocked',
      changed_paths:record.snapshot.evidence.files.map(f=>f.path),change_evidence:record.snapshot.evidence,
      git_commit:record.snapshot.evidence.head_commit};
    const log=await remote.raw('POST',`/api/closeouts/${job.id}/checkpoints`,body,job.upload_token);this.guard(p);
    if(!uuid(log.id) || log.closeout_id!==job.id || log.evidence_hash!==record.snapshot.evidence_hash || !hash(log.receipt_hash))
      throw new HubError('CLOSEOUT_RECEIPT_MISMATCH');
    receipts[job.id]={log};(job.worklog_ids??={})[record.operation_id]=log.id;
    await c.work.store.write(record);await c.persist();this.guard(p);
    if(!record.snapshot.baseline)record.snapshot.baseline=await codec.baseline(binding?.workspace || record.workspace,record.snapshot.evidence.base_commit);
    const archive=codec.encode({...record,server_receipt:log});
    const result=await remote.raw('POST',`/api/closeouts/${job.id}/checkpoints/${log.id}/snapshot`,archive.data,job.upload_token,
      {binary:true,contentHash:archive.archive_sha256,idempotency:record.operation_id});this.guard(p);
    if(result.worklog_id!==log.id || result.evidence_hash!==record.snapshot.evidence_hash || result.manifest_hash!==archive.manifest_hash ||
        !hash(result.receipt_hash) || result.server_code_backup!==true || result.working_tree_restorable!==true)throw new HubError('CLOSEOUT_RECEIPT_MISMATCH');
    receipts[job.id].code=result;record.server_receipt=log;record.code_receipt=result;
    record.state='shared';record.backup_state='shared';record.backup_error=null;record.last_error=null;
    await c.work.store.write(record);c.emitState();
  }
  async report(p,remote,job){
    const result=await remote.raw('POST',`/api/closeouts/${job.id}/cleanup-report`,job.report,job.upload_token);this.guard(p);
    if(result.state!==job.report.result || result.cleanup_verification!=='client_report_only')throw new HubError('CLOSEOUT_RECEIPT_MISMATCH');
    job.local_state=result.state;job.error=job.report.conflicts?'CONFIGURATION_CONFLICT':null;
    if(job.local_state==='conflicts')delete job.report;
    await this.c.persist();this.c.emitState();
    if(job.notified_state!==job.local_state)this.c.emit('notification',{title:job.local_state==='complete'?'撤权收尾已完成':'撤权清理遇到冲突',
      body:job.local_state==='complete'?'工作已保存在服务器，本项目受管框架已卸载；共用组件和本机检查点保留。':'工作已上传，修改过的框架文件已保留，请在桌面查看。'});
    job.notified_state=job.local_state;
  }
}
module.exports={CloseoutRuntime};
