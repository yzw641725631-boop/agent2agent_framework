'use strict';
const crypto=require('node:crypto'),zlib=require('node:zlib');
const observer=require('./git-observer.cjs'),{HubError}=require('./remote.cjs');
const limits=require('./backup-limits.cjs');
const MiB=1024*1024,MAX_BASE=limits.BASE_BYTES,MAX_ARCHIVE=limits.ARCHIVE_BYTES,MAX_JSON=limits.V1_JSON_BYTES;
const fail=(code='WORK_ARCHIVE_INVALID')=>{throw new HubError(code);};
const hash=data=>crypto.createHash('sha256').update(data).digest('hex');
const gitHash=(kind,data)=>crypto.createHash('sha1').update(kind+' '+data.length+'\0').update(data).digest('hex');
function safePath(value){
  observer.keyPath(value);
  if(/["<>|?*]/.test(value))fail();
  for(const part of value.toLowerCase().split('/')){
    if(/^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$/.test(part) ||
       ['.git','.ssh','.aws','.gnupg','.npmrc','.pypirc','.netrc','.env'].includes(part) ||
       part.startsWith('.env.') && part!=='.env.example' || part.includes('明文') || /\.(pem|key|pfx|p12)$/.test(part))fail('WORK_ARCHIVE_SECRET_PATH');
  }
  return value.toLowerCase();
}
function secret(data){
  if(/-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})/.test(data.toString('utf8')))
    fail('WORK_ARCHIVE_SECRET_CONTENT');
}
function bytes(value,max=limits.BASE_FILE_BYTES){
  if(typeof value!=='string' || value.length>Math.ceil(max/3)*4)fail();
  const data=Buffer.from(value,'base64');
  if(data.length>max || data.toString('base64')!==value)fail();
  secret(data);return data;
}
function treeHash(files){
  const root=new Map();
  for(const file of files){
    const parts=file.path.split('/');let node=root;
    for(const part of parts.slice(0,-1)){
      if(!node.has(part))node.set(part,new Map());
      node=node.get(part);if(!(node instanceof Map))fail();
    }
    if(node.has(parts.at(-1)))fail();node.set(parts.at(-1),file);
  }
  function visit(node){
    const rows=[...node].map(([name,value])=>{
      const directory=value instanceof Map,mode=directory?'40000':value.mode;
      const oid=directory?visit(value):value.git_oid;
      return {key:Buffer.from(name+(directory?'/':'')),data:Buffer.concat([Buffer.from(mode+' '+name+'\0'),Buffer.from(oid,'hex')])};
    }).sort((a,b)=>Buffer.compare(a.key,b.key));
    return gitHash('tree',Buffer.concat(rows.map(r=>r.data)));
  }
  return visit(root);
}
function workingFiles(archive){
  const result=new Map();
  for(const file of archive.baseline.files)result.set(safePath(file.path),{path:file.path,mode:file.mode,data:bytes(file.content_base64)});
  const content=new Map(archive.contents.map(f=>[f.path,f.content_base64]));
  for(const file of archive.evidence.files){
    const key=safePath(file.path);
    if(file.status==='deleted'){result.delete(key);continue;}
    if(result.has(key) && result.get(key).path!==file.path)fail();
    result.set(key,{path:file.path,mode:file.mode,data:bytes(content.get(file.path),limits.CHANGE_FILE_BYTES)});
  }
  const directories=new Map();
  for(const [key,file] of result){
    const parts=file.path.split('/');
    for(let i=1;i<parts.length;i++){
      const parent=parts.slice(0,i).join('/'),folded=parent.toLowerCase();
      if(result.has(folded) || directories.has(folded) && directories.get(folded)!==parent)fail();
      directories.set(folded,parent);
    }
    if(directories.has(key))fail();
  }
  return [...result.values()].sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);
}
function validate(value,{worklogId,evidenceHash,manifestHash}={}){
  const large=value?.schema===2,baseFiles=large?limits.BASE_FILES:limits.V1_BASE_FILES,baseBytes=large?MAX_BASE:limits.V1_BASE_BYTES;
  if(!value || ![1,2].includes(value.schema) || !value.baseline || !Array.isArray(value.baseline.files) || !Array.isArray(value.contents) ||
     !value.evidence || !Array.isArray(value.evidence.files) || value.evidence.files.length>100 || value.contents.length>100 ||
     value.baseline.files.length>baseFiles || value.evidence_hash!==observer.digest(value.evidence) ||
     worklogId && value.worklog_id!==worklogId || evidenceHash && value.evidence_hash!==evidenceHash ||
     manifestHash && observer.digest(value)!==manifestHash)fail();
  const commit=bytes(value.baseline.commit_base64);
  if(commit.length>65536 || gitHash('commit',commit)!==value.evidence.base_commit)fail('WORK_ARCHIVE_BASE_MISMATCH');
  let total=0;const seen=new Set();
  for(const file of value.baseline.files){
    const key=safePath(file.path),data=bytes(file.content_base64,large?limits.BASE_FILE_BYTES:limits.CHANGE_FILE_BYTES);
    if(seen.has(key) || !['100644','100755'].includes(file.mode) || gitHash('blob',data)!==file.git_oid)fail('WORK_ARCHIVE_BASE_MISMATCH');
    seen.add(key);total+=data.length;if(total>baseBytes)fail('WORK_ARCHIVE_LIMIT');
  }
  if(!commit.subarray(0,46).equals(Buffer.from('tree '+treeHash(value.baseline.files)+'\n')))fail('WORK_ARCHIVE_BASE_MISMATCH');
  const changes=new Map();let changedBytes=0;
  for(const file of value.evidence.files){
    const key=safePath(file.path);
    if(changes.has(key))fail();changes.set(key,file);
    if(file.status==='deleted'){
      if(file.sha256!==null || file.bytes!==0 || file.mode!=='000000')fail();
    }else if(!['added','modified'].includes(file.status) || !['100644','100755'].includes(file.mode))fail();
  }
  seen.clear();
  for(const file of value.contents){
    const key=safePath(file.path),entry=changes.get(key),data=bytes(file.content_base64,limits.CHANGE_FILE_BYTES);
    if(seen.has(key) || !entry || entry.status==='deleted' || entry.path!==file.path || entry.bytes!==data.length || entry.sha256!==hash(data))fail();
    seen.add(key);changedBytes+=data.length;
  }
  if(changedBytes>16*MiB || seen.size!==[...changes.values()].filter(f=>f.status!=='deleted').length)fail();
  checkLayout(value);return value;
}
function checkLayout(value){
  const paths=new Map(value.baseline.files.map(f=>[safePath(f.path),f.path]));
  for(const file of value.evidence.files){
    const key=safePath(file.path);
    if(file.status==='deleted')paths.delete(key);
    else{if(paths.has(key) && paths.get(key)!==file.path)fail();paths.set(key,file.path);}
  }
  const directories=new Map();
  for(const [key,file] of paths){
    const parts=file.split('/');
    for(let i=1;i<parts.length;i++){
      const parent=parts.slice(0,i).join('/'),folded=parent.toLowerCase();
      if(paths.has(folded) || directories.has(folded) && directories.get(folded)!==parent)fail();
      directories.set(folded,parent);
    }
    if(directories.has(key))fail();
  }
}
function archiveSchema(snapshot){
  const files=snapshot.baseline?.files || [];let total=0;
  for(const file of files){const size=Buffer.byteLength(file.content_base64 || '', 'base64');total+=size;
    if(size>limits.CHANGE_FILE_BYTES)return 2;}
  return files.length>limits.V1_BASE_FILES || total>limits.V1_BASE_BYTES?2:1;
}
async function baseline(workspace,commitId){
  if(!/^[a-f0-9]{40}$/.test(commitId))fail('WORK_ARCHIVE_GIT_FORMAT_UNSUPPORTED');
  const commit=await observer.git(workspace,['cat-file','commit',commitId]);
  const listing=await observer.git(workspace,['ls-tree','--full-tree','-r','-l','-z',commitId],{maxBuffer:limits.MANIFEST_BYTES});
  const decoder=new TextDecoder('utf-8',{fatal:true});
  const rows=decoder.decode(listing).split('\0').filter(Boolean).map(line=>{
    const tab=line.indexOf('\t'),header=line.slice(0,tab).trim().split(/\s+/),file=line.slice(tab+1),size=Number(header[3]);
    if(tab<0 || header[1]!=='blob' || !['100644','100755'].includes(header[0]) || !/^[a-f0-9]{40}$/.test(header[2]))fail('WORK_ARCHIVE_GIT_FORMAT_UNSUPPORTED');
    if(!Number.isSafeInteger(size) || size<0 || size>limits.BASE_FILE_BYTES)fail('WORK_ARCHIVE_LIMIT');
    safePath(file);return {path:file,mode:header[0],git_oid:header[2],size};
  });
  if(rows.length>limits.BASE_FILES || rows.reduce((n,f)=>n+f.size,0)>MAX_BASE)fail('WORK_ARCHIVE_LIMIT');
  const batch=await observer.git(workspace,['cat-file','--batch'],{input:rows.map(f=>f.git_oid).join('\n')+'\n',maxBuffer:MAX_BASE+2*MiB});
  let offset=0,total=0;
  for(const row of rows){
    const end=batch.indexOf(10,offset);if(end<0)fail();
    const [oid,kind,length]=batch.subarray(offset,end).toString('ascii').split(' '),size=Number(length);
    if(oid!==row.git_oid || kind!=='blob' || !Number.isSafeInteger(size) || size!==row.size)fail('WORK_ARCHIVE_LIMIT');
    offset=end+1;const data=batch.subarray(offset,offset+size);offset+=size;
    if(data.length!==size || batch[offset++]!==10 || gitHash('blob',data)!==oid)fail();
    secret(data);total+=size;if(total>MAX_BASE)fail('WORK_ARCHIVE_LIMIT');row.content_base64=data.toString('base64');delete row.size;
  }
  if(!commit.subarray(0,46).equals(Buffer.from('tree '+treeHash(rows)+'\n')))fail('WORK_ARCHIVE_BASE_MISMATCH');
  return {commit_base64:commit.toString('base64'),files:rows};
}
function encode(record){
  const value=validate({schema:1,worklog_id:record.server_receipt.id,evidence:record.snapshot.evidence,
    evidence_hash:record.snapshot.evidence_hash,baseline:record.snapshot.baseline,contents:record.snapshot.contents});
  const raw=Buffer.from(observer.canonical(value));if(raw.length>MAX_JSON)fail('WORK_ARCHIVE_LIMIT');
  const data=zlib.gzipSync(raw,{level:6});if(data.length>limits.V1_ARCHIVE_BYTES)fail('WORK_ARCHIVE_LIMIT');
  return {data,manifest_hash:observer.digest(value),archive_sha256:hash(data)};
}
function decode(data,expected){
  if(data.length>limits.V1_ARCHIVE_BYTES)fail('WORK_ARCHIVE_LIMIT');
  try{const value=JSON.parse(zlib.gunzipSync(data,{maxOutputLength:MAX_JSON}));
    if(value.schema!==1)fail();return validate(value,expected);}
  catch(error){if(error instanceof HubError)throw error;fail();}
}
async function encodeArchive(record){return archiveSchema(record.snapshot)===1?encode(record):require('./backup-v2.cjs').encode(record);}
async function decodeArchive(data,expected){return require('./backup-v2.cjs').decode(data,expected);}
module.exports={baseline,encode,decode,encodeArchive,decodeArchive,archiveSchema,validate,workingFiles,checkLayout,safePath,gitHash,treeHash,hash,bytes,MAX_ARCHIVE};
