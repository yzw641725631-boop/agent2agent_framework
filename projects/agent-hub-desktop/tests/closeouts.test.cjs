'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),{execFile}=require('node:child_process'),{promisify}=require('node:util');
const host=require('../core/host-install.cjs'),observer=require('../core/git-observer.cjs'),codec=require('../core/backup-codec.cjs');
const {CloseoutRuntime}=require('../core/closeout-runtime.cjs'),{WorkRuntime}=require('../core/work-runtime.cjs'),{identity}=require('../core/remote.cjs');
const execute=promisify(execFile),id=()=>crypto.randomUUID();
async function temp(t){const base=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(base,'hub-closeout-'));
  t.after(async()=>{assert.equal(path.dirname(await fs.realpath(dir)),base);assert.match(path.basename(dir),/^hub-closeout-/);await fs.rm(dir,{recursive:true,force:true});});return dir;}
const entry={type:'stdio',command:'agent-hub',args:['project-bridge'],env:{}};
test('uninstall removes owned scripts/tests and one MCP entry, retains edited/unknown files and other project',async t=>{
  const workspace=await temp(t),receipts=await temp(t),name='agent-hub-project-one';
  await fs.writeFile(path.join(workspace,'.mcp.json'),'{// personal setting\n"theme":"dark","mcpServers":{"other":{"command":"other"}}}\n');
  await host.install({workspace,hosts:['claude','codex','cursor'],receiptDirectory:receipts,name,entry});
  await host.deployFramework({workspace,receiptDirectory:receipts,name,files:[{path:'scripts/stop.ps1',bytes:Buffer.from('owned script')},
    {path:'tests/check.py',bytes:Buffer.from('owned test')},{path:'rules.md',bytes:Buffer.from('owned rules')}]});
  const root=path.join(workspace,'.agent-hub/framework',name);await fs.writeFile(path.join(root,'rules.md'),'user changed rules');
  await fs.writeFile(path.join(root,'personal.txt'),'unregistered');
  const result=await host.uninstallProject({workspace,receiptDirectories:[receipts],name});
  assert.equal(result.removed_files,2);assert.equal(result.removed_entries,3);assert.equal(result.state,'conflicts');
  await assert.rejects(fs.access(path.join(root,'scripts/stop.ps1')),{code:'ENOENT'});
  assert.equal(await fs.readFile(path.join(root,'rules.md'),'utf8'),'user changed rules');
  assert.equal(await fs.readFile(path.join(root,'personal.txt'),'utf8'),'unregistered');
  const config=await fs.readFile(path.join(workspace,'.mcp.json'),'utf8');assert.match(config,/personal setting/);assert.match(config,/other/);assert.doesNotMatch(config,/agent-hub-project-one/);
  const again=await host.uninstallProject({workspace,receiptDirectories:[receipts],name});assert.equal(again.removed_files,0);
});
test('changed MCP entry and tampered receipt paths cannot delete external or business files',async t=>{
  const workspace=await temp(t),receipts=await temp(t),outside=await temp(t),name='agent-hub-project-two';
  await host.install({workspace,hosts:['claude'],receiptDirectory:receipts,name,entry});
  const config=path.join(workspace,'.mcp.json');await fs.writeFile(config,JSON.stringify({mcpServers:{[name]:{command:'my custom command'}}}));
  const victim=path.join(outside,'keep.txt');await fs.writeFile(victim,'keep');
  await fs.writeFile(path.join(receipts,'framework.json'),JSON.stringify({version:1,workspace,name,files:{'../../../keep.txt':observer.digest('keep')}}));
  const result=await host.uninstallProject({workspace,receiptDirectories:[receipts],name});assert.equal(result.removed_files,0);assert.ok(result.conflicts.length>=2);
  assert.match(await fs.readFile(config,'utf8'),/my custom command/);assert.equal(await fs.readFile(victim,'utf8'),'keep');
});
test('projects sharing a workspace keep independent framework ownership and MCP entries',async t=>{
  const workspace=await temp(t),first=await temp(t),second=await temp(t);
  const one='agent-hub-project-one',two='agent-hub-project-two';
  for(const [name,receiptDirectory] of [[one,first],[two,second]]){
    await host.install({workspace,hosts:['claude'],receiptDirectory,name,entry});
    await host.deployFramework({workspace,receiptDirectory,name,files:[{path:'scripts/agent.cjs',bytes:Buffer.from('same framework script')}]});
  }
  const result=await host.uninstallProject({workspace,receiptDirectories:[first],name:one});
  assert.equal(result.state,'complete');assert.equal(result.removed_files,1);assert.equal(result.removed_entries,1);
  const keep=path.join(workspace,'.agent-hub/framework',two,'scripts/agent.cjs');
  assert.equal(await fs.readFile(keep,'utf8'),'same framework script');
  assert.ok(JSON.parse(await fs.readFile(path.join(workspace,'.mcp.json'),'utf8')).mcpServers[two]);
  const wrongReceipt=await host.uninstallProject({workspace,receiptDirectories:[second],name:one});
  assert.equal(wrongReceipt.state,'conflicts');assert.equal(await fs.readFile(keep,'utf8'),'same framework script');
});

test('junction in the managed directory is rejected without following it',async t=>{
  const workspace=await temp(t),receipts=await temp(t),outside=await temp(t),name='agent-hub-project-three';
  await host.deployFramework({workspace,receiptDirectory:receipts,name,files:[{path:'scripts/test.cjs',bytes:Buffer.from('owned')}]});
  const scripts=path.join(workspace,'.agent-hub/framework',name,'scripts');await fs.unlink(path.join(scripts,'test.cjs'));await fs.rmdir(scripts);
  await fs.writeFile(path.join(outside,'test.cjs'),'owned');await fs.symlink(outside,scripts,'junction');
  const result=await host.uninstallProject({workspace,receiptDirectories:[receipts],name});assert.equal(result.state,'conflicts');
  assert.equal(await fs.readFile(path.join(outside,'test.cjs'),'utf8'),'owned');
});
function protector(){const key=crypto.randomBytes(32);return {
  async encryptStringAsync(value){const iv=crypto.randomBytes(12),c=crypto.createCipheriv('aes-256-gcm',key,iv);const bytes=Buffer.concat([c.update(value,'utf8'),c.final()]);return Buffer.concat([iv,c.getAuthTag(),bytes]);},
  async decryptStringAsync(bytes){const c=crypto.createDecipheriv('aes-256-gcm',key,bytes.subarray(0,12));c.setAuthTag(bytes.subarray(12,28));return {result:Buffer.concat([c.update(bytes.subarray(28)),c.final()]).toString('utf8')};}};}
async function fixture(t){
  const workspace=await temp(t),directory=await temp(t),git=async(...args)=>execute('git',['-C',workspace,...args],{windowsHide:true});
  await git('init','-q');await fs.mkdir(path.join(workspace,'src'));await fs.writeFile(path.join(workspace,'src/main.ts'),'baseline\n');
  await git('add','.');await git('-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','base');
  const baseline=await observer.initialize(workspace),run=id(),project=id(),binding={id:id(),projectId:project,repositoryId:id(),workspace,status:'active'};
  const profile={origin:'https://hub.example',identity:identity(),user:{id:id()},deviceId:id(),bindings:[binding],workRuns:{}};
  const owner={user_id:profile.user.id,device_id:profile.deviceId,origin:profile.origin};
  profile.workRuns[run]={owner,run_id:run,task_id:id(),lease_epoch:1,binding_id:binding.id,project_id:project,context_id:id(),
    base_commit:baseline.base_commit,scope:{paths:['src']},policy:{protected_paths:[]},acknowledged:true,last_activity:Date.now()};
  const c={directory,profile,connection:'reauth_required',vault:{protector:protector()},projectOperations:new Set(),
    persist:async()=>{},emitState(){},emit(){},cancelWatch(){},descriptorPath:b=>path.join(directory,'bindings',b.id+'.json'),
    configuration:b=>({mcpServers:{['agent-hub-'+b.id.slice(0,8)]:entry}})};
  const name=Object.keys(c.configuration(binding).mcpServers)[0],receiptDirectory=path.join(directory,'setup-projects',binding.id);
  await host.install({workspace,receiptDirectory,hosts:['claude'],name,entry});
  await host.deployFramework({workspace,receiptDirectory: path.join(directory,'framework-deployments',binding.id),name,files:[{path:'scripts/agent.cjs',bytes:Buffer.from('framework script')},{path:'tests/agent.test.cjs',bytes:Buffer.from('framework test')}]});
  binding.secret='binding-secret';await fs.mkdir(path.join(directory,'bindings'));await fs.writeFile(c.descriptorPath(binding),JSON.stringify({binding:binding.id,secret:binding.secret}));
  await fs.writeFile(path.join(workspace,'src/main.ts'),'unsubmitted code\n');
  const job={id:id(),project_id:project,user_id:owner.user_id,device_id:owner.device_id,scope:{repository_ids:null},reason:'member_revoked',
    created_at:new Date().toISOString(),expires_at:new Date(Date.now()+86400000).toISOString(),state:'pending',upload_token:'co1_'+'a'.repeat(64)};
  const logs=new Map(),archives=new Map();let fault=null,hook=null,sealed=false,reported=false;
  const response=body=>Response.json(body);
  c.transport=async(url,options)=>{
    const target=new URL(url),body=options.headers['Content-Type']==='application/json'?JSON.parse(options.body):null;
    if(target.pathname==='/api/setup/status')return response({active_origin:profile.origin});
    if(target.pathname==='/api/closeouts/discover')return response({items:fault==='no_job'?[]:[job]});
    assert.equal(options.headers.Authorization,'DPoP '+job.upload_token,'normal credentials must never be used');
    if(target.pathname.endsWith('/checkpoints')){
      const existing=logs.get(body.operation_id),log=existing || {id:id(),summary:body.summary,evidence_hash:observer.digest(body.change_evidence),receipt_hash:'b'.repeat(64),closeout_id:job.id};
      logs.set(body.operation_id,log);return response(log);
    }
    if(target.pathname.endsWith('/snapshot')){
      if(fault==='offline')throw new Error('offline');
      const data=codec.decode(options.body);archives.set(data.worklog_id,data);await hook?.();
      return response({id:id(),worklog_id:data.worklog_id,evidence_hash:data.evidence_hash,manifest_hash:fault==='bad_receipt'?'0'.repeat(64):observer.digest(data),
        receipt_hash:'c'.repeat(64),server_code_backup:true,working_tree_restorable:true});
    }
    if(target.pathname.endsWith('/seal')){assert.equal(body.worklog_ids.length,logs.size);for(const log of logs.values())assert.ok(archives.has(log.id));
      sealed=true;return response({id:id(),job_id:job.id,receipt_hash:'d'.repeat(64),server_code_backup:true,checkpoints:logs.size});}
    if(target.pathname.endsWith('/cleanup-report')){assert.ok(sealed);reported=true;return response({state:body.result,cleanup_verification:'client_report_only'});}
    throw new Error('unexpected request '+target.pathname);
  };
  c.work=new WorkRuntime(c);c.closeouts=new CloseoutRuntime(c);t.after(()=>c.closeouts.drain());
  return {c,workspace,directory,binding,job,logs,archives,config:path.join(workspace,'.mcp.json'),
    name,setFault(value){fault=value;},setHook(value){hook=value;},reported:()=>reported,sealed:()=>sealed};
}
test('real Git final snapshot is uploaded before deterministic cleanup; business code and encrypted checkpoint remain',async t=>{
  const f=await fixture(t);await f.c.closeouts.tick();const state=f.c.closeouts.status(f.binding);
  assert.equal(state.items[0].state,'complete');assert.ok(f.reported());assert.equal(f.binding.status,'uninstalled');
  assert.equal(await fs.readFile(path.join(f.workspace,'src/main.ts'),'utf8'),'unsubmitted code\n');
  assert.ok(!(await fs.readFile(f.config,'utf8')).includes('agent-hub-'));
  await assert.rejects(fs.access(path.join(f.workspace,'.agent-hub/framework',f.name,'scripts/agent.cjs')),{code:'ENOENT'});
  assert.equal((await f.c.work.store.list(f.c.work.owner())).length,1);
  assert.ok(!JSON.stringify(state).includes('co1_'));const count=f.logs.size;await f.c.closeouts.tick();assert.equal(f.logs.size,count);assert.equal(f.binding.status,'uninstalled');
});
test('older pending work uploads before the final observed snapshot for default handoff selection',async t=>{
  const f=await fixture(t),r=Object.values(f.c.profile.workRuns)[0],operation=id();
  const snapshot=await observer.capture(f.workspace,r.base_commit,r.scope,r.policy,
    {ignoreUntracked:await host.ownedProjectPaths(f.c.closeouts.options(f.binding))});
  await f.c.work.store.write({schema:1,operation_id:operation,owner:r.owner,binding_id:f.binding.id,
    project_id:f.binding.projectId,repository_id:f.binding.repositoryId,task_id:r.task_id,run_id:r.run_id,
    lease_epoch:r.lease_epoch,workspace:f.workspace,created_at:new Date(Date.now()-1000).toISOString(),
    state:'pending',backup_state:'pending',snapshot,body:{context_id:r.context_id,summary:'earlier manual work',
      notes:'before last edit',tests:'not run',next_steps:'handoff',outcome:'checkpoint'}},{create:true});
  await fs.writeFile(path.join(f.workspace,'src/main.ts'),'final edit after manual log');
  await f.c.closeouts.tick();assert.ok(f.reported());
  assert.deepEqual([...f.logs.values()].map(log=>log.summary),['earlier manual work','撤权自动收尾检查点']);
  const latest=[...f.logs.values()].at(-1),archive=f.archives.get(latest.id);
  const actual=codec.workingFiles(archive).find(file=>file.path==='src/main.ts');
  assert.equal(actual.data.toString('utf8'),'final edit after manual log');
});

test('offline or mismatched archive receipt retains framework and retries the same saved checkpoint',async t=>{
  for(const fault of ['offline','bad_receipt']){
    const f=await fixture(t);f.setFault(fault);await f.c.closeouts.tick();
    assert.equal(f.c.closeouts.status(f.binding).items[0].state,'blocked');assert.ok(!f.sealed());assert.match(await fs.readFile(f.config,'utf8'),/agent-hub-/);
    assert.equal(f.binding.status,'blocked');f.setFault(null);await f.c.closeouts.tick();assert.ok(f.reported());assert.equal(f.logs.size,1);
  }
});
test('changes during upload block cleanup and a later attempt uploads a new final snapshot',async t=>{
  const f=await fixture(t);f.setHook(async()=>{await fs.writeFile(path.join(f.workspace,'src/main.ts'),'edited while uploading');});
  await f.c.closeouts.tick();assert.equal(f.c.closeouts.status(f.binding).items[0].error,'WORKTREE_CHANGED');assert.ok(!f.reported());
  assert.match(await fs.readFile(f.config,'utf8'),/agent-hub-/);f.setHook(null);await f.c.closeouts.tick();
  assert.equal(f.logs.size,2);assert.ok(f.reported());assert.equal(await fs.readFile(path.join(f.workspace,'src/main.ts'),'utf8'),'edited while uploading');
});
test('no signed revocation job, corrupt local checkpoint, or account switch never authorize cleanup',async t=>{
  const f=await fixture(t);f.setFault('no_job');await f.c.closeouts.tick();assert.equal(f.binding.status,'active');
  f.setFault(null);await fs.mkdir(f.c.work.store.directory);await fs.writeFile(f.c.work.store.file(id()),'corrupt');
  await f.c.closeouts.tick();assert.equal(f.c.closeouts.status(f.binding).items[0].error,'WORK_STORE_UNAVAILABLE');assert.ok(!f.sealed());
  assert.match(await fs.readFile(f.config,'utf8'),/agent-hub-/);
  const switched=await fixture(t);switched.setHook(async()=>{switched.c.profile={...switched.c.profile,user:{id:id()}};});
  await assert.rejects(switched.c.closeouts.tick(),{code:'LOGIN_REQUIRED'});assert.match(await fs.readFile(switched.config,'utf8'),/agent-hub-/);
});
test('another project and a later reauthorized binding retain their connection and files',async t=>{
  const f=await fixture(t),other={id:id(),projectId:id(),workspace:await temp(t),status:'active'};
  const later={id:id(),projectId:f.job.project_id,workspace:await temp(t),status:'active',authorization:{grant_id:id()}};
  f.c.profile.bindings.push(other,later);await fs.writeFile(path.join(other.workspace,'keep.txt'),'another project');
  await f.c.closeouts.tick();assert.equal(other.status,'active');assert.equal(later.status,'active');assert.equal(await fs.readFile(path.join(other.workspace,'keep.txt'),'utf8'),'another project');
});
test('revoked MCP advertises only closeout tools and preserves mandatory stop instructions',async()=>{
  const {dispatch}=require('../core/mcp.cjs');
  const list=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>({capabilities:[],closeout_required:true}));
  assert.deepEqual(list.result.tools.map(tool=>tool.name),['hub_closeout_status','hub_run_closeout']);
  const result=await dispatch({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'hub_get_project',arguments:{}}},async()=>{
    throw Object.assign(new Error('stop'),{code:'PROJECT_CLOSEOUT_REQUIRED',details:{normal_operations_allowed:false,required_action:'upload_work_then_uninstall'}});
  });
  const details=JSON.parse(result.result.content[0].text);assert.equal(details.normal_operations_allowed,false);assert.equal(details.required_action,'upload_work_then_uninstall');
});
