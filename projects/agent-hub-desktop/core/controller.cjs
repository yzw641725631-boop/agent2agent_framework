'use strict';
const {EventEmitter}=require('node:events');
const crypto=require('node:crypto');
const fs=require('node:fs/promises');
const path=require('node:path');
const {Remote,HubError,origin,identity,parseEvents}=require('./remote.cjs');
const {Broker,equalSecret}=require('./broker.cjs');
const {dispatch:dispatchMcp}=require('./mcp.cjs');
const workspaces=require('./workspaces.cjs');
const {Management}=require('./management.cjs');
const {SetupConnection}=require('./setup-connection.cjs');
const {WorkRuntime}=require('./work-runtime.cjs');
const {RepositoryRuntime}=require('./repository-runtime.cjs');
const {version}=require('../package.json');

const stopped=new Set(['PROJECT_ACCESS_REVOKED','ACCOUNT_INACTIVE','DEVICE_NOT_ALLOWED','SESSION_REVOKED',
  'REFRESH_REPLAY_FAMILY_REVOKED','PROJECT_NOT_ACCESSIBLE','LOGIN_REQUIRED','INVALID_SESSION',
  'SESSION_EXPIRED_OR_REVOKED','DEVICE_APPROVAL_REQUIRED','EMAIL_VERIFICATION_REQUIRED','DEVICE_KEY_MISMATCH']);
const identityStopped=new Set(['ACCOUNT_INACTIVE','DEVICE_NOT_ALLOWED','DEVICE_APPROVAL_REQUIRED',
  'EMAIL_VERIFICATION_REQUIRED','DEVICE_KEY_MISMATCH','SESSION_REVOKED']);
const transient=new Set(['NETWORK_UNAVAILABLE','UNAVAILABLE','INVALID_RESPONSE','INVALID_EVENT_STREAM']);
const uuid=value=>typeof value==='string' && /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
class Controller extends EventEmitter {
  constructor({vault,directory,executable,bridgeScript,transport,updater,recoveryDelay=5000}) {
    super(); Object.assign(this,{vault,directory,executable,bridgeScript,transport,updater,recoveryDelay});
    this.projects=[]; this.grants=[]; this.watches=new Map(); this.connection='signed_out'; this.message='';
    this.issuance=new Map();this.projectOperations=new Set();this.closed=false;this.draining=false;
    this.broker=new Broker(directory,request=>this.dispatch(request));
    this.management=new Management(this);
    this.setup=new SetupConnection(this);
    this.work=new WorkRuntime(this);
    this.repositories=new RepositoryRuntime(this);
  }
  get profile(){return this.vault.data.profiles[this.vault.data.current] || null;}
  persist(){return this.vault.save();}
  emitState(){this.emit('state',this.state());}
  ensureCurrent(profile,remote){
    if(this.closed || this.profile!==profile || this.remote!==remote)throw new HubError('LOGIN_REQUIRED');
  }
  cancelWatch(projectId){this.watches.get(projectId)?.abort();this.watches.delete(projectId);}
  cancelRecovery(){clearTimeout(this.recoveryTimer);this.recoveryTimer=null;}
  scheduleRecovery(){
    if(this.closed || this.recoveryTimer || !this.profile?.sessions.platform)return;
    const remote=this.remote;
    this.recoveryTimer=setTimeout(()=>{
      this.recoveryTimer=null;
      if(!this.closed && this.remote===remote)this.refresh().catch(()=>{});
    },this.recoveryDelay);
    this.recoveryTimer.unref?.();
  }
  async failure(error,{profile=this.profile,remote=this.remote,projectId,platform=false}={}){
    if(this.closed || this.profile!==profile || this.remote!==remote)return;
    const terminal=stopped.has(error.code);
    if(!terminal && !transient.has(error.code))return;
    if(identityStopped.has(error.code) || (platform && terminal)){
      this.connection='reauth_required';this.message=error.message;this.stopWatches();this.cancelRecovery();
      for(const b of profile.bindings){b.status='blocked';b.message=error.message;b.errorCode=error.code;}
    }else if(projectId){
      if(terminal)profile.events=(profile.events || []).filter(event=>event.project_id!==projectId);
      for(const b of profile.bindings.filter(b=>b.projectId===projectId)){
        b.status=terminal?'blocked':'offline';b.message=error.message || '进度连接中断，正在重连。';b.errorCode=error.code;
      }
    }else if(transient.has(error.code)){
      this.connection='offline';this.message=error.message;
      for(const b of profile.bindings.filter(b=>b.status!=='blocked')){b.status='offline';b.message=error.message;}
      this.scheduleRecovery();
    }
    await this.persist();this.emitState();
  }
  state(){
    const p=this.profile;
    const visible=['active','offline'].includes(this.connection);
    return {connection:this.connection,message:this.message,origin:p?.origin || 'https://47.109.23.86:18180',
      login:p?.login || '',user:visible?p?.user || null:null,deviceId:p?.enrollment?.device_id || p?.deviceId || null,
      emailRequired:!!p?.enrollment?.email_verification_required,approval:p?.enrollment?.device_status || null,
      deviceApprovalMode:p?.enrollment?.device_approval_mode || 'superadmin',
      managementEnabled:visible && !!p?.management,
      setupEnabled:!!this.setup.binding,
      setupAccountMatches:visible && !!this.setup.binding && !!this.setup.matches(this.setup.binding),
      projects:visible?this.projects:[],bindings:(visible?p?.bindings || []:[]).map(b=>({id:b.id,projectId:b.projectId,projectName:b.projectName,
        repositoryId:b.repositoryId || null,repositoryName:b.repositoryName || null,
        workspace:b.workspace,status:b.status,message:b.message || '',configuration:this.configuration(b),
        installed:b.installed || null})),
      events:(visible?p?.events || []:[]).slice(-100).reverse(),
      workRuns:visible?Object.values(p?.workRuns || {}).map(r=>({task_id:r.task_id,run_id:r.run_id,binding_id:r.binding_id,
        acknowledged:r.acknowledged,policy_version:r.policy.version,stopped:r.stopped || null,
        observation_error:r.observation_error || null,last_activity:r.last_activity})):[],version};
  }
  async start(){
    await this.broker.start();
    await this.setup.restore();
    this.work.start();
    const p=this.profile;
    if(!p)return this.state();
    this.remote=new Remote(p,()=>this.persist(),this.transport);
    try{
      await this.remote.init();
      if(p.sessions.platform){await this.loadAccount();await this.restoreBindings();}
      else this.connection=p.enrollment?'pending':'signed_out';
    }catch(error){
      this.connection=transient.has(error.code)?'offline':'reauth_required';this.message=error.message;
      if(p.sessions.platform)await this.failure(error,{platform:true});
    }
    return this.state();
  }
  async login(data){
    if(['active','offline'].includes(this.connection) && this.profile?.sessions.platform)throw new HubError('ALREADY_LOGGED_IN','请先退出当前账号再切换账号。');
    if(typeof data.login!=='string' || !/^[a-z0-9][a-z0-9_.-]{2,79}$/.test(data.login) ||
       typeof data.password!=='string' || data.password.length>256 || !data.password ||
       typeof data.deviceName!=='string' || !data.deviceName.trim() || data.deviceName.length>100)throw new HubError('INVALID_REQUEST');
    const base=origin(data.origin),key=base+'|'+data.login;
    const p=this.vault.data.profiles[key] || {origin:base,login:data.login,identity:identity(),sessions:{},bindings:[],events:[],cursors:{}};
    this.stopWatches();this.cancelRecovery();this.remote?.close();this.accountRefresh=null;this.issuance.clear();
    const remote=new Remote(p,()=>this.persist(),this.transport);
    // Persist the identity before enrollment: a lost response must not create a new device on retry.
    this.vault.data.profiles[key]=p;this.vault.data.current=key;this.remote=remote;
    this.projects=[];this.grants=[];this.connection='signed_out';await this.persist();
    await remote.init();
    const enrollment=await remote.raw('POST','/api/auth/enrollments',{
      login:data.login,password:data.password,device_name:data.deviceName.trim(),os:process.platform==='darwin'?'macos':'windows',
    });
    p.enrollment=enrollment;p.sessions={};delete p.challengeId;
    this.vault.data.profiles[key]=p;this.vault.data.current=key;this.remote=remote;
    await this.persist();this.connection='pending';this.message='';
    if(enrollment.device_status==='approved' && !enrollment.email_verification_required)await this.finishLogin();
    this.emitState();return this.state();
  }
  async sendEmail(){
    if(!this.profile?.enrollment)throw new HubError('LOGIN_REQUIRED');
    const result=await this.remote.raw('POST','/api/devices/email-challenges',{},this.profile.enrollment.enrollment_token);
    this.profile.challengeId=result.challenge_id;await this.persist();return {sent:true};
  }
  async verifyEmail(code){
    const p=this.profile;
    if(!p?.challengeId || typeof code!=='string' || !/^\d{8}$/.test(code))throw new HubError('INVALID_REQUEST','请先发送验证码，再输入八位数字。');
    const result=await this.remote.raw('POST','/api/devices/email-verifications',{
      challenge_id:p.challengeId,code,
    },p.enrollment.enrollment_token);
    p.enrollment.email_verification_required=false;
    if(!result.device_approval_required){
      p.enrollment.device_status='approved';await this.finishLogin();
    }
    await this.persist();this.emitState();return this.state();
  }
  async checkApproval(){
    const p=this.profile;
    if(!p?.enrollment)throw new HubError('LOGIN_REQUIRED');
    const current=await this.remote.raw('GET','/api/devices/enrollment',undefined,p.enrollment.enrollment_token);
    p.enrollment.device_status=current.status;p.enrollment.email_verification_required=current.email_verification_required;
    if(current.device_approval_mode)p.enrollment.device_approval_mode=current.device_approval_mode;
    if(current.status==='approved' && !current.email_verification_required)await this.finishLogin();
    await this.persist();this.emitState();return this.state();
  }
  async finishLogin(){
    const p=this.profile;
    p.sessions.platform=await this.remote.raw('POST','/api/auth/sessions',{},p.enrollment.enrollment_token);
    p.deviceId=p.enrollment.device_id;await this.persist();
    await this.refresh();
  }
  async loadAccount(profile=this.profile,remote=this.remote){
    const me=await remote.request('GET','/api/me');
    const grants=(await remote.request('GET','/api/me/grants')).items;
    const projects=me.user.platform_role==='superadmin'?(await remote.request('GET','/api/projects')).items:
      Array.from(new Map(grants.map(g=>[g.project_id,{id:g.project_id,name:g.project_name,slug:g.project_slug}])).values());
    this.ensureCurrent(profile,remote);
    profile.user=me.user;profile.deviceId=me.device_id;this.grants=grants;this.projects=projects;
    if(me.user.platform_role!=='superadmin'){
      profile.events=(profile.events || []).filter(event=>grants.some(grant=>grant.project_id===event.project_id &&
        (!event.repository_id || (grant.repository_permissions || []).some(permission=>permission.repository_id===event.repository_id && permission.capabilities.includes('code:read')))));
    }
    for(const [slot,session] of Object.entries(profile.sessions)){
      if(slot!=='platform' && !grants.some(g=>g.project_id===slot && g.id===session.grant_id)){
        this.cancelWatch(slot);delete profile.sessions[slot];
      }
    }
    this.connection='active';this.message='';await this.persist();
    await this.setup.claim();
  }
  async refresh(){
    if(!this.remote || !this.profile?.sessions.platform)throw new HubError('LOGIN_REQUIRED');
    if(this.accountRefresh)return this.accountRefresh;
    const profile=this.profile,remote=this.remote;
    const action=(async()=>{
      try{
        await remote.init();await this.loadAccount(profile,remote);await this.restoreBindings();
        this.ensureCurrent(profile,remote);this.cancelRecovery();
        if(profile.bindings.some(b=>b.status==='offline'))this.scheduleRecovery();
        this.emitState();return this.state();
      }catch(error){await this.failure(error,{profile,remote,platform:true});throw error;}
    })();
    this.accountRefresh=action;
    try{return await action;}finally{if(this.accountRefresh===action)this.accountRefresh=null;}
  }
  async projectSlot(projectId){
    if(!this.profile?.sessions.platform)throw new HubError('LOGIN_REQUIRED');
    if(this.profile.user?.platform_role==='superadmin')return 'platform';
    if(!this.profile.sessions[projectId]){
      if(this.issuance.has(projectId)){await this.issuance.get(projectId);return projectId;}
      const profile=this.profile,remote=this.remote;
      const grant=this.grants.find(g=>g.project_id===projectId);
      if(!grant)throw new HubError('PROJECT_NOT_ACCESSIBLE');
      const action=(async()=>{
        const session=await remote.request('POST','/api/auth/project-sessions',{grant_id:grant.id});
        this.ensureCurrent(profile,remote);
        if(!this.grants.some(g=>g.id===grant.id && g.project_id===projectId))throw new HubError('PROJECT_ACCESS_REVOKED');
        profile.sessions[projectId]=session;await this.persist();
      })();
      this.issuance.set(projectId,action);
      try{await action;}finally{if(this.issuance.get(projectId)===action)this.issuance.delete(projectId);}
    }
    return projectId;
  }
  async context(projectId){
    const profile=this.profile,remote=this.remote;
    const slot=await this.projectSlot(projectId);
    this.ensureCurrent(profile,remote);
    const result=await remote.request('GET',`/api/projects/${projectId}/context`,undefined,{slot});
    this.ensureCurrent(profile,remote);
    if(result.resource_visibility && this.applyResourceVisibility(projectId,result.resource_visibility)){
      await this.persist();this.ensureCurrent(profile,remote);this.emitState();
    }
    return {...result,capabilities:profile.user.platform_role==='superadmin' && !result.capabilities.length?['project:read','tasks:write','documents:read']:result.capabilities};
  }
  applyResourceVisibility(projectId,visibility){
    if(!visibility || !Array.isArray(visibility.code_read_ids) || !Array.isArray(visibility.document_read_ids))return;
    const p=this.profile;if(!p)return;
    let changed=JSON.stringify(p.resourceVisibility?.[projectId])!==JSON.stringify(visibility);
    const count=(p.events || []).length;
    (p.resourceVisibility??={})[projectId]=visibility;
    p.events=(p.events || []).filter(event=>event.project_id!==projectId || visibility.all_repositories ||
      (event.repository_id?visibility.code_read_ids.includes(event.repository_id):visibility.unscoped));
    for(const binding of p.bindings.filter(binding=>binding.projectId===projectId && binding.repositoryId)){
      if(!visibility.code_read_ids.includes(binding.repositoryId) && !visibility.document_read_ids.includes(binding.repositoryId)){
        if(binding.status!=='blocked' || binding.errorCode!=='REPOSITORY_NOT_ACCESSIBLE')changed=true;
        binding.status='blocked';binding.errorCode='REPOSITORY_NOT_ACCESSIBLE';binding.message='该仓库的授权已失效，请保留未交付工作。';
      }
    }
    return changed || count!==p.events.length;
  }
  descriptorPath(binding){return path.join(this.directory,'bindings',binding.id+'.json');}
  configuration(binding){
    return {mcpServers:{[(binding.kind==='management'?'agent-hub-management-':'agent-hub-')+binding.id.slice(0,8)]:{
      type:'stdio',command:this.executable,args:[this.bridgeScript,'--binding',this.descriptorPath(binding)],
      env:{ELECTRON_RUN_AS_NODE:'1'},
    }}};
  }
  async writeDescriptor(binding){
    const target=this.descriptorPath(binding);
    await fs.mkdir(path.dirname(target),{recursive:true,mode:0o700});
    const descriptor={version:1,protocol:'desktop-mcp-v1',endpoint:this.broker.endpoint,binding:binding.id,secret:binding.secret};
    await fs.writeFile(target+'.next',JSON.stringify(descriptor),{mode:0o600});await fs.rename(target+'.next',target);
  }
  async bind({projectId,workspace,repositoryId}){
    if(!uuid(projectId) || !this.projects.some(p=>p.id===projectId))throw new HubError('PROJECT_NOT_ACCESSIBLE');
    const actual=await workspaces.directory(workspace);
    const p=this.profile;
    if(p.bindings.some(b=>b.workspace===actual))throw new HubError('WORKSPACE_ALREADY_BOUND','这个目录已绑定连接，请先停用原连接。');
    const context=await this.context(projectId);
    let repository;
    if(repositoryId){
      if(!uuid(repositoryId))throw new HubError('INVALID_REQUEST');
      const slot=await this.projectSlot(projectId),profile=this.profile,remote=this.remote;
      const registry=await remote.request('GET',`/api/projects/${projectId}/repositories`,undefined,{slot});
      this.ensureCurrent(profile,remote);
      repository=registry.items.find(repo=>repo.id===repositoryId && repo.status==='active' && repo.capabilities.some(cap=>['code:read','documents:read'].includes(cap)));
      if(!repository)throw new HubError('REPOSITORY_NOT_ACCESSIBLE');
    }
    const binding={id:crypto.randomUUID(),secret:crypto.randomBytes(32).toString('base64url'),projectId,
      ...(repository?{repositoryId,repositoryName:repository.name}:{}),
      projectName:context.name,workspace:actual,status:'active'};
    p.bindings.push(binding);await this.writeDescriptor(binding);await this.persist();
    this.watch(projectId);this.emitState();return this.state();
  }
  async restoreBindings(){
    const profile=this.profile,remote=this.remote;
    await this.management.restore();
    for(const binding of profile.bindings){
      try{
        const context=await this.context(binding.projectId);this.ensureCurrent(profile,remote);
        if(binding.repositoryId && context.resource_visibility &&
          !context.resource_visibility.code_read_ids.includes(binding.repositoryId) && !context.resource_visibility.document_read_ids.includes(binding.repositoryId))
          throw new HubError('REPOSITORY_NOT_ACCESSIBLE');
        binding.status='active';binding.message='';delete binding.errorCode;
        await this.writeDescriptor(binding);this.watch(binding.projectId);
      }catch(error){
        this.ensureCurrent(profile,remote);
        if(stopped.has(error.code))this.cancelWatch(binding.projectId);
        const blocked=stopped.has(error.code) || error.code==='REPOSITORY_NOT_ACCESSIBLE';
        binding.status=blocked?'blocked':'offline';binding.message=error.message;binding.errorCode=error.code;
        if(!blocked)this.scheduleRecovery();
      }
    }
    await this.persist();
  }
  binding(id){const binding=this.profile?.bindings.find(b=>b.id===id);if(!binding)throw new HubError('BINDING_NOT_FOUND');return binding;}
  async disconnect(id){
    const binding=this.binding(id);await fs.unlink(this.descriptorPath(binding)).catch(e=>{if(e.code!=='ENOENT')throw e;});
    this.profile.bindings=this.profile.bindings.filter(b=>b.id!==id);
    if(!this.profile.bindings.some(b=>b.projectId===binding.projectId)){
      this.watches.get(binding.projectId)?.abort();this.watches.delete(binding.projectId);
    }
    await this.persist();this.emitState();return this.state();
  }
  async configurationPreview(id){
    const b=this.binding(id);const configuration=this.configuration(b).mcpServers,name=Object.keys(configuration)[0];
    const result=await workspaces.preview(b.workspace,name,configuration[name],{entry:b.installed?.entry,fileHash:b.installed?.installedHash});
    return {target:result.target,previousHash:result.previousHash,previous:result.previous,next:result.next};
  }
  async installConfiguration({id,expectedHash}){
    const b=this.binding(id);const config=this.configuration(b).mcpServers,name=Object.keys(config)[0];
    b.installed=await workspaces.apply(b.workspace,name,config[name],expectedHash,{entry:b.installed?.entry,fileHash:b.installed?.installedHash});
    await this.persist();this.emitState();return b.installed;
  }
  async dispatch(request){
    if(!request || !uuid(request.binding) || !request.args || Array.isArray(request.args) || typeof request.args!=='object')throw new HubError('INVALID_BRIDGE_REQUEST');
    if(this.setup.owns(request.binding))return this.setup.dispatch(request);
    if(this.management.owns(request.binding))return this.management.dispatch(request);
    const b=this.binding(request.binding);
    if(!equalSecret(request.secret,b.secret))throw new HubError('BRIDGE_AUTHENTICATION_FAILED','本机连接认证失败。');
    const checkVersion=this.updater ? ()=>this.updater.check({fresh:true}) : undefined;
    if(request.action==='mcp')return dispatchMcp(
      request.args.request,(action,args)=>this.invokeProject(b,action,args),checkVersion,{omitToolText:true}
    );
    if(request.action==='hub_check_version' && checkVersion && !Object.keys(request.args).length)return checkVersion();
    return this.invokeProject(b,request.action,request.args);
  }
  async invokeProject(b,action,args){
    if(['hub_assemble_project','hub_fetch_repository','hub_push_task'].includes(action)){
      const operation=this.repositories.invoke(b,action,args);
      this.projectOperations.add(operation);
      try{return await operation;}finally{this.projectOperations.delete(operation);}
    }
    const localActions={hub_prepare_work:'prepare',hub_acknowledge_work:'acknowledge',hub_check_changes:'check',
      hub_list_local_work:'list'};
    const prepared=args?.run_id && this.profile?.workRuns?.[args.run_id];
    const method=localActions[action] || (prepared && action==='hub_write_worklog'?'save':
      prepared && action==='hub_submit_task'?'submit':null);
    if(method){
      if(this.closed || this.binding(b.id)!==b || !this.profile?.sessions.platform || this.connection==='signed_out')throw new HubError('LOGIN_REQUIRED');
      require('./task-tools.cjs').validate(action,args);
      const operation=this.work[method](b,args);
      this.projectOperations.add(operation);
      try{return await operation;}finally{this.projectOperations.delete(operation);}
    }
    const result=await this.invokeProjectRemote(b,action,args);
    if(action==='hub_release_task' && prepared){prepared.stopped='released';await this.persist();}
    return result;
  }
  async invokeProjectRemote(b,action,args,{internal=false}={}){
    if(this.draining)throw new HubError('CLIENT_UPDATE_REQUIRED','桌面端正在切换版本，请等待重启。');
    if(this.updater && action!=='metadata'){
      const update=await this.updater.check();
      if(!update.can_continue)throw new HubError(update.code,update.message,{update});
    }
    if(this.draining)throw new HubError('CLIENT_UPDATE_REQUIRED','桌面端正在切换版本，请等待重启。');
    if(this.closed || !this.profile?.sessions.platform || !['active','offline'].includes(this.connection))throw new HubError('LOGIN_REQUIRED');
    if(this.connection==='offline')throw new HubError('NETWORK_UNAVAILABLE','服务器连接正在恢复，请稍后重试。');
    if(b.status==='blocked')throw new HubError(b.errorCode || 'PROJECT_ACCESS_REVOKED');
    if(this.binding(b.id)!==b)throw new HubError('LOGIN_REQUIRED');
    const operation=this.projectRequest(b,action,args,{internal});
    this.projectOperations.add(operation);
    try{return await operation;}finally{this.projectOperations.delete(operation);}
  }
  async drainProjectOperations(){
    this.draining=true;
    await Promise.allSettled(this.projectOperations);
    await this.work.drain();
  }
  async projectRequest(b,action,args,{internal=false}={}){
    const profile=this.profile,remote=this.remote;
    try{
      const context=await this.context(b.projectId);
      this.ensureCurrent(profile,remote);
      if(this.connection!=='active' || this.binding(b.id)!==b)throw new HubError('LOGIN_REQUIRED');
      if(b.status==='blocked')throw new HubError(b.errorCode || 'PROJECT_ACCESS_REVOKED');
      const slot=await this.projectSlot(b.projectId);
      this.ensureCurrent(profile,remote);
      if(action==='metadata')return {project_id:b.projectId,capabilities:context.capabilities,repository_id:b.repositoryId || null,
        repository_code_read:b.repositoryId?context.resource_visibility?.code_read_ids.includes(b.repositoryId):
          !!context.resource_visibility?.code_read_ids.length,
        repository_code_write:!!b.repositoryId && !!context.resource_visibility?.code_write_ids.includes(b.repositoryId)};
      if(action==='hub_get_project'){
        if(Object.keys(args).length)throw new HubError('INVALID_REQUEST');
        return {...context,workspace:b.workspace,repository_id:b.repositoryId || null};
      }
      if(['hub_list_repositories','hub_list_document_sources'].includes(action)){
        require('./repository-tools.cjs').validate(action,args);
        return await remote.request('GET',`/api/projects/${b.projectId}/${action==='hub_list_repositories'?'repositories':'documents/sources'}`,undefined,{slot});
      }
      if(action==='hub_register_repository'){
        require('./repository-tools.cjs').validate(action,args);
        if(!context.capabilities.includes('repositories:manage'))throw new HubError('CAPABILITY_REQUIRED');
        const {operation_id,...body}=args;
        return await remote.request('POST',`/api/projects/${b.projectId}/repositories`,body,{slot,idempotency:operation_id});
      }
      const taskRequest=require('./task-tools.cjs').request(action,args,context.capabilities,b.projectId,{internal});
      if(taskRequest){
        if(b.repositoryId){
          if(args.task_id){
            const {task}=await remote.request('GET',`/api/projects/${b.projectId}/tasks/${args.task_id}`,undefined,{slot});
            if(task.repository_id!==b.repositoryId)throw new HubError('TASK_REPOSITORY_MISMATCH');
          }
          if(action==='hub_create_task'){
            if(args.repository_id && args.repository_id!==b.repositoryId)throw new HubError('TASK_REPOSITORY_MISMATCH');
            taskRequest.body.repository_id=b.repositoryId;
          }
          if(action==='hub_list_tasks')taskRequest.path+=(taskRequest.path.includes('?')?'&':'?')+'repository_id='+b.repositoryId;
        }
        const result=await remote.request(taskRequest.method,taskRequest.path,taskRequest.body,
          {slot,idempotency:taskRequest.operation_id});
        this.ensureCurrent(profile,remote);
        return result;
      }
      if(action==='hub_list_progress'){
        if(Object.keys(args).some(k=>!['after','limit'].includes(k)) ||
           (args.after!==undefined && (typeof args.after!=='string' || !/^\d{1,19}$/.test(args.after) || BigInt(args.after)>9223372036854775807n)) ||
           (args.limit!==undefined && (!Number.isInteger(args.limit) || args.limit<1 || args.limit>100)))throw new HubError('INVALID_REQUEST');
        return await remote.request('GET',`/api/projects/${b.projectId}/progress?after=${args.after || '0'}&limit=${args.limit || 50}`,undefined,{slot});
      }
      if(['hub_get_document_index','hub_search_documents','hub_read_document'].includes(action)){
        if(!context.capabilities.includes('documents:read'))throw new HubError('CAPABILITY_REQUIRED','当前项目授权不包含文档读取。');
        const repositoryId=args.repository_id || b.repositoryId;
        if(repositoryId && (!uuid(repositoryId) || b.repositoryId && repositoryId!==b.repositoryId))throw new HubError('DOCUMENT_SOURCE_NOT_ACCESSIBLE');
        if(!repositoryId && context.repository_mode==='scoped')throw new HubError('DOCUMENT_SOURCE_REQUIRED','请先列出文档源，再指定 repository_id。');
        const base=`/api/projects/${b.projectId}${repositoryId?'/repositories/'+repositoryId:''}/documents`;
        if(action==='hub_get_document_index'){
          if(Object.keys(args).some(key=>key!=='repository_id'))throw new HubError('INVALID_REQUEST');
          return await remote.request('GET',`${base}/repository`,undefined,{slot});
        }
        const search=action==='hub_search_documents',allowed=search?['query','offset','limit','snapshot_id','repository_id']:['path','snapshot_id','repository_id'];
        if(Object.keys(args).some(k=>!allowed.includes(k)) ||
           (args.snapshot_id!==undefined && !uuid(args.snapshot_id)) ||
           (search && ((args.query!==undefined && (typeof args.query!=='string' || args.query.length>200)) ||
             (args.offset!==undefined && (!Number.isInteger(args.offset) || args.offset<0 || args.offset>1000)) ||
             (args.limit!==undefined && (!Number.isInteger(args.limit) || args.limit<1 || args.limit>50)))) ||
           (!search && (typeof args.path!=='string' || !args.path.length || args.path.length>800)))throw new HubError('INVALID_REQUEST');
        const {repository_id,...body}=args;
        return await remote.request('POST',`${base}/${search?'search':'read'}`,body,{slot});
      }
      if(action==='hub_report_progress'){
        if(!context.capabilities.includes('tasks:write'))throw new HubError('CAPABILITY_REQUIRED','当前项目授权不包含进度上报，读取连接仍可使用。');
        const {operation_id,...body}=args;
        if(b.repositoryId){
          if(body.repository_id && body.repository_id!==b.repositoryId)throw new HubError('TASK_REPOSITORY_MISMATCH');
          body.repository_id=b.repositoryId;
        }
        if(!uuid(operation_id))throw new HubError('INVALID_REQUEST','上报进度需要稳定的 operation_id（UUID）。');
        const result=await remote.request('POST',`/api/projects/${b.projectId}/progress`,body,{slot,idempotency:operation_id});
        return {...result,operation_id};
      }
      throw new HubError('UNKNOWN_BRIDGE_ACTION');
    }catch(error){
      await this.failure(error,{profile,remote,projectId:b.projectId});
      throw error;
    }
  }
  stopWatches(){for(const controller of this.watches.values())controller.abort();this.watches.clear();}
  watch(projectId){
    if(this.watches.has(projectId))return;
    const controller=new AbortController();this.watches.set(projectId,controller);
    this.stream(projectId,controller.signal).catch(()=>{}).finally(()=>{
      if(this.watches.get(projectId)===controller)this.watches.delete(projectId);
    });
  }
  async stream(projectId,signal){
    const profile=this.profile,remote=this.remote;
    let backoff=1000,caughtUp=false;
    while(!signal.aborted && this.profile===profile){
      let idleTimer;
      try{
        const slot=await this.projectSlot(projectId),after=profile.cursors[projectId] || '0';
        if(signal.aborted || this.closed || this.remote!==remote)return;
        const idle=new AbortController();
        const activity=()=>{clearTimeout(idleTimer);idleTimer=setTimeout(()=>idle.abort(new HubError('NETWORK_UNAVAILABLE','进度通道超时，正在重连。')),30000);};
        activity();
        const connectionSignal=AbortSignal.any([signal,idle.signal]);
        const response=await remote.request('GET',`/api/projects/${projectId}/progress/stream?after=${after}`,undefined,{slot,stream:true,signal:connectionSignal});
        let ready=false;
        for await(const event of parseEvents(response.body,{onActivity:activity})){
          if(signal.aborted || this.closed || this.profile!==profile || this.remote!==remote)return;
          if(event.event==='resource_visibility'){
            this.applyResourceVisibility(projectId,event.data);await this.persist();this.emitState();continue;
          }
          if(event.event==='ready'){
            ready=true;caughtUp=true;backoff=1000;
            let changed=false;
            for(const b of profile.bindings.filter(b=>b.projectId===projectId && b.status!=='blocked')){
              if(b.status!=='active' || b.message){b.status='active';b.message='';delete b.errorCode;changed=true;}
            }
            if(changed){await this.persist();this.emitState();}
            continue;
          }
          if(event.event==='access_status'){
            if(event.data.code==='SESSION_EXPIRED_OR_REVOKED')await remote.refresh(slot);
            else throw new HubError(event.data.code);
            break;
          }
          if(event.event!=='progress' || event.data.project_id!==projectId || !/^\d{1,19}$/.test(event.id || ''))continue;
          if(signal.aborted || this.closed || this.profile!==profile || this.remote!==remote)return;
          const cursor=profile.cursors[projectId] || '0';
          if(BigInt(event.id)<=BigInt(cursor))continue;
          profile.cursors[projectId]=event.id;
          profile.events.push(event.data);profile.events=profile.events.slice(-100);
          await this.persist();this.emitState();
          if(signal.aborted || this.closed || this.profile!==profile || this.remote!==remote)return;
          if((ready || caughtUp) && event.data.device_id!==profile.deviceId)this.emit('notification',{title:'项目有新的进度',body:event.data.summary});
        }
      }catch(error){
        if(signal.aborted || this.closed || this.profile!==profile || this.remote!==remote)return;
        if(!(error instanceof HubError))error=new HubError('NETWORK_UNAVAILABLE','进度连接中断，正在重连。');
        await this.failure(error,{profile,remote,projectId,platform:profile.user?.platform_role==='superadmin'});
        if(stopped.has(error.code)){this.emit('notification',{title:'项目连接已停止',body:'权限或会话已变化，请打开 Agent Hub 查看。'});return;}
      }finally{clearTimeout(idleTimer);}
      await new Promise(resolve=>{
        if(signal.aborted){resolve();return;}
        const done=()=>{clearTimeout(timer);signal.removeEventListener('abort',done);resolve();};
        const timer=setTimeout(done,backoff);signal.addEventListener('abort',done,{once:true});
      });
      backoff=Math.min(backoff*2,20000);
    }
  }
  async logout(){
    this.stopWatches();this.cancelRecovery();this.issuance.clear();this.accountRefresh=null;let complete=true;
    const p=this.profile;
    const sessions={...p?.sessions},canonical=this.remote?.canonical;
    this.remote?.close();this.remote=null;
    if(p){
      p.sessions={};delete p.enrollment;delete p.challengeId;
    }
    this.connection='signed_out';this.projects=[];this.grants=[];this.message=complete?'':'本机已退出，服务器会话撤销未全部确认。';
    // Freeze local access and persist before any network wait; old refreshes cannot repopulate sessions.
    await this.persist();this.emitState();
    if(p){
      for(const binding of p.bindings)await fs.unlink(this.descriptorPath(binding)).catch(error=>{if(error.code!=='ENOENT')complete=false;});
      if(p.management)await fs.unlink(this.descriptorPath(p.management)).catch(error=>{if(error.code!=='ENOENT')complete=false;});
      const revoker=new Remote({...p,sessions},async()=>{},this.transport);
      if(canonical)revoker.canonical=canonical;
      try{
        for(const [slot] of Object.entries(sessions)){
          try{await revoker.request('POST','/api/auth/logout',{}, {slot});}
          catch(error){if(error.code!=='REFRESH_REPLAY_FAMILY_REVOKED')complete=false;}
        }
      }finally{revoker.close();}
    }
    this.message=complete?'':'本机已退出，部分服务器会话撤销或连接清理未确认。';this.emitState();
    return {serverLogoutComplete:complete};
  }
  async close(){this.closed=true;this.work.stop();this.cancelRecovery();this.stopWatches();this.remote?.close();
    await Promise.allSettled(this.projectOperations);await this.work.drain();await this.broker.close();await this.vault.pending;}
}
module.exports={Controller};
