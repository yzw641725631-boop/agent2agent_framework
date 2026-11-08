'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {HubError}=require('./remote.cjs');
const observer=require('./git-observer.cjs');
const uuid=value=>typeof value==='string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
class WorkStore{
  constructor(directory,protector){this.directory=path.join(directory,'work-checkpoints');this.protector=protector;this.pending=Promise.resolve();}
  file(id){if(!uuid(id))throw new HubError('INVALID_REQUEST');return path.join(this.directory,id+'.bin');}
  async read(id){
    try{
      const stat=await fs.lstat(this.file(id));
      if(stat.isSymbolicLink() || !stat.isFile() || stat.size>32*1024*1024)throw new HubError('WORK_STORE_UNAVAILABLE');
      const result=await this.protector.decryptStringAsync(await fs.readFile(this.file(id)));
      const record=JSON.parse(result.result);
      if(record.schema!==1 || record.operation_id!==id)throw new HubError('WORK_STORE_UNAVAILABLE');
      return record;
    }catch(error){if(error.code==='ENOENT')return null;throw new HubError('WORK_STORE_UNAVAILABLE','无法解锁本地检查点，原文件已保留。');}
  }
  write(record,{create=false}={}){
    const job=this.pending.catch(()=>{}).then(async()=>{
      await fs.mkdir(this.directory,{recursive:true,mode:0o700});
      const dir=await fs.lstat(this.directory);
      if(dir.isSymbolicLink() || !dir.isDirectory())throw new HubError('WORK_PATH_UNSAFE');
      if(create && await this.read(record.operation_id))throw new HubError('WORK_RECORD_EXISTS');
      const names=await fs.readdir(this.directory);
      const exists=await this.read(record.operation_id);
      if(!exists && names.filter(n=>n.endsWith('.bin')).length>=100)throw new HubError('WORK_STORE_LIMIT','本地已保留 100 个检查点，请导出并整理后继续。');
      const bytes=await this.protector.encryptStringAsync(JSON.stringify(record));
      if(bytes.length>32*1024*1024)throw new HubError('WORK_SNAPSHOT_LIMIT');
      const target=this.file(record.operation_id),temporary=target+'.'+crypto.randomUUID()+'.next';
      try{
        const handle=await fs.open(temporary,'wx',0o600);
        try{await handle.writeFile(bytes);await handle.sync();}finally{await handle.close();}
        await fs.rename(temporary,target);
      }finally{await fs.unlink(temporary).catch(e=>{if(e.code!=='ENOENT')throw e;});}
      return record;
    });
    this.pending=job;return job;
  }
  async list(owner){
    this.readErrors=[];
    let names;try{names=await fs.readdir(this.directory);}catch(e){if(e.code==='ENOENT')return [];throw e;}
    const rows=[];
    for(const name of names.filter(n=>/^[a-f0-9-]+\.bin$/i.test(n)).slice(0,101)){
      let record;
      try{record=await this.read(name.slice(0,-4));}catch(error){this.readErrors.push({id:name.slice(0,-4),code:error.code});continue;}
      if(record && sameOwner(record.owner,owner)){
        // Retain metadata only: 100 full snapshots can otherwise exhaust desktop memory.
        const {contents,...snapshot}=record.snapshot;rows.push({...record,snapshot});
      }
    }
    return rows.sort((a,b)=>b.created_at.localeCompare(a.created_at));
  }
  async removeExported(record){
    const job=this.pending.catch(()=>{}).then(async()=>{
    const previous=await this.read(record.operation_id);
    if(!previous || observer.digest(previous)!==observer.digest(record))throw new HubError('WORK_RECORD_CHANGED');
    if(record.state!=='shared' || !record.exported_to || !path.isAbsolute(record.exported_to))throw new HubError('WORK_EXPORT_NOT_CONFIRMED');
    const target=await fs.realpath(record.exported_to);
    const receipt=await observer.safeRead(target,'checkpoint.json');
    const exported=JSON.parse(receipt.data.toString('utf8'));
    if(observer.digest(exported)!==observer.digest(this.exportContent(record)))throw new HubError('WORK_EXPORT_NOT_CONFIRMED');
    for(const file of record.snapshot.evidence.files){
      if(file.status==='deleted')continue;
      const data=await observer.safeRead(path.join(record.exported_to,'changed-files'),file.path);
      if(crypto.createHash('sha256').update(data.data).digest('hex')!==file.sha256)throw new HubError('WORK_EXPORT_NOT_CONFIRMED');
    }
    await fs.unlink(this.file(record.operation_id));return {removed:true,exported_to:record.exported_to};
    });
    this.pending=job;return job;
  }
  exportContent(record){
    return {task_id:record.task_id,run_id:record.run_id,
      evidence:record.snapshot.evidence,evidence_hash:record.snapshot.evidence_hash,worklog:record.body,
      server_receipt:record.server_receipt || null,restore:'Checkout base_commit separately, apply changed-files, then apply listed deletions after review.'};
  }
  async export(record,destination){
    if(!path.isAbsolute(destination))throw new HubError('INVALID_WORKSPACE');
    const parent=await fs.realpath(path.dirname(destination)),target=path.join(parent,path.basename(destination));
    if(await fs.realpath(parent)!==parent || target===path.parse(target).root)throw new HubError('WORK_PATH_UNSAFE');
    await fs.mkdir(target,{recursive:false});
    const content=new Map(record.snapshot.contents.map(f=>[f.path,f.content_base64]));
    // Export into a new directory only. Deleted paths are instructions; never delete source files.
    for(const file of record.snapshot.evidence.files){
      observer.keyPath(file.path);
      if(file.status==='deleted')continue;
      const bytes=Buffer.from(content.get(file.path) || '','base64');
      if(bytes.length!==file.bytes || crypto.createHash('sha256').update(bytes).digest('hex')!==file.sha256)
        throw new HubError('WORK_STORE_UNAVAILABLE','检查点内容与回执不匹配，导出已停止。');
      const output=path.join(target,'changed-files',...file.path.split('/'));
      await fs.mkdir(path.dirname(output),{recursive:true});await fs.writeFile(output,bytes,{flag:'wx',mode:0o600});
    }
    await fs.writeFile(path.join(target,'checkpoint.json'),JSON.stringify(this.exportContent(record),null,2),{flag:'wx',mode:0o600});
    return {directory:target,base_commit:record.snapshot.evidence.base_commit,files:record.snapshot.evidence.files.length};
  }
}
function sameOwner(a,b){return !!a && !!b && a.user_id===b.user_id && a.device_id===b.device_id && a.origin===b.origin;}
module.exports={WorkStore,sameOwner};
