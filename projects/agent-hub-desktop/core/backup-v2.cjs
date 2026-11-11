'use strict';
// A canonical manifest followed by bounded raw file bodies, inside one gzip stream.
// No filesystem paths are extracted from the container by a generic archive tool.
const {Readable}=require('node:stream'),zlib=require('node:zlib');
const codec=require('./backup-codec.cjs'),observer=require('./git-observer.cjs');
const limits=require('./backup-limits.cjs'),{HubError}=require('./remote.cjs');
const MAGIC=Buffer.from('HUBW2\n');
const fail=(code='WORK_ARCHIVE_INVALID')=>{throw new HubError(code);};
const object=(value,keys)=>value && !Array.isArray(value) && typeof value==='object' &&
  Object.keys(value).sort().join(',')===[...keys].sort().join(',');
const hex=value=>typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
function wire(record){
  const value=codec.validate({schema:2,worklog_id:record.server_receipt.id,evidence:record.snapshot.evidence,
    evidence_hash:record.snapshot.evidence_hash,baseline:record.snapshot.baseline,contents:record.snapshot.contents});
  const metadata=file=>{const data=codec.bytes(file.content_base64);return {path:file.path,bytes:data.length,sha256:codec.hash(data)};};
  const manifest={...value,baseline:{commit_base64:value.baseline.commit_base64,files:value.baseline.files.map(file=>
    ({...metadata(file),mode:file.mode,git_oid:file.git_oid}))},contents:value.contents.map(metadata)};
  const raw=Buffer.from(observer.canonical(manifest));
  if(raw.length>limits.MANIFEST_BYTES)fail('WORK_ARCHIVE_LIMIT');
  return {value,manifest,raw};
}
function checkManifest(value){
  if(!object(value,['schema','worklog_id','evidence','evidence_hash','baseline','contents']) || value.schema!==2 ||
    !object(value.baseline,['commit_base64','files']) || !Array.isArray(value.baseline.files) ||
    value.baseline.files.length>limits.BASE_FILES || !Array.isArray(value.contents) || value.contents.length>limits.CHANGE_FILES ||
    !value.evidence || !Array.isArray(value.evidence.files) || value.evidence.files.length>limits.CHANGE_FILES ||
    !hex(value.evidence_hash) || observer.digest(value.evidence)!==value.evidence_hash)fail();
  let total=0;const seen=new Set();
  for(const file of value.baseline.files){
    if(!object(file,['path','bytes','sha256','mode','git_oid']) || !['100644','100755'].includes(file.mode) ||
      !/^[a-f0-9]{40}$/.test(file.git_oid) || !Number.isSafeInteger(file.bytes) || file.bytes<0 ||
      file.bytes>limits.BASE_FILE_BYTES || !hex(file.sha256))fail();
    const key=codec.safePath(file.path);if(seen.has(key))fail();seen.add(key);total+=file.bytes;
  }
  if(total>limits.BASE_BYTES)fail('WORK_ARCHIVE_LIMIT');
  const expected=new Map(value.evidence.files.filter(f=>f.status!=='deleted').map(f=>[f.path,f]));seen.clear();total=0;
  if(value.contents.length!==expected.size)fail();
  for(const file of value.contents){
    if(!object(file,['path','bytes','sha256']) || !Number.isSafeInteger(file.bytes) || file.bytes<0 ||
      file.bytes>limits.CHANGE_FILE_BYTES || !hex(file.sha256))fail();
    const key=codec.safePath(file.path),entry=expected.get(file.path);
    if(seen.has(key) || !entry || entry.bytes!==file.bytes || entry.sha256!==file.sha256)fail();
    seen.add(key);total+=file.bytes;
  }
  if(total>limits.CHANGE_BYTES)fail('WORK_ARCHIVE_LIMIT');
  codec.checkLayout(value);return value;
}
async function encode(record){
  const {value,manifest,raw}=wire(record),prefix=Buffer.alloc(MAGIC.length+4);
  MAGIC.copy(prefix);prefix.writeUInt32BE(raw.length,MAGIC.length);
  async function* bodies(){
    yield prefix;yield raw;
    for(const file of [...value.baseline.files,...value.contents])yield Buffer.from(file.content_base64,'base64');
  }
  const input=Readable.from(bodies()),gzip=input.pipe(zlib.createGzip({level:6}));
  const chunks=[];let size=0;
  try{
    for await(const chunk of gzip){size+=chunk.length;if(size>limits.ARCHIVE_BYTES)fail('WORK_ARCHIVE_LIMIT');chunks.push(chunk);}
  }finally{input.destroy();gzip.destroy();}
  const data=Buffer.concat(chunks,size);
  return {data,manifest_hash:observer.digest(manifest),archive_sha256:codec.hash(data),archive_schema:2};
}
class Reader{
  constructor(stream){this.iterator=stream[Symbol.asyncIterator]();this.chunk=Buffer.alloc(0);this.position=0;this.total=0;}
  async read(size,{eof=false}={}){
    const result=Buffer.alloc(size);let written=0;
    while(written<size){
      if(this.position===this.chunk.length){
        const next=await this.iterator.next();
        if(next.done){if(eof && written===0)return null;fail();}
        this.chunk=next.value;this.position=0;this.total+=this.chunk.length;
        if(this.total>limits.MANIFEST_BYTES+limits.BASE_BYTES+limits.CHANGE_BYTES+10)fail('WORK_ARCHIVE_LIMIT');
      }
      const count=Math.min(size-written,this.chunk.length-this.position);
      this.chunk.copy(result,written,this.position,this.position+count);this.position+=count;written+=count;
    }
    return result;
  }
}
async function decode(data,{worklogId,evidenceHash,manifestHash}={}){
  if(data.length>limits.ARCHIVE_BYTES)fail('WORK_ARCHIVE_LIMIT');
  const input=Readable.from([data]),stream=input.pipe(zlib.createGunzip()),reader=new Reader(stream);
  try{
    const magic=await reader.read(MAGIC.length);
    if(!magic.equals(MAGIC))return codec.decode(data,{worklogId,evidenceHash,manifestHash});
    const length=(await reader.read(4)).readUInt32BE();
    if(length>limits.MANIFEST_BYTES || length===0)fail('WORK_ARCHIVE_LIMIT');
    const raw=await reader.read(length),text=new TextDecoder('utf-8',{fatal:true}).decode(raw);
    const manifest=checkManifest(JSON.parse(text));
    // Exact canonical encoding also rejects duplicate keys and noncanonical numbers.
    if(observer.canonical(manifest)!==text || worklogId && manifest.worklog_id!==worklogId ||
      evidenceHash && manifest.evidence_hash!==evidenceHash || manifestHash && observer.digest(manifest)!==manifestHash)fail();
    const baseline=[],contents=[];
    for(const [files,destination,base] of [[manifest.baseline.files,baseline,true],[manifest.contents,contents,false]]){
      for(const file of files){
        const body=await reader.read(file.bytes);
        if(codec.hash(body)!==file.sha256)fail('WORK_ARCHIVE_HASH_MISMATCH');
        destination.push(base?{path:file.path,mode:file.mode,git_oid:file.git_oid,content_base64:body.toString('base64')}:
          {path:file.path,content_base64:body.toString('base64')});
      }
    }
    if(await reader.read(1,{eof:true})!==null)fail();
    return codec.validate({...manifest,baseline:{commit_base64:manifest.baseline.commit_base64,files:baseline},contents},
      {worklogId,evidenceHash});
  }catch(error){if(error instanceof HubError)throw error;fail();}
  finally{input.destroy();stream.destroy();}
}
module.exports={encode,decode,checkManifest,MAGIC};
