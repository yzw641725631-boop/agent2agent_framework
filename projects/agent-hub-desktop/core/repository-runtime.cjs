'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const execute=promisify(execFile),{HubError}=require('./remote.cjs');
const {directory}=require('./workspaces.cjs'),hostInstall=require('./host-install.cjs');
const {validate}=require('./repository-tools.cjs');
const observer=require('./git-observer.cjs');
const fail=(code,message)=>{throw new HubError(code,message);};
async function safePath(value){
  for(let current=path.resolve(value);;current=path.dirname(current)){
    try{if((await fs.lstat(current)).isSymbolicLink())fail('WORK_PATH_UNSAFE','工作目录不能经过符号链接或目录联接。');}
    catch(error){if(error.code!=='ENOENT')throw error;}
    if(path.dirname(current)===current)break;
  }
}
async function git(workspace,args,{origin,ticket,support,signal}={}){
  const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!key.toUpperCase().startsWith('GIT_')));
  Object.assign(env,{GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:path.join(support,'empty.config'),
    GIT_CONFIG_SYSTEM:path.join(support,'empty.config'),GIT_TEMPLATE_DIR:path.join(support,'empty-hooks'),
    GIT_TERMINAL_PROMPT:'0'});
  const entries=[['credential.helper',''],['core.hooksPath',path.join(support,'empty-hooks')],
    ['core.fsmonitor','false'],['http.followRedirects','false'],['protocol.file.allow','never'],
    ['http.extraHeader',''],...(ticket?[[`http.${origin}.extraHeader`,'Authorization: Basic '+Buffer.from('agent-hub:'+ticket).toString('base64')]]:[])];
  env.GIT_CONFIG_COUNT=String(entries.length);
  entries.forEach(([key,value],i)=>{env['GIT_CONFIG_KEY_'+i]=key;env['GIT_CONFIG_VALUE_'+i]=value;});
  try{
    const result=await execute('git',args,{cwd:workspace,env,signal,windowsHide:true,maxBuffer:256*1024,timeout:120000,encoding:'utf8'});
    return result.stdout;
  }catch(error){
    throw new HubError('REPOSITORY_GIT_FAILED','Git 操作未完成，已有代码和工作目录已保留。请核对网络、仓库授权和 Git 可用性。',
      {exit_code:error.code});
  }
}
class RepositoryRuntime{
  constructor(controller){this.c=controller;this.operations=Promise.resolve();}
  async support(){
    const support=path.join(this.c.directory,'git-runtime');await safePath(support);
    await fs.mkdir(path.join(support,'empty-hooks'),{recursive:true,mode:0o700});
    await fs.writeFile(path.join(support,'empty.config'),'',{mode:0o600});return support;
  }
  async registry(binding){return this.c.invokeProjectRemote(binding,'hub_list_repositories',{});}
  guard(profile,remote,binding){
    const c=this.c;c.ensureCurrent(profile,remote);
    if(c.draining || c.connection!=='active' || c.binding(binding.id)!==binding || binding.status!=='active')fail('LOGIN_REQUIRED');
  }
  async ticket(binding,repo,body){
    const c=this.c,p=c.profile,remote=c.remote,slot=await c.projectSlot(binding.projectId);
    const result=await remote.request('POST',`/api/projects/${binding.projectId}/repositories/${repo.id}/git-ticket`,body,{slot});
    this.guard(p,remote,binding);
    if(result.git_path!==`/api/git/${repo.id}.git` || !result.ticket || Date.parse(result.expires_at)<=Date.now())fail('INVALID_RESPONSE');
    return {...result,origin:remote.base+result.git_path};
  }
  async invoke(binding,name,args){
    validate(name,args);
    if(name==='hub_assemble_project'){
      const profile=this.c.profile,remote=this.c.remote;
      const action=this.operations.catch(()=>{}).then(()=>{this.guard(profile,remote,binding);return this.assemble(binding,args);});
      this.operations=action.catch(()=>{});return action;
    }
    return this.transfer(binding,name,args);
  }
  async assemble(binding,args){
    const c=this.c,p=c.profile,remote=c.remote,registry=await this.registry(binding);
    this.guard(p,remote,binding);
    if(binding.repositoryId)fail('PROJECT_GROUP_WORKSPACE_REQUIRED','请使用项目组根目录的连接组装仓库。');
    const root=await directory(binding.workspace);await safePath(root);
    const requested=args.repository_ids || registry.items.filter(repo=>repo.capabilities.includes('code:read')).map(repo=>repo.id);
    if(!requested.length)fail('REPOSITORY_NOT_ACCESSIBLE','当前没有获准的代码仓库。');
    const selected=requested.map(id=>{
      const repo=registry.items.find(repo=>repo.id===id && repo.status==='active' && repo.capabilities.includes('code:read'));
      if(!repo)fail('REPOSITORY_NOT_ACCESSIBLE','所选仓库没有代码读取权限，未开始拉取。');return repo;
    });
    const operations=p.assemblyOperations??={},previous=operations[args.operation_id];
    const key=JSON.stringify({binding:binding.id,ids:[...requested].sort(),harness:args.harness || null});
    if(previous && previous.key!==key)fail('IDEMPOTENCY_PAYLOAD_CHANGED');
    const op=previous || (operations[args.operation_id]={key,items:{}});
    const support=await this.support(),items=[];
    for(const repo of selected){
      this.guard(p,remote,binding);
      if(!/^[a-z][a-z0-9-]{1,79}$/.test(repo.slug) || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/i.test(repo.slug))fail('INVALID_RESPONSE');
      const workspace=path.join(root,repo.slug);await safePath(workspace);
      let receipt=op.items[repo.id];
      if(receipt && path.resolve(receipt.workspace)!==workspace)fail('WORKSPACE_CHANGED');
      if(receipt?.state==='ready'){
        if(await directory(receipt.workspace)!==workspace)fail('WORKSPACE_CHANGED');
        await this.verifyOrigin(receipt.workspace,repo,support,remote.base);
        const child=p.bindings.find(item=>item.id===receipt.binding_id && item.repositoryId===repo.id && item.workspace===workspace);
        if(!child || child.status!=='active')fail('ASSEMBLY_CONNECTION_UNAVAILABLE','仓库代码仍在，但原连接已停用。请从接入入口重新连接此目录，不会重复拉取或覆盖代码。');
        items.push({...receipt});continue;
      }
      let cloned=false;
      if(receipt){
        const entries=await fs.readdir(workspace).catch(error=>{if(error.code==='ENOENT')return null;throw error;});
        if(entries?.length){
          // Only resume our complete, unchanged clone. Never reset, clean or remove an interrupted directory.
          try{
            await this.verifyOrigin(workspace,repo,support,remote.base);
            await observer.initialize(workspace);cloned=true;
          }catch(error){
            fail('ASSEMBLY_RECOVERY_REQUIRED','上次拉取的目录已有改动或尚未完整，代码已保留。请检查目录并手动重新接入，自动重试不会覆盖文件。');
          }
        }else if(entries===null)await fs.mkdir(workspace,{mode:0o700});
      }else{
        try{await fs.mkdir(workspace,{mode:0o700});}catch(error){
          if(error.code==='EEXIST')fail('ASSEMBLY_DIRECTORY_EXISTS','同名目录已经存在，已保留原工作。请选择新的项目组目录。');throw error;
        }
        receipt=op.items[repo.id]={repository_id:repo.id,workspace,state:'cloning'};
        await c.persist();
      }
      try{
        if(!cloned){
          const access=await this.ticket(binding,repo,{action:'read'});
          await git(root,['clone','--single-branch','--no-recurse-submodules','--branch',repo.branch,'--',access.origin,workspace],
            {...access,support,signal:remote.lifetime?.signal});
        }
        this.guard(p,remote,binding);await safePath(workspace);
        // Framework configuration is local; do not change the business repository's .gitignore.
        const exclude=path.join(workspace,'.git','info','exclude');await safePath(exclude);
        await fs.mkdir(path.dirname(exclude),{recursive:true});
        const exclusions='\n# Agent Hub local connection\n/.mcp.json\n/.codex/config.toml\n/.cursor/mcp.json\n';
        const existing=await fs.readFile(exclude,'utf8').catch(error=>{if(error.code==='ENOENT')return '';throw error;});
        if(!existing.includes(exclusions))await fs.appendFile(exclude,exclusions);
        let child=p.bindings.find(item=>item.workspace===workspace);
        if(child && (child.repositoryId!==repo.id || child.projectId!==binding.projectId || child.status!=='active'))
          fail('ASSEMBLY_CONNECTION_UNAVAILABLE','已有目录连接与当前仓库不匹配或已停用，请手动重新接入。');
        if(!child){await c.bind({projectId:binding.projectId,repositoryId:repo.id,workspace});child=p.bindings.find(item=>item.workspace===workspace && item.repositoryId===repo.id);}
        this.guard(p,remote,binding);
        if(args.harness){
          const config=c.configuration(child).mcpServers,name=Object.keys(config)[0];
          const relative={codex:'.codex/config.toml',claude:'.mcp.json',cursor:'.cursor/mcp.json'}[args.harness];
          const tracked=(await git(workspace,['ls-files','--',relative],{support})).trim();
          if(tracked)receipt.connection_installation='manual-tracked-configuration';
          else {
            await hostInstall.install({hosts:[args.harness],workspace,name,entry:config[name],
              receiptDirectory:path.join(c.directory,'assembled-projects',child.id),guard:()=>this.guard(p,remote,binding)});
            receipt.connection_installation='installed';
          }
        }
        this.guard(p,remote,binding);
        Object.assign(receipt,{state:'ready',binding_id:child.id,repository_name:repo.name,
          baseline_commit:(await observer.initialize(workspace)).base_commit});
        delete receipt.error;
        await c.persist();c.emitState();items.push({...receipt});
      }catch(error){
        // A failed operation never recursively deletes a clone or a user's later edits.
        if(c.profile===p){receipt.state='interrupted';receipt.error=error.code || 'ASSEMBLY_INTERRUPTED';await c.persist();}
        throw error;
      }
    }
    return {project_id:binding.projectId,workspace:root,items,upstream_credentials_exposed:false,
      message:'获准仓库已拉取，各目录绑定独立仓库连接。请在相应仓库目录重新加载开发工具并完成信任确认。'};
  }
  async verifyOrigin(workspace,repo,support,base){
    await safePath(workspace);
    const actual=(await git(workspace,['config','--local','--get','remote.origin.url'],{support})).trim();
    if(![base+`/api/git/${repo.id}.git`,repo.repository_url,repo.repository_url+'.git'].includes(actual))fail('TASK_REPOSITORY_MISMATCH','本机 Git 来源与获准仓库不一致。');
    // Reject local URL rewrites/includes before issuing an authenticated operation.
    const config=await git(workspace,['config','--local','--no-includes','--name-only','--list'],{support});
    if(config.split(/\r?\n/).some(key=>/^(url\.|include\.|includeif\.|remote\..*\.(uploadpack|receivepack)$)/i.test(key)))fail('REPOSITORY_CONFIGURATION_UNSAFE','本机 Git 配置包含额外地址改写或程序入口，请先核对。');
  }
  async verifyTaskBinding(binding,task){
    if(!task.repository_id)return;
    if(binding.repositoryId!==task.repository_id)fail('TASK_REPOSITORY_MISMATCH','请在该任务所属仓库的固定连接中开始开发。');
    const registry=await this.registry(binding),repo=registry.items.find(repo=>repo.id===binding.repositoryId);
    if(!repo || !repo.capabilities.includes('code:write'))fail('REPOSITORY_NOT_ACCESSIBLE');
    await this.verifyOrigin(binding.workspace,repo,await this.support(),this.c.remote.base);
  }
  async transfer(binding,name,args){
    const c=this.c,p=c.profile,remote=c.remote,registry=await this.registry(binding);
    this.guard(p,remote,binding);
    const repo=registry.items.find(repo=>repo.id===binding.repositoryId && repo.status==='active' && repo.capabilities.includes('code:read'));
    if(!repo)fail('REPOSITORY_BINDING_REQUIRED','请使用具体仓库的固定连接。');
    const support=await this.support();await this.verifyOrigin(binding.workspace,repo,support,remote.base);
    let body={action:'read'};
    if(name==='hub_push_task'){
      if(!repo.capabilities.includes('code:write'))fail('REPOSITORY_NOT_ACCESSIBLE');
      const {task}=await c.invokeProjectRemote(binding,'hub_get_task',{task_id:args.task_id});
      await this.verifyTaskBinding(binding,task);
      const observed=await observer.initialize(binding.workspace);
      body={action:'write',...args,expected_commit:observed.base_commit};
    }else if(name!=='hub_fetch_repository')fail('UNKNOWN_BRIDGE_ACTION');
    this.guard(p,remote,binding);
    const access=await this.ticket(binding,repo,body),options={...access,support,signal:remote.lifetime?.signal};
    if(name==='hub_fetch_repository'){
      await git(binding.workspace,['fetch','--no-tags','--no-recurse-submodules','--',access.origin,repo.branch],options);
      this.guard(p,remote,binding);
      return {repository_id:repo.id,fetched:true,working_files_changed:false,
        fetched_commit:(await git(binding.workspace,['rev-parse','FETCH_HEAD'],{support})).trim()};
    }
    await git(binding.workspace,['push','--porcelain','--',access.origin,'HEAD:'+access.write_ref],options);
    this.guard(p,remote,binding);
    const verify=await this.ticket(binding,repo,{action:'read'});
    const actual=(await git(binding.workspace,['ls-remote','--',verify.origin,access.write_ref],{...verify,support,
      signal:remote.lifetime?.signal})).trim().split(/\s/)[0];
    if(actual!==body.expected_commit)fail('GIT_REMOTE_VERIFICATION_FAILED','远程提交尚未核验成功，请保留本机工作并重新查询。');
    const logs=await c.invokeProjectRemote(binding,'hub_list_worklogs',{task_id:args.task_id,limit:1});
    if(!logs.items?.[0]?.id)fail('GIT_RECORDED_CHECKPOINT_REQUIRED');
    const slot=await c.projectSlot(binding.projectId);
    const receipt=await remote.request('POST',`/api/projects/${binding.projectId}/tasks/${args.task_id}/deliveries`,
      {run_id:args.run_id,lease_epoch:args.lease_epoch,worklog_id:logs.items[0].id},{slot});
    this.guard(p,remote,binding);
    if(receipt.git_commit!==actual || receipt.ref!==access.write_ref || receipt.worklog_id!==logs.items[0].id ||
       !/^[a-f0-9]{64}$/.test(receipt.receipt_hash || ''))fail('GIT_REMOTE_VERIFICATION_FAILED');
    return {repository_id:repo.id,task_id:args.task_id,run_id:args.run_id,git_commit:actual,ref:access.write_ref,
      remote_verified:true,delivery_receipt:receipt,merged:false,deployed:false};
  }
}
module.exports={RepositoryRuntime,git,safePath};
