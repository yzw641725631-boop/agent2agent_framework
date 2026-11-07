'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {Controller}=require('../core/controller.cjs');
const {Remote,identity,HubError}=require('../core/remote.cjs');
const {Broker,call}=require('../core/broker.cjs');
const workspaces=require('../core/workspaces.cjs');
const json=(data,status=200)=>new Response(JSON.stringify(data),{status});
const session=()=>({access_token:'access',refresh_token:'refresh',expires_at:new Date(Date.now()+300000).toISOString()});
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};}
async function temporary(t){
  const base=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(base,'agent-hub-review-'));
  t.after(async()=>{const relative=path.relative(base,dir);assert.ok(relative.startsWith('agent-hub-review-') && !relative.includes(path.sep));await fs.rm(dir,{recursive:true,force:true});});return dir;
}
function fixture(directory,transport){
  const projectId=crypto.randomUUID(),bindingId=crypto.randomUUID();
  const profile={origin:'https://hub.example',login:'member',identity:identity(),sessions:{platform:session()},
    user:{id:crypto.randomUUID(),platform_role:'member'},bindings:[{id:bindingId,projectId,projectName:'project',workspace:directory,secret:'local-secret',status:'active'}],events:[],cursors:{}};
  const vault={data:{version:1,current:'one',profiles:{one:profile}},save:async()=>{},pending:Promise.resolve()};
  const controller=new Controller({vault,directory,executable:process.execPath,bridgeScript:'unused',transport,recoveryDelay:30});
  controller.connection='active';controller.remote=new Remote(profile,async()=>{},transport);
  return {profile,controller,projectId,bindingId,request:{binding:bindingId,secret:'local-secret',action:'hub_get_project',args:{}}};
}

test('document tools require current read permission and keep requests inside the binding',async t=>{
  const dir=await temporary(t),f=fixture(dir),calls=[];t.after(()=>f.controller.close());
  let capabilities=['project:read'];
  f.controller.context=async()=>({id:f.projectId,capabilities});f.controller.projectSlot=async()=>f.projectId;
  f.controller.remote={close(){},async request(method,api,body,options){calls.push({method,api,body,options});return {items:[],snapshot_id:crypto.randomUUID()};}};
  await assert.rejects(f.controller.dispatch({...f.request,action:'hub_search_documents',args:{query:'文档'}}),{code:'CAPABILITY_REQUIRED'});
  capabilities.push('documents:read');
  const args={query:'中文 %_ ;/path',limit:20};
  await f.controller.dispatch({...f.request,action:'hub_search_documents',args});
  assert.equal(calls[0].api,`/api/projects/${f.projectId}/documents/search`);assert.deepEqual(calls[0].body,args);
  assert.equal(calls[0].options.slot,f.projectId);
  await assert.rejects(f.controller.dispatch({...f.request,action:'hub_search_documents',args:{project_id:crypto.randomUUID()}}),{code:'INVALID_REQUEST'});
  await assert.rejects(f.controller.dispatch({...f.request,action:'hub_read_document',args:{path:'docs/a.md',snapshot_id:'bad'}}),{code:'INVALID_REQUEST'});
  await assert.rejects(f.controller.dispatch({...f.request,action:'hub_get_document_index',args:{token:'injected'}}),{code:'INVALID_REQUEST'});
  assert.equal(calls.length,1);assert.equal(f.controller.connection,'active');
  capabilities=['project:read'];
  await assert.rejects(f.controller.dispatch({...f.request,action:'hub_read_document',args:{path:'docs/a.md'}}),{code:'CAPABILITY_REQUIRED'});
});

test('MCP document tools are advertised only with documents read permission',async()=>{
  const {dispatch}=require('../core/bridge.cjs');
  let capabilities=['project:read'];
  const invoke=async()=>({capabilities});
  const request={jsonrpc:'2.0',id:1,method:'tools/list'};
  assert.equal((await dispatch(request,invoke)).result.tools.length,2);
  capabilities.push('documents:read');
  const tools=(await dispatch(request,invoke)).result.tools;
  assert.deepEqual(tools.filter(tool=>tool.name.includes('document')).map(tool=>tool.name),
    ['hub_get_document_index','hub_search_documents','hub_read_document']);
  assert.ok(tools.every(tool=>tool.annotations.readOnlyHint));
});
async function until(fn){for(let i=0;i<100;i++){if(await fn())return;await new Promise(r=>setTimeout(r,10));}throw new Error('condition timed out');}
test('logout immediately freezes new calls and rejects a call waiting for context',async t=>{
  const dir=await temporary(t),waitContext=deferred(),waitLogout=deferred(),enteredLogout=deferred();
  const f=fixture(dir,async url=>{assert.ok(url.endsWith('/auth/logout'));enteredLogout.resolve();await waitLogout.promise;return json({logged_out:true});});
  t.after(()=>f.controller.close());
  f.controller.context=async()=>{await waitContext.promise;return {id:f.projectId,capabilities:['project:read']};};
  const inflight=assert.rejects(f.controller.dispatch(f.request),{code:'LOGIN_REQUIRED'});
  const logout=f.controller.logout();await enteredLogout.promise;
  assert.deepEqual(f.profile.sessions,{});assert.equal(f.controller.connection,'signed_out');
  assert.deepEqual(f.controller.state().events,[]);assert.deepEqual(f.controller.state().bindings,[]);
  await assert.rejects(f.controller.dispatch(f.request),{code:'LOGIN_REQUIRED'});
  waitContext.resolve();await inflight;waitLogout.resolve();assert.equal((await logout).serverLogoutComplete,true);
});
test('a late refresh response cannot restore a cleared login',async()=>{
  const gate=deferred(),started=deferred(),profile={origin:'https://hub.example',identity:identity(),sessions:{platform:session()}};
  let writes=0;const remote=new Remote(profile,async()=>{writes++;},async()=>{started.resolve();await gate.promise;return json({...session(),access_token:'late-secret'});});
  const refreshed=assert.rejects(remote.refresh(),{code:'LOGIN_REQUIRED'});await started.promise;
  profile.sessions={};remote.close();gate.resolve();await refreshed;assert.deepEqual(profile.sessions,{});assert.equal(writes,0);
});
test('new approved grant cancels the old project stream before restoring bindings',async t=>{
  const dir=await temporary(t),f=fixture(dir);t.after(()=>f.controller.close());
  f.profile.sessions[f.projectId]={...session(),grant_id:'old-grant'};
  const watch=new AbortController();f.controller.watches.set(f.projectId,watch);
  f.controller.remote={close(){},async request(method,api){
    return api==='/api/me'?{user:f.profile.user,device_id:'device'}:{items:[{id:'new-grant',project_id:f.projectId,project_name:'project',project_slug:'project'}]};
  }};
  await f.controller.loadAccount();assert.equal(watch.signal.aborted,true);assert.equal(f.controller.watches.has(f.projectId),false);
  assert.equal(f.profile.sessions[f.projectId],undefined);assert.equal(f.controller.grants[0].id,'new-grant');
});
test('concurrent first calls create one project session',async t=>{
  const dir=await temporary(t),f=fixture(dir);t.after(()=>f.controller.close());let issued=0;
  f.controller.grants=[{id:'approved',project_id:f.projectId}];
  f.controller.remote={close(){},async request(){issued++;await new Promise(r=>setTimeout(r,15));return {...session(),grant_id:'approved'};}};
  const slots=await Promise.all([f.controller.projectSlot(f.projectId),f.controller.projectSlot(f.projectId)]);
  assert.deepEqual(slots,[f.projectId,f.projectId]);assert.equal(issued,1);
});
test('expired refresh and revoked device stop the stream instead of retrying offline',async t=>{
  const dir=await temporary(t);
  for(const code of ['SESSION_EXPIRED_OR_REVOKED','INVALID_SESSION','DEVICE_APPROVAL_REQUIRED','EMAIL_VERIFICATION_REQUIRED']){
    const f=fixture(dir);let attempts=0;
    f.controller.projectSlot=async()=>f.projectId;
    f.controller.remote={close(){},async request(){attempts++;throw new HubError(code);}};
    await f.controller.stream(f.projectId,new AbortController().signal);
    assert.equal(attempts,1);assert.equal(f.profile.bindings[0].status,'blocked');
    if(code.includes('REQUIRED'))assert.equal(f.controller.connection,'reauth_required');
    await f.controller.close();
  }
});
test('a denied write or invalid argument leaves the read connection active',async t=>{
  const dir=await temporary(t),f=fixture(dir);t.after(()=>f.controller.close());
  f.controller.context=async()=>({id:f.projectId,capabilities:['project:read']});f.controller.projectSlot=async()=>f.projectId;
  await assert.rejects(f.controller.dispatch({...f.request,action:'hub_report_progress',args:{summary:'x',operation_id:crypto.randomUUID()}}),{code:'CAPABILITY_REQUIRED'});
  await assert.rejects(f.controller.dispatch({...f.request,args:{otherProject:'injected'}}),{code:'INVALID_REQUEST'});
  assert.equal(f.profile.bindings[0].status,'active');assert.equal((await f.controller.dispatch(f.request)).id,f.projectId);
});
test('offline startup retries automatically without changing device identity',async t=>{
  const dir=await temporary(t);let online=false;
  const f=fixture(dir,async url=>{
    if(!online)throw new TypeError('offline');
    if(url.endsWith('/setup/status'))return json({active_origin:'https://hub.example'});
    if(url.endsWith('/me'))return json({user:f.profile.user,device_id:'same-device'});
    if(url.endsWith('/me/grants'))return json({items:[]});
    throw new Error('unexpected request');
  });f.profile.bindings=[];t.after(()=>f.controller.close());
  const key=f.profile.identity.privateKey;await f.controller.start();assert.equal(f.controller.connection,'offline');
  online=true;await until(()=>f.controller.connection==='active');assert.equal(f.profile.identity.privateKey,key);
});
test('lost enrollment response preserves the device key used by a retry',async t=>{
  const dir=await temporary(t);let failedKey;
  const f=fixture(dir,async(url,options)=>{
    if(url.endsWith('/setup/status'))return json({active_origin:'https://hub.example'});
    const key=JSON.parse(Buffer.from(options.headers.DPoP.split('.')[0],'base64url')).jwk;
    if(!failedKey){failedKey=key;throw new TypeError('response lost');}
    assert.deepEqual(key,failedKey);return json({device_id:crypto.randomUUID(),device_status:'pending',enrollment_token:'enroll',email_verification_required:true});
  });f.profile.sessions={};f.controller.connection='signed_out';t.after(()=>f.controller.close());
  const input={origin:'https://hub.example',login:'same-member',password:'test-password',deviceName:'test'};
  await assert.rejects(f.controller.login(input),{code:'NETWORK_UNAVAILABLE'});
  const retained=f.controller.profile.identity.privateKey;await f.controller.login(input);
  assert.equal(f.controller.profile.identity.privateKey,retained);assert.equal(f.controller.connection,'pending');
});
test('owned config can be upgraded while keeping other entries and detecting user edits',async t=>{
  const dir=await temporary(t),before={command:'old.exe',args:['bridge']},after={command:'new.exe',args:['bridge']};
  let preview=await workspaces.preview(dir,'hub',before);let installed=await workspaces.apply(dir,'hub',before,preview.previousHash);
  const file=path.join(dir,'.mcp.json'),data=JSON.parse(await fs.readFile(file,'utf8'));data.mcpServers.other={command:'other'};await fs.writeFile(file,JSON.stringify(data));
  preview=await workspaces.preview(dir,'hub',after,{entry:installed.entry});installed=await workspaces.apply(dir,'hub',after,preview.previousHash,{entry:installed.entry});
  const updated=JSON.parse(await fs.readFile(file,'utf8'));assert.equal(updated.mcpServers.hub.command,'new.exe');assert.equal(updated.mcpServers.other.command,'other');
  updated.mcpServers.hub.command='edited-by-user';await fs.writeFile(file,JSON.stringify(updated));
  await assert.rejects(workspaces.preview(dir,'hub',before,{entry:installed.entry,fileHash:installed.installedHash}),{code:'CONFIGURATION_CONFLICT'});
});
test('0.1.0 full-file ownership allows a safe first upgrade',async t=>{
  const dir=await temporary(t);const preview=await workspaces.preview(dir,'hub',{command:'old.exe'});
  const installed=await workspaces.apply(dir,'hub',{command:'old.exe'},preview.previousHash);
  const upgrade=await workspaces.preview(dir,'hub',{command:'new.exe'},{fileHash:installed.installedHash});
  assert.equal(JSON.parse(upgrade.next).mcpServers.hub.command,'new.exe');
});
test('broker accepts a full valid Unicode progress page',async t=>{
  const dir=await temporary(t),items=Array.from({length:100},()=>({summary:'😀'.repeat(300),detail:'😀'.repeat(8000),task_reference:'😀'.repeat(128)}));
  const broker=new Broker(dir,async()=>({items}));await broker.start();t.after(()=>broker.close());
  const result=await call({endpoint:broker.endpoint,binding:'test',secret:'test'},{action:'page',args:{}});
  assert.equal(result.items.length,100);assert.equal(result.items[99].detail,items[99].detail);
});
test('stdio shutdown drains a large tool response before exiting',async t=>{
  const dir=await temporary(t),items=Array.from({length:100},()=>({detail:'😀'.repeat(8000)}));
  const {dispatch}=require('../core/mcp.cjs');
  const broker=new Broker(dir,async request=>{
    assert.equal(request.action,'mcp');
    return dispatch(request.args.request,async()=>({items}),undefined,{omitToolText:true});
  });await broker.start();t.after(()=>broker.close());
  const file=path.join(dir,'binding.json');await fs.writeFile(file,JSON.stringify({version:1,protocol:'desktop-mcp-v1',endpoint:broker.endpoint,binding:'test',secret:'test'}));
  const child=spawn(process.execPath,[path.resolve(__dirname,'../core/bridge.cjs'),'--binding',file],{stdio:['pipe','pipe','pipe']});
  const chunks=[];child.stdout.on('data',data=>chunks.push(data));
  child.stdin.end(JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'hub_list_progress',arguments:{}}})+'\n');
  assert.equal((await once(child,'close'))[0],0);
  const data=JSON.parse(Buffer.concat(chunks).toString());assert.equal(data.result.structuredContent.items.length,100);
  assert.equal(data.result.structuredContent.items[99].detail,items[99].detail);
  assert.equal(JSON.parse(data.result.content[0].text).items[99].detail,items[99].detail);
});
