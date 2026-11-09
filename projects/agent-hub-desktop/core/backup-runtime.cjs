'use strict';
const codec=require('./backup-codec.cjs'),observer=require('./git-observer.cjs');
const {HubError}=require('./remote.cjs');
const {sameOwner}=require('./work-store.cjs');
class BackupRuntime{
  constructor(controller){this.c=controller;this.operations=Promise.resolve();}
  serialize(action){const job=this.operations.catch(()=>{}).then(action);this.operations=job.catch(()=>{});return job;}
  async prepare(binding,snapshot){
    return this.serialize(async()=>{snapshot.baseline=await codec.baseline(binding.workspace,snapshot.evidence.base_commit);});
  }
  async deliver(binding,record){
    return this.serialize(async()=>{
      const c=this.c,p=c.profile,remote=c.remote;
      if(!sameOwner(record.owner,c.work.owner()) || !record.server_receipt?.id)throw new HubError('WORK_RECORD_NOT_ACCESSIBLE');
      if(c.updater){const update=await c.updater.check();if(!update.can_continue)throw new HubError(update.code,update.message);}
      c.ensureCurrent(p,remote);
      if(!record.snapshot.baseline)record.snapshot.baseline=await codec.baseline(binding.workspace,record.snapshot.evidence.base_commit);
      const archive=codec.encode(record),slot=await c.projectSlot(binding.projectId);
      c.ensureCurrent(p,remote);
      const result=await remote.request('POST',`/api/projects/${binding.projectId}/tasks/${record.task_id}/worklogs/${record.server_receipt.id}/snapshot`,
        archive.data,{slot,binary:true,contentHash:archive.archive_sha256,idempotency:record.operation_id});
      c.ensureCurrent(p,remote);
      if(result.worklog_id!==record.server_receipt.id || result.evidence_hash!==record.snapshot.evidence_hash ||
         result.manifest_hash!==archive.manifest_hash || result.server_code_backup!==true || result.working_tree_restorable!==true ||
         !/^[a-f0-9-]{36}$/.test(result.id || '') || !/^[a-f0-9]{64}$/.test(result.receipt_hash || ''))throw new HubError('WORK_ARCHIVE_RECEIPT_MISMATCH');
      record.code_receipt=result;record.backup_state='shared';record.backup_error=null;return result;
    });
  }
  async download(binding,args){
    return this.serialize(async()=>{
      const c=this.c,p=c.profile,remote=c.remote,requestHash=observer.digest(args);
      const log=await c.invokeProjectRemote(binding,'hub_read_worklog',{task_id:args.task_id,worklog_id:args.worklog_id});
      if(!log.snapshot)throw new HubError('WORK_ARCHIVE_NOT_FOUND');
      const existing=await c.work.store.read(args.operation_id);
      if(existing){
        if(!sameOwner(existing.owner,c.work.owner()) || existing.binding_id!==binding.id || existing.request_hash!==requestHash)
          throw new HubError('IDEMPOTENCY_PAYLOAD_CHANGED');
        return c.work.receipt(existing);
      }
      const slot=await c.projectSlot(binding.projectId);
      const data=await remote.request('GET',`/api/projects/${binding.projectId}/tasks/${args.task_id}/worklogs/${args.worklog_id}/snapshot/content`,undefined,{slot,binary:true});
      c.ensureCurrent(p,remote);
      if(codec.hash(data)!==log.snapshot.archive_sha256)throw new HubError('WORK_ARCHIVE_HASH_MISMATCH');
      const archive=codec.decode(data,{worklogId:log.id,evidenceHash:log.evidence_hash,manifestHash:log.snapshot.manifest_hash});
      const record={schema:1,operation_id:args.operation_id,request_hash:requestHash,owner:c.work.owner(),binding_id:binding.id,
        project_id:binding.projectId,task_id:args.task_id,run_id:log.run_id,lease_epoch:log.lease_epoch,workspace:binding.workspace,
        created_at:new Date().toISOString(),state:'shared',backup_state:'shared',recovered_from:log.snapshot.id,
        body:log,snapshot:{evidence:archive.evidence,evidence_hash:archive.evidence_hash,baseline:archive.baseline,contents:archive.contents},
        server_receipt:log,code_receipt:log.snapshot};
      await c.work.store.write(record,{create:true});c.emitState();
      return {...c.work.receipt(record),next_action:'Open desktop checkpoints and export into a new directory. Review working-tree before adopting it; this does not claim a task or restore Git history.'};
    });
  }
}
module.exports={BackupRuntime};
