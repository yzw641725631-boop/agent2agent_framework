'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const hooks=require('../core/host-hooks.cjs'),install=require('../core/hook-install.cjs');
const {HookRuntime}=require('../core/hook-runtime.cjs'),{digest}=require('../core/git-observer.cjs');
async function fixture(t,harness='codex'){
  const parent=await fs.realpath(os.tmpdir()),root=await fs.mkdtemp(path.join(parent,'agent-hub-managed-hooks-'));
  t.after(async()=>{assert.equal(path.dirname(root),parent);assert.ok(path.basename(root).startsWith('agent-hub-managed-hooks-'));await fs.rm(root,{recursive:true,force:true});});
  const workspace=path.join(root,'repo');await fs.mkdir(workspace);
  const binding={id:crypto.randomUUID(),projectId:crypto.randomUUID(),workspace,status:'active'};
  const connection={command:process.execPath,args:[path.resolve(__dirname,'../core/bridge.cjs'),'--binding',path.join(root,'binding.json')]};
  const options={workspace,directory:path.join(root,'desktop','hook-installations',binding.id),harness,bindingId:binding.id,connection};
  const plan=await install.plan(options);await install.install(options,plan.preview_hash);
  return {root,workspace,binding,options,plan};
}
test('owned hook merge, upgrade and removal retain comments, foreign hooks and unrelated settings',async t=>{
  const f=await fixture(t),target=f.plan.target;
  const parsed=JSON.parse(await fs.readFile(target,'utf8'));
  parsed.hooks.Stop.push({hooks:[{type:'command',command:'user-existing-check'}]});parsed.editor={tabSize:8};
  await fs.writeFile(target,'// keep user comment\n'+JSON.stringify(parsed,null,2));
  const fresh=await install.plan(f.options);await install.install(f.options,fresh.preview_hash);
  assert.equal((await install.status(f.options)).configuration_matches,true);
  assert.ok((await fs.readFile(target,'utf8')).startsWith('// keep user comment'));
  const result=await install.uninstall({workspace:f.workspace,hookDirectory:f.options.directory,harness:'codex'});
  assert.equal(result.conflicts.length,0);assert.ok(result.removed_entries>=6);
  const remaining=JSON.parse((await fs.readFile(target,'utf8')).split('\n').slice(1).join('\n'));
  assert.equal(remaining.editor.tabSize,8);assert.equal(remaining.hooks.Stop[0].hooks[0].command,'user-existing-check');
  assert.equal((await install.status(f.options)).state,'not_installed');
  assert.equal((await install.uninstall({workspace:f.workspace,hookDirectory:f.options.directory})).removed_entries,0);
});
test('stale preview, edited managed command, duplicate JSON keys and links never get overwritten',async t=>{
  const f=await fixture(t),before=await fs.readFile(f.plan.target);
  await assert.rejects(install.install(f.options,'0'.repeat(64)),{code:'HOOK_PREVIEW_STALE'});
  const edited=JSON.parse(before);edited.hooks.Stop[0].hooks[0].command='user changed owned command';
  await fs.writeFile(f.plan.target,JSON.stringify(edited));
  assert.equal((await install.status(f.options)).state,'configuration_changed');
  assert.equal((await install.uninstall({workspace:f.workspace,hookDirectory:f.options.directory})).conflicts.length,1);
  assert.equal(JSON.parse(await fs.readFile(f.plan.target)).hooks.Stop[0].hooks[0].command,'user changed owned command');
  assert.throws(()=>install.merge(Buffer.from('{"hooks":{},"hooks":{}}'),{hooks:{}},null));
  const outside=path.join(f.root,'outside');await fs.mkdir(outside);
  const other=path.join(f.root,'linked');await fs.mkdir(other);
  await fs.symlink(outside,path.join(other,'.codex'),process.platform==='win32'?'junction':'dir');
  await assert.rejects(install.plan({...f.options,workspace:other,directory:path.join(f.root,'new-receipts')}),{code:'CONFIGURATION_CONFLICT'});
});
test('host adapters use the host event schema and remove prompt, contents and command text',()=>{
  const cwd=path.resolve('.'),id='b'.repeat(64);
  const value=hooks.normalize({session_id:'s',cwd,hook_event_name:'PreToolUse',prompt:'secret',transcript_path:'never-read',
    tool_name:'apply_patch',tool_input:{command:'*** Begin Patch\n*** Update File: src/a.js\n*** Move to: src/b.js\n@@\n-secret\n+new\n*** End Patch'}},'PreToolUse','codex',id);
  assert.deepEqual(value.operation.paths,['src/a.js','src/b.js']);assert.ok(!JSON.stringify(value).includes('secret'));
  const shell=hooks.operation({tool_name:'Bash',tool_input:{command:'git status --short; delete-secret'}});
  assert.equal(shell.safe_read,false);assert.ok(!JSON.stringify(shell).includes('delete-secret'));
  const cursor=hooks.normalize({conversation_id:'s',generation_id:'g',workspace_roots:[cwd],tool_name:'Write',tool_input:{file_path:'src/a.js',content:'secret'}},'preToolUse','cursor',id);
  assert.equal(cursor.event,'PreToolUse');assert.equal(cursor.session_id,'s');
  assert.deepEqual(hooks.operation({tool_name:'Delete',tool_input:{file_path:'src/old.js'}}).paths,['src/old.js']);
  const mcp=hooks.normalize({conversation_id:'s',workspace_roots:[cwd],mcp_server_name:'agent-hub-test',tool_name:'hub_get_project',tool_input:'private'},'beforeMCPExecution','cursor',id);
  assert.deepEqual(mcp.operation,{kind:'mcp',server:'agent-hub-test',name:'hub_get_project'});
  assert.deepEqual(hooks.output({hookSpecificOutput:{additionalContext:'context'}},'cursor','beforeSubmitPrompt'),{continue:true});
  assert.equal(hooks.output({hookSpecificOutput:{permissionDecision:'deny',permissionDecisionReason:'blocked'}},'cursor','preToolUse').permission,'deny');
  for(const host of ['codex','claude-code','cursor','qoder','codebuddy'])for(const platform of ['win32','darwin']){
    const config=hooks.configuration({command:'/app/Agent Hub',args:['bridge.cjs']},host,id,platform);
    assert.ok(Object.keys(config.hooks).length>=6);
  }
  assert.throws(()=>hooks.configuration({},'workbuddy',id),{code:'HOST_HOOKS_UNAVAILABLE'});
});

test('Cursor MCP uses the separate fail-closed server identity check and session start never replaces a prompt turn',async t=>{
  const f=await fixture(t,'cursor'),profile={},remote={};let starts=0;
  const c={directory:path.join(f.root,'desktop'),profile,remote,connection:'active',configuration:()=>({mcpServers:{'agent-hub-test':f.options.connection}}),
    persist:async()=>{},ensureCurrent:()=>{},updater:{check:async()=>({can_continue:true})},
    turns:{async hook(binding,input){
      await new Promise(resolve=>setImmediate(resolve));starts++;
      const key=digest({binding_id:binding.id,harness:'cursor',session_id:input.session_id});
      (profile.workSessions??={})[key]={turn_id:'prompt-turn'};
      return {hookSpecificOutput:{additionalContext:'prompt-turn'}};
    }}};
  const runtime=new HookRuntime(c),base={harness:'cursor',installation_id:f.plan.receipt.installation_id,session_id:'s',cwd:f.workspace};
  const [prompt,start]=await Promise.all([runtime.event(f.binding,{...base,event:'UserPromptSubmit'}),runtime.event(f.binding,{...base,event:'SessionStart'})]);
  assert.equal(starts,1);assert.ok(start.hookSpecificOutput.additionalContext.includes('prompt-turn'));assert.ok(prompt);
  const check=operation=>runtime.event(f.binding,{...base,event:'BeforeMCPExecution',operation});
  assert.deepEqual(await check({kind:'mcp',server:'agent-hub-test',name:'hub_get_project'}),{});
  for(const server of ['','other-server'])assert.equal((await check({kind:'mcp',server,name:'hub_get_project'})).hookSpecificOutput.permissionDecision,'deny');
  assert.equal(JSON.parse(await fs.readFile(f.plan.target,'utf8')).hooks.beforeMCPExecution[0].failClosed,true);
});
test('pre-tool guard requires bound active work, rejects escape and control files, and checks proposed paths',async t=>{
  const f=await fixture(t),profile={},remote={},checks=[];
  const c={directory:path.join(f.root,'desktop'),profile,remote,connection:'active',configuration:()=>({mcpServers:{'agent-hub-test':f.options.connection}}),
    persist:async()=>{},ensureCurrent(p,r){assert.equal(p,profile);assert.equal(r,remote);},
    work:{async check(b,args){checks.push(args);if(args.proposed_paths.includes('src/protected.js'))throw Object.assign(Error(),{code:'PROTECTED_PATH'});}},
    updater:{check:async()=>({can_continue:true})}};
  const runtime=new HookRuntime(c),event={harness:'codex',event:'PreToolUse',installation_id:f.plan.receipt.installation_id,session_id:'s',cwd:f.workspace};
  const invoke=operation=>runtime.event(f.binding,{...event,operation});
  assert.equal((await invoke({kind:'edit',paths:['src/a.js']})).hookSpecificOutput.permissionDecision,'deny');
  const run_id=crypto.randomUUID();profile.workSessions={[digest({binding_id:f.binding.id,harness:'codex',session_id:'s'})]:{run_id,phase:'active'}};
  profile.workRuns={[run_id]:{task_id:crypto.randomUUID(),run_id,lease_epoch:1}};
  assert.deepEqual(await invoke({kind:'edit',paths:['src/a.js']}),{});assert.deepEqual(checks[0].proposed_paths,['src/a.js']);
  for(const file of ['../outside.txt','.codex/hooks.json','src/protected.js'])assert.equal((await invoke({kind:'edit',paths:[file]})).hookSpecificOutput.permissionDecision,'deny');
  assert.equal((await invoke({kind:'shell',safe_read:false})).hookSpecificOutput.permissionDecision,'deny');
  c.connection='offline';assert.equal((await invoke({kind:'edit',paths:['src/a.js']})).hookSpecificOutput.permissionDecision,'deny');
});
test('real bridge strips host payload and exits 2 when the pre-tool broker is unavailable',async t=>{
  const {Broker}=require('../core/broker.cjs'),{spawn}=require('node:child_process');
  const f=await fixture(t),seen=[];
  const broker=new Broker(f.root,async request=>{seen.push(request);return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:'scope'}};});
  await broker.start();t.after(()=>broker.close());
  const descriptor={version:1,binding:f.binding.id,secret:crypto.randomBytes(32).toString('hex'),endpoint:broker.endpoint};
  const file=path.join(f.root,'binding.json');await fs.writeFile(file,JSON.stringify(descriptor));
  async function run(){
    const child=spawn(process.execPath,[path.resolve(__dirname,'../core/bridge.cjs'),'--binding',file,'--lifecycle-hook','PreToolUse','--harness','codex','--hook-installation',f.plan.receipt.installation_id],{windowsHide:true,stdio:['pipe','pipe','pipe']});
    let out='',err='';child.stdout.on('data',v=>out+=v);child.stderr.on('data',v=>err+=v);
    const exit=new Promise((resolve,reject)=>{child.once('error',reject);child.once('close',resolve);});
    child.stdin.end(JSON.stringify({session_id:'s',cwd:f.workspace,tool_name:'Write',tool_input:{file_path:'src/a.js',content:'private'},prompt:'private',transcript_path:'private'}));
    return {code:await exit,out,err};
  }
  const first=await run();assert.equal(first.code,0);assert.equal(JSON.parse(first.out).hookSpecificOutput.permissionDecision,'deny');
  assert.equal(seen[0].action,'lifecycle_hook');assert.ok(!JSON.stringify(seen[0]).includes('private'));
  await broker.close();const stopped=await run();assert.equal(stopped.code,2);assert.ok(stopped.err.includes('DESKTOP_OFFLINE'));
});
