'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {execFile}=require('node:child_process'),{promisify}=require('node:util'),execute=promisify(execFile);
const {FrameworkRuntime,build}=require('../core/framework-runtime.cjs');
const host=require('../core/host-install.cjs'),definitions=require('../core/framework-tools.cjs'),{dispatch}=require('../core/mcp.cjs');
const ids=['project-manager','architect','designer','developer','reviewer','tester','publisher'];
function catalog(language='zh'){
  const zh=['项目经理','架构师','产品设计','开发工程师','代码审查','测试工程师','发布负责人'];
  return {schema_version:1,catalog_version:1,content_hash:'a'.repeat(64),language,
    roles:ids.map((id,i)=>({id,number:String(i).padStart(2,'0'),mode:i<4?'primary':'gate',
      name_zh:zh[i],name_en:['ProjectManager','Architect','Designer','Developer','Reviewer','Tester','Publisher'][i],
      responsibilities:[language==='zh'?'只处理当前授权范围。':'Work within the authorized task scope.'],
      outputs:[language==='zh'?'实际交接依据':'Actual handoff evidence'],grants_permissions:false}))};
}
async function fixture(t){
  const temp=await fs.realpath(os.tmpdir()),root=await fs.mkdtemp(path.join(temp,'agent-hub-framework-'));
  t.after(async()=>{assert.equal(path.dirname(root),temp);assert.ok(path.basename(root).startsWith('agent-hub-framework-'));await fs.rm(root,{recursive:true,force:true});});
  const workspace=path.join(root,'repo'),directory=path.join(root,'desktop');await fs.mkdir(workspace);
  await fs.writeFile(path.join(workspace,'business.txt'),'business stays');
  const binding={id:crypto.randomUUID(),projectId:crypto.randomUUID(),workspace,status:'active'};
  const c={directory,profile:{},remote:{},connection:'active',binding:id=>id===binding.id?binding:null,
    ensureCurrent(p,r){if(p!==this.profile || r!==this.remote)throw Object.assign(Error('changed'),{code:'LOGIN_REQUIRED'});},
    configuration:()=>({mcpServers:{'agent-hub-role-test':{}}}),
    async invokeProjectRemote(b,action,args){assert.equal(b,binding);assert.equal(action,'hub_get_roles');return catalog(args.language);}};
  const runtime=new FrameworkRuntime(c);
  return {c,binding,runtime,workspace,directory,name:'agent-hub-role-test'};
}
for(const [names,body] of [['zh','zh'],['en','en'],['en','zh'],['zh','en']])test('generated framework runs self checks: '+names+'/'+body,async t=>{
  const f=await fixture(t),args={doc_name_lang:names,doc_content_lang:body};
  const preview=await f.runtime.invoke(f.binding,'hub_preview_framework',args);
  assert.equal(preview.files.length,12);assert.equal((await fs.readdir(f.workspace)).length,1);
  const result=await f.runtime.invoke(f.binding,'hub_install_framework',{...args,preview_hash:preview.preview_hash});
  assert.equal(result.installed,true);assert.equal(result.automatic_agent_launch,false);
  const checker=path.join(preview.directory,'scripts/check-framework.cjs'),smoke=path.join(preview.directory,'tests/framework.test.cjs');
  const checked=JSON.parse((await execute(process.execPath,[checker],{windowsHide:true})).stdout);
  assert.equal(checked.project_id,f.binding.projectId);assert.equal(checked.verified,11);
  await execute(process.execPath,['--test',smoke],{windowsHide:true});
  const content=await fs.readFile(result.entrypoint,'utf8');assert.ok(content.includes(f.binding.projectId));
  const rolePath=preview.files.find(file=>file.path.startsWith(names==='zh'?'角色/':'roles/')).path;
  await fs.appendFile(path.join(preview.directory,rolePath),'user edit');
  await assert.rejects(execute(process.execPath,[checker],{windowsHide:true}),error=>error.stderr.includes('FILE_CHANGED'));
  await assert.rejects(f.runtime.invoke(f.binding,'hub_install_framework',{...args,preview_hash:preview.preview_hash}),{code:'CONFIGURATION_CONFLICT'});
});
test('framework tools reject arbitrary directories, stale previews and malformed catalogs',async t=>{
  const f=await fixture(t);
  for(const args of [{workspace:'C:/'},{doc_name_lang:'../../'}, {doc_content_lang:'fr'}, {project_id:crypto.randomUUID()}]){
    assert.throws(()=>definitions.validate('hub_preview_framework',args),{code:'INVALID_REQUEST'});
  }
  await assert.rejects(f.runtime.invoke(f.binding,'hub_install_framework',{preview_hash:'0'.repeat(64)}),{code:'FRAMEWORK_PREVIEW_STALE'});
  const bad=catalog();bad.roles[0].responsibilities={injected:'command'};
  assert.throws(()=>build(f.binding,f.name,bad),{code:'INVALID_RESPONSE'});
  const wrong=catalog();wrong.roles[1].number='00';assert.throws(()=>build(f.binding,f.name,wrong),{code:'INVALID_RESPONSE'});
  assert.deepEqual(await fs.readdir(f.workspace),['business.txt']);
});
test('framework updates keep backups outside the workspace and uninstall both language sets',async t=>{
  const f=await fixture(t),first=await f.runtime.invoke(f.binding,'hub_preview_framework',{});
  await f.runtime.invoke(f.binding,'hub_install_framework',{preview_hash:first.preview_hash});
  const args={doc_name_lang:'en',doc_content_lang:'en'},next=await f.runtime.invoke(f.binding,'hub_preview_framework',args);
  await Promise.all([1,2].map(()=>f.runtime.invoke(f.binding,'hub_install_framework',{...args,preview_hash:next.preview_hash})));
  const backup=path.join(f.directory,'framework-deployments',f.binding.id,'backups');
  assert.ok((await fs.readdir(backup)).some(file=>file.endsWith('.bak')));
  const all=await fs.readdir(first.directory,{recursive:true});assert.ok(!all.some(file=>file.endsWith('.bak')));
  const other='agent-hub-another-project',second=path.join(f.directory,'other');
  await host.deployFramework({workspace:f.workspace,name:other,receiptDirectory:second,files:[{path:'main.md',bytes:Buffer.from('other project')}]});
  const result=await host.uninstallProject({workspace:f.workspace,name:f.name,receiptDirectories:[path.join(f.directory,'framework-deployments',f.binding.id)]});
  assert.equal(result.state,'complete');assert.equal(result.removed_files,19);
  assert.equal(await fs.readFile(path.join(f.workspace,'business.txt'),'utf8'),'business stays');
  assert.equal(await fs.readFile(path.join(f.workspace,'.agent-hub/framework',other,'main.md'),'utf8'),'other project');
  await assert.rejects(fs.stat(first.directory),{code:'ENOENT'});
});
test('account change during deployment rolls back files and receipt',async t=>{
  const f=await fixture(t),preview=await f.runtime.invoke(f.binding,'hub_preview_framework',{});
  let checks=0;f.c.ensureCurrent=function(p,r){if(++checks===8)this.profile={};if(this.profile!==p || this.remote!==r)throw Object.assign(Error('changed'),{code:'LOGIN_REQUIRED'});};
  await assert.rejects(f.runtime.invoke(f.binding,'hub_install_framework',{preview_hash:preview.preview_hash}),{code:'LOGIN_REQUIRED'});
  await assert.rejects(fs.stat(path.join(f.directory,'framework-deployments',f.binding.id,'framework.json')),{code:'ENOENT'});
  const files=await fs.readdir(f.workspace,{recursive:true});assert.ok(!files.some(file=>file.endsWith('.md') || file.endsWith('.cjs')));
});
test('framework checker rejects backslashes and linked files',async t=>{
  const f=await fixture(t),preview=await f.runtime.invoke(f.binding,'hub_preview_framework',{});
  await f.runtime.invoke(f.binding,'hub_install_framework',{preview_hash:preview.preview_hash});
  const indexFile=path.join(preview.directory,'doc_index.json'),index=JSON.parse(await fs.readFile(indexFile,'utf8'));
  index.files[0].path='roles\\file.md';await fs.writeFile(indexFile,JSON.stringify(index));
  const checker=path.join(preview.directory,'scripts/check-framework.cjs');
  await assert.rejects(execute(process.execPath,[checker],{windowsHide:true}),error=>error.stderr.includes('INVALID_PATH'));
  index.files[0].path='main.md';await fs.writeFile(indexFile,JSON.stringify(index));
  await fs.unlink(path.join(preview.directory,'main.md'));await fs.symlink(f.workspace,path.join(preview.directory,'main.md'),'junction');
  await assert.rejects(execute(process.execPath,[checker],{windowsHide:true}),error=>error.stderr.includes('LINK_NOT_ALLOWED'));
});
test('framework tools are confined to project connections and disappear on revocation',async()=>{
  for(const scope of ['setup','management']){
    const listed=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>({platform_role:'superadmin',capabilities:[]}),null,{scope});
    assert.ok(listed.result.tools.every(tool=>!definitions.tools.some(item=>item.name===tool.name)));
  }
  const revoked=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>({closeout_required:true,capabilities:[]}));
  assert.ok(revoked.result.tools.every(tool=>!definitions.tools.some(item=>item.name===tool.name)));
});
