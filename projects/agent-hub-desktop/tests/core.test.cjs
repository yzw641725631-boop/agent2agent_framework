'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {identity,proof,Remote,origin,parseEvents,HubError}=require('../core/remote.cjs');
const {Broker,call,equalSecret}=require('../core/broker.cjs');
const {Vault}=require('../core/vault.cjs');
const {Controller}=require('../core/controller.cjs');
const workspaces=require('../core/workspaces.cjs');
const {dispatch}=require('../core/bridge.cjs');
async function temporary(t){
  const target=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'agent-hub-desktop-test-')));
  t.after(async()=>{
    const relative=path.relative(await fs.realpath(os.tmpdir()),target);
    assert.ok(relative.startsWith('agent-hub-desktop-test-') && !relative.includes(path.sep));
    await fs.rm(target,{recursive:true,force:true});
  });return target;
}
const response=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const session=(access='access',refresh='refresh')=>({access_token:access,refresh_token:refresh,expires_at:new Date(Date.now()+300000).toISOString()});
test('DPoP verifies ES256, token binding, canonical path and fresh nonce',()=>{
  const key=identity(),jwt=proof(key,'https://hub.example','GET','/api/projects?after=5','private-token');
  const [header,payload,signature]=jwt.split('.');
  const claims=JSON.parse(Buffer.from(payload,'base64url'));
  assert.equal(claims.htu,'https://hub.example/api/projects');assert.equal(claims.htm,'GET');
  assert.equal(claims.ath,crypto.createHash('sha256').update('private-token').digest('base64url'));
  assert.equal(JSON.parse(Buffer.from(header,'base64url')).jwk.d,undefined);
  assert.ok(crypto.verify('sha256',Buffer.from(header+'.'+payload),{key:crypto.createPublicKey({key:key.jwk,format:'jwk'}),dsaEncoding:'ieee-p1363'},Buffer.from(signature,'base64url')));
  assert.notEqual(jwt,proof(key,'https://hub.example','GET','/api/projects?after=5','private-token'));
  for(const invalid of ['http://47.0.0.1:80','https://root:pass@example.com','https://hub.example/path','file:///tmp'])assert.throws(()=>origin(invalid));
  assert.equal(origin('http://127.0.0.1:8765'),'http://127.0.0.1:8765');
});
test('concurrent expired requests refresh once and keep operation id on retry',async()=>{
  const profile={origin:'https://hub.example',identity:identity(),sessions:{platform:session()}};
  let refreshes=0,persisted=0;const seen=[];
  const remote=new Remote(profile,async()=>{persisted++;},async(url,options)=>{
    if(url.endsWith('/auth/refresh')){refreshes++;await new Promise(r=>setTimeout(r,20));return response(session('new-access','new-refresh'));}
    seen.push(options.headers['Idempotency-Key']);
    return options.headers.Authorization==='DPoP access'?response({code:'SESSION_EXPIRED_OR_REVOKED'},401):response({done:true});
  });
  const results=await Promise.all([remote.request('POST','/api/test',{}, {idempotency:'operation-a'}),remote.request('POST','/api/test',{}, {idempotency:'operation-b'})]);
  assert.deepEqual(results,[{done:true},{done:true}]);assert.equal(refreshes,1);assert.equal(persisted,1);
  assert.deepEqual(seen.sort(),['operation-a','operation-a','operation-b','operation-b']);
});
test('SSE parses split UTF-8, heartbeats and replay cursors',async()=>{
  const data=Buffer.from(': heartbeat\r\n\r\nid: 19\r\nevent: progress\r\ndata: {"summary":"检查点"}\r\n\r\nevent: ready\ndata: {}\n\n');
  async function* chunks(){for(let i=0;i<data.length;i+=3)yield data.subarray(i,i+3);}
  const events=[];for await(const event of parseEvents(chunks()))events.push(event);
  assert.deepEqual(events,[{event:'progress',id:'19',data:{summary:'检查点'}},{event:'ready',id:undefined,data:{}}]);
});
test('vault saves encrypted snapshots, restores keys and refuses plaintext fallback',async t=>{
  const dir=await temporary(t),key=crypto.randomBytes(32);
  const protector={isAsyncEncryptionAvailable:async()=>true,
    encryptStringAsync:async value=>{const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);const data=Buffer.concat([cipher.update(value),cipher.final()]);return Buffer.concat([iv,cipher.getAuthTag(),data]);},
    decryptStringAsync:async sealed=>{const decipher=crypto.createDecipheriv('aes-256-gcm',key,sealed.subarray(0,12));decipher.setAuthTag(sealed.subarray(12,28));return {result:Buffer.concat([decipher.update(sealed.subarray(28)),decipher.final()]).toString('utf8'),shouldReEncrypt:false};}};
  const vault=new Vault(dir,protector);await vault.load();vault.data.profiles.one={secret:'never-plaintext'};
  const a=vault.save();vault.data.current='one';await Promise.all([a,vault.save()]);
  assert.equal((await fs.readFile(path.join(dir,'vault.bin'))).includes(Buffer.from('never-plaintext')),false);
  const restored=new Vault(dir,protector);assert.deepEqual(await restored.load(),vault.data);
  await assert.rejects(new Vault(dir,{isAsyncEncryptionAvailable:async()=>false}).load(),{code:'SECURE_STORAGE_UNAVAILABLE'});
});
test('configuration preview preserves other entries, backs up and detects edits',async t=>{
  const dir=await temporary(t),file=path.join(dir,'.mcp.json');
  const previous=JSON.stringify({mcpServers:{existing:{command:'other'}}});await fs.writeFile(file,previous);
  const preview=await workspaces.preview(dir,'agent-hub-test',{command:'hub'});
  assert.equal(await fs.readFile(file,'utf8'),previous);
  const result=await workspaces.apply(dir,'agent-hub-test',{command:'hub'},preview.previousHash);
  assert.equal(await fs.readFile(result.backup,'utf8'),previous);
  assert.equal(JSON.parse(await fs.readFile(file,'utf8')).mcpServers.existing.command,'other');
  await assert.rejects(workspaces.preview(dir,'agent-hub-test',{}),{code:'CONFIGURATION_CONFLICT'});
  const next=await workspaces.preview(dir,'second',{});await fs.writeFile(file,'{"edited":true}');
  await assert.rejects(workspaces.apply(dir,'second',{},next.previousHash),{code:'CONFIGURATION_CHANGED'});
  assert.equal(await fs.readFile(file,'utf8'),'{"edited":true}');
});
test('configuration refuses workspace junction replacement',async t=>{
  const root=await temporary(t),bound=path.join(root,'bound'),outside=path.join(root,'outside');
  await fs.mkdir(bound);await fs.mkdir(outside);const actual=await workspaces.directory(bound);
  await fs.rename(bound,path.join(root,'old'));await fs.symlink(outside,bound,process.platform==='win32'?'junction':'dir');
  await assert.rejects(workspaces.preview(actual,'test',{}),{code:'WORKSPACE_CHANGED'});
});
test('broker authenticates local binding and scopes calls to project',async t=>{
  const dir=await temporary(t),id=crypto.randomUUID(),other=crypto.randomUUID();
  const profile={sessions:{platform:session()},bindings:[{id,secret:'local-secret',projectId:id,workspace:dir,status:'active'}],user:{platform_role:'superadmin'},events:[]};
  const vault={data:{current:'one',profiles:{one:profile}},save:async()=>{}};
  const controller=new Controller({vault,directory:dir,executable:process.execPath,bridgeScript:'unused'});
  controller.connection='active';
  controller.context=async(projectId)=>({id:projectId,capabilities:['project:read','tasks:write']});
  await controller.broker.start();t.after(()=>controller.close());
  const descriptor={endpoint:controller.broker.endpoint,binding:id,secret:'local-secret'};
  await assert.rejects(call({...descriptor,secret:'wrong'},{action:'metadata',args:{}}),{code:'BRIDGE_AUTHENTICATION_FAILED'});
  await assert.rejects(call(descriptor,{action:'hub_get_project',args:{project_id:other}}),{code:'INVALID_REQUEST'});
  assert.equal((await call(descriptor,{action:'hub_get_project',args:{}})).id,id);
  controller.context=async()=>{throw new HubError('PROJECT_ACCESS_REVOKED');};
  await assert.rejects(call(descriptor,{action:'hub_get_project',args:{}}),{code:'PROJECT_ACCESS_REVOKED'});
  assert.equal(profile.bindings[0].status,'blocked');
  assert.equal(equalSecret('é','a'),false);
});
test('MCP only lists authorized tools and reports permission failures as tool errors',async()=>{
  const request={jsonrpc:'2.0',id:1,method:'tools/list'};
  const read=await dispatch(request,async()=>({capabilities:['project:read']}));assert.equal(read.result.tools.length,14);
  assert.ok(!read.result.tools.some(tool=>['hub_push_task','hub_assemble_project'].includes(tool.name)));
  const write=await dispatch(request,async()=>({capabilities:['project:read','tasks:write']}));assert.equal(write.result.tools.length,23);
  const denied=await dispatch({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'hub_report_progress',arguments:{summary:'x'}}},async()=>{throw new HubError('PROJECT_ACCESS_REVOKED');});
  assert.equal(denied.result.isError,true);assert.match(denied.result.content[0].text,/PROJECT_ACCESS_REVOKED/);
  assert.equal((await dispatch({...request,method:'arbitrary_shell'},async()=>{})).error.code,-32601);
});
test('standalone stdio bridge emits JSON-RPC only and reads binding on each call',async t=>{
  const dir=await temporary(t),id=crypto.randomUUID();
  const broker=new Broker(dir,async request=>{
    assert.equal(request.binding,id);assert.equal(request.secret,'bridge-secret');return {capabilities:['project:read']};
  });await broker.start();t.after(()=>broker.close());
  const file=path.join(dir,'binding.json');await fs.writeFile(file,JSON.stringify({version:1,endpoint:broker.endpoint,binding:id,secret:'bridge-secret'}));
  const child=spawn(process.execPath,[path.resolve(__dirname,'../core/bridge.cjs'),'--binding',file],{stdio:['pipe','pipe','pipe']});
  const chunks=[],errors=[];child.stdout.on('data',v=>chunks.push(v));child.stderr.on('data',v=>errors.push(v));
  child.stdin.end(JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-11-25'}})+'\n'+JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'})+'\n'+JSON.stringify({jsonrpc:'2.0',id:2,method:'tools/list'})+'\n');
  const [code]=await once(child,'exit');assert.equal(code,0,Buffer.concat(errors).toString());
  const messages=Buffer.concat(chunks).toString().trim().split('\n').map(v=>JSON.parse(v));
  assert.equal(messages.length,2);assert.equal(messages[0].result.protocolVersion,'2025-11-25');assert.equal(messages[1].result.tools.length,14);
});
