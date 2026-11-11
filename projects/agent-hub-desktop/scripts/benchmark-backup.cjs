'use strict';
// Explicit operator benchmark. Creates synthetic repositories in a new directory only.
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const codec=require('../core/backup-codec.cjs'),observer=require('../core/git-observer.cjs');
const {WorkStore}=require('../core/work-store.cjs'),limits=require('../core/backup-limits.cjs');
const execute=promisify(execFile),git=(dir,...args)=>execute('git',['-C',dir,'-c','gc.auto=0','-c','maintenance.auto=false',...args],{windowsHide:true,timeout:180000,maxBuffer:2*1024*1024});
async function run({directory,protector,full=true,protectorName='operator-provided',resume=false}){
  if(!path.isAbsolute(directory) || !protector)throw new Error('An absolute new directory and explicit protector are required.');
  const start=performance.now(),source=path.join(directory,'source');
  const count=full?limits.BASE_FILES:1100,budget=full?limits.BASE_BYTES:20*1024*1024,first=full?limits.BASE_FILE_BYTES:5*1024*1024;
  if(!resume){
    await fs.mkdir(directory,{recursive:false});await fs.mkdir(source);await fs.mkdir(path.join(source,'src'));
    await git(source,'init','-q');await git(source,'config','core.autocrlf','false');
    let remaining=budget-first;
    for(let i=0;i<count;i++){
      const size=i===0?first:Math.floor(remaining/(count-i));if(i)remaining-=size;
      await fs.writeFile(path.join(source,'src',`baseline-${i}.bin`),crypto.randomBytes(size));
    }
    await git(source,'add','.');await git(source,'-c','user.name=Benchmark','-c','user.email=benchmark@example.org','commit','-qm','synthetic baseline');
  }
  const initialized=await observer.initialize(source),baseline=await codec.baseline(source,initialized.base_commit);
  if(baseline.files.length!==count || baseline.files.reduce((sum,f)=>sum+Buffer.byteLength(f.content_base64,'base64'),0)!==budget)
    throw new Error('The synthetic baseline does not match the requested capacity');
  for(let i=1;i<=4;i++)await fs.writeFile(path.join(source,'src',`baseline-${i}.bin`),crypto.randomBytes(limits.CHANGE_FILE_BYTES));
  await fs.unlink(path.join(source,'src','baseline-5.bin'));
  const captured=await observer.capture(source,initialized.base_commit,{paths:['src']},{protected_paths:[]});captured.baseline=baseline;
  const record={schema:1,operation_id:crypto.randomUUID(),task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),state:'shared',
    owner:{user_id:crypto.randomUUID(),device_id:crypto.randomUUID(),origin:'https://benchmark.example.org'},
    created_at:new Date().toISOString(),body:{summary:'Synthetic capacity handoff'},server_receipt:{id:crypto.randomUUID()},snapshot:captured};
  const encoded=await codec.encodeArchive(record);await fs.writeFile(path.join(directory,'archive.gz'),encoded.data,{flag:'wx'});
  const log={id:record.server_receipt.id,change_evidence:captured.evidence,evidence_hash:captured.evidence_hash};
  await fs.writeFile(path.join(directory,'log.json'),JSON.stringify(log),{flag:'wx'});
  const store=new WorkStore(directory,protector);await store.write(record,{create:true});
  const recovered=await codec.decodeArchive(encoded.data,{worklogId:log.id,evidenceHash:log.evidence_hash,manifestHash:encoded.manifest_hash});
  const saved=await store.read(record.operation_id);
  if(observer.digest(saved.snapshot.baseline)!==observer.digest(recovered.baseline))throw new Error('Encrypted baseline mismatch');
  const exported=await store.export(saved,path.join(directory,'handoff'));
  const expected={};for(const file of codec.workingFiles(recovered)){
    const actual=await fs.readFile(path.join(exported.working_tree_directory,...file.path.split('/')));
    if(!actual.equals(file.data))throw new Error('Export mismatch');expected[file.path]=codec.hash(actual);
  }
  await fs.writeFile(path.join(directory,'expected.json'),JSON.stringify(expected),{flag:'wx'});
  const report={schema:1,source_class:'synthetic-real-git',passed:true,archive_schema:encoded.archive_schema,
    baseline_files:baseline.files.length,baseline_bytes:budget,largest_baseline_file:first,changed_bytes:captured.bytes,
    changed_entries:captured.evidence.files.length,working_files:Object.keys(expected).length,compressed_bytes:encoded.data.length,
    encrypted_record_bytes:(await fs.stat(store.file(record.operation_id))).size,protector:protectorName,
    archive_sha256:encoded.archive_sha256,manifest_hash:encoded.manifest_hash,roundtrip_and_export_verified:true,
    elapsed_seconds:Math.round((performance.now()-start)/10)/100,peak_rss_bytes:process.resourceUsage().maxRSS*1024};
  await fs.writeFile(path.join(directory,'report.json'),JSON.stringify(report,null,2),{flag:'wx'});return report;
}
module.exports={run};
