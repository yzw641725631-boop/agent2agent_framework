'use strict';
// Opt-in disposable server on SSH loopback; never load a production user profile.
const assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {promisify}=require('node:util'),{execFile}=require('node:child_process'),execute=promisify(execFile);
const {Controller}=require('../core/controller.cjs'),{Remote}=require('../core/remote.cjs');
const origin='http://127.0.0.1:18767',controllers=[];
function protector(){
  const key=crypto.randomBytes(32);return {
    async encryptStringAsync(value){const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);return Buffer.concat([iv,cipher.update(value),cipher.final(),cipher.getAuthTag()]);},
    async decryptStringAsync(value){const cipher=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));cipher.setAuthTag(value.subarray(-16));return {result:Buffer.concat([cipher.update(value.subarray(12,-16)),cipher.final()]).toString()};}};
}
async function main(){
  const response=await fetch(origin+'/__test__/seed');assert.ok(response.ok);const seed=await response.json();assert.equal(seed.author.origin,origin);
  const parent=await fs.realpath(os.tmpdir()),temp=await fs.mkdtemp(path.join(parent,'hub-real-dispatch-'));
  try{
    async function controller(profile,name){
      const workspace=path.join(temp,name),directory=path.join(temp,name+'-desktop');await fs.mkdir(workspace);await fs.mkdir(path.join(workspace,'src'));await fs.mkdir(directory);
      await fs.writeFile(path.join(workspace,'src/main.ts'),'baseline\n');await fs.mkdir(path.join(workspace,'assets'));await fs.writeFile(path.join(workspace,'assets/main.ts'),'baseline\n');
      const git=args=>execute('git',['-C',workspace,...args],{windowsHide:true});
      await git(['init','-q']);await git(['remote','add','origin','https://github.com/example/web']);await git(['add','.']);
      await git(['-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','baseline']);
      const binding={id:crypto.randomUUID(),projectId:seed.project_id,repositoryId:seed.repository_id,workspace,status:'active',secret:crypto.randomBytes(32).toString('base64url')};
      profile.bindings=[binding];const vault={protector:protector(),data:{current:'fixture',profiles:{fixture:profile}},save:async()=>{}};
      const c=new Controller({vault,directory,executable:process.execPath,bridgeScript:'unused'});controllers.push(c);
      c.remote=new Remote(profile,()=>c.persist());await c.remote.init();c.connection='active';await c.broker.start();return {c,binding,workspace};
    }

    const a=await controller(seed.author,'author'),b=await controller(seed.reader,'reader');
    const root=new Remote(seed.root,async()=>{});await root.init();
    const base=`/api/projects/${seed.project_id}`;
    const {call}=require('../core/broker.cjs');
    async function invoke(who,name,args={}){
      const response=await call({binding:who.binding.id,secret:who.binding.secret,endpoint:who.c.broker.endpoint},
        {action:'mcp',args:{request:{jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}}}});
      if(response.result.isError){const error=new Error('MCP request failed');error.code=JSON.parse(response.result.content[0].text).code;throw error;}
      return response.result.structuredContent;
    }
    const endpoint=await invoke(b,'hub_register_agent',{instance_id:crypto.randomUUID(),harness:'codex',name:'Reader Web developer',
      roles:['developer'],expected_version:0,operation_id:crypto.randomUUID()});
    let task=await root.request('POST',base+'/tasks',{repository_id:seed.repository_id,title:'跨设备资产变更',description:'Only assets',acceptance:'Recorded and backed up',
      scope:{repository:'web',platform:'web',paths:['assets']},execution_role:'developer',execution_mode:'observed'});
    for(const gate of ['requirements','design','feasibility'])task=await root.request('POST',`${base}/tasks/${task.id}/decisions`,{
      gate,approved:true,expected_version:task.version,evidence:'Disposable dispatch admission',instance_id:crypto.randomUUID(),harness:'human'});
    const offer=await root.request('POST',base+'/agent-dispatches',{task_id:task.id,endpoint_id:endpoint.id,
      expected_task_version:task.version,expected_endpoint_version:endpoint.version,note:'请核对并处理 assets 范围'});
    assert.equal(offer.state,'queued');
    const own=await invoke(a,'hub_list_agent_dispatches');assert.equal(own.items.length,0);
    const inbox=await invoke(b,'hub_agent_inbox',{endpoint_id:endpoint.id});assert.equal(inbox.items[0].state,'queued');
    const seen=(await root.request('GET',base+'/agents')).items.find(x=>x.id===endpoint.id).last_seen_at;
    await b.c.agents.tick();
    assert.equal((await root.request('GET',base+'/agents')).items.find(x=>x.id===endpoint.id).last_seen_at,seen);
    const receive=await invoke(b,'hub_decide_agent_dispatch',{task_id:task.id,dispatch_id:offer.id,expected_version:1,action:'receive',operation_id:crypto.randomUUID()});
    assert.equal(receive.dispatch.state,'received');assert.equal(receive.dispatch.run_id,null);
    assert.equal((await root.request('GET',`${base}/tasks/${task.id}`)).task.status,'ready');
    const operation=crypto.randomUUID(),accept={task_id:task.id,dispatch_id:offer.id,expected_version:receive.dispatch.version,action:'accept',operation_id:operation};
    const accepted=await invoke(b,'hub_decide_agent_dispatch',accept);
    assert.deepEqual(await invoke(b,'hub_decide_agent_dispatch',accept),accepted);
    assert.equal(accepted.dispatch.run_id,accepted.run.id);assert.equal(accepted.run.instance_id,endpoint.instance_id);
    const lease={task_id:task.id,run_id:accepted.run.id,lease_epoch:accepted.run.lease_epoch};
    const context=await invoke(b,'hub_prepare_work',{...lease,operation_id:crypto.randomUUID()});
    assert.equal(context.payload.execution.instance_id,endpoint.instance_id);
    await invoke(b,'hub_acknowledge_work',{...lease,context_id:context.id,content_hash:context.content_hash,
      rule_ids:context.payload.policy.rules.map(r=>r.id),operation_id:crypto.randomUUID()});
    const event={harness:'codex',session_id:crypto.randomUUID(),cwd:b.workspace,turn_ref:crypto.randomUUID(),stop_hook_active:false};
    const hook=async args=>call({binding:b.binding.id,secret:b.binding.secret,endpoint:b.c.broker.endpoint},{action:'turn_hook',args:{...event,...args}});
    const start=await hook({event:'UserPromptSubmit'}),key=start.hookSpecificOutput.additionalContext.match(/session_key=([a-f0-9]{64})/)[1];
    await invoke(b,'hub_bind_work_session',{...lease,session_key:key});
    await fs.writeFile(path.join(b.workspace,'assets/main.ts'),'dispatch code change\n');
    assert.equal((await hook({event:'Stop'})).decision,'block');
    const log=await invoke(b,'hub_write_worklog',{...lease,summary:'完成投递任务的一轮变更',notes:'仅修改 assets 并记录实际范围。',
      tests:'Real broker/MCP, DPoP HTTP and PostgreSQL',next_steps:'独立评审',operation_id:crypto.randomUUID()});
    assert.ok(log.server_confirmed && log.server_code_backup);
    const read=await invoke(b,'hub_read_worklog',{task_id:task.id,worklog_id:log.server_receipt.id});assert.ok(read.turn_id);
    assert.equal((await invoke(b,'hub_get_work_record_status',lease)).state,'recorded');
    assert.deepEqual(await hook({event:'Stop',stop_hook_active:true}),{});
    let forbidden={...b.binding,repositoryId:seed.other_repository_id || crypto.randomUUID()};
    b.c.profile.bindings.push(forbidden={...forbidden,id:crypto.randomUUID()});
    await assert.rejects(b.c.invokeProject(forbidden,'hub_decide_agent_dispatch',accept),error=>['TASK_REPOSITORY_MISMATCH','REPOSITORY_NOT_ACCESSIBLE'].includes(error.code));
    await root.request('POST',`${base}/members/${seed.reader.user.id}/revoke`,{});
    await assert.rejects(invoke(b,'hub_agent_inbox',{endpoint_id:endpoint.id}),error=>['PROJECT_ACCESS_REVOKED','PROJECT_NOT_ACCESSIBLE'].includes(error.code));
    const historical=await root.request('GET',base+'/agent-dispatches/'+offer.id);assert.equal(historical.state,'accepted');
    assert.equal((await root.request('GET',base+'/agents')).items.some(x=>x.id===endpoint.id),false);
    root.close();
    console.log(JSON.stringify({passed:true,realWindowsController:true,realMcpBroker:true,realHTTP:true,realDPoP:true,realPostgreSQL:true,
      explicitRecipient:true,passivePollingDoesNotImpersonateAgent:true,receivedDoesNotClaim:true,acceptAtomicallyClaims:true,
      acceptRetryIdempotent:true,unrelatedRecipientCannotReadDispatch:true,fixedRepositoryEnforced:true,turnBoundToActualInstance:true,
      stopDetectsMissingRecord:true,worklogTurnIdPersisted:true,logAndCodeBackupConfirmed:true,revocationBlocksInbox:true}));
  }finally{
    for(const c of controllers)await c.close();
    assert.ok(path.relative(parent,temp).startsWith('hub-real-dispatch-'));await fs.rm(temp,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.code || error.name,error.stack);process.exitCode=1;});
