'use strict';
// Runs only against the isolated, server-side hub_test fixture through a loopback tunnel.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const {spawn,execFile}=require('node:child_process');
const {once}=require('node:events');
const {promisify}=require('node:util');
const {Remote,identity,proof}=require('../core/remote.cjs');
const {Controller}=require('../core/controller.cjs');
const {call}=require('../core/broker.cjs');
const {sessionHook}=require('../core/session-hook.cjs');
const {powershell}=require('../core/extract-win.cjs');
const {hashFile}=require('../core/update-integrity.cjs');
const {version}=require('../package.json');
const {_electron,chromium}=require(process.env.AGENT_HUB_PLAYWRIGHT || 'D:/agent_cache/codex/tmp/agent-hub-browser-tools/node_modules/playwright-core');
const base=process.env.AGENT_HUB_TEST_ORIGIN || 'http://127.0.0.1:18766';
assert.equal(new URL(base).hostname,'127.0.0.1');assert.equal(new URL(base).port,'18766');
const desktop=path.resolve(__dirname,'..');
const portable=process.argv.includes('--portable-update');
const previousVersion='0.3.0';
const previousBundle=path.join(desktop,'dist','AgentHub-'+previousVersion+'-win-x64');
let executable=path.join(desktop,'node_modules/electron/dist/electron.exe');
const setupToken='test-browser-setup-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const password='desktop-test-password-2026';
const rootProfile={origin:base,identity:identity(),sessions:{}};
const rootRemote=new Remote(rootProfile,async()=>{});
let electron,controllerB,clientA,clientB,clientSecond,temp;
async function setup(method,api,body){
  const result=await fetch(base+api,{method,headers:{'Content-Type':'application/json','X-Setup-Token':setupToken,
    DPoP:proof(rootProfile.identity,rootRemote.canonical,method,api,setupToken),'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
  const data=await result.json();assert.ok(result.ok,'Setup failed: '+data.code);return data;
}
const rootCall=(method,api,body)=>rootRemote.request(method,api,body);
async function mail(email){const data=await (await fetch(base+'/__test__/mail')).json();return data.messages.filter(m=>m.email===email).at(-1).code;}
async function approve(deviceId){
  const approval=(await rootCall('GET','/api/admin/approvals')).items.find(a=>a.kind==='device' && a.payload.device_id===deviceId);
  assert.ok(approval,'Device approval must exist');
  return rootCall('POST',`/api/admin/approvals/${approval.id}/decision`,{approve:true,expected_version:approval.version,expected_hash:approval.payload_hash});
}
async function grant(userId,deviceId,projectId,capabilities=['project:read','tasks:write','documents:read']){return rootCall('POST','/api/admin/grants',{
  project_id:projectId,user_id:userId,device_id:deviceId,capabilities,
});}
async function until(action,message,timeout=20000){
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline){const result=await action();if(result)return result;await new Promise(r=>setTimeout(r,150));}
  throw new Error('Timed out: '+message);
}
function mcp(configuration){
  const server=Object.values(configuration.mcpServers)[0];
  const child=spawn(server.command,server.args,{env:{...process.env,...server.env},windowsHide:true,stdio:['pipe','pipe','pipe']});
  const pending=new Map();let buffer='',counter=0;
  child.stdout.setEncoding('utf8');child.stdout.on('data',chunk=>{
    buffer+=chunk;let end;while((end=buffer.indexOf('\n'))!==-1){
      const line=buffer.slice(0,end);buffer=buffer.slice(end+1);const data=JSON.parse(line);
      const item=pending.get(data.id);if(item){clearTimeout(item.timer);pending.delete(data.id);item.resolve(data);}
    }
  });
  child.on('error',error=>{for(const item of pending.values())item.reject(error);pending.clear();});
  return {async request(method,params){
    const id=++counter;return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pending.delete(id);reject(new Error('MCP timeout: '+method));},25000);
      pending.set(id,{resolve,reject,timer});child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,...(params?{params}:{})})+'\n');
    });
  },async close(){if(child.exitCode!==null)return;child.stdin.end();await once(child,'exit');}};
}
async function documentConsole(project){
  const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);await page.locator('[name=login]').fill('desktop-root');await page.locator('[name=password]').fill(password);
    await page.locator('[name=device_name]').fill('document-console-test');await page.getByRole('button',{name:'登录',exact:true}).click();
    await page.getByRole('heading',{name:'验证当前设备',exact:true}).waitFor();
    await page.getByRole('button',{name:'发送验证码',exact:true}).click();await page.locator('#notice').filter({hasText:'验证码已发送'}).waitFor();
    await page.locator('[name=code]').fill(await mail('desktop-root@example.org'));await page.getByRole('button',{name:'验证邮箱',exact:true}).click();
    await page.locator('#notice').filter({hasText:'邮箱验证通过'}).waitFor();
    const device=(await rootCall('GET','/api/admin/devices')).items.find(d=>d.name==='document-console-test');await approve(device.id);
    await page.getByRole('button',{name:'检查审批并继续',exact:true}).click();await page.getByRole('heading',{name:'工作台',exact:true}).waitFor();
    await page.getByRole('combobox',{name:'当前项目'}).selectOption(project.id);
    await page.getByRole('button',{name:'项目文档',exact:true}).click();await page.getByRole('heading',{name:'文档源设置',exact:true}).waitFor();
    await page.locator('[name=repository_url]').fill('https://github.com/example/docs.git');
    await page.locator('[name=include_paths]').fill('docs\nAGENTS.md');await page.locator('[name=token]').fill('test-only-read-token');
    await page.getByRole('button',{name:'保存文档源',exact:true}).click();await page.getByRole('button',{name:'同步文档',exact:true}).waitFor();
    await page.getByRole('button',{name:'同步文档',exact:true}).click();
    await until(async()=>(await rootCall('GET',`/api/projects/${project.id}/documents/repository`)).snapshot_id,'index worker');
    await page.getByRole('button',{name:'刷新状态',exact:true}).click();await page.getByRole('heading',{name:'检索文档',exact:true}).waitFor();
    await page.locator('[name=query]').fill('共享文档');await page.getByRole('button',{name:'搜索文档',exact:true}).click();
    await page.getByRole('cell',{name:'docs/架构.md',exact:false}).waitFor();
    await until(async()=>(await page.getByRole('button',{name:'读取',exact:true}).count())===1,'search result');
    await page.getByRole('button',{name:'读取',exact:true}).click();
    await page.locator('pre.document-content').waitFor();assert.match(await page.locator('pre.document-content').textContent(),/<script>/);
    assert.equal(await page.evaluate(()=>globalThis.documentInjected),undefined);assert.deepEqual(errors,[]);
    await page.screenshot({path:'D:/agent_cache/codex/tmp/documents-console.png',fullPage:true});
    console.log('PASS: actual browser document configuration, encrypted credential field, worker sync, Chinese search, text-only Markdown display.');
  }finally{await browser.close();}
}
async function stopPortableWindows(){
  if(!portable || !temp)return;
  const real=await fs.realpath(temp);
  assert.equal(path.dirname(real),await fs.realpath(os.tmpdir()));
  assert.match(path.basename(real),/^agent-hub-integration-/);
  const script=[
    "$prefix = [IO.Path]::GetFullPath($env:AGENT_HUB_PORTABLE_TEST_ROOT).TrimEnd('\u005c') + '\u005c'",
    "Get-CimInstance Win32_Process -Filter \"Name = 'Agent Hub.exe'\" | Where-Object {",
    "  $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -and",
    "  $_.CommandLine -like '*--user-data-dir=*'",
    "} | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }",
  ].join('\n');
  await promisify(execFile)(powershell(),['-NoProfile','-NonInteractive','-Command',script],{
    windowsHide:true,env:{...process.env,AGENT_HUB_PORTABLE_TEST_ROOT:real},
  });
}
async function verifyPortableUpdate(dataDirectory,configuration,projectId){
  const bootstrap=path.join(temp,'portable-previous');
  await fs.cp(previousBundle,bootstrap,{recursive:true});
  const previousExecutable=path.join(bootstrap,'Agent Hub.exe');
  const [name,entry]=Object.entries(configuration.mcpServers)[0];
  const previousEntry={...entry,command:previousExecutable,
    args:[path.join(bootstrap,'resources/app/core/bridge.cjs'),...entry.args.slice(1)]};
  const client=mcp({mcpServers:{[name]:previousEntry}});
  try{
    await verifyPortableSwitch(dataDirectory,previousEntry,client,projectId);
  }finally{await client.close();}
}
async function verifyPortableSwitch(dataDirectory,entry,client,projectId){
  const launchOriginal=async()=>{
    const original=spawn(entry.command,['--user-data-dir='+dataDirectory],{
      cwd:path.dirname(entry.command),windowsHide:true,stdio:'ignore',
    });
    await once(original,'spawn');
  };
  await launchOriginal();
  const versionStatus=async()=>{
    const result=await client.request('tools/call',{name:'hub_check_version',arguments:{}});
    return result.result?.structuredContent;
  };
  await until(async()=>{const status=await versionStatus();return status?.installed_version===previousVersion &&
    status.code==='CLIENT_UPDATE_REQUIRED' && status.can_continue===false;},'outdated client starts update',35000);
  const blocked=await client.request('tools/call',{name:'hub_get_project',arguments:{}});
  assert.equal(blocked.result.isError,true);
  assert.equal(JSON.parse(blocked.result.content[0].text).code,'CLIENT_UPDATE_REQUIRED');
  console.log('PASS: outdated packaged client starts HTTPS update and blocks its project call.');

  const hook=sessionHook(entry,'claude').hooks.SessionStart[0].hooks[0];
  const hookResult=await promisify(execFile)(hook.command,hook.args,{windowsHide:true,timeout:15000});
  assert.equal(JSON.parse(hookResult.stdout).hookSpecificOutput.hookEventName,'SessionStart');
  const active=await until(async()=>{
    const status=await versionStatus();
    if(status?.code==='CLIENT_UPDATE_FAILED')throw new Error(status.message);
    if(status?.installed_version!==version || !status.can_continue)return null;
    return fs.readFile(path.join(dataDirectory,'desktop-updates/active.json')).then(JSON.parse).catch(error=>{
      if(error.code==='ENOENT')return null;
      throw error;
    });
  },'verified restart and old MCP relay reconnect',480000);
  assert.ok(active.id);
  const result=await client.request('tools/call',{name:'hub_get_project',arguments:{}});
  assert.equal(result.result.structuredContent.id,projectId);
  const codex=sessionHook(entry,'codex').hooks.SessionStart[0].hooks[0];
  const codexResult=await promisify(execFile)(powershell(),['-NoProfile','-NonInteractive','-Command',codex.command],{
    windowsHide:true,timeout:15000,
  });
  assert.match(JSON.parse(codexResult.stdout).hookSpecificOutput.additionalContext,/客户端已是可用版本/);
  console.log('PASS: actual signed ZIP activated, existing vault and MCP relay recovered, both hook commands ran.');

  await stopPortableWindows();
  await until(async()=>{
    const response=await client.request('tools/call',{name:'hub_get_project',arguments:{}});
    return response.result?.isError && response.result.content[0].text.includes('DESKTOP_OFFLINE');
  },'stop isolated updated desktop');
  await launchOriginal();
  await until(async()=>{const status=await versionStatus();return status?.installed_version===version && status.can_continue;},
    'original launcher opens the active version',35000);
  console.log('PASS: the original executable opens the active release using the same profile.');
  await stopPortableWindows();
  await until(async()=>{
    const response=await client.request('tools/call',{name:'hub_get_project',arguments:{}});
    return response.result?.isError && response.result.content[0].text.includes('DESKTOP_OFFLINE');
  },'stop isolated redirected desktop');
  const completed=spawn(process.execPath,['-e',''],{windowsHide:true,stdio:'ignore'});
  await once(completed,'exit');
  const previous=path.join(dataDirectory,'desktop-updates/versions',
    'AgentHub-'+version+'-win-x64-'+active.id,'Agent Hub.exe');
  const supervisor=spawn(process.execPath,[path.join(desktop,'core/update-launch.cjs'),
    String(completed.pid),previous,dataDirectory,process.execPath,crypto.randomUUID(),'0.3.2'],{windowsHide:true,stdio:'ignore'});
  assert.equal((await once(supervisor,'exit'))[0],0);
  await until(async()=>{const status=await versionStatus();return status?.installed_version===version && status.can_continue;},
    'original desktop recovery after candidate exits',35000);
  assert.equal(JSON.parse(await fs.readFile(path.join(dataDirectory,'desktop-updates/failed.json'))).version,'0.3.2');
  assert.equal((await client.request('tools/call',{name:'hub_get_project',arguments:{}})).result.structuredContent.id,projectId);
  console.log('PASS: supervisor restores the last working portable desktop after candidate startup failure.');
}
async function main(){
  const fixture=await fetch(base+'/__test__/mail');assert.ok(fixture.ok,'Test fixture not available');
  await rootRemote.init();assert.equal((await (await fetch(base+'/api/setup/status')).json()).setup_required,true,'Fixture must be reset before this test');
  await setup('PUT','/api/setup/configuration',{expected_version:0,public_url:null,smtp_host:'smtp.example.org',smtp_port:587,
    smtp_from:'sender@example.org',smtp_username:'sender@example.org',smtp_password:'test-only-smtp-password',smtp_transport:'starttls'});
  const attempt=await setup('POST','/api/setup/challenges',{login:'desktop-root',password,email:'desktop-root@example.org',device_name:'integration authority',os:'windows'});
  rootProfile.sessions.platform=await setup('POST','/api/setup/complete',{attempt_id:attempt.attempt_id,code:await mail('desktop-root@example.org')});
  const first=await rootCall('POST','/api/admin/projects',{slug:'desktop-first',name:'框架协作项目'});
  const second=await rootCall('POST','/api/admin/projects',{slug:'desktop-second',name:'独立项目 B'});
  await documentConsole(first);
  const user=(await rootCall('POST',`/api/projects/${first.id}/member-invitations`,{new_user:{login:'desktop-member',password,email:'desktop-member@example.org'}})).user;
  await rootCall('POST',`/api/projects/${second.id}/member-invitations`,{user_id:user.id});
  temp=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'agent-hub-integration-')));
  const workspace=path.join(temp,'workspace-a'),workspaceSecond=path.join(temp,'workspace-b'),workspaceB=path.join(temp,'other-device');
  await Promise.all([workspace,workspaceSecond,workspaceB].map(dir=>fs.mkdir(dir)));
  await fs.writeFile(path.join(workspace,'.mcp.json'),JSON.stringify({mcpServers:{other:{command:'existing-tool'}}}));
  const dataDirectory=path.join(temp,'electron-data');
  if(portable){
    const bootstrap=path.join(temp,'portable-bootstrap');
    await fs.cp(path.join(desktop,'dist','AgentHub-'+version+'-win-x64'),bootstrap,{recursive:true});
    executable=path.join(bootstrap,'Agent Hub.exe');
  }
  electron=await _electron.launch({executablePath:executable,args:portable?['--user-data-dir='+dataDirectory]:[desktop],
    env:{...process.env,AGENT_HUB_TEST_DATA:dataDirectory},timeout:60000});
  const page=await electron.firstWindow();page.setDefaultTimeout(20000);const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.getByRole('heading',{name:'连接你的协作空间'}).waitFor();
  assert.equal(await electron.evaluate(({app})=>app.getPath('userData')),dataDirectory);
  if(portable){
    const sha=await hashFile(path.join(previousBundle,'files.sha256.json'));
    const verified=await electron.evaluate(async({app},{directory,version,sha})=>{
      const {verifyTree}=process.mainModule.require(app.getAppPath()+'/core/update-integrity.cjs');
      return verifyTree(directory,{version,files_sha256:sha});
    },{directory:previousBundle,version:previousVersion,sha});
    assert.equal(verified,path.join(previousBundle,'Agent Hub.exe'));
    console.log('PASS: Electron validates physical ASAR bytes in a signed release tree.');
  }
  await page.locator('#origin').fill(base);await page.locator('#login').fill('desktop-member');
  await page.locator('#password').fill(password);await page.locator('#deviceName').fill('桌面联调设备 A');
  await page.getByRole('button',{name:'登录并验证设备',exact:true}).click();
  await page.getByRole('heading',{name:'验证当前设备',exact:true}).waitFor();
  const invoke=async(action,data={})=>page.evaluate(({action,data})=>window.hub.invoke(action,data),{action,data});
  await page.getByRole('button',{name:'发送验证码',exact:true}).click();
  await page.locator('#notice').filter({hasText:'验证码已发送'}).waitFor();
  await page.locator('#emailCode').fill(await mail('desktop-member@example.org'));
  await page.getByRole('button',{name:'验证邮箱',exact:true}).click();
  await page.locator('#notice').filter({hasText:'邮箱验证通过'}).waitFor();
  const deviceA=(await invoke('state')).deviceId;
  await approve(deviceA);await grant(user.id,deviceA,first.id);await grant(user.id,deviceA,second.id);
  await page.getByRole('button',{name:'检查审批并继续',exact:true}).click();
  await page.getByRole('heading',{name:'让每一份进展，保持同步。'}).waitFor();
  assert.equal((await invoke('state')).projects.length,2);
  console.log('PASS: real Electron login, email verification, root device approval, two project grants.');
  async function bind(projectId,folder){
    await electron.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]});},folder);
    await page.getByRole('button',{name:'绑定项目',exact:true}).click();
    await page.getByRole('dialog').locator('#project').selectOption(projectId);
    await page.getByRole('button',{name:'选择文件夹',exact:true}).click();
    await page.getByRole('button',{name:'建立连接',exact:true}).click();
    await until(async()=>(await invoke('state')).bindings.some(b=>b.projectId===projectId),'workspace binding');
  }
  await bind(first.id,workspace);await bind(second.id,workspaceSecond);
  const stateA=await invoke('state'),bindingA=stateA.bindings.find(b=>b.projectId===first.id),bindingSecond=stateA.bindings.find(b=>b.projectId===second.id);
  assert.equal(JSON.stringify(stateA).includes('access_token'),false);assert.equal(JSON.stringify(stateA).includes('privateKey'),false);
  await page.getByRole('button',{name:'MCP 配置',exact:true}).first().click();
  await page.getByRole('button',{name:'预览 .mcp.json',exact:true}).click();
  await page.getByRole('button',{name:'确认写入',exact:true}).click();
  await page.locator('#notice').filter({hasText:'配置已写入'}).waitFor();
  const config=JSON.parse(await fs.readFile(path.join(workspace,'.mcp.json'),'utf8'));
  assert.equal(config.mcpServers.other.command,'existing-tool');assert.equal(Object.keys(config.mcpServers).length,2);
  assert.ok((await fs.readdir(workspace)).some(name=>name.endsWith('.bak')));
  clientA=mcp(bindingA.configuration);clientSecond=mcp(bindingSecond.configuration);
  const init=await clientA.request('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'integration',version:'1'}});
  assert.equal(init.result.protocolVersion,'2025-11-25');assert.equal((await clientA.request('tools/list')).result.tools.length,portable?7:6);
  assert.equal((await clientA.request('tools/call',{name:'hub_get_project',arguments:{}})).result.structuredContent.id,first.id);
  assert.equal((await clientSecond.request('tools/call',{name:'hub_get_project',arguments:{}})).result.structuredContent.id,second.id);
  const inject=await clientA.request('tools/call',{name:'hub_get_project',arguments:{project_id:second.id}});assert.equal(inject.result.isError,true);
  console.log('PASS: two independently authorized stdio MCP connections, actual Electron run-as-node, config preview and backup.');
  const documentSearch=await clientA.request('tools/call',{name:'hub_search_documents',arguments:{query:'共享文档'}});
  assert.equal(documentSearch.result.isError,false);const document=documentSearch.result.structuredContent.items[0];
  assert.equal(document.path,'docs/架构.md');assert.equal(document.git_commit,'a'.repeat(40));
  const documentRead=await clientA.request('tools/call',{name:'hub_read_document',arguments:{path:document.path,snapshot_id:document.snapshot_id}});
  assert.match(documentRead.result.structuredContent.content,/MCP/);
  const documentInject=await clientA.request('tools/call',{name:'hub_search_documents',arguments:{project_id:second.id}});assert.equal(documentInject.result.isError,true);
  const secondIndex=await clientSecond.request('tools/call',{name:'hub_search_documents',arguments:{query:'共享文档'}});
  assert.equal(secondIndex.result.isError,true);assert.match(secondIndex.result.content[0].text,/DOCUMENT_INDEX_NOT_READY/);
  console.log('PASS: actual stdio MCP Chinese document search, immutable commit reference, full read and project scope.');
  const vaultB={data:{version:1,profiles:{},current:null},save:async()=>{},pending:Promise.resolve()};
  controllerB=new Controller({vault:vaultB,directory:path.join(temp,'device-b'),executable,bridgeScript:path.join(desktop,'core/bridge.cjs')});
  await controllerB.start();await controllerB.login({origin:base,login:'desktop-member',password,deviceName:'设备 B'});
  await controllerB.sendEmail();await controllerB.verifyEmail(await mail('desktop-member@example.org'));
  const deviceB=controllerB.state().deviceId;await approve(deviceB);await grant(user.id,deviceB,first.id,['project:read']);await controllerB.checkApproval();
  await controllerB.bind({projectId:first.id,workspace:workspaceB});const bindingB=controllerB.state().bindings[0];
  clientB=mcp(bindingB.configuration);const notifications=[];controllerB.on('notification',event=>notifications.push(event));
  assert.equal((await clientB.request('tools/list')).result.tools.length,2);
  await grant(user.id,deviceB,first.id);await controllerB.refresh();
  await new Promise(r=>setTimeout(r,2500));
  assert.equal(controllerB.state().bindings[0].status,'active');
  assert.equal((await clientB.request('tools/list')).result.tools.length,6);
  console.log('PASS: approved permission increase replaces the old grant and stream without a stale revocation blocking the new connection.');
  const operation=crypto.randomUUID(),args={summary:'桌面与 MCP 联调完成',status:'checkpoint',detail:'双项目隔离、进度与通知验证',operation_id:operation};
  const report=await clientA.request('tools/call',{name:'hub_report_progress',arguments:args});assert.equal(report.result.isError,false);
  const repeat=await clientA.request('tools/call',{name:'hub_report_progress',arguments:args});assert.equal(repeat.result.structuredContent.sequence,report.result.structuredContent.sequence);
  await until(()=>controllerB.state().events.some(event=>event.summary===args.summary),'other-device progress push');
  assert.ok(notifications.some(n=>n.title==='项目有新的进度'));
  const listB=await clientB.request('tools/call',{name:'hub_list_progress',arguments:{}});assert.equal(listB.result.structuredContent.items.length,1);
  const listSecond=await clientSecond.request('tools/call',{name:'hub_list_progress',arguments:{}});assert.equal(listSecond.result.structuredContent.items.length,0);
  controllerB.stopWatches();
  await clientA.request('tools/call',{name:'hub_report_progress',arguments:{operation_id:crypto.randomUUID(),summary:'重连补齐检查点'}});
  await until(()=>!controllerB.watches.size,'stream stopped');controllerB.watch(first.id);
  await until(()=>controllerB.state().events.length===2,'reconnect replay');
  assert.equal(new Set(controllerB.state().events.map(e=>e.sequence)).size,2);
  await page.getByRole('button',{name:'协作概览',exact:true}).click();
  await page.getByRole('heading',{name:'桌面与 MCP 联调完成',exact:true}).waitFor();
  const screenshot=process.env.AGENT_HUB_TEST_SCREENSHOT || 'D:/agent_cache/codex/tmp/desktop-overview.png';await page.screenshot({path:screenshot,fullPage:true});
  console.log('PASS: cross-device SSE, durable reports, idempotency, project isolation, replay and notification event.');
  await controllerB.close();let online=false;
  controllerB=new Controller({vault:vaultB,directory:path.join(temp,'device-b'),executable,bridgeScript:path.join(desktop,'core/bridge.cjs'),recoveryDelay:100,
    transport:async(...args)=>{if(!online)throw new TypeError('simulated offline');return fetch(...args);}});
  await controllerB.start();assert.equal(controllerB.connection,'offline');online=true;
  await until(()=>controllerB.connection==='active' && controllerB.watches.size===1,'offline startup automatic recovery');
  assert.equal((await clientB.request('tools/call',{name:'hub_get_project',arguments:{}})).result.structuredContent.id,first.id);
  console.log('PASS: offline desktop restart automatically restores the existing identity, project session, descriptor and running MCP client.');
  await rootCall('POST',`/api/projects/${first.id}/members/${user.id}/revoke`,{});
  await until(()=>controllerB.state().bindings[0].status==='blocked','revocation stream');
  const denied=await clientA.request('tools/call',{name:'hub_get_project',arguments:{}});assert.equal(denied.result.isError,true);assert.match(denied.result.content[0].text,/PROJECT_ACCESS_REVOKED/);
  const deniedDocument=await clientA.request('tools/call',{name:'hub_read_document',arguments:{path:document.path,snapshot_id:document.snapshot_id}});
  assert.equal(deniedDocument.result.isError,true);assert.match(deniedDocument.result.content[0].text,/PROJECT_ACCESS_REVOKED/);
  const unaffected=await clientSecond.request('tools/call',{name:'hub_get_project',arguments:{}});assert.equal(unaffected.result.structuredContent.id,second.id);
  await until(async()=>(await invoke('state')).bindings.find(b=>b.projectId===first.id).status==='blocked','UI revocation state');
  assert.equal(await fs.readFile(path.join(workspace,'.mcp.json'),'utf8')!==null,true);
  await rootCall('POST',`/api/admin/devices/${deviceB}/revoke`,{});
  await assert.rejects(controllerB.refresh(),{code:'DEVICE_APPROVAL_REQUIRED'});
  assert.equal(controllerB.connection,'reauth_required');assert.deepEqual(controllerB.state().events,[]);
  assert.deepEqual(controllerB.state().bindings,[]);
  console.log('PASS: revoked device requires login and stops reconnecting; signed-out state does not expose cached project data.');
  const sealed=await fs.readFile(path.join(temp,'electron-data/vault.bin'));
  assert.equal(sealed.includes(Buffer.from(password)),false);assert.equal(sealed.includes(Buffer.from('access_token')),false);
  assert.deepEqual(errors,[]);
  await electron.close();electron=null;
  const offline=await clientSecond.request('tools/call',{name:'hub_get_project',arguments:{}});assert.equal(offline.result.isError,true);assert.match(offline.result.content[0].text,/DESKTOP_OFFLINE/);
  console.log('PASS: revoke one project stops its live stream and MCP; second project stays usable; full desktop exit rejects further MCP calls.');
  if(portable)await verifyPortableUpdate(dataDirectory,bindingSecond.configuration,second.id);
}
main().catch(async error=>{
  console.error(error.stack);if(electron){const windows=electron.windows();if(windows[0])console.error((await windows[0].locator('body').innerText()).slice(-3000));}process.exitCode=1;
}).finally(async()=>{
  for(const client of [clientA,clientB,clientSecond])await client?.close().catch(()=>{});
  await controllerB?.close().catch(()=>{});await electron?.close().catch(()=>{});
  await stopPortableWindows().catch(error=>{console.error('Unable to stop isolated test windows:',error.message);process.exitCode=1;});
  // Keep failed-run data for diagnosis; all accounts belong only to hub_test.
  if(temp && !process.exitCode){const relative=path.relative(await fs.realpath(os.tmpdir()),temp);assert.ok(relative.startsWith('agent-hub-integration-') && !relative.includes(path.sep));await fs.rm(temp,{recursive:true,force:true});}
});
