'use strict';
const root=document.getElementById('app');
const notice=document.getElementById('notice');
const modal=document.getElementById('dialog');
let state,info={},releaseState,view='overview',selectedFolder=null;
let taskBinding=null,taskVersion=0,taskTimer,openTaskBinding=null,taskDialogVersion=0;
modal.addEventListener('close',()=>{taskDialogVersion++;openTaskBinding=null;});
const taskLabels={draft:'待准入',ready:'待认领',in_progress:'开发中',review:'待评审',testing:'待测试',release_ready:'待发布确认',completed:'已完成'};
const labels={active:'已连接',pending:'等待验证 / 审批',offline:'连接中断',blocked:'已拦截',signed_out:'未登录',
  reauth_required:'需要重新登录',running:'执行中',checkpoint:'检查点',completed:'已完成',approved:'已批准'};
const titles={overview:'协作概览',workspaces:'项目与 MCP',tasks:'任务与日志',activity:'项目动态',connection:'账号与设备',desktop:'桌面设置'};
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
    el('div',{class:'card'},form),text('p','超级管理员通过密码和本设备邮箱验证码登录；其他账号按设备审批流程登录。','note'));
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
    ):null,
    dialogActions(button('复制通用配置',async()=>{await invoke('copyConfiguration',{id:binding.id});toast('MCP 配置已复制。',true);}),
      button('预览 .mcp.json',()=>previewDialog(binding),'primary')));
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
    el('div',{class:'row between'},text('h3',binding.projectName),pill(binding.status)),
    text('div',binding.workspace,'meta mono'),binding.message?text('p',binding.message,'warning'):null,
    el('div',{class:'row'},button('MCP 配置',()=>configDialog(binding)),button('停用连接',()=>{
      openDialog(text('h2','停用这个 MCP 连接'),text('p',binding.projectName),
        text('p','此连接会立即失效。工作文件和已经写入的 .mcp.json 会保留，你可以在 harness 配置里移除对应项。'),
        dialogActions(button('确认停用',async()=>{await invoke('disconnect',{id:binding.id});closeDialog();await update();},'danger')));
    },'quiet')))));
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
function tasksScreen(){
  const bindings=state.bindings.filter(b=>b.status==='active');
  taskBinding=bindings.some(b=>b.id===taskBinding)?taskBinding:bindings[0]?.id;
  const selector=el('select',{'aria-label':'任务所属连接'},bindings.map(b=>el('option',{value:b.id},b.projectName+' · '+b.workspace)));
  selector.value=taskBinding || '';
  selector.addEventListener('change',()=>{taskBinding=selector.value;closeDialog();openTaskBinding=null;render();});
  const rows=el('div',{}),sync=text('p','正在读取…','note'),generation=taskVersion,bindingId=taskBinding;
  const current=()=>view==='tasks' && taskVersion===generation && taskBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  let offset=0;
  const load=async()=>{
    if(!current())return;
    try{
      const data=await invoke('taskQuery',{bindingId,tool:'hub_list_tasks',args:{offset,limit:50}});
      if(!current())return;
      rows.replaceChildren(...data.items.map(task=>el('article',{class:'binding'},
        el('div',{class:'row between'},text('h3',task.title),text('span',taskLabels[task.status],'pill')),
        text('p',`${task.scope.repository} · ${task.scope.platform} · ${task.scope.paths.join('、')}`,'note'),
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
    el('section',{class:'card'},bindings.length?el('div',{},field('项目连接',selector),sync,rows):
      empty('先连接项目','前往“项目与 MCP”绑定获准项目的工程目录。')),
    text('p','人员认领、创建任务和流程确认可在网站“任务与日志”操作。日志尚不包含代码快照。','note'),
    button('打开网站后台',()=>invoke('openAdmin')));
}
async function taskDetail(bindingId,taskId,generation,offset=0){
  const ticket=++taskDialogVersion,account=state.user?.id;
  const current=()=>view==='tasks' && ticket===taskDialogVersion && account===state.user?.id && taskBinding===bindingId && state.bindings.some(b=>b.id===bindingId && b.status==='active');
  const data=await invoke('taskQuery',{bindingId,tool:'hub_get_task',args:{task_id:taskId}});
  const logs=await invoke('taskQuery',{bindingId,tool:'hub_list_worklogs',args:{task_id:taskId,offset,limit:20}});
  if(!current())return;
  const task=data.task;openTaskBinding=bindingId;
  openDialog(text('h2',task.title),text('p',(taskLabels[task.status] || task.status)+' · 版本 '+task.version),
    text('pre',task.description+'\n\n验收：'+task.acceptance),
    text('h3',logs.visibility==='project'?'共享工作日志':'本人工作日志'),
    ...logs.items.map(log=>el('details',{},text('summary',new Date(log.created_at).toLocaleString('zh-CN')+' · '+log.summary+' · '+log.outcome),
      text('pre',`${log.notes}\n\n改动：${log.changed_paths.join('、')}\n验证：${log.tests}\n下一步：${log.next_steps}`),
      text('p','Git 引用：'+(log.git_commit || '未提供')+'（未核验）','mono'),text('p','回执：'+log.receipt_hash,'mono'))),
    !logs.items.length?text('p','没有当前账号可见的日志。','note'):null,
    el('div',{class:'actions'},offset?button('较新日志',()=>taskDetail(bindingId,taskId,generation,Math.max(0,offset-20))):null,
      logs.has_more?button('较早日志',()=>taskDetail(bindingId,taskId,generation,logs.next_offset)):null),
    text('h3','执行身份'),...data.runs.map(run=>text('p',`${run.role} · ${run.harness} · ${run.user_id.slice(0,8)} / ${run.device_id.slice(0,8)} · ${run.state}`)),
    text('h3','可见流程证据'),...data.decisions.map(d=>el('details',{},text('summary',`${d.gate} · ${d.approved?'通过':'退回'}`),
      text('pre',d.evidence+(d.override_reason?'\n自审原因：'+d.override_reason:'')))),
    text('p','当前保存日志原文和提交引用；代码快照备份、自动逐轮钩子仍待接入。','note'),
    el('div',{class:'actions end'},button('刷新',()=>taskDetail(bindingId,taskId,generation,offset)),button('关闭',()=>{openTaskBinding=null;closeDialog();},'primary')));
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
  clearTimeout(taskTimer);taskVersion++;
  if(openTaskBinding && (view!=='tasks' || !state.bindings.some(b=>b.id===openTaskBinding && b.status==='active'))){
    openTaskBinding=null;modal.replaceChildren();closeDialog();
  }
  const authenticated=state.connection==='active' || (state.connection==='offline' && !!state.user);
  const sidebar=el('aside',{class:'sidebar'},el('div',{class:'brand'},
    el('img',{src:'../assets/agent-hub.png',alt:'',class:'brand-icon'}),
    el('div',{},'Agent Hub',text('small','CONNECT · BUILD · SYNC'))),
    el('nav',{class:'nav'},Object.entries(titles).map(([name,title])=>button(title,()=>{
      if(authenticated || name==='desktop'){view=name;render();}
    },view===name && (authenticated || name==='desktop')?'active':''))),
    el('div',{class:'side-foot'},button('网站管理后台',()=>invoke('openAdmin'),'admin-shortcut'),
      text('strong',authenticated?state.login:'项目协作桌面端'),text('div','v'+(info.version || state.version)+' · Windows'),
      text('div',info.desktop?.closeToTray?'关闭窗口后留在托盘':'关闭窗口后退出后台')));
  let content;
  if(view==='desktop')content=desktopScreen();
  else if(!authenticated)content=state.connection==='pending'?pendingScreen():loginScreen();
  else content=view==='overview'?overview():view==='workspaces'?workspaces():view==='tasks'?tasksScreen():view==='activity'?
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
