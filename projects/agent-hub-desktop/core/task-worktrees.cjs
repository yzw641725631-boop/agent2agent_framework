'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const observer=require('./git-observer.cjs'),hostInstall=require('./host-install.cjs');
const {directory}=require('./workspaces.cjs'),{HubError}=require('./remote.cjs');
const fail=(code,message)=>{throw new HubError(code,message);};

async function create(runtime,binding,args){
  const {git,safePath}=require('./repository-runtime.cjs');
  const c=runtime.c,p=c.profile,remote=c.remote;
  const guard=()=>runtime.guard(p,remote,binding);
  if(!binding.repositoryId || binding.taskId)fail('TASK_WORKTREE_SOURCE_REQUIRED','请从原仓库连接创建独立任务目录。');
  const {task}=await c.invokeProjectRemote(binding,'hub_get_task',{task_id:args.task_id});
  await runtime.verifyTaskBinding(binding,task);guard();
  if(task.execution_mode!=='observed')fail('TASK_WORKTREE_CODE_REQUIRED');
  const lease={task_id:args.task_id,run_id:args.run_id,lease_epoch:args.lease_epoch};
  await c.invokeProjectRemote(binding,'hub_heartbeat_task',{...lease,operation_id:crypto.randomUUID()});guard();
  const source=await directory(binding.workspace);await safePath(source);
  const head=(await observer.repository(source)).head,support=await runtime.support();
  const settings=await git(source,['config','--no-includes','--name-only','--list'],{support});
  if(settings.split(/\r?\n/).some(key=>/^filter\..*\.(smudge|process)$/i.test(key)))
    fail('WORKTREE_FILTER_UNSUPPORTED','此仓库配置了检出过滤程序。请人工创建并检查工作目录后接入，受管创建不会执行这些程序。');
  const common=await fs.realpath(path.resolve(source,(await git(source,['rev-parse','--git-common-dir'],{support})).trim()));
  await safePath(common);
  const operations=p.worktreeOperations??={},key=JSON.stringify({binding:binding.id,...lease,harness:args.harness || null});
  let op=operations[args.operation_id];
  if(op && op.key!==key)fail('IDEMPOTENCY_PAYLOAD_CHANGED');
  if(Object.entries(operations).some(([id,other])=>id!==args.operation_id && other.run_id===args.run_id))
    fail('TASK_WORKTREE_EXISTS','本执行实例已有工作目录，请使用原操作 ID 重试或进入已有连接。');
  const prepared=p.workRuns?.[args.run_id];
  if(prepared && prepared.binding_id!==op?.binding_id)fail('TASK_WORKTREE_ALREADY_PREPARED','此运行已在另一个目录准备，请先保存、释放，再用新认领创建独立目录。');
  const parent=path.join(path.dirname(source),'.agent-hub-worktrees');
  const workspace=path.join(parent,args.task_id,args.run_id),branch=`hub/${args.task_id}/${args.run_id}`;
  await safePath(workspace);
  if(!op){
    if(await fs.lstat(workspace).catch(error=>{if(error.code==='ENOENT')return null;throw error;}))
      fail('WORKTREE_DIRECTORY_EXISTS','任务目录已存在，原内容已保留，请核对后重新接入。');
    op=operations[args.operation_id]={key,source,workspace,common,branch,base_commit:head,run_id:args.run_id,state:'reserved'};
    await c.persist();
    await fs.mkdir(path.dirname(workspace),{recursive:true,mode:0o700});await safePath(workspace);
    // Exclusive reservation; retries only resume a directory recorded by this operation.
    await fs.mkdir(workspace,{mode:0o700});
    op.directory_reserved=true;await c.persist();
  }
  if(op.source!==source || op.workspace!==workspace || op.common!==common || op.branch!==branch)fail('WORKSPACE_CHANGED');
  if(!op.directory_reserved)fail('WORKTREE_RECOVERY_REQUIRED');
  try{
    const marker=await fs.lstat(path.join(workspace,'.git')).catch(error=>{if(error.code==='ENOENT')return null;throw error;});
    if(marker){
      if(marker.isSymbolicLink() || !marker.isFile())fail('WORKTREE_RECOVERY_REQUIRED');
      const actual=await fs.realpath(path.resolve(workspace,(await git(workspace,['rev-parse','--git-common-dir'],{support})).trim()));
      const current=(await git(workspace,['symbolic-ref','--quiet','--short','HEAD'],{support})).trim();
      if(actual!==common || current!==branch)fail('WORKTREE_RECOVERY_REQUIRED');
    }else{
      if(op.state==='ready')fail('WORKTREE_RECOVERY_REQUIRED');
      await fs.mkdir(workspace,{mode:0o700}).catch(error=>{if(error.code!=='EEXIST')throw error;});
      if((await fs.readdir(workspace)).length)fail('WORKTREE_RECOVERY_REQUIRED','上次创建的目录有其他文件，已保留，自动恢复不会覆盖它们。');
      guard();await safePath(workspace);
      await git(source,['worktree','add','-b',branch,'--',workspace,op.base_commit],{support,signal:remote.lifetime?.signal});
    }
    guard();await safePath(workspace);
    let child=p.bindings.find(b=>b.workspace===workspace);
    if(child && (child.projectId!==binding.projectId || child.repositoryId!==binding.repositoryId || child.status!=='active' ||
        child.taskId && (child.taskId!==args.task_id || child.worktreeRunId!==args.run_id)))fail('WORKTREE_CONNECTION_UNAVAILABLE');
    if(op.state==='ready' && (!child || child.id!==op.binding_id))fail('WORKTREE_CONNECTION_UNAVAILABLE');
    if(!child){await c.bind({projectId:binding.projectId,repositoryId:binding.repositoryId,workspace});
      child=p.bindings.find(b=>b.workspace===workspace);}
    if(!child)fail('WORKTREE_CONNECTION_UNAVAILABLE');
    Object.assign(child,{taskId:args.task_id,worktreeRunId:args.run_id,worktreeSource:source,initial_commit:op.base_commit});
    op.binding_id=child.id;await c.persist();guard();
    await runtime.verifyOrigin(workspace,{id:binding.repositoryId,
      ...(await runtime.registry(binding)).items.find(r=>r.id===binding.repositoryId)},support,remote.base);
    if(args.harness){
      const config=c.configuration(child).mcpServers,name=Object.keys(config)[0];
      const relative={codex:'.codex/config.toml',claude:'.mcp.json',cursor:'.cursor/mcp.json'}[args.harness];
      if((await git(workspace,['ls-files','--',relative],{support})).trim())op.connection_installation='manual-tracked-configuration';
      else{
        await hostInstall.install({hosts:[args.harness],workspace,name,entry:config[name],
          receiptDirectory:path.join(c.directory,'task-worktrees',child.id),guard});
        op.connection_installation='installed';
      }
    }
    await c.invokeProjectRemote(binding,'hub_heartbeat_task',{...lease,operation_id:crypto.randomUUID()});guard();
    op.state='ready';delete op.error;await c.persist();c.emitState();
    return {task_id:args.task_id,run_id:args.run_id,workspace,binding_id:child.id,branch,baseline_commit:op.base_commit,
      connection_installation:op.connection_installation || 'manual',configuration:c.configuration(child),
      original_working_files_changed:false,git_objects_shared:true,code_confidentiality_boundary:false,agent_started:false,
      message:'请在此独立目录重新加载开发工具、信任工程，再用任务连接准备和确认开工。原目录工作与新目录均保留，撤权只按收据清理框架文件。'};
  }catch(error){if(c.profile===p){op.state='interrupted';op.error=error.code || 'WORKTREE_INTERRUPTED';await c.persist();}throw error;}
}
module.exports={create};
