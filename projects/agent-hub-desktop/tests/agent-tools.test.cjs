'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const tools=require('../core/agent-tools.cjs'),{AgentRuntime}=require('../core/agent-runtime.cjs');
const caps=['tasks:claim','tasks:write'],projectId=crypto.randomUUID(),repo=crypto.randomUUID(),other=crypto.randomUUID();
test('agent tools separate recipient and publisher rights and pin repository and task filters',()=>{
  assert.ok(!tools.tools(['tasks:claim']).some(t=>t.name==='hub_register_agent'));
  assert.ok(!tools.tools(caps).some(t=>t.name==='hub_dispatch_task'));
  const taskId=crypto.randomUUID();
  const request=tools.request('hub_list_agent_dispatches',{},[],{projectId,repositoryId:repo,taskId});
  assert.ok(request.path.includes('repository_id='+repo) && request.path.includes('task_id='+taskId));
  assert.throws(()=>tools.request('hub_list_agents',{repository_id:other},[],{projectId,repositoryId:repo}),{code:'AGENT_REPOSITORY_MISMATCH'});
  assert.throws(()=>tools.validate('hub_decide_agent_dispatch',{task_id:taskId,dispatch_id:crypto.randomUUID(),expected_version:1,
    action:'decline',operation_id:crypto.randomUUID()}),{code:'INVALID_REQUEST'});
});
test('Windows hook preview executes the real bridge and strips private host payloads',async t=>{
  if(process.platform!=='win32'){t.skip('Windows PowerShell hook');return;}
  const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
  const {spawn}=require('node:child_process'),{once}=require('node:events');
  const {Broker}=require('../core/broker.cjs'),{turnHooks}=require('../core/session-hook.cjs');
  const parent=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(parent,'agent-hub-hook-'));
  const requests=[],binding=crypto.randomUUID(),secret=crypto.randomBytes(32).toString('hex');
  const broker=new Broker(dir,async r=>{assert.equal(r.binding,binding);assert.equal(r.secret,secret);requests.push(r);
    return {decision:'block',reason:'请记录本轮实际改动。'};});
  await broker.start();
  t.after(async()=>{await broker.close();assert.ok(path.relative(parent,dir).startsWith('agent-hub-hook-'));await fs.rm(dir,{recursive:true,force:true});});
  const file=path.join(dir,'binding.json');await fs.writeFile(file,JSON.stringify({version:1,binding,secret,endpoint:broker.endpoint}));
  const configuration=turnHooks({command:process.execPath,args:[path.resolve(__dirname,'../core/bridge.cjs'),'--binding',file]},'codex');
  const command=configuration.hooks.Stop[0].hooks[0].command,encoded=command.split(' -EncodedCommand ')[1];
  const child=spawn('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',encoded],{windowsHide:true,stdio:['pipe','pipe','pipe']});
  let output='',errors='';child.stdout.on('data',x=>output+=x);child.stderr.on('data',x=>errors+=x);
  const finished=once(child,'close');child.stdin.end(JSON.stringify({hook_event_name:'Stop',session_id:'host-session',cwd:dir,
    turn_id:'turn-one',stop_hook_active:false,prompt:'private-prompt',transcript_path:'C:/must-not-read',last_assistant_message:'private-answer'}));
  const [code]=await finished;assert.equal(code,0,errors);assert.equal(JSON.parse(output).decision,'block');
  assert.equal(requests.length,1);assert.equal(requests[0].action,'turn_hook');
  assert.deepEqual(Object.keys(requests[0].args).sort(),['cwd','event','harness','session_id','stop_hook_active','turn_ref']);
  assert.equal(JSON.stringify(requests[0]).includes('private'),false);
});
test('passive inbox monitoring never refreshes agent presence or accepts tasks and deduplicates notifications',async()=>{
  const id=crypto.randomUUID(),binding={id:crypto.randomUUID(),projectId,status:'active'},offer={id:crypto.randomUUID(),state:'queued',expired:false};
  const saved={binding_id:binding.id,endpoint:{id,name:'Web',enabled:true,last_seen_at:'old'}};
  const profile={bindings:[binding],agentEndpoints:{[id]:saved}},calls=[],notifications=[];
  const c={profile,remote:{},connection:'active',persist:async()=>{},emitState(){},emit:(type,data)=>notifications.push([type,data]),
    ensureCurrent(p,r){assert.equal(p,this.profile);assert.equal(r,this.remote);},async invokeProjectRemote(b,action,args){calls.push(action);return {items:action==='hub_list_agent_dispatches'?[offer]:[]};}};
  const runtime=new AgentRuntime(c);await runtime.tick();await runtime.tick();
  assert.equal(saved.endpoint.last_seen_at,'old');assert.equal(saved.pending_count,1);assert.equal(notifications.length,1);
  assert.deepEqual(calls,['hub_list_agent_dispatches','hub_list_guides','hub_list_guides','hub_list_agent_dispatches','hub_list_guides','hub_list_guides']);
  c.connection='signed_out';await runtime.tick();assert.equal(calls.length,6);
});

test('guide reminders follow the original instance and deduplicate the same pending version without claiming',async()=>{
  const instance=crypto.randomUUID(),binding={id:crypto.randomUUID(),projectId,status:'active'},notifications=[],calls=[];
  const saved={binding_id:binding.id,endpoint:{id:crypto.randomUUID(),instance_id:instance,name:'Web',enabled:true}};
  const profile={bindings:[binding],agentEndpoints:{[saved.endpoint.id]:saved}};
  const guide={id:crypto.randomUUID(),state:'queued',version:1};
  const c={profile,remote:{},connection:'active',persist:async()=>{},emitState(){},emit:(type,value)=>notifications.push(value),
    ensureCurrent(p,r){assert.equal(p,this.profile);assert.equal(r,this.remote);},async invokeProjectRemote(b,action,args){
      calls.push(action);if(action==='hub_list_agent_dispatches')return {items:[]};
      assert.equal(args.instance_id,instance);return {items:args.state===guide.state?[guide]:[]};
    }};
  const runtime=new AgentRuntime(c);await runtime.tick();await runtime.tick();assert.equal(notifications.length,1);
  guide.version++;await runtime.tick();assert.equal(notifications.length,2);assert.equal(saved.pending_guides,1);
  assert.ok(calls.every(a=>['hub_list_agent_dispatches','hub_list_guides'].includes(a)));
});
