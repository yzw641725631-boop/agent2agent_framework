'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const observer=require('../core/git-observer.cjs'),codec=require('../core/backup-codec.cjs');
const {WorkRuntime}=require('../core/work-runtime.cjs'),{BackupRuntime}=require('../core/backup-runtime.cjs');
const {Remote,identity,HubError}=require('../core/remote.cjs');
const execute=promisify(execFile),policy={content_hash:'a'.repeat(64),protected_paths:[],rules:[]},scope={paths:['src']};
async function temporary(t){
  const base=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(base,'hub-backup-test-'));
  t.after(async()=>{assert.equal(path.dirname(dir),base);assert.ok(path.basename(dir).startsWith('hub-backup-test-'));await fs.rm(dir,{recursive:true,force:true});});return dir;
}
const git=(dir,...args)=>execute('git',['-C',dir,...args],{windowsHide:true});
function protector(){
  const key=crypto.randomBytes(32);return {
    async encryptStringAsync(value){const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);
      return Buffer.concat([iv,cipher.update(value,'utf8'),cipher.final(),cipher.getAuthTag()]);},
    async decryptStringAsync(data){const cipher=crypto.createDecipheriv('aes-256-gcm',key,data.subarray(0,12));cipher.setAuthTag(data.subarray(-16));
      return {result:Buffer.concat([cipher.update(data.subarray(12,-16)),cipher.final()]).toString('utf8')};},
  };
}
async function repository(t){
  const dir=await temporary(t);await git(dir,'init','-q');await git(dir,'config','core.autocrlf','false');
  await fs.mkdir(path.join(dir,'src'));await fs.mkdir(path.join(dir,'docs'));
  await fs.writeFile(path.join(dir,'src/main.ts'),'baseline\n');await fs.writeFile(path.join(dir,'src/remove.ts'),'delete this\n');
  await fs.writeFile(path.join(dir,'docs/design.md'),'unchanged design\n');
  await git(dir,'add','.');await git(dir,'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','baseline');return dir;
}
async function changedRecord(t){
  const dir=await repository(t),base=await observer.initialize(dir);
  await fs.writeFile(path.join(dir,'src/main.ts'),'committed change\n');await git(dir,'add','.');
  await git(dir,'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','task commit');
  await fs.writeFile(path.join(dir,'src/main.ts'),'latest uncommitted code\n');await fs.unlink(path.join(dir,'src/remove.ts'));
  await fs.writeFile(path.join(dir,'src/new.bin'),Buffer.from([0,255,1,13,10,128]));
  const snapshot=await observer.capture(dir,base.base_commit,scope,policy);snapshot.baseline=await codec.baseline(dir,base.base_commit);
  return {dir,record:{schema:1,operation_id:crypto.randomUUID(),server_receipt:{id:crypto.randomUUID()},snapshot}};
}
test('self-contained Git checkpoint restores baseline, committed/uncommitted, deleted and binary files in a new directory',async t=>{
  const {dir,record}=await changedRecord(t),archive=codec.encode(record),decoded=codec.decode(archive.data,{worklogId:record.server_receipt.id,
    evidenceHash:record.snapshot.evidence_hash,manifestHash:archive.manifest_hash});
  const files=codec.workingFiles(decoded);assert.deepEqual(files.map(f=>f.path),['docs/design.md','src/main.ts','src/new.bin']);
  assert.equal(files[1].data.toString(),'latest uncommitted code\n');assert.deepEqual(files[2].data,Buffer.from([0,255,1,13,10,128]));
  const {WorkStore}=require('../core/work-store.cjs'),data=await temporary(t),store=new WorkStore(data,protector());
  Object.assign(record,{owner:{user_id:'one',device_id:'device',origin:'https://hub.example'},created_at:new Date().toISOString(),body:{notes:'handoff'},state:'shared'});
  await store.write(record,{create:true});
  assert.ok(!(await fs.readFile(store.file(record.operation_id))).includes(Buffer.from('latest uncommitted code')));
  const metadata=await store.list(record.owner);assert.equal(metadata[0].snapshot.baseline,undefined);assert.equal(metadata[0].snapshot.contents,undefined);
  assert.equal(metadata[0].snapshot.baseline_available,true);
  const result=await store.export(record,path.join(data,'export'));
  assert.equal(result.git_history_included,false);assert.equal(await fs.readFile(path.join(result.working_tree_directory,'docs/design.md'),'utf8'),'unchanged design\n');
  await assert.rejects(fs.stat(path.join(result.working_tree_directory,'src/remove.ts')),{code:'ENOENT'});
  assert.equal(await fs.readFile(path.join(dir,'src/main.ts'),'utf8'),'latest uncommitted code\n');
  await assert.rejects(store.export(record,path.join(data,'export')),{code:'EEXIST'});
  record.exported_to=result.directory;await store.write(record);
  await fs.writeFile(path.join(result.working_tree_directory,'docs/design.md'),'changed after export');
  await assert.rejects(store.removeExported(record),{code:'WORK_EXPORT_NOT_CONFIRMED'});assert.ok(await store.read(record.operation_id));
});
test('archive rejects altered/missing baselines, mismatched evidence, credential paths and Windows collisions',async t=>{
  const {record}=await changedRecord(t),archive=codec.decode(codec.encode(record).data),clone=()=>structuredClone(archive);
  let bad=clone();bad.baseline.files[0].content_base64=Buffer.from('altered').toString('base64');
  assert.throws(()=>codec.validate(bad),{code:'WORK_ARCHIVE_BASE_MISMATCH'});
  bad=clone();bad.baseline.files=[];assert.throws(()=>codec.validate(bad),{code:'WORK_ARCHIVE_BASE_MISMATCH'});
  bad=clone();bad.contents[0].content_base64='';assert.throws(()=>codec.validate(bad),{code:'WORK_ARCHIVE_INVALID'});
  assert.throws(()=>codec.decode(codec.encode(record).data,{manifestHash:'0'.repeat(64)}),{code:'WORK_ARCHIVE_INVALID'});
  for(const name of ['../escape','C:/escape','src/.git/config','src/.env','src/secret.pem','NUL.txt','src/明文.txt'])assert.throws(()=>codec.safePath(name));
  bad=clone();bad.baseline.files.push({...bad.baseline.files[0],path:bad.baseline.files[0].path.toUpperCase()});assert.throws(()=>codec.validate(bad));
  const outside=await temporary(t),linked=(await changedRecord(t)).dir;await fs.writeFile(path.join(outside,'hidden.txt'),'outside');
  await fs.symlink(outside,path.join(linked,'src/link'),'junction');
  await assert.rejects(observer.safeRead(linked,'src/link/hidden.txt'),{code:'WORK_PATH_UNSAFE'});
  // Native Git object content is validated too, not trusted merely because it came from git.
  assert.throws(()=>codec.validate({...archive,baseline:{...archive.baseline,commit_base64:Buffer.from('not a commit').toString('base64')}}));
});
async function fixture(t){
  const dir=await repository(t),data=await temporary(t),projectId=crypto.randomUUID(),runId=crypto.randomUUID(),taskId=crypto.randomUUID(),contextId=crypto.randomUUID();
  const binding={id:crypto.randomUUID(),repositoryId:crypto.randomUUID(),projectId,workspace:dir,status:'active'};
  const profile={origin:'https://hub.example',user:{id:crypto.randomUUID()},deviceId:crypto.randomUUID(),bindings:[binding],workRuns:{}};
  const remote={},c={profile,remote,directory:data,connection:'active',vault:{protector:protector()},persist:async()=>{},emitState(){},
    projectSlot:async()=>projectId,ensureCurrent(p,r){if(this.profile!==p || this.remote!==r)throw new HubError('LOGIN_REQUIRED');}};
  let offline=false,backupFailure=null,log=null,bytes=null,uploads=0;
  c.invokeProjectRemote=async(b,action,args)=>{
    if(offline)throw new HubError('NETWORK_UNAVAILABLE');
    if(action==='hub_prepare_work')return {id:contextId,content_hash:'b'.repeat(64),payload:{policy,task:{scope,execution_mode:'observed'}}};
    if(action==='hub_acknowledge_work')return {};
    if(action==='hub_write_worklog'){uploads++;log={...args,id:crypto.randomUUID(),run_id:runId,evidence_hash:observer.digest(args.change_evidence)};return structuredClone(log);}
    if(action==='hub_read_worklog')return structuredClone(log);
    if(action==='hub_heartbeat_task')return {};
    throw new Error(action);
  };
  remote.request=async(method,url,body)=>{
    if(backupFailure)throw new HubError(backupFailure);
    if(method==='GET')return bytes;
    bytes=Buffer.from(body);const decoded=codec.decode(bytes);
    const receipt={id:crypto.randomUUID(),worklog_id:log.id,evidence_hash:decoded.evidence_hash,manifest_hash:observer.digest(decoded),
      archive_sha256:codec.hash(bytes),receipt_hash:'f'.repeat(64),server_code_backup:true,working_tree_restorable:true};
    log.snapshot=receipt;return receipt;
  };
  c.work=new WorkRuntime(c);c.backups=new BackupRuntime(c);t.after(()=>c.work.stop());
  const lease={task_id:taskId,run_id:runId,lease_epoch:1};
  await c.work.prepare(binding,lease);await c.work.acknowledge(binding,{...lease,context_id:contextId,content_hash:'b'.repeat(64)});
  await fs.writeFile(path.join(dir,'src/main.ts'),'pending full backup\n');
  return {c,dir,data,binding,profile,lease,remote,log:()=>log,uploads:()=>uploads,
    args:{...lease,summary:'round',notes:'handoff context',tests:'checked',next_steps:'review',operation_id:crypto.randomUUID()},
    failBackup(code){backupFailure=code;},offline(value){offline=value;c.connection=value?'offline':'active';}};
}
test('log sharing and code backup are independent; background retry uploads the original captured bytes only once',async t=>{
  const f=await fixture(t);f.failBackup('NETWORK_UNAVAILABLE');
  const saved=await f.c.work.save(f.binding,f.args);assert.equal(saved.server_confirmed,true);assert.equal(saved.server_code_backup,false);assert.equal(saved.backup_state,'pending');
  await fs.writeFile(path.join(f.dir,'src/main.ts'),'newer work must not replace checkpoint');
  const record=await f.c.work.store.read(saved.local_checkpoint_id);record.next_backup=0;await f.c.work.store.write(record);f.failBackup(null);
  await f.c.work.tick();const delivered=await f.c.work.store.read(saved.local_checkpoint_id);
  assert.equal(delivered.backup_state,'shared');assert.ok(delivered.code_receipt);assert.equal(f.uploads(),1);
  assert.equal(Buffer.from(delivered.snapshot.contents[0].content_base64,'base64').toString(),'pending full backup\n');
  await f.c.work.retry(saved.local_checkpoint_id);assert.equal(f.uploads(),1);
});
test('offline records retain the full baseline and repository backup denial never masquerades as a shared code backup',async t=>{
  const f=await fixture(t);f.offline(true);let saved=await f.c.work.save(f.binding,f.args);
  assert.equal(saved.server_confirmed,false);assert.ok((await f.c.work.store.read(saved.local_checkpoint_id)).snapshot.baseline);
  f.offline(false);f.failBackup('REPOSITORY_NOT_ACCESSIBLE');saved=await f.c.work.retry(saved.local_checkpoint_id);
  assert.equal(saved.server_confirmed,true);assert.equal(saved.server_code_backup,false);assert.equal(saved.backup_state,'blocked');
  assert.ok((await f.c.work.store.read(saved.local_checkpoint_id)).snapshot.contents.length);
  f.binding.status='blocked';await assert.rejects(f.c.work.retry(saved.local_checkpoint_id),{code:'PROJECT_ACCESS_REVOKED'});
});
test('a permitted colleague downloads encrypted work, exports it without overwriting workspace, and rechecks permission on replay',async t=>{
  const author=await fixture(t);await author.c.work.save(author.binding,author.args);
  const reader=await fixture(t);reader.remote.request=author.remote.request;
  reader.c.invokeProjectRemote=author.c.invokeProjectRemote;
  const args={task_id:author.lease.task_id,worklog_id:author.log().id,operation_id:crypto.randomUUID()};
  const saved=await reader.c.backups.download(reader.binding,args);assert.equal(saved.server_code_backup,true);assert.ok(saved.recovered_from);
  const result=await reader.c.work.export(saved.local_checkpoint_id,path.join(reader.data,'handoff'));
  assert.equal(await fs.readFile(path.join(result.working_tree_directory,'src/main.ts'),'utf8'),'pending full backup\n');
  assert.equal(await fs.readFile(path.join(reader.dir,'src/main.ts'),'utf8'),'pending full backup\n');
  assert.equal((await reader.c.backups.download(reader.binding,args)).local_checkpoint_id,saved.local_checkpoint_id);
  author.offline(true);await assert.rejects(reader.c.backups.download(reader.binding,args),{code:'NETWORK_UNAVAILABLE'});
  reader.c.profile={...reader.profile,user:{id:crypto.randomUUID()}};
  assert.equal((await reader.c.work.list()).items.length,0);
});
test('account change during code download cannot save the old project under the new identity',async t=>{
  const f=await fixture(t);await f.c.work.save(f.binding,f.args);
  const original=f.remote.request;f.remote.request=async(...args)=>{const result=await original(...args);f.c.profile={...f.profile,user:{id:crypto.randomUUID()}};return result;};
  const operation=crypto.randomUUID();await assert.rejects(f.c.backups.download(f.binding,{task_id:f.lease.task_id,worklog_id:f.log().id,operation_id:operation}),{code:'LOGIN_REQUIRED'});
  assert.equal(await f.c.work.store.read(operation),null);
});
function binaryProfile(){return {origin:'https://hub.example',identity:identity(),sessions:{platform:{access_token:'first',refresh_token:'refresh',expires_at:new Date(Date.now()+300000).toISOString()}}};}
test('binary Remote refresh preserves bytes, DPoP binding and idempotency; download verifies the server digest',async()=>{
  const profile=binaryProfile(),data=Buffer.from('compressed checkpoint'),seen=[];
  const remote=new Remote(profile,async()=>{},async(url,options)=>{
    if(url.endsWith('/auth/refresh'))return Response.json({...profile.sessions.platform,access_token:'second'});
    seen.push(options);const claims=JSON.parse(Buffer.from(options.headers.DPoP.split('.')[1],'base64url'));
    assert.equal(claims.ath,crypto.createHash('sha256').update(options.headers.Authorization.slice(5)).digest('base64url'));
    if(options.method==='GET')return new Response(data,{headers:{'X-Content-SHA256':codec.hash(data)}});
    assert.deepEqual(Buffer.from(options.body),data);assert.equal(options.headers['Content-Type'],'application/gzip');
    return options.headers.Authorization==='DPoP first'?Response.json({code:'SESSION_EXPIRED_OR_REVOKED'},{status:401}):Response.json({stored:true});
  });
  assert.deepEqual(await remote.request('POST','/api/snapshot',data,{binary:true,contentHash:codec.hash(data),idempotency:'same-operation'}),{stored:true});
  assert.equal(seen[0].headers['Idempotency-Key'],seen[1].headers['Idempotency-Key']);assert.notEqual(seen[0].headers.DPoP,seen[1].headers.DPoP);
  assert.deepEqual(await remote.request('GET','/api/snapshot',undefined,{binary:true}),data);
});
test('binary download truncation, forged content hashes and excessive size fail without yielding a checkpoint',async()=>{
  const remoteFor=transport=>new Remote(binaryProfile(),async()=>{},transport);
  await assert.rejects(remoteFor(async()=>new Response('damaged',{headers:{'X-Content-SHA256':'0'.repeat(64)}})).request('GET','/api/snapshot',undefined,{binary:true}),{code:'WORK_ARCHIVE_HASH_MISMATCH'});
  await assert.rejects(remoteFor(async()=>new Response('',{headers:{'Content-Length':String(37*1024*1024)}})).request('GET','/api/snapshot',undefined,{binary:true}),{code:'WORK_ARCHIVE_LIMIT'});
  const broken=new ReadableStream({start(controller){controller.enqueue(new Uint8Array([1,2]));controller.error(new TypeError('connection closed'));}});
  await assert.rejects(remoteFor(async()=>new Response(broken)).request('GET','/api/snapshot',undefined,{binary:true}),{code:'NETWORK_UNAVAILABLE'});
});
