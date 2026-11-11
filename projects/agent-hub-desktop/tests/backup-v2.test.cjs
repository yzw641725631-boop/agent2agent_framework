'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto'),zlib=require('node:zlib');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const codec=require('../core/backup-codec.cjs'),v2=require('../core/backup-v2.cjs');
const observer=require('../core/git-observer.cjs'),{WorkStore}=require('../core/work-store.cjs');
const execute=promisify(execFile),git=(dir,...args)=>execute('git',['-C',dir,...args],{windowsHide:true});
async function directory(t){
  const root=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(root,'hub-large-test-'));
  t.after(async()=>{assert.equal(path.dirname(dir),root);assert.ok(path.basename(dir).startsWith('hub-large-test-'));await fs.rm(dir,{recursive:true,force:true});});return dir;
}
function record(){
  const original=Buffer.from('original\n'),change=Buffer.from('changed\n');
  const files=[{path:'src/a.txt',mode:'100644',git_oid:codec.gitHash('blob',original),content_base64:original.toString('base64')}];
  const commit=Buffer.from('tree '+codec.treeHash(files)+'\nauthor Test <test@example.org> 1 +0000\n\nbase\n');
  const base=codec.gitHash('commit',commit),evidence={schema_version:1,source:'desktop-git-observer',base_commit:base,head_commit:base,
    files:[{path:'src/a.txt',status:'modified',mode:'100644',bytes:change.length,sha256:codec.hash(change)}]};
  return {schema:1,operation_id:crypto.randomUUID(),task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),state:'shared',owner:{user_id:'test',device_id:'test',origin:'https://example.org'},
    created_at:new Date().toISOString(),body:{summary:'synthetic handoff'},server_receipt:{id:crypto.randomUUID()},
    snapshot:{evidence,evidence_hash:observer.digest(evidence),baseline:{commit_base64:commit.toString('base64'),files},
      contents:[{path:'src/a.txt',content_base64:change.toString('base64')}]}};
}
function unpack(data){const raw=zlib.gunzipSync(data),end=10+raw.readUInt32BE(6);return {manifest:JSON.parse(raw.subarray(10,end)),bodies:raw.subarray(end)};}
function pack(manifest,bodies,text=observer.canonical(manifest)){
  const raw=Buffer.from(text),prefix=Buffer.alloc(10);v2.MAGIC.copy(prefix);prefix.writeUInt32BE(raw.length,6);
  return zlib.gzipSync(Buffer.concat([prefix,raw,bodies]));
}
test('v2 rejects tampering, truncation, duplicate JSON, forged metadata, trailing data, unsafe paths and bombs',async()=>{
  const r=record(),encoded=await v2.encode(r),{manifest,bodies}=unpack(encoded.data);
  const decoded=await codec.decodeArchive(encoded.data,{worklogId:r.server_receipt.id,manifestHash:encoded.manifest_hash});
  assert.equal(decoded.schema,2);assert.deepEqual(codec.workingFiles(decoded).map(f=>f.data.toString()),['changed\n']);
  await assert.rejects(codec.decodeArchive(encoded.data,{manifestHash:'0'.repeat(64)}));
  const changed=Buffer.from(bodies);changed[0]^=1;await assert.rejects(codec.decodeArchive(pack(manifest,changed)),{code:'WORK_ARCHIVE_HASH_MISMATCH'});
  for(const data of [encoded.data.subarray(0,-1),encoded.data.subarray(0,12),Buffer.from('bad'),
    pack(manifest,Buffer.concat([bodies,Buffer.from('extra')])),Buffer.concat([encoded.data,zlib.gzipSync(Buffer.from('extra'))])])
    await assert.rejects(codec.decodeArchive(data));
  const badTrailer=Buffer.from(encoded.data);badTrailer[badTrailer.length-8]^=1;await assert.rejects(codec.decodeArchive(badTrailer));
  const duplicate=observer.canonical(manifest).replace('"schema":2','"schema":1,"schema":2');
  await assert.rejects(codec.decodeArchive(pack(manifest,bodies,duplicate)));
  for(const mutate of [m=>m.baseline.files[0].bytes=16*1024*1024+1,m=>m.baseline.files[0].bytes=-1,
    m=>m.baseline.files[0].path='../escape',m=>m.baseline.files[0].git_oid='a'.repeat(40),
    m=>m.contents[0].sha256='a'.repeat(64),m=>m.baseline.files.push({...m.baseline.files[0],path:'SRC/A.TXT'}),
    m=>m.baseline.files=Array(10001).fill(m.baseline.files[0])]){
    const bad=structuredClone(manifest);mutate(bad);await assert.rejects(codec.decodeArchive(pack(bad,bodies)));
  }
  const prefix=Buffer.alloc(10);v2.MAGIC.copy(prefix);prefix.writeUInt32BE(8*1024*1024+1,6);
  await assert.rejects(codec.decodeArchive(zlib.gzipSync(prefix)),{code:'WORK_ARCHIVE_LIMIT'});
  await assert.rejects(codec.decodeArchive(pack(manifest,Buffer.concat([bodies,Buffer.alloc(4*1024*1024)]))));
  const legacy=codec.encode(r);assert.equal((await codec.decodeArchive(legacy.data,{manifestHash:legacy.manifest_hash})).schema,1);
});
test('real Git with over 1000 baseline files, over 16 MiB total and a 5 MiB file survives encrypted store and new-directory export',async t=>{
  const root=await directory(t),source=path.join(root,'source');await fs.mkdir(source);await fs.mkdir(path.join(source,'src'));
  await git(source,'init','-q');await git(source,'config','core.autocrlf','false');
  const body=crypto.randomBytes(16*1024);
  for(let i=0;i<1010;i++)await fs.writeFile(path.join(source,'src',`file-${i}.bin`),body);
  const large=crypto.randomBytes(5*1024*1024);await fs.writeFile(path.join(source,'src','large.bin'),large);
  await git(source,'add','.');await git(source,'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','large baseline');
  const base=await observer.initialize(source),r=record();r.snapshot.baseline=await codec.baseline(source,base.base_commit);
  await fs.writeFile(path.join(source,'src','file-0.bin'),'pending handoff\n');await fs.unlink(path.join(source,'src','file-1.bin'));
  const captured=await observer.capture(source,base.base_commit,{paths:['src']},{protected_paths:[]});
  r.snapshot.evidence=captured.evidence;r.snapshot.evidence_hash=captured.evidence_hash;r.snapshot.contents=captured.contents;
  assert.equal(codec.archiveSchema(r.snapshot),2);const encoded=await codec.encodeArchive(r);
  const recovered=await codec.decodeArchive(encoded.data,{manifestHash:encoded.manifest_hash});
  r.snapshot={evidence:recovered.evidence,evidence_hash:recovered.evidence_hash,baseline:recovered.baseline,contents:recovered.contents};
  const key=crypto.randomBytes(32),protector={async encryptStringAsync(text){const iv=crypto.randomBytes(12),c=crypto.createCipheriv('aes-256-gcm',key,iv);
    return Buffer.concat([iv,c.update(text,'utf8'),c.final(),c.getAuthTag()]);},async decryptStringAsync(data){
    const c=crypto.createDecipheriv('aes-256-gcm',key,data.subarray(0,12));c.setAuthTag(data.subarray(-16));return {result:Buffer.concat([c.update(data.subarray(12,-16)),c.final()]).toString('utf8')};}};
  const store=new WorkStore(root,protector);await store.write(r,{create:true});const saved=await store.read(r.operation_id);
  const exported=await store.export(saved,path.join(root,'handoff'));
  assert.deepEqual(await fs.readFile(path.join(exported.working_tree_directory,'src/large.bin')),large);
  assert.equal(await fs.readFile(path.join(exported.working_tree_directory,'src/file-0.bin'),'utf8'),'pending handoff\n');
  await assert.rejects(fs.stat(path.join(exported.working_tree_directory,'src/file-1.bin')),{code:'ENOENT'});
  await assert.rejects(store.export(saved,path.join(root,'handoff')),{code:'EEXIST'});
  saved.exported_to=exported.directory;await store.write(saved);assert.equal((await store.removeExported(saved)).removed,true);
  assert.deepEqual(await fs.readFile(path.join(source,'src/large.bin')),large);
});
