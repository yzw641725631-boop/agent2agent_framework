'use strict';
const root=document.getElementById('app');
const notice=document.getElementById('notice');
const modal=document.getElementById('dialog');
let state,info={},releaseState,view='overview',selectedFolder=null;
let taskBinding=null,taskVersion=0,taskTimer,openTaskBinding=null,taskDialogVersion=0;
let memoryBinding=null,memoryQuery='',memoryOffset=0,memoryLayer='extracts',memoryHistory=false;
let distillationState='queued';
let guideQuery='',guideOffset=0;
let auditOffset=0;
let openAuditBinding=null,openAuditView=null;
let frameworkDialogVersion=0;
modal.addEventListener('close',()=>{taskDialogVersion++;frameworkDialogVersion++;openTaskBinding=null;openAuditBinding=null;});
const taskLabels={draft:'待准入',ready:'待认领',in_progress:'开发中',review:'待评审',testing:'待测试',release_ready:'待发布确认',completed:'已完成'};
const dispatchLabels={queued:'等待接收',received:'已接收，待决定',accepted:'已接受并认领',declined:'已拒绝',cancelled:'已取消',expired:'已过期'};
const deliveryLabels={pull_request:'合并请求',ci:'自动测试',merge:'合并',deployment:'部署',rollback:'回滚'};
const deliveryStates={missing:'尚未核验',open:'已打开',closed:'已关闭',draft:'草稿',merged:'已合并',completed:'已结束',success:'成功',failure:'失败',
  error:'错误',inactive:'已停用',in_progress:'进行中',queued:'排队中',pending:'等待中',waiting:'等待中',requested:'已请求','no-status':'尚无结果'};
const roleLabels={'project-manager':'项目经理',architect:'架构师',designer:'产品与交互设计',developer:'开发工程师',reviewer:'代码审查',tester:'测试工程师',publisher:'发布负责人'};
const labels={active:'已连接',pending:'等待验证 / 审批',offline:'连接中断',blocked:'已拦截',signed_out:'未登录',
  reauth_required:'需要重新登录',running:'执行中',checkpoint:'检查点',completed:'已完成',approved:'已批准'};
const titles={overview:'协作概览',workspaces:'项目与 MCP',tasks:'任务与日志',audits:'代码规范审计',execution:'本机执行',guides:'功能操作说明',memory:'共享记忆',usage:'用量与预算',checkpoints:'本机检查点',closeouts:'撤权交接',activity:'项目动态',connection:'账号与设备',desktop:'桌面设置'};
function el(tag,props={},...children){
  const element=document.createElement(tag);
  for(const [key,value] of Object.entries(props)){
    if(key.startsWith('on'))element.addEventListener(key.slice(2).toLowerCase(),value);
    else if(key==='class')element.className=value;
    else if(key==='text')element.textContent=value;
    else if(value!==undefined && value!==null)element.setAttribute(key,String(value));
  }
  element.append(...children.flat().filter(v=>v!==undefined && v!==null));return element;
}
const text=(tag,value,cls)=>el(tag,{text:value,class:cls || ''});
function toast(message,success=false){
  notice.replaceChildren(text('span',message),el('button',{class:'quiet',onClick:()=>notice.hidden=true},'关闭'));
  notice.className=success?'success':'';notice.hidden=false;
}
function button(label,action,cls=''){
  const element=el('button',{type:'button',class:cls},label);
  element.addEventListener('click',async()=>{
    element.disabled=true;
    try{await action();}catch(error){toast(error.message || '操作未完成，请重试。');}
    finally{element.disabled=false;}
  });return element;
}
const pill=(status)=>text('span',labels[status] || status,'pill '+status);
function field(label,input,hint){return el('label',{},label,input,hint?text('small',hint):null);}
function empty(title,body){return el('div',{class:'empty'},text('strong',title),body);}
function heading(kicker,title,body){return el('section',{class:'intro'},text('div',kicker,'eyebrow'),text('h1',title),text('p',body));}
function openDialog(...children){modal.replaceChildren(...children.flat().filter(v=>v!==null));if(!modal.open)modal.showModal();}
function closeDialog(){modal.close();}
function dialogActions(...buttons){return el('div',{class:'actions end'},button('取消',closeDialog),...buttons);}
async function invoke(action,data){return window.hub.invoke(action,data);}
async function update(){state=await invoke('state');render();}
function events(limit=100){
  const items=state.events.slice(0,limit);
  if(!items.length)return empty('还没有项目动态','绑定项目后，MCP 上报的进度会保存在服务器，并同步到这里。');
  return el('div',{},items.map(event=>{
    const project=state.projects.find(p=>p.id===event.project_id);
    return el('article',{class:'event'},el('span',{class:'dot'}),el('div',{},
      el('div',{class:'row between'},text('h3',event.summary),pill(event.status)),
      text('small',`${project?.name || '项目'} · ${new Date(event.created_at).toLocaleString('zh-CN')} · ${event.device_id===state.deviceId?'本设备':'其他设备'}`),
      event.detail?text('p',event.detail):null,
      event.task_reference?text('small','任务引用：'+event.task_reference):null,
      event.git_commit?text('div','Git '+event.git_commit,'mono'):null));
  }));
}
function loginScreen(){
  const server=el('input',{id:'origin',type:'url',value:state.origin,required:true,autocomplete:'url'});
  const login=el('input',{id:'login',value:state.login,required:true,autocomplete:'username',maxlength:80});
  const password=el('input',{id:'password',type:'password',required:true,autocomplete:'current-password',maxlength:256});
  const device=el('input',{id:'deviceName',value:info.deviceName || '我的电脑',required:true,maxlength:100});
  const submit=el('button',{type:'submit',class:'primary'},'登录并验证设备');
  const form=el('form',{},el('div',{class:'fields'},field('服务器地址',server,'使用当前服务器的 HTTPS 地址。'),
    field('登录名',login),field('密码',password),field('本设备名称',device)),el('div',{class:'actions'},submit));
  if(state.connection==='offline')form.append(el('div',{class:'actions'},
    button('重试连接',async()=>{await invoke('refresh');await update();}),
    button('退出登录',async()=>{await invoke('logout');await update();},'quiet')));
  form.addEventListener('submit',async event=>{
    event.preventDefault();submit.disabled=true;notice.hidden=true;
    try{await invoke('login',{origin:server.value.trim(),login:login.value.trim(),password:password.value,deviceName:device.value.trim()});password.value='';await update();}
    catch(error){toast(error.message);}finally{submit.disabled=false;}
  });
  return el('section',{class:'auth'},heading('YOUR WORK, CONNECTED','连接你的协作空间','登录后，为每个项目建立独立的 MCP 连接。邮箱验证和设备审批沿用服务器规则。'),
    el('div',{class:'card'},form,el('div',{class:'actions'},button('找回密码 / 领取邮箱邀请',()=>invoke('openAdmin')))),
    text('p','超级管理员通过密码和本设备邮箱验证码登录；其他账号按设备审批流程登录。找回及邀请领取在当前服务器的登录页办理。','note'));
}
function pendingScreen(){
  const selfConfirm=state.deviceApprovalMode==='root_email';
  const code=el('input',{id:'emailCode',inputmode:'numeric',pattern:'[0-9]{8}',maxlength:8,autocomplete:'one-time-code',placeholder:'八位邮箱验证码'});
  const email=state.emailRequired?el('div',{},field('邮箱验证码',code),el('div',{class:'actions'},
    button('发送验证码',async()=>{await invoke('sendEmail');toast('验证码已发送，请检查绑定邮箱。',true);}),
    button(selfConfirm?'验证并登录':'验证邮箱',async()=>{
      const next=await invoke('verifyEmail',{code:code.value.trim()});
      toast(next.connection==='active'?'邮箱验证通过，已登录。':'邮箱验证通过，请检查设备审批。',true);await update();
    },'primary'))):
    text('p',selfConfirm?'本设备邮箱已验证，点击下方按钮继续登录。':'邮箱验证已满足。等待超级管理员在管理后台批准当前设备。');
  return el('section',{class:'auth'},heading('DEVICE VERIFICATION','验证当前设备',selfConfirm?
    '你是超级管理员。使用本账号绑定邮箱的验证码确认这台设备后即可登录。':'请使用已有可信设备打开管理后台，处理本次设备申请。'),
    el('div',{class:'card'},el('div',{class:'card-head'},text('h2',state.login),pill('pending')),email,
      text('p','设备 ID：'+state.deviceId,'device-code'),
      el('div',{class:'actions'},button(selfConfirm?'检查验证并登录':'检查审批并继续',async()=>{await invoke('checkApproval');await update();},'primary'),
        button('打开管理后台',()=>invoke('openAdmin')),button('退出登录',async()=>{await invoke('logout');await update();},'quiet'))));
}
function bindDialog(){
  selectedFolder=null;
  const project=el('select',{id:'project'},state.projects.map(p=>el('option',{value:p.id},p.name)));
  const folder=text('div','尚未选择文件夹','note mono');
  openDialog(text('h2','绑定项目工作目录'),text('p','每个连接固定对应一个项目。文件夹由你选择，绑定后可以预览 MCP 配置。'),
    el('div',{class:'fields'},field('项目',project),el('div',{},text('h3','本地工作目录'),folder,
      button('选择文件夹',async()=>{selectedFolder=await invoke('chooseWorkspace');folder.textContent=selectedFolder || '尚未选择文件夹';}))),
    dialogActions(button('建立连接',async()=>{
      if(!selectedFolder)throw new Error('请先选择项目文件夹。');
      await invoke('bind',{projectId:project.value});closeDialog();view='workspaces';await update();toast('项目已绑定，可以预览或复制 MCP 配置。',true);
    },'primary')));
}
function configDialog(binding){
  openDialog(text('h2','接入项目 MCP'),text('p',binding.projectName+' · '+binding.workspace),
    text('p','通用 stdio 配置可复制到支持 MCP 的客户端。Claude Code 可以预览后写入这个工作目录的 .mcp.json。','note'),
    text('pre',JSON.stringify(binding.configuration,null,2)),
    text('p','配置绑定本机路径，请勿提交到 Git。移动应用文件夹后，需要重新生成配置。','note'),
    releaseState?text('p','把对话启动规则加入 harness。Codex 和 Claude Code 可以配置会话启动钩子。','note'):null,
    releaseState?el('div',{class:'actions'},
      button('复制对话启动规则',async()=>{await invoke('copySessionRule');toast('启动规则已复制。',true);}),
      button('复制 Codex 启动钩子',async()=>{await invoke('copySessionHook',{id:binding.id,host:'codex'});toast('已复制。请合并到项目的 .codex/hooks.json，再审查钩子。',true);}),
      button('复制 Claude Code 启动钩子',async()=>{await invoke('copySessionHook',{id:binding.id,host:'claude'});toast('已复制。请合并到项目的 .claude/settings.local.json。',true);}),
      button('复制 Codex 逐轮检查钩子',async()=>{await invoke('copyTurnHooks',{id:binding.id,harness:'codex'});toast('已复制。合并到项目 .codex/hooks.json 并审查启用；首次对话须绑定工作运行。',true);}),
      button('复制 Claude Code 逐轮检查钩子',async()=>{await invoke('copyTurnHooks',{id:binding.id,harness:'claude-code'});toast('已复制。合并到 .claude/settings.local.json 并启用，随后绑定工作运行。',true);}),
    ):null,
    dialogActions(button('复制通用配置',async()=>{await invoke('copyConfiguration',{id:binding.id});toast('MCP 配置已复制。',true);}),
      button('安装与检查逐轮保护',()=>hooksDialog(binding)),
      button('预览 .mcp.json',()=>previewDialog(binding),'primary')));
}
async function hooksDialog(binding){
  let preview=null;
  const host=el('select',{},...['codex','claude-code','cursor','qoder','codebuddy'].map(name=>el('option',{value:name},name)));
  const summary=el('div',{}),details=el('pre',{});
  const call=(action,args={})=>invoke('hookAction',{bindingId:binding.id,action,args:{harness:host.value,...args}});
  async function refresh(){
    const value=await call('hub_get_hook_status');
    const names={not_installed:'尚未安装',awaiting_host:'已安装，等待开发工具实际触发',observed:'已观察到宿主事件',configuration_changed:'配置已被修改，需要处理冲突'};
    summary.replaceChildren(text('p',names[value.state] || value.state),
      text('p',value.activity?'已触发：'+Object.keys(value.activity.events).join('、'):'尚无实际触发记录。','note'));
  }
  host.addEventListener('change',()=>{preview=null;details.textContent='';refresh().catch(error=>toast(error.message));});
  openDialog(text('h2','逐轮检查与修改前保护'),text('p',binding.projectName+' · '+binding.workspace),field('开发工具',host),summary,
    text('p','安装后请按开发工具的正常流程审查、信任并重新加载，再在任务中绑定本对话。未识别的命令默认停止，避免命令绕过禁改路径。','note'),
    text('p','此保护覆盖宿主实际调用的钩子。它不能限制用户直接编辑文件，也不等于系统级隔离。WorkBuddy、TRAE 的原生钩子接口仍待核验。','note'),details,
    dialogActions(button('刷新触发状态',refresh),button('预览安装',async()=>{preview=await call('hub_preview_hooks');details.textContent=JSON.stringify(preview.configuration,null,2);}),
      button('安装或更新',async()=>{if(!preview)throw new Error('请先预览本次安装。');await call('hub_install_hooks',{preview_hash:preview.preview_hash});preview=null;await refresh();toast('配置已安装。请在开发工具中信任并重新加载，再检查实际触发状态。',true);},'primary'),
      button('卸载本工具检查',async()=>{const value=await call('hub_get_hook_status');const result=await call('hub_uninstall_hooks',{preview_hash:value.uninstall_hash});preview=null;await refresh();toast(result.conflicts.length?'部分条目有冲突，已保留，请检查配置。':'已移除本连接登记的钩子，其他配置保留。',!result.conflicts.length);})));
  await refresh();
}
async function managementDialog(){
  const configuration=await invoke('managementConfiguration');
  openDialog(text('h2','接入项目管理 MCP'),
    text('p','此连接使用当前超级管理员账号，允许 Agent 列出和创建服务器项目。可以先接入它，再创建第一个项目。'),
    text('pre',JSON.stringify(configuration,null,2)),
    text('p','把配置加入 AI 开发工具的 MCP 设置并重新加载，然后说：“列出已有项目；如果没有 kms，就创建标识为 kms、名称为 kms 的项目。”','note'),
    text('p','退出登录会停止管理操作，登录同一账号后可恢复。停用此连接后，原配置立即失效。','note'),
    dialogActions(button('停用管理连接',async()=>{
      await invoke('disconnectManagement');closeDialog();await update();toast('管理 MCP 已停用。',true);
    },'quiet'),button('复制管理 MCP 配置',async()=>{
      await invoke('copyManagementConfiguration');toast('项目管理 MCP 配置已复制，请加入 AI 工具的 MCP 设置。',true);
    },'primary')));
}
async function previewDialog(binding){
  const preview=await invoke('preview',{id:binding.id});
  openDialog(text('h2','确认 MCP 配置'),text('p',preview.target,'mono'),
    text('p',preview.previous?'写入前会保存原文件备份，并保留现有其他连接。':'将创建这个文件。'),
    preview.previous?el('details',{},text('summary','查看原配置'),text('pre',preview.previous)):null,
    text('h3','写入后的内容'),text('pre',preview.next),
    dialogActions(button('确认写入',async()=>{
      const result=await invoke('install',{id:binding.id,expectedHash:preview.previousHash});closeDialog();await update();
      toast(result.backup?'配置已写入，备份保存在原文件旁。请在 harness 中重新加载 MCP。':'配置已写入，请在 harness 中重新加载 MCP。',true);
    },'primary')));
}
function bindings(limit=100){
  const items=state.bindings.slice(0,limit);
  if(!items.length)return empty('连接第一个项目','选择服务端项目和本地工作目录，再将生成的 MCP 配置加入 harness。');
  return el('div',{},items.map(binding=>el('article',{class:'binding'},
    el('div',{class:'row between'},text('h3',binding.projectName+(binding.repositoryName?' / '+binding.repositoryName:' / 项目组')),pill(binding.status)),
    text('div',binding.workspace,'meta mono'),binding.message?text('p',binding.message,'warning'):null,
    el('div',{class:'row'},button('MCP 配置',()=>configDialog(binding)),
      binding.status==='active'?button('部署职责',()=>frameworkDialog(binding)):null,
      binding.repositoryId ? button('获取更新',async()=>{
        const result=await invoke('fetchRepository',{bindingId:binding.id});toast('已获取更新 '+result.fetched_commit.slice(0,8)+'，当前代码未合并。',true);
      }) : button('组装仓库',()=>assembleDialog(binding)),button('停用连接',()=>{
      openDialog(text('h2','停用这个 MCP 连接'),text('p',binding.projectName),
        text('p','此连接会立即失效。工作文件和已经写入的 .mcp.json 会保留，你可以在 harness 配置里移除对应项。'),
        dialogActions(button('确认停用',async()=>{await invoke('disconnect',{id:binding.id});closeDialog();await update();},'danger')));
    },'quiet')))));
}
function frameworkDialog(binding){
  const ticket=++frameworkDialogVersion,account=state.user?.id;
  const current=()=>ticket===frameworkDialogVersion && account===state.user?.id &&
    state.bindings.some(b=>b.id===binding.id && b.status==='active');
  const names=el('select',{},el('option',{value:'zh'},'中文'),el('option',{value:'en'},'English'));
  const contents=el('select',{},el('option',{value:'zh'},'中文'),el('option',{value:'en'},'English'));
  const results=el('div',{}),status=text('p','','note');
  let preview=null;
  const args=()=>({doc_name_lang:names.value,doc_content_lang:contents.value});
  const install=button('确认部署',async()=>{
    if(!current() || !preview)return;
    const result=await invoke('frameworkDeploy',{bindingId:binding.id,args:{...args(),preview_hash:preview.preview_hash}});
    if(!current())return;
    toast(result.changed?'职责框架已部署，下次开工准备会加载当前角色。':'职责框架已经是当前版本。',true);closeDialog();
  },'primary');
  install.disabled=true;
  const reset=()=>{preview=null;install.disabled=true;results.replaceChildren();status.textContent='语言已改变，请重新预览。'};
  names.addEventListener('change',reset);contents.addEventListener('change',reset);
  const inspect=button('预览文件',async()=>{
    const selected=args();preview=null;install.disabled=true;status.textContent='正在验证项目权限并生成预览…';
    const result=await invoke('taskQuery',{bindingId:binding.id,tool:'hub_preview_framework',args:selected});
    if(!current() || JSON.stringify(selected)!==JSON.stringify(args()))return;
    preview=result;status.textContent='共 '+result.files.length+' 个文件；只写入本工程的职责目录。';
    results.replaceChildren(text('p',result.directory,'mono'),...result.files.map(f=>text('p',f.path+' · '+f.bytes+' B','meta mono')));
    install.disabled=false;
  });
  openDialog(text('h2','部署项目职责框架'),text('p',binding.projectName+(binding.repositoryName?' / '+binding.repositoryName:'')),
    text('p','包含七种职业职责、流程、文档索引和本地自检脚本。部署会保存安装收据；撤权交接成功后按收据清理。'),
    text('p','职责按当前任务加载。职业名称不增加账号或仓库权限，跨端自动调度尚未接入。','note'),
    el('div',{class:'fields'},field('角色文件名语言',names),field('文档正文语言',contents)),inspect,status,results,dialogActions(install));
}
async function assembleDialog(binding){
  const registry=await invoke('repositoryList',{bindingId:binding.id});
  const repositories=registry.items.filter(repo=>repo.status==='active' && repo.capabilities.includes('code:read'));
  if(!repositories.length)throw new Error('当前没有获准的代码仓库，请先在后台接入仓库并批准权限。');
  const picks=repositories.map(repo=>({repo,input:el('input',{type:'checkbox',checked:true})}));
  const harness=el('select',{},[['codex','Codex'],['claude','Claude Code'],['cursor','Cursor']].map(([value,label])=>el('option',{value},label)));
  const progress=text('p','','note');
  const operationId=crypto.randomUUID();
  openDialog(text('h2','组装项目组仓库'),text('p',binding.workspace,'mono'),
    text('p','每个选中仓库会拉取到独立子目录，并配置固定的仓库 MCP 连接。同名已有目录会保留。'),
    el('div',{class:'fields'},...picks.map(({repo,input})=>field(repo.name+' · '+repo.platform,input)),field('接入开发工具',harness)),progress,
    dialogActions(button('拉取并接入',async()=>{
      const repositoryIds=picks.filter(item=>item.input.checked).map(item=>item.repo.id);
      if(!repositoryIds.length)throw new Error('请至少选择一个仓库。');
      progress.textContent='正在验证权限、拉取仓库和配置连接，请保持桌面端运行…';
      try{
        const result=await invoke('assembleRepositories',{bindingId:binding.id,repositoryIds,harness:harness.value,operationId});
        progress.textContent='';await update();
        openDialog(text('h2','项目组已组装'),...result.items.map(item=>el('div',{},text('h3',item.repository_name),text('p',item.workspace,'mono'),
          item.connection_installation==='manual-tracked-configuration'?text('p','仓库内已有受版本管理的开发工具配置，已保留原文。请从该仓库连接复制 MCP 配置并手动接入。','warning'):null)),
          text('p','在相应仓库目录打开开发工具，重新加载 MCP 并完成信任确认，然后认领该仓库的任务。'),dialogActions());
      }catch(error){progress.textContent=error.message;throw error;}
    },'primary')));
}
function overview(){
  const active=state.bindings.filter(b=>b.status==='active').length;
  return el('div',{},heading('WORKSPACE COMMAND CENTER','让每一份进展，保持同步。','连接你正在开发的项目，让不同设备上的 harness 共享服务器保存的进度记录。'),
    el('div',{class:'cards'},[['已授权项目',state.projects.length,'由服务器实时确认'],['本机 MCP 连接',active,`共 ${state.bindings.length} 个绑定`],['近期进度',state.events.length,'显示最近 100 条记录']].map(([label,value,note])=>
      el('div',{class:'card'},text('div',label,'stat-label'),text('div',String(value),'stat-number'),text('div',note,'stat-note')))),
    el('div',{class:'split'},el('section',{class:'card'},el('div',{class:'card-head'},text('h2','项目动态'),
      button('查看全部',()=>{view='activity';render();},'quiet')),events(5)),
      el('section',{class:'card'},el('div',{class:'card-head'},text('h2','本机连接'),state.projects.length?button('绑定项目',bindDialog,'primary'):null),bindings(3))));
}
function closeoutsScreen(){
  const names={pending:'待收尾',uploading:'正在上传',uninstalling:'正在卸载',report_pending:'结果待回报',
    blocked:'收尾阻塞，原工作保留',conflicts:'已上传，存在清理冲突',complete:'已完成'};
  return el('div',{},heading('ACCESS REVOCATION','撤权交接与卸载','权限失效后停止普通开发。工作上传并取得服务器确认后，程序按安装收据卸载本项目框架；共享组件和本机检查点保留。'),
    el('section',{class:'card'},el('div',{class:'card-head'},text('h2','本设备收尾记录'),
      button('重试收尾',async()=>{await invoke('retryCloseout');await update();},'primary')),
      ...(state.closeouts || []).map(job=>el('article',{class:'binding'},text('h3',names[job.state] || job.state),
        text('p','项目 '+job.project_id,'mono'),text('p',job.message,'note'),
        text('p',job.server_upload_confirmed?'已有服务器上传确认':'尚未确认完整上传'),
        job.error?text('p','阻塞原因：'+job.error,'warning'):null,
        job.cleanup?text('p',`已移除 ${job.cleanup.removed_files} 个受管文件、${job.cleanup.removed_entries} 个连接项；${job.cleanup.conflicts} 个冲突。`):null,
        text('small','收尾授权有效期：'+new Date(job.expires_at).toLocaleString('zh-CN')))),
      !(state.closeouts || []).length?empty('没有撤权收尾任务','正常退出登录、会话自然过期或网络中断不会自行卸载项目。'):null));
}
function executionScreen(){
  const runs=(state.workRuns || []).filter(r=>r.acknowledged && !r.stopped && ['codex','claude-code'].includes(r.harness));
  const selected=el('select',{},runs.map(r=>el('option',{value:r.run_id},r.harness+' · '+r.task_id)));
  const timeout=el('input',{type:'number',value:600,min:30,max:1800});
  const config=state.executions || {adapters:[],items:[]};
  return el('div',{},heading('LOCAL EXECUTION','本机执行','由本人批准具体任务、目录和时限。执行时沿用本人 CLI 登录和宿主权限；断网、撤权或超时会请求停止，原工作继续按框架流程保全。'),
    el('section',{class:'card'},text('h2','本人开发工具'),text('p','已登记：'+(config.adapters.join('、') || '尚未登记')),
      el('div',{class:'actions'},...['codex','claude-code'].map(harness=>button('登记 '+harness,async()=>{await invoke('configureExecution',{harness});await update();}))),
      text('p','当前支持两种原生 CLI。其他工具可通过 MCP 接收和处理待办。程序更新后须重新登记摘要；框架不提供系统级隔离。','note')),
    el('section',{class:'card'},text('h2','批准单次任务'),runs.length?el('div',{},field('已准备并确认的本人运行',selected),field('最长运行秒数',timeout),button('预览本次启动',async()=>{
      const run=runs.find(r=>r.run_id===selected.value);if(!run)throw new Error('请先准备并确认任务。');
      const preview=await invoke('previewExecution',{bindingId:run.binding_id,args:{task_id:run.task_id,run_id:run.run_id,lease_epoch:run.lease_epoch,harness:run.harness,timeout_seconds:Number(timeout.value)}});
      openDialog(text('h2','确认本机执行范围'),text('p','开发工具：'+preview.harness),text('p','任务：'+preview.task_id,'mono'),
        text('p','工作目录：'+preview.workspace,'mono'),text('p','最长运行：'+preview.timeout_seconds+' 秒'),
        text('p','程序：'+preview.executable.file,'mono'),text('p','启动后仍须遵守宿主本身的登录、信任和权限要求。'),
        el('details',{},text('summary','查看核验信息'),text('pre',JSON.stringify(preview,null,2))),dialogActions(button('核对后启动',async()=>{
        const result=await invoke('approveExecution',{id:preview.id,previewHash:preview.preview_hash});if(!result.cancelled)closeDialog();await update();},'primary')));
    },'primary')):empty('没有可启动的运行','在开发工具中认领代码任务，准备并确认规则及记忆，再回到这里。')),
    el('section',{class:'card'},text('h2','真实运行记录'),...config.items.slice().reverse().map(item=>el('article',{class:'binding'},text('h3',item.harness+' · '+({starting:'启动中',running:'执行中',stopping:'停止中',stopped:'已停止',exited:'已退出',failed:'执行失败',cancelled:'已取消',interrupted:'异常中断'}[item.state] || item.state)),
      text('p',item.task_id,'mono'),text('p',item.reason || (item.state==='exited'?'程序已退出。业务任务仍以服务器评审流程为准。':'本人设备运行中。')),
      ['starting','running','stopping'].includes(item.state)?button('停止运行',async()=>{await invoke('stopExecution',{id:item.id});await update();}):
        item.requires_process_check?button('检查并确认旧执行已停止',async()=>{await invoke('confirmExecutionStopped',{id:item.id});await update();}):
        text('small','退出码 '+item.exit_code+' · '+(item.receipt_shared?'回执已共享':'回执仅在本机，尚未共享'))))));
}
function informationScreen(kind){
  const available=state.bindings.filter(b=>b.status==='active'),generation=taskVersion;
  const panel=el('section',{class:'card'});
  if(!available.length)return el('div',{},heading('PROJECT INFORMATION',titles[kind],'先接入获准项目，再读取服务器记录。'),empty('没有可用项目连接','请在“项目与 MCP”建立连接。'));
  memoryBinding=available.some(b=>b.id===memoryBinding)?memoryBinding:available[0].id;
  const bindingId=memoryBinding,picker=el('select',{},available.map(b=>el('option',{value:b.id,...(b.id===bindingId?{selected:true}:{})},b.projectName+' · '+(b.repositoryName || b.workspace))));
  picker.addEventListener('change',()=>{memoryBinding=picker.value;guideOffset=0;render();});
  const query=el('input',{value:guideQuery,maxlength:200,placeholder:'按功能名称或正文查找'});
  const controls=el('div',{class:'actions'},field('项目连接',picker),kind==='guides'?field('说明搜索',query):null,
    kind==='guides'?button('搜索',()=>{guideQuery=query.value;guideOffset=0;render();}):null,button('刷新',()=>render()));
  const current=()=>view===kind && generation===taskVersion && memoryBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  const call=(tool,args={})=>invoke('taskQuery',{bindingId,tool,args});
  call(kind==='guides'?'hub_list_guides':'hub_get_usage',kind==='guides'?{query:guideQuery,offset:guideOffset,limit:20}:{}).then(data=>{
    if(!current())return;
    if(kind==='usage'){
      const number=value=>Number(value || 0).toLocaleString('zh-CN');
      panel.append(text('h2','本月用量报告'),
        text('p','上报输入：'+number(data.reported_input_tokens)+' Token · 上报输出：'+number(data.reported_output_tokens)+' Token'),
        text('p','其中缓存输入：'+number(data.reported_cached_input_tokens)+' Token'),
        text('p','已测开工包：'+number(data.measured_context_bytes)+' 字节'),
        text('p','上报费用：'+(data.reported_cost_micro_usd==null?'未提供':(data.reported_cost_micro_usd/1000000).toFixed(6)+' USD')),
        text('h3','项目额度'),text('p',(data.budget.mode==='observe'?'只观察':'达到额度暂停受管开发')+' · 月度上报额度：'+(data.budget.monthly_reported_token_limit?number(data.budget.monthly_reported_token_limit)+' Token':'未设上限')),
        text('p',data.budget.managed_work_available?'当前允许受管工作。':'新受管修改已暂停，仍可保存日志与交接。',data.budget.managed_work_available?'note':'warning'),
        text('p','仅显示当前权限下已收到的报告；未上报的消耗未知，费用尚未核对账单。开工包字节数不等于计费 Token。','note'),
        button('在后台调整额度',()=>invoke('openAdmin')));return;
    }
    const names={queued:'待生成 / 待重试',running:'生成中',review:'待核对',approved:'已核对，待功能发布',published:'已发布'};
    if(!data.items.length)panel.append(empty('没有可见说明','需要说明的开发任务提交后会自动建立独立生成待办。已发布指南按阅读范围开放。'));
    for(const item of data.items)panel.append(el('article',{class:'binding'},text('h3',item.title),text('p',(item.product_version || '待生成')+' · '+(names[item.state] || item.state)),
      button('查看说明',async()=>{const detail=await call('hub_read_guide',{job_id:item.id});if(!current())return;
        openDialog(text('h2',detail.title),detail.content?el('div',{},Object.entries(detail.content).map(([key,value])=>el('section',{},text('h3',({purpose:'用途',prerequisites:'前提',entry:'入口',steps:'操作步骤',inputs:'输入校验',results:'结果',recovery:'异常恢复',cautions:'提示',examples:'示例',changes:'版本变化',limitations:'限制',verification:'核验依据',user_roles:'使用角色',product_version:'适用版本',title:'功能名称'})[key] || key),
          Array.isArray(value)?el('ol',{},value.map(v=>text('li',v))):text('p',value)))):text('p','说明待办已在服务器保留，由负责实例通过 MCP 生成。'),
          detail.reason?text('p',detail.reason,'warning'):null,dialogActions(button('打开后台审核',()=>invoke('openAdmin'))));
      })));
    panel.append(el('div',{class:'actions'},guideOffset?button('上一页',()=>{guideOffset=Math.max(0,guideOffset-20);render();}):null,
      data.has_more?button('下一页',()=>{guideOffset=data.next_offset;render();}):null));
  }).catch(error=>{if(current())panel.append(text('p',error.message,'warning'));});
  return el('div',{},heading('PROJECT INFORMATION',titles[kind],kind==='guides'?'查看适用版本的操作说明及真实生成进度。草稿须经产品和测试核对，发布后按授权供产品人员与 Skill 使用者查阅。':'查看已上报消耗及项目额度。达到额度时暂停受管开发，日志保存和交接继续可用。'),controls,panel);
}
function memoryScreen(){
  const generation=taskVersion,available=state.bindings.filter(b=>b.status==='active');
  const container=el('section',{class:'card'},text('h2','共享记忆'));
  const intro=heading('SHARED MEMORY','共享记忆','查看日志交接、文档摘录及经审核的长期知识。原文保留，默认加载当前有效内容。');
  if(!available.length)return el('div',{},intro,empty('尚无可用项目连接','先在“项目与 MCP”接入一个获准工程。'));
  memoryBinding=available.some(b=>b.id===memoryBinding)?memoryBinding:available[0].id;
  const picker=el('select',{},available.map(b=>el('option',{value:b.id,...(b.id===memoryBinding?{selected:true}:{})},b.projectName+' · '+(b.repositoryName || b.workspace))));
  picker.addEventListener('change',()=>{memoryBinding=picker.value;memoryOffset=0;render();});
  const query=el('input',{value:memoryQuery,maxlength:200,placeholder:'问题或关键词，例如换电脑怎样验证身份'});
  const mode=el('select',{},el('option',{value:'hybrid'},'语义与关键词'),el('option',{value:'keyword'},'仅关键词'));
  mode.value=window.memorySearchMode || 'hybrid';mode.addEventListener('change',()=>{window.memorySearchMode=mode.value;memoryOffset=0;render();});
  const layer=el('select',{},el('option',{value:'extracts'},'日志与文档摘录'),el('option',{value:'knowledge'},'长期知识'),el('option',{value:'distillations'},'提炼待办'));
  layer.value=memoryLayer;layer.addEventListener('change',()=>{memoryLayer=layer.value;memoryOffset=0;render();});
  const history=el('select',{},el('option',{value:'current'},'当前有效'),el('option',{value:'all'},'候选与历史'));
  history.value=memoryHistory?'all':'current';history.addEventListener('change',()=>{memoryHistory=history.value==='all';memoryOffset=0;render();});
  const isKnowledge=memoryLayer==='knowledge';
  const batchState=el('select',{},Object.entries({queued:'待处理',running:'已认领',succeeded:'已提交结果',obsolete:'已失效'}).map(([value,label])=>el('option',{value},label)));
  batchState.value=distillationState;batchState.addEventListener('change',()=>{distillationState=batchState.value;memoryOffset=0;render();});
  const isDistillation=memoryLayer==='distillations';
  const controls=el('div',{class:'actions'},field('项目工程',picker),field('资料类型',layer),
    ...(isDistillation?[field('批次状态',batchState),button('刷新',()=>render())]:[field('版本范围',history),field('检索方式',mode),field('问题或关键词',query),button('检索',()=>{memoryQuery=query.value;memoryOffset=0;render();})]));
  const counter=text('p','正在核验授权…','note'),bindingId=memoryBinding;
  container.append(controls,counter);
  const list=el('div');container.append(list);
  const current=()=>view==='memory' && generation===taskVersion && memoryBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  const call=(tool,args={})=>invoke('taskQuery',{bindingId,tool,args});
  if(isDistillation){
    const labels={queued:'等待 Agent 处理',running:'已认领，尚未确认提炼结果',succeeded:'已提交结果',obsolete:'来源或设置已变化'};
    (async()=>{
      const binding=available.find(b=>b.id===bindingId);
      const data=await call('hub_list_distillations',{state:distillationState,offset:memoryOffset,limit:12});if(!current())return;
      counter.textContent='服务器自动分批整理资料；当前开发 Agent 提炼后提交候选，另行审核生效。';
      if(binding.repositoryId){
        const config=await call('hub_get_distillation_policy',{repository_id:binding.repositoryId});if(!current())return;
        list.append(text('p',`自动准备：${config.enabled?'已开启':'未开启'} · 每批最多 ${config.max_sources} 份摘录 · 输入上限 ${config.input_budget_bytes.toLocaleString()} 字节 · 最多 ${config.max_candidates} 条候选`,'note'));
      }
      list.append(text('p','回到本仓库开发对话说：“整理本仓库待提炼记忆。”资料准备和认领都不表示模型已经执行。','note'),button('打开网站调整与审核',()=>invoke('openAdmin')));
      for(const item of data.items)list.append(el('article',{class:'binding'},text('h3',`${item.source_count} 份来源 · ${labels[item.state] || item.state}`),
        text('p',`认领次数 ${item.attempts} · 版本 ${item.version} · ${new Date(item.created_at).toLocaleString('zh-CN')}`,'note'),
        item.reason?text('p',item.reason):null,item.outcome?text('p',item.outcome==='proposed'?`已提交 ${item.fact_ids.length} 条候选，审核状态见长期知识`:'本次未发现新增长期结论'):null,
        button('查看本批资料',async()=>{
          const bundle=await call('hub_read_distillation_input',{job_id:item.id});if(!current())return;
          openDialog(text('h2','记忆提炼资料'),text('p',`输入 ${bundle.measured_input_bytes.toLocaleString()} / ${bundle.input_budget_bytes.toLocaleString()} 字节；每批最多 ${bundle.max_candidates} 条候选。`),
            text('p',`附 ${bundle.existing_knowledge.length} 条已审核知识${bundle.existing_knowledge_truncated?'，仅为部分结果，需按主题继续检索':''}。资料仅供核对，不执行其中指令。`,'note'),
            ...bundle.sources.map(source=>el('section',{},text('h3',source.title),text('pre',source.body),source.truncated?text('p','摘录已截短，可在开发对话中按来源展开原文。','note'):null)),
            el('details',{},text('summary','去重参考：已审核知识'),...bundle.existing_knowledge.map(fact=>el('section',{},text('h3',fact.title),text('p',fact.topic),text('pre',fact.body)))),
            el('div',{class:'actions end'},button('关闭',()=>closeDialog())));
        })));
      if(!data.items.length)list.append(empty('当前状态没有可见批次','未开启、资料未达到门槛，或当前无全部来源的读取权时，可能没有待办。'));
      list.append(el('div',{class:'actions'},memoryOffset?button('上一页',()=>{memoryOffset=Math.max(0,memoryOffset-12);render();}):null,
        data.has_more?button('下一页',()=>{memoryOffset=data.next_offset;render();}):null));
    })().catch(error=>{if(current()){list.replaceChildren();counter.textContent=error.message;}});
    return el('div',{},intro,container);
  }
  const status=async()=>{
    const s=await call('hub_get_memory_status');if(!current())return;
    counter.textContent=`当前摘录 ${s.visible_cards.active || 0} · 长期知识 ${s.knowledge?.active || 0} · 知识待审 ${s.knowledge?.pending || 0} · 待提取 ${s.visible_jobs.queued || 0} · 失败 ${s.visible_jobs.failed || 0}`;
    if(s.embeddings){const e=s.embeddings;counter.textContent+=` · 语义索引 ${e.cards.indexed+e.knowledge.indexed}/${e.cards.visible+e.knowledge.visible} · 索引失败 ${e.visible_jobs.failed || 0}`;}
    taskTimer=setTimeout(()=>status().catch(error=>{if(current()){list.replaceChildren();counter.textContent=error.message;}}),5000);
  };
  (async()=>{
    const data=await call(isKnowledge?'hub_search_knowledge':'hub_search_memory',{query:memoryQuery,search_mode:mode.value,offset:memoryOffset,limit:12,include_history:memoryHistory});if(!current())return;
    list.append(text('p',data.semantic_search==='ready'?'已结合语义和关键词排序；结果需核对来源。':data.semantic_search==='unavailable-keyword-fallback'?'语义服务暂不可用，本次已退回关键词。':data.semantic_search==='keyword-requested'?'本次仅按关键词匹配。':data.semantic_search==='browse'?'当前列出获准资料。':'当前按关键词检索。','note'));
    if(data.candidate_limit_reached)list.append(text('p','已达到候选上限，请缩小问题或仓库范围。','note'));
    for(const card of data.items){
      const raw=el('div'),source=card.source;
      list.append(el('article',{class:'event'},el('span',{class:'dot'}),el('div',{},text('h3',card.title),
        text('small',source.kind==='document'?`${source.path} · 行 ${card.line_start}–${card.line_end}`:`工作日志 ${source.id.slice(0,8)} · 任务 ${source.task_id.slice(0,8)}`),
        isKnowledge?el('div',{},text('small',`${card.topic} · ${card.state} · 版本 ${card.version}`)):null,
        text('pre',card.body),card.truncated?text('p','摘录已截短，可展开来源原文。','note'):null,
        isKnowledge?text('p',card.state_reason,'note'):null,
        button('展开来源',async()=>{
          const original=await call('hub_read_memory_source',{card_id:isKnowledge?card.source_card_id:card.id,start_line:card.line_start || 1,limit:100});
          if(current())raw.replaceChildren(text('pre',original.content),original.has_more?text('small','此处仅显示一段，完整内容可通过 MCP 分段继续读取。'):text('small','来源原文已显示。'));
        }),raw)));
    }
    if(!data.items.length)list.append(empty('当前没有匹配卡片','可调整关键词，查看文档同步及提取队列；这不代表没有其他历史记录。'));
    const pagination=el('div',{class:'actions'});
    if(memoryOffset)pagination.append(button('上一页',()=>{memoryOffset=Math.max(0,memoryOffset-12);render();}));
    if(data.has_more)pagination.append(button('下一页',()=>{memoryOffset=data.next_offset;render();}));
    if(isKnowledge)list.append(button('打开网站审核知识',()=>invoke('openAdmin')));
    list.append(pagination);await status();
  })().catch(error=>{if(current()){list.replaceChildren();counter.textContent=error.message;}});
  return el('div',{},intro,container);
}
function tasksScreen(){
  const bindings=state.bindings.filter(b=>b.status==='active');
  taskBinding=bindings.some(b=>b.id===taskBinding)?taskBinding:bindings[0]?.id;
  const selector=el('select',{'aria-label':'任务所属连接'},bindings.map(b=>el('option',{value:b.id},b.projectName+' · '+b.workspace)));
  selector.value=taskBinding || '';
  selector.addEventListener('change',()=>{taskBinding=selector.value;closeDialog();openTaskBinding=null;render();});
  const rows=el('div',{}),inbox=el('div',{}),sync=text('p','正在读取…','note'),generation=taskVersion,bindingId=taskBinding;
  const current=()=>view==='tasks' && taskVersion===generation && taskBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  let offset=0;
  const load=async()=>{
    if(!current())return;
    try{
      const data=await invoke('taskQuery',{bindingId,tool:'hub_list_tasks',args:{offset,limit:50}});
      if(!current())return;
      const [agents,offers]=await Promise.all([
        invoke('taskQuery',{bindingId,tool:'hub_list_agents',args:{limit:100}}),
        invoke('taskQuery',{bindingId,tool:'hub_list_agent_dispatches',args:{limit:20}})]);
      if(!current())return;
      inbox.replaceChildren(text('h3','执行者与任务投递'),
        text('p',agents.items.length?agents.items.map(a=>`${a.name} · ${a.harness} · ${a.recent_agent_activity?'最近有 Agent 活动':'暂无近期活动'}`).join('；'):
          '尚未登记执行者。在开发工具中通过 MCP 登记自己的实例后，负责人可以投递任务。','note'),
        text('p','投递和接收均未占用任务；接受成功后才取得开发租约。不会自动启动其他开发工具。','note'),
        ...offers.items.map(o=>el('article',{class:'binding'},text('h4',o.task_title),
          text('p',(dispatchLabels[o.state] || o.state)+(o.expired && ['queued','received'].includes(o.state)?' · 已超时':'')),
          text('p',o.note,'note'),o.reason?text('p',o.reason):null,
          text('p',o.run_id?'认领回执：'+o.run_id:'尚无认领回执','mono'))),
        offers.has_more?text('p','这里只展示最近 20 条；完整历史可通过项目 MCP 按任务翻页。','note'):null);
      rows.replaceChildren(...data.items.map(task=>el('article',{class:'binding'},
        el('div',{class:'row between'},text('h3',task.title),text('span',taskLabels[task.status],'pill')),
        text('p',`${roleLabels[task.execution_role] || task.execution_role || '开发工程师'} · ${task.scope.repository} · ${task.scope.platform} · ${task.scope.paths.join('、')}`,'note'),
        task.dependencies_satisfied===false?text('p','前置任务尚未完成','warning'):null,
        text('p',task.active_run_id?'认领到期 '+new Date(task.lease_until).toLocaleString('zh-CN'):'当前没有开发租约','note'),
        button('查看日志与流程',()=>taskDetail(bindingId,task.id,generation)))));
      if(!data.items.length)rows.append(empty('没有任务','在网站后台创建，或通过项目 MCP 让架构师拆解任务。'));
      rows.append(el('div',{class:'actions'},
        offset?button('上一页',async()=>{offset=Math.max(0,offset-50);await load();}):null,
        data.has_more?button('下一页',async()=>{offset=data.next_offset;await load();}):null));
      sync.textContent='最近同步 '+new Date().toLocaleTimeString('zh-CN')+' · 每 5 秒刷新';
    }catch(error){if(current())sync.textContent=error.message;}
    finally{if(current()){clearTimeout(taskTimer);taskTimer=setTimeout(load,5000);}}
  };
  if(bindingId)load();
  return el('div',{},heading('TASKS & HANDOFF','任务与工作日志','查看与网站共用的任务、执行角色和流程记录。Agent 通过项目 MCP 认领与提交。'),
    el('section',{class:'card'},bindings.length?el('div',{},field('项目连接',selector),sync,rows,inbox):
      empty('先连接项目','前往“项目与 MCP”绑定获准项目的工程目录。')),
    text('p','代码任务通过工程 MCP 认领和保存；创建任务与流程确认在网站操作。日志共享、代码备份与 Git 交付分别显示回执。','note'),
    button('打开网站后台',()=>invoke('openAdmin')));
}
function checkpointsScreen(){
  const rows=el('div',{}),account=state.user?.id;
  invoke('localWork').then(data=>{
    if(view!=='checkpoints' || state.user?.id!==account || !rows.isConnected)return;
    const names={pending:'待上传',shared:'日志已共享',blocked:'上传被拦截，工作保留',local_only:'仅本机后台观察'};
    rows.replaceChildren(...data.items.map(item=>el('article',{class:'binding'},
      el('div',{class:'row between'},text('h3',item.summary),text('span',names[item.state] || item.state,'pill')),
      text('p',`${new Date(item.created_at).toLocaleString('zh-CN')} · ${item.files} 个变更文件`,'note'),
      text('p',item.message,'note'),item.last_error?text('p',item.last_error,'warning'):null,
      text('p','代码备份：'+(item.server_code_backup?'服务器已接收':({pending:'等待补传',blocked:'补传受阻',local_only:'仅本机'}[item.backup_state] || '尚未确认')),'note'),
      item.backup_error?text('p','备份未完成：'+item.backup_error,'warning'):null,
      ['pending','shared'].includes(item.state) && !item.server_code_backup?button('重试上传',async()=>{
        const result=await invoke('retryCheckpoint',{id:item.local_checkpoint_id});toast(result.message,result.server_code_backup);render();}):null,
      button('导出检查点',async()=>{const result=await invoke('exportCheckpoint',{id:item.local_checkpoint_id});
        if(!result.cancelled){toast(result.working_tree_directory?'完整代码工作树已导出到 '+result.working_tree_directory+'。请审查后接续开发；不含 Git 历史。':
          '已导出到 '+result.directory+'。需单独检出基线，再核对变更清单恢复。',true);render();}}),
      item.can_remove?button('移除已导出的本机副本',async()=>{const result=await invoke('removeExportedCheckpoint',{id:item.local_checkpoint_id});
        if(!result.cancelled){toast('导出内容核验通过，本机加密副本已清理。',true);render();}}):null)));
    if(!data.items.length)rows.append(empty('还没有本机检查点','通过项目 MCP 准备代码任务并确认规则后，后台观察变更；每轮保存工作日志时另留独立检查点。'));
    if(data.unreadable_records)rows.prepend(text('p',`${data.unreadable_records} 个加密记录暂时无法读取，原文件已保留。请保留本机凭证并检查存储状态。`,'warning'));
  }).catch(error=>{rows.replaceChildren(text('p',error.message,'warning'));});
  return el('div',{},heading('LOCAL CHECKPOINTS','本机检查点与待发记录','本机检查点加密保存。“日志已共享”与“代码备份成功”各有独立回执；只有后者确认服务器接收了完整代码工作树。'),
    el('section',{class:'card'},el('div',{class:'actions'},button('刷新',render)),
      ...(state.workRuns || []).map(r=>el('div',{},
        r.observation_error?text('p','后台观察未完成：'+r.observation_error+' · '+r.task_id,'warning'):null,
        text('p',(r.record_status?({missing:'存在未记录变更',pending:'日志已记，等待共享',blocked:'日志上传被拦截',recorded:'当前观察未发现漏记'}[r.record_status.state] || r.record_status.state):'尚未核对记录')+' · '+r.task_id,
          r.record_status?.missing_record?'warning':'note'),
        text('p',r.record_status?.hook_bound?'已绑定宿主对话':'未绑定逐轮钩子，仅后台 Git 观察；逐轮覆盖尚未建立。','note'))),rows));
}
async function taskDetail(bindingId,taskId,generation,offset=0){
  const ticket=++taskDialogVersion,account=state.user?.id;
  const current=()=>view==='tasks' && ticket===taskDialogVersion && account===state.user?.id && taskBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  const data=await invoke('taskQuery',{bindingId,tool:'hub_get_task',args:{task_id:taskId}});
  const logs=await invoke('taskQuery',{bindingId,tool:'hub_list_worklogs',args:{task_id:taskId,offset,limit:20}});
  if(!current())return;
  const task=data.task;openTaskBinding=bindingId;
  openDialog(text('h2',task.title),text('p',(taskLabels[task.status] || task.status)+' · 版本 '+task.version),
    text('p','负责角色：'+(roleLabels[task.execution_role] || task.execution_role || '开发工程师')),
    text('pre',task.description+'\n\n验收：'+task.acceptance),
    data.coordination?el('section',{},text('h3','前置任务与范围占用'),
      text('p',data.coordination.dependencies_satisfied?'前置任务已满足。':'前置任务未完成，当前不能认领。'),
      ...data.coordination.dependencies.map(t=>text('p',`${t.title} · ${taskLabels[t.status]} · ${t.id}`)),
      data.coordination.has_restricted_dependencies?text('p','另有当前不可读取的前置关系，请由负责人协调。','note'):null,
      ...data.coordination.scope_conflicts.map(t=>text('p',`范围占用：${t.paths.join('、')} · ${taskLabels[t.status]} · ${t.task_id}`,'warning'))):null,
    text('h3',logs.visibility==='project'?'共享工作日志':'本人工作日志'),
    ...logs.items.map(log=>{
      const body=el('div',{}),details=el('details',{},text('summary',new Date(log.created_at).toLocaleString('zh-CN')+' · '+log.summary+' · '+log.outcome),body);
      let loaded=false;
      details.addEventListener('toggle',async()=>{
        if(!details.open || loaded)return;loaded=true;
        try{const full=await invoke('taskQuery',{bindingId,tool:'hub_read_worklog',args:{task_id:taskId,worklog_id:log.id}});
          if(!current() || !details.isConnected)return;
          body.replaceChildren(text('pre',`${full.notes}\n\n改动：${full.changed_paths.join('、')}\n验证：${full.tests}\n下一步：${full.next_steps}`),
            text('p','Git 引用：'+(full.git_commit || '未提供')+(full.delivery?'（服务器已核验任务分支）':'（上游未核验）'),'mono'),
            text('p','日志回执：'+full.receipt_hash,'mono'),
            full.snapshot?el('div',{},text('p',`服务器代码备份：${full.snapshot.baseline_files} 个基线文件 / ${full.snapshot.changed_files} 个新增或修改文件。可恢复代码工作树，不含 Git 历史。`,'note'),
              text('p','备份回执：'+full.snapshot.receipt_hash,'mono'),button('取回到本机检查点',async()=>{
                await invoke('recoverCheckpoint',{bindingId,taskId,worklogId:log.id});
                if(current())toast('代码已加密保存到本机；在“本机检查点”导出到新目录后审查。',true);
              })):text('p','尚无服务器代码备份回执。','warning'),
            full.delivery?text('p','Git 交付回执：'+full.delivery.receipt_hash+' · '+full.delivery.ref+'（未合并、未部署）','mono'):null);
        }catch(error){loaded=false;body.replaceChildren(text('p',error.message,'warning'));}
      });return details;
    }),
    !logs.items.length?text('p','没有当前账号可见的日志。','note'):null,
    el('div',{class:'actions'},offset?button('较新日志',()=>taskDetail(bindingId,taskId,generation,Math.max(0,offset-20))):null,
      logs.has_more?button('较早日志',()=>taskDetail(bindingId,taskId,generation,logs.next_offset)):null),
    text('h3','执行身份'),...data.runs.map(run=>text('p',`${roleLabels[run.role] || run.role} · ${run.harness} · ${run.user_id.slice(0,8)} / ${run.device_id.slice(0,8)} · ${run.state}`)),
    text('h3','可见流程证据'),...data.decisions.map(d=>el('details',{},text('summary',`${d.gate} · ${d.approved?'通过':'退回'}`),
      text('pre',d.evidence+(d.override_reason?'\n自审原因：'+d.override_reason:'')))),
    text('p','取回代码会保存为本机加密检查点，导出到新目录后审查。自动逐轮记录仍需宿主钩子配合。','note'),
    el('div',{class:'actions end'},task.repository_id?button('交付核验',()=>deliveryDialog(bindingId,task,generation)):null,
      task.repository_id?button('代码规范审计',()=>auditTaskDialog(bindingId,task,generation)):null,
      button('刷新',()=>taskDetail(bindingId,taskId,generation,offset)),button('关闭',()=>{openTaskBinding=null;closeDialog();},'primary')));
}
function auditLabel(item){return `${({queued:'待认领',running:'审查中',completed:'已提交报告'})[item.state]} · 规范 ${item.policy_version} · ${item.current?'当前来源':'已失效来源'}`;}
function auditScreen(){
  const generation=taskVersion,available=state.bindings.filter(b=>b.status==='active');
  if(!available.length)return empty('没有可用项目连接','请先接入获准的项目和仓库。');
  memoryBinding=available.some(b=>b.id===memoryBinding)?memoryBinding:available[0].id;
  const bindingId=memoryBinding,account=state.user?.id,panel=el('section',{class:'card'});
  const current=()=>view==='audits' && taskVersion===generation && memoryBinding===bindingId && state.user?.id===account;
  const picker=el('select',{},available.map(b=>el('option',{value:b.id,...(b.id===bindingId?{selected:true}:{})},b.projectName+' · '+(b.repositoryName || b.workspace))));
  picker.addEventListener('change',()=>{memoryBinding=picker.value;auditOffset=0;render();});
  const load=async()=>{
    try{
      const data=await invoke('taskQuery',{bindingId,tool:'hub_list_code_audits',args:{offset:auditOffset,limit:20}});
      if(!current())return;
      panel.replaceChildren(!data.items.length?empty('暂无审计','仓库开启代码规范后，经过服务器核验的代码推送会自动排队。'):el('div',{},
        ...data.items.map(item=>el('article',{class:'card'},text('h3',auditLabel(item)),text('p',item.git_commit,'mono'),
          text('p',item.reason || '等待获准审查者执行'),item.self_review?text('p','超管自审：'+item.override_reason,'warning'):null,
          button('报告与复审记录',()=>auditReportDialog(bindingId,item.id,'audits')))),
        el('div',{class:'actions'},auditOffset?button('上一页',()=>{auditOffset=Math.max(0,auditOffset-20);render();}):null,
          data.has_more?button('下一页',()=>{auditOffset=data.next_offset;render();}):null)));
    }catch(error){if(current())panel.replaceChildren(text('p',error.message,'warning'));}
    finally{if(current()){clearTimeout(taskTimer);taskTimer=setTimeout(load,5000);}}
  };load();
  return el('div',{},heading('CODE REVIEW','代码规范审计','按提交和规则版本跟踪审查。审查者在开发工具中执行，问题与每次复审保留；报告通过和必需 CI 分别核验。'),
    el('div',{class:'actions'},field('项目连接',picker),button('刷新',()=>render())),panel);
}
async function auditTaskDialog(bindingId,task,generation){
  const ticket=++taskDialogVersion,account=state.user?.id;
  const data=await invoke('taskQuery',{bindingId,tool:'hub_list_code_audits',args:{task_id:task.id,limit:30}});
  if(view!=='tasks' || generation!==taskVersion || ticket!==taskDialogVersion || account!==state.user?.id)return;
  const gate=data.gate;
  openDialog(text('h2','代码规范审计 · '+task.title),text('p',!gate.required?'本仓库尚未开启代码审计。':gate.ready?'当前审查和必需 CI 满足要求。':'当前审查或必需 CI 尚未满足要求，验收将被拦截。',gate.ready?'note':'warning'),
    text('p','开发工具使用 agent-hub-code-review 认领并执行。排队和通知不代表已经审查。','note'),
    ...data.items.map(item=>el('section',{class:'card'},text('h3',auditLabel(item)),text('p',item.git_commit,'mono'),
      text('p',item.reason || '尚未提交审查报告'),button('报告与复审记录',()=>auditReportDialog(bindingId,item.id,'tasks',()=>auditTaskDialog(bindingId,task,generation))))),
    !data.items.length?text('p','没有审计记录；发布负责人可在网页“仓库与资料 → 代码规范”设置。','note'):null,
    el('div',{class:'actions end'},button('刷新',()=>auditTaskDialog(bindingId,task,generation)),button('返回任务',()=>taskDetail(bindingId,task.id,generation)),button('关闭',closeDialog)));
}
async function auditReportDialog(bindingId,jobId,expectedView,back=null){
  openAuditBinding=bindingId;openAuditView=expectedView;
  const ticket=++taskDialogVersion,generation=taskVersion,account=state.user?.id;
  const current=()=>view===expectedView && ticket===taskDialogVersion && generation===taskVersion && state.user?.id===account && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  const data=await invoke('taskQuery',{bindingId,tool:'hub_read_code_audit',args:{job_id:jobId}});if(!current())return;
  const reason=el('textarea',{minlength:10,maxlength:2000,rows:3,placeholder:'说明阻塞、误报或复审原因；原报告保留。'});
  openDialog(text('h2','代码审计报告'),text('p',auditLabel(data)),text('p',data.git_commit,'mono'),
    data.self_review?text('p','超管自审：'+data.override_reason,'warning'):null,
    ...data.reports.map(r=>el('section',{class:'card'},text('h3',`第 ${r.lease_epoch} 次 · ${r.passed?'审查通过（CI另行核验）':r.content.conclusion==='incomplete'?'未完成覆盖':'审查未通过'}`),
      text('p',r.content.summary),text('p','审查人：'+r.author_id+' · '+new Date(r.created_at).toLocaleString('zh-CN')),
      text('p','逐规则结果：'+Object.entries(r.content.rule_checks).map(([k,v])=>k+' '+v).join('；')),
      ...r.content.findings.map(f=>el('details',{},text('summary',`${f.severity} · ${f.title} · ${f.path}:${f.line}`),text('p',f.evidence),text('p','建议：'+f.recommendation))),
      ...r.content.gaps.map(g=>text('p','未覆盖：'+g,'warning')),text('p',r.content.verification),text('p','报告回执：'+r.receipt_hash,'mono'))),
    !data.reports.length?text('p','尚无审查报告。','note'):null,text('h3','过程记录'),
    ...data.events.map(e=>text('p',`${new Date(e.created_at).toLocaleString('zh-CN')} · ${e.kind} · ${e.payload.reason || ''}`)),
    data.current?el('div',{},field('重新排队原因',reason),button('保留旧报告并重新排队',async()=>{
      if(reason.value.trim().length<10)throw new Error('请填写至少 10 字的原因。');
      await invoke('auditAction',{bindingId,tool:'hub_retry_code_audit',args:{job_id:jobId,expected_version:data.version,reason:reason.value.trim(),operation_id:crypto.randomUUID()}});
      if(current()){toast('已保存复审原因。',true);await auditReportDialog(bindingId,jobId,expectedView,back);}
    }),text('small','审查需要评审与共享日志权限；已完成报告的复审还需要发布权限。')):text('p','这些报告已失效，不能用于当前验收。','warning'),
    el('div',{class:'actions end'},back?button('返回审计列表',back):null,button('刷新',()=>auditReportDialog(bindingId,jobId,expectedView,back)),button('关闭',closeDialog)));
}
async function deliveryDialog(bindingId,task,generation,offset=0){
  const ticket=++taskDialogVersion,account=state.user?.id;
  const current=()=>view==='tasks' && ticket===taskDialogVersion && account===state.user?.id && taskBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  const data=await invoke('taskQuery',{bindingId,tool:'hub_list_delivery_evidence',args:{task_id:task.id,offset,limit:20}});
  if(!current())return;
  const release=data.release;
  task={...task,version:data.task_version,submission_id:release.worklog_id || task.submission_id};
  const refresh=()=>deliveryDialog(bindingId,task,generation,offset);
  const form=el('form',{},text('h3','从 GitHub 读取一次最新结果'),
    task.submission_id?el('div',{},text('p','核验此任务当前提交的工作检查点。','note'),el('input',{type:'hidden',name:'worklog_id',value:task.submission_id})):
      field('工作检查点 ID',el('input',{name:'worklog_id',required:true})),
    field('核验环节',el('select',{name:'kind'},Object.entries(deliveryLabels).map(([value,label])=>el('option',{value},label)))),
    field('GitHub 编号',el('input',{name:'provider_id',type:'number',min:1,max:Number.MAX_SAFE_INTEGER,required:true})),
    el('details',{},text('summary','合并与回滚关联（按需填写）'),
      field('合并凭据 ID（核验合并后提交时填写）',el('input',{name:'merge_evidence_id'})),
      field('被替换的部署凭据 ID（回滚时填写）',el('input',{name:'rollback_of_id'})),
      field('恢复目标的部署凭据 ID（回滚时填写）',el('input',{name:'rollback_to_id'}))),
    el('button',{type:'submit',class:'primary'},'核验并保存回执'));
  form.addEventListener('submit',async event=>{
    event.preventDefault();const submit=form.querySelector('[type=submit]');submit.disabled=true;
    const args=Object.fromEntries(new FormData(form));args.provider_id=Number(args.provider_id);args.task_id=task.id;
    args.expected_task_version=task.version;args.operation_id=crypto.randomUUID();
    for(const name of ['merge_evidence_id','rollback_of_id','rollback_to_id'])if(!args[name])delete args[name];
    try{await invoke('deliveryAction',{bindingId,tool:'hub_verify_delivery_evidence',args});if(current()){toast('服务器已保存外部观察回执。',true);await refresh();}}
    catch(error){toast(error.message);}finally{submit.disabled=false;}
  });
  openDialog(text('h2','交付核验 · '+task.title),text('p',release.ready?'当前提交的外部凭据满足已配置要求。':'当前提交的外部凭据尚未满足要求。',release.ready?'note':'warning'),
    text('p','规则版本 '+release.policy_version+'。这些是最近一次读取的提供方结果，不能证明目标服务持续健康。','note'),
    !release.checks.length?text('p','当前未设置必需的外部核验环节；可由发布负责人在网页“仓库与资料”中配置。','note'):null,
    ...release.checks.map(c=>text('p',`${deliveryLabels[c.kind]}${c.subject?' · '+c.subject:''}：${c.passed?'满足要求':!c.evidence_id?'尚未核验':!c.fresh?'已过有效期':'尚未成功'}${c.observed_at?' · '+new Date(c.observed_at).toLocaleString('zh-CN'):''}`,c.passed?'note':'warning')),
    text('h3','已保存的外部观察'),...data.items.map(item=>el('details',{},text('summary',`${deliveryLabels[item.kind]} · ${item.successful?'提供方报告成功':'提供方尚未报告成功'} · ${new Date(item.observation.observed_at).toLocaleString('zh-CN')}`),
      text('p',`状态：${deliveryStates[item.observation.state] || '其他状态'} · ${item.observation.workflow || item.observation.environment || ''}`),
      text('p',`实际核验提交：${item.observation.verified_commit}`,'mono'),text('p',`凭据 ID：${item.id}`,'mono'),
      text('p',`原工作提交：${item.git_commit}`,'mono'),text('p',`回执：${item.receipt_hash}`,'mono'))),
    el('div',{class:'actions'},offset?button('较新记录',()=>deliveryDialog(bindingId,task,generation,Math.max(0,offset-20))):null,
      data.has_more?button('较早记录',()=>deliveryDialog(bindingId,task,generation,data.next_offset)):null),data.can_verify?form:null,
    el('div',{class:'actions end'},button('返回任务',()=>taskDetail(bindingId,task.id,generation)),button('刷新',refresh),button('关闭',closeDialog)));
}
function workspaces(){return el('div',{},heading('PROJECT CONNECTIONS','项目与 MCP','每个连接绑定一个项目和一个工作目录。服务端在每次 MCP 调用时检查当前权限。'),
  state.user?.platform_role==='superadmin'?el('section',{class:'card management-card'},
    el('div',{class:'card-head'},text('h2','让 Agent 创建项目'),button('管理 MCP 配置',managementDialog,'primary')),
    text('p','连接项目管理 MCP 后，可直接让 Agent 列出和创建项目。创建完成的项目会出现在本机列表中，再选择工程文件夹建立项目连接。')):null,
  el('section',{class:'card'},el('div',{class:'card-head'},text('h2','已绑定的工作目录'),state.projects.length?button('绑定项目',bindDialog,'primary'):null),bindings()),
  !state.projects.length?text('p','当前设备还没有可用项目。请由超级管理员创建项目并批准对应授权。','warning'):null,
  text('p','当前提供项目读取、文档查询、进度查询与上报。','note'));}
function desktopScreen(){
  const settings=info.desktop || {};
  const toggle=(key,label,detail,{disabled=false}={})=>{
    const input=el('input',{type:'checkbox','aria-label':label});
    input.checked=!!settings[key];input.disabled=disabled;
    input.addEventListener('change',async()=>{
      input.disabled=true;
      try{info.desktop=await invoke('saveDesktopSettings',{[key]:input.checked});render();}
      catch(error){input.checked=!!settings[key];toast(error.message);}
      finally{input.disabled=disabled;}
    });
    return el('label',{class:'setting-row'},el('span',{},text('strong',label),text('small',detail)),input);
  };
  return el('div',{},heading('DESKTOP PREFERENCES','桌面与消息推送','关闭窗口后保留托盘后台，继续接收项目进度和权限变化。'),
    el('section',{class:'card online-update'},text('h2','PC 客户端在线升级'),
      text('p','当前版本 v'+(releaseState?.installed_version || info.version || state.version)+
        (releaseState?.latest_version?' · 发布版本 v'+releaseState.latest_version:'')),
      text('p',releaseState?.message || '安装版支持直接下载、校验并重启到新版。','note'),
      onlineUpdateButton()),
    el('section',{class:'card'},text('h2','Agent 一键安装连接'),
      text('p',state.setupEnabled?(state.setupAccountMatches?'已启用，连接绑定当前登录账号。':'已启用；首次登录后绑定账号，切换账号需要重新授权。'):'由一键安装入口启用并配置，可让 Agent 检查登录、列出项目和接入工程。'),
      el('div',{class:'actions'},
        !state.setupEnabled?button('启用安装连接',async()=>{await invoke('enableSetup');await update();toast('已启用，请运行仓库安装入口自动配置开发工具。',true);},'primary'):null,
        state.setupEnabled?button('重新授权（旧连接失效）',async()=>{await invoke('resetSetup');await update();toast('已重新授权，请重新运行安装入口更新 MCP 配置。',true);}):null,
        state.setupEnabled?button('停用安装连接',async()=>{await invoke('disableSetup');await update();toast('安装连接已停用，已有项目连接保持各自授权。',true);},'danger'):null),
      text('p','账号密码和验证码在桌面端填写。安装连接用于接入；开发使用绑定到具体工程的项目连接。','note')),
    el('section',{class:'card settings'},
      toggle('closeToTray','关闭窗口后在托盘运行','点击右上角关闭按钮仅隐藏窗口；退出后台请使用托盘菜单。'),
      toggle('notifications','接收消息推送','收到其他设备的项目进度、补齐的离线消息和连接权限变化时提醒。'),
      toggle('showNotificationDetails','在推送中显示工作摘要','关闭时只提示有新动态，工作内容在应用内查看。'),
      toggle('startAtLogin','开机启动','登录 Windows 后自动在托盘运行，可随时在此关闭。',
        {disabled:!settings.startupAvailable}),
      el('div',{class:'actions'},button('发送测试通知',async()=>{
        const result=await invoke('testNotification');
        toast(result.sent?'测试通知已提交给 Windows。若未弹出，请检查系统通知与免打扰设置。':'当前系统暂不支持桌面通知。',result.sent);
      },'primary'),button('打开网站管理后台',()=>invoke('openAdmin'))),
      text('p','推送需要桌面后台保持运行并连接服务器。退出后台后停止接收；重新连接后补齐项目记录。','note')));
}
function connection(){return el('div',{},heading('ACCOUNT & DEVICE','账号与设备','项目授权由服务器管理。桌面窗口关闭后，托盘后台会继续提供 MCP 连接。'),
  el('section',{class:'card'},el('div',{class:'card-head'},text('h2',state.login),pill(state.connection)),
    el('dl',{class:'account-details'},text('dt','服务器'),text('dd',state.origin,'mono'),text('dt','账号角色'),
      text('dd',state.user?.platform_role==='superadmin'?'超级管理员':state.user?.admin_eligible?'管理员账号':'项目成员'),
      text('dt','设备 ID'),text('dd',state.deviceId || '—','mono'),text('dt','客户端版本'),text('dd',info.version || '0.1.0')),
    el('div',{class:'actions'},button('刷新授权与连接',async()=>{await invoke('refresh');await update();toast('授权和连接状态已刷新。',true);},'primary'),
      releaseState?button('检查客户端更新',checkUpdate):null,
      button('打开管理后台',()=>invoke('openAdmin')),button('退出登录',()=>{
        openDialog(text('h2','退出当前账号'),text('p','本机 MCP 将停止响应项目请求。工作目录和配置文件会保留。'),
          dialogActions(button('确认退出',async()=>{const result=await invoke('logout');closeDialog();await update();
            toast(result.serverLogoutComplete?'已退出当前账号。':'本机已退出，服务器会话撤销未全部确认。',result.serverLogoutComplete);},'danger')));
      },'danger'))));}
async function checkUpdate(){
  releaseState=await invoke('checkUpdate');renderUpdate();
  if(releaseState?.can_continue)toast('客户端已是可用版本。',true);
}
function onlineUpdateButton(){
  const busy=['downloading','staging','restarting'].includes(releaseState?.status);
  const result=button(busy?'正在升级…':'在线升级',checkUpdate,'primary online-upgrade');
  result.disabled=busy;return result;
}
function renderUpdate(){
  const busy=['downloading','staging','restarting'].includes(releaseState?.status);
  for(const control of document.querySelectorAll('.online-upgrade')){
    control.disabled=busy;control.textContent=busy?'正在升级…':'在线升级';
  }
  const banner=document.getElementById('desktop-update');
  if(!banner)return;
  banner.hidden=!releaseState || releaseState.can_continue;
  if(banner.hidden)return;
  const versionText='当前 '+releaseState.installed_version+(releaseState.latest_version?' · 最新 '+releaseState.latest_version:'');
  const retry=['failed','unavailable'].includes(releaseState.status);
  banner.replaceChildren(el('div',{class:'row between'},
    el('div',{},text('strong',versionText),text('div',releaseState.message,'note')),
    retry?button('重试更新',checkUpdate):null),
    releaseState.status==='downloading'?el('progress',{max:100,value:releaseState.progress,'aria-label':'客户端下载进度'}):null);
}
function render(){
  if(!state)return;
  if(openAuditBinding && (view!==openAuditView || state.connection!=='active' ||
      !state.bindings.some(b=>b.id===openAuditBinding && b.status==='active'))){
    openAuditBinding=null;modal.replaceChildren();closeDialog();
  }
  clearTimeout(taskTimer);taskVersion++;
  if(openTaskBinding && (view!=='tasks' || !state.bindings.some(b=>b.id===openTaskBinding && b.status==='active'))){
    openTaskBinding=null;modal.replaceChildren();closeDialog();
  }
  const authenticated=state.connection==='active' || (state.connection==='offline' && !!state.user);
  const sidebar=el('aside',{class:'sidebar'},el('div',{class:'brand'},
    el('img',{src:'../assets/agent-hub.png',alt:'',class:'brand-icon'}),
    el('div',{},'Agent Hub',text('small','CONNECT · BUILD · SYNC'))),
    el('nav',{class:'nav'},Object.entries(titles).map(([name,title])=>button(title,()=>{
      if(authenticated || ['desktop','closeouts'].includes(name)){view=name;render();}
    },view===name && (authenticated || ['desktop','closeouts'].includes(name))?'active':''))),
    el('div',{class:'side-foot'},button('网站管理后台',()=>invoke('openAdmin'),'admin-shortcut'),
      text('strong',authenticated?state.login:'项目协作桌面端'),text('div','v'+(info.version || state.version)+' · Windows'),
      text('div',info.desktop?.closeToTray?'关闭窗口后留在托盘':'关闭窗口后退出后台')));
  let content;
  if(view==='desktop')content=desktopScreen();
  else if(view==='closeouts')content=closeoutsScreen();
  else if(!authenticated)content=state.connection==='pending'?pendingScreen():loginScreen();
  else content=view==='overview'?overview():view==='workspaces'?workspaces():view==='tasks'?tasksScreen():view==='audits'?auditScreen():view==='execution'?executionScreen():['guides','usage'].includes(view)?informationScreen(view):view==='memory'?memoryScreen():view==='checkpoints'?checkpointsScreen():view==='activity'?
    el('div',{},heading('PROJECT ACTIVITY','项目动态','记录来自服务器。断线恢复后按游标补齐，重复记录会自动合并。'),el('section',{class:'card'},events())):connection();
  root.replaceChildren(el('div',{class:'layout'},sidebar,el('main',{class:'content'},
    el('header',{class:'topbar'},text('div',authenticated || view==='desktop'?titles[view]:'欢迎使用 Agent Hub'),
      el('div',{class:'connection-line'},text('span',state.origin,'server-url'),pill(state.connection),
        authenticated && state.user?.platform_role==='superadmin'?button('管理 MCP',managementDialog):null,
        onlineUpdateButton(),
        button('打开网站后台',()=>invoke('openAdmin')))),
    el('section',{id:'desktop-update',class:'card update-status',role:'status',hidden:true}),
    state.message?text('div',state.message,'warning'):null,content)));
  renderUpdate();
}
window.hub.onState(next=>{state=next;render();});
window.hub.onUpdate(next=>{releaseState=next;if(view==='desktop')render();else renderUpdate();});
window.hub.onDesktop(next=>{info.desktop=next;render();});
window.hub.onNavigate(next=>{if(next==='activity'){view='activity';render();}});
(async()=>{info=await invoke('appInfo');releaseState=await invoke('updateInfo');await update();})().catch(error=>toast(error.message));
