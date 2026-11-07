'use strict';
const root=document.getElementById('app');
const notice=document.getElementById('notice');
const modal=document.getElementById('dialog');
let state,info={},releaseState,view='overview',selectedFolder=null;
const labels={active:'已连接',pending:'等待验证 / 审批',offline:'连接中断',blocked:'已拦截',signed_out:'未登录',
  reauth_required:'需要重新登录',running:'执行中',checkpoint:'检查点',completed:'已完成',approved:'已批准'};
const titles={overview:'协作概览',workspaces:'项目与 MCP',activity:'项目动态',connection:'账号与设备',desktop:'桌面设置'};
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
      text('dd',state.user?.platform_role==='superadmin'?'超级管理员':state.user?.platform_role==='admin'?'管理员':'项目成员'),
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
  else content=view==='overview'?overview():view==='workspaces'?workspaces():view==='activity'?
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
