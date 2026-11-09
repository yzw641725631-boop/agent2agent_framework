'use strict';
const {app,BrowserWindow,ipcMain,dialog,clipboard,shell,Tray,Menu,Notification,nativeImage,session}=require('electron');
const path=require('node:path');
const fs=require('node:fs');
const {pathToFileURL}=require('node:url');
const os=require('node:os');
const crypto=require('node:crypto');
const {Vault}=require('./core/vault.cjs');
const {Controller}=require('./core/controller.cjs');
const {HubError}=require('./core/remote.cjs');
const {Updater}=require('./core/updater.cjs');
const {startSupervisor}=require('./core/update-launch.cjs');
const {sessionRule,sessionHook}=require('./core/session-hook.cjs');
const {icon}=require('./scripts/icon.cjs');
const desktop=require('./core/desktop-settings.cjs');

app.setName('Agent Hub');app.setAppUserModelId('org.agenthub.desktop');
const requestedData=app.commandLine.getSwitchValue('user-data-dir');
if(requestedData && path.isAbsolute(requestedData)){
  fs.mkdirSync(requestedData,{recursive:true});app.setPath('userData',requestedData);
}
if(process.env.AGENT_HUB_TEST_DATA && !app.isPackaged)app.setPath('userData',path.resolve(process.env.AGENT_HUB_TEST_DATA));
const isolatedTest=!!process.env.AGENT_HUB_TEST_DATA && !app.isPackaged;
const activationId=app.commandLine.getSwitchValue('agent-hub-activation');
if(!app.requestSingleInstanceLock()){app.quit();}
else start().catch(()=>{
  if(!activationId)dialog.showErrorBox('Agent Hub 无法启动','本机凭证或桌面后台未能初始化，原有配置已保留。');
  app.exit(1);
});

async function start(){
  await app.whenReady();
  let window,tray,windowReady,quitting=false,restarting=false,selectedWorkspace=null;
  let mutation=Promise.resolve();
  const directory=app.getPath('userData');
  const updater=app.isPackaged && process.platform==='win32' && process.arch==='x64' ? new Updater({
    directory,version:app.getVersion(),channel:require('./core/update-channel.cjs'),restart,
  }) : null;
  await updater?.open();
  const boot=await updater?.boot(process.execPath,activationId);
  if(boot?.target){
    app.relaunch({execPath:boot.target,args:['--user-data-dir='+directory,
      ...(app.commandLine.hasSwitch('hidden')?['--hidden']:[])]});app.quit();return;
  }
  const entry=pathToFileURL(path.join(__dirname,'renderer/index.html')).href;
  const vault=new Vault(directory,require('electron').safeStorage);
  await vault.load();
  const appIcon=nativeImage.createFromBuffer(icon());
  if(!updater || path.relative(updater.versions,process.execPath).startsWith('..')){
    vault.data.desktopLauncher=process.execPath;await vault.save();
  }
  const startupAvailable=app.isPackaged && process.platform==='win32';
  const startupOptions=enabled=>({openAtLogin:enabled,
    path:vault.data.desktopLauncher || process.execPath,args:['--hidden','--user-data-dir='+directory]});
  const desktopInfo=()=>({...desktop.preferences(vault.data),notificationsSupported:Notification.isSupported(),
    startupAvailable,startAtLogin:startupAvailable?
      app.getLoginItemSettings({path:startupOptions(false).path,args:startupOptions(false).args}).openAtLogin:false});
  const controller=new Controller({vault,directory,executable:process.execPath,
    bridgeScript:path.join(__dirname,'core/bridge.cjs'),updater});
  session.defaultSession.setPermissionRequestHandler((_webContents,_permission,callback)=>callback(false));
  session.defaultSession.setPermissionCheckHandler(()=>false);

  function show(){
    if(window && !window.isDestroyed()){window.show();window.focus();return windowReady;}
    window=new BrowserWindow({width:1240,height:850,minWidth:920,minHeight:680,show:false,
      title:'Agent Hub',backgroundColor:'#f4f7fb',icon:appIcon,
      webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,sandbox:true,
        nodeIntegration:false,webSecurity:true,devTools:!app.isPackaged}});
    window.setMenuBarVisibility(false);
    window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    window.webContents.on('will-navigate',(event,url)=>{if(url!==entry)event.preventDefault();});
    window.on('close',event=>{if(!quitting && !isolatedTest){
      event.preventDefault();if(desktop.preferences(vault.data).closeToTray)window.hide();else app.quit();
    }});
    window.once('ready-to-show',()=>{if(!app.commandLine.hasSwitch('hidden'))window.show();});
    windowReady=window.loadURL(entry);return windowReady;
  }
  async function restart(release){
    restarting=true;
    try{
      await mutation;
      await controller.drainProjectOperations();
      if(quitting)throw new Error('Application is closing');
      await startSupervisor(process.execPath,directory,{...release,hidden:app.commandLine.hasSwitch('hidden')});
      app.quit();
    }catch(error){
      restarting=false;controller.draining=false;throw error;
    }
  }
  const sendState=state=>{if(window && !window.isDestroyed())window.webContents.send('hub:state',state);};
  controller.on('state',sendState);
  updater?.on('change',status=>{
    if(window && !window.isDestroyed())window.webContents.send('hub:update',status);
    refreshTray();
  });
  async function showActivity(){await show();window.webContents.send('hub:navigate','activity');}
  function notify(data,{test=false}={}){
    const content=desktop.notificationContent(data,desktop.preferences(vault.data),{test});
    if(!content || !Notification.isSupported())return {sent:false,supported:Notification.isSupported()};
    const notification=new Notification({...content,icon:appIcon,silent:false});
    notification.on('click',()=>{showActivity().catch(()=>{});});
    notification.show();return {sent:true,supported:true};
  }
  controller.on('notification',data=>{if(!isolatedTest)notify(data);});
  const openAdmin=()=>shell.openExternal(controller.profile?.origin || 'https://47.109.23.86:18180');
  async function saveDesktopSettings(patch){
    desktop.changes(patch);
    if(Object.hasOwn(patch,'startAtLogin') && !startupAvailable)throw new HubError('INVALID_DESKTOP_REQUEST',
      '请安装正式 Windows 客户端后配置开机启动。');
    const previous=desktop.preferences(vault.data),next={...previous,...patch};
    if(Object.hasOwn(patch,'startAtLogin'))app.setLoginItemSettings(startupOptions(next.startAtLogin));
    vault.data.desktop=next;
    try{await vault.save();}catch(error){
      vault.data.desktop=previous;
      if(Object.hasOwn(patch,'startAtLogin'))app.setLoginItemSettings(startupOptions(previous.startAtLogin));
      throw error;
    }
    refreshTray();
    if(window && !window.isDestroyed())window.webContents.send('hub:desktop',desktopInfo());
    return desktopInfo();
  }
  function refreshTray(){
    if(!tray)return;
    const settings=desktopInfo();
    const toggle=key=>item=>saveDesktopSettings({[key]:item.checked}).catch(()=>refreshTray());
    tray.setContextMenu(Menu.buildFromTemplate([
      {label:'打开 Agent Hub',click:show},{label:'打开网站管理后台',click:openAdmin},
      {label:'查看项目动态',click:showActivity},{type:'separator'},
      {label:'在线升级 PC 客户端',enabled:!!updater,
        click:()=>updater?.check({fresh:true,retry:true}).catch(()=>{})},
      {label:'接收消息推送',type:'checkbox',checked:settings.notifications,click:toggle('notifications')},
      {label:'关闭窗口后在托盘运行',type:'checkbox',checked:settings.closeToTray,click:toggle('closeToTray')},
      {label:'开机启动',type:'checkbox',checked:settings.startAtLogin,enabled:startupAvailable,
        click:toggle('startAtLogin')},
      {label:'发送测试通知',click:()=>notify({title:'Agent Hub 测试通知',
        body:'消息推送已连接。关闭窗口后，托盘后台可继续接收项目动态。'},{test:true})},
      {type:'separator'},{label:'退出桌面后台',click:()=>app.quit()},
    ]));
  }
  const actions={
    state:()=>controller.state(),login:data=>controller.login(data),sendEmail:()=>controller.sendEmail(),
    verifyEmail:data=>controller.verifyEmail(data.code),checkApproval:()=>controller.checkApproval(),
    refresh:()=>controller.refresh(),logout:()=>controller.logout(),
    localWork:()=>{
      if(controller.connection==='signed_out')throw new HubError('LOGIN_REQUIRED');
      return controller.work.list();
    },
    retryCheckpoint:data=>controller.work.retry(data.id),
    retryCloseout:async()=>{await controller.closeouts.tick();return controller.closeouts.status();},
    recoverCheckpoint:data=>controller.invokeProject(controller.binding(data.bindingId),'hub_download_work_backup',{
      task_id:data.taskId,worklog_id:data.worklogId,operation_id:crypto.randomUUID()}),
    exportCheckpoint:async data=>{
      if(controller.connection==='signed_out')throw new HubError('LOGIN_REQUIRED');
      if(typeof data.id!=='string' || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(data.id))throw new HubError('INVALID_DESKTOP_REQUEST');
      const result=await dialog.showOpenDialog(window,{title:'选择检查点导出的父目录（会新建独立文件夹）',properties:['openDirectory']});
      if(result.canceled)return {cancelled:true};
      return controller.work.export(data.id,path.join(result.filePaths[0],'agent-hub-checkpoint-'+data.id));
    },
    removeExportedCheckpoint:async data=>{
      if(controller.connection==='signed_out')throw new HubError('LOGIN_REQUIRED');
      const result=await dialog.showMessageBox(window,{type:'question',title:'移除已导出的本机副本',
        message:'只清理已共享日志且已导出的加密检查点。',detail:'清理前会重新核验导出说明和代码。请妥善保存导出目录，并确认服务器代码备份状态。',
        buttons:['取消','核验导出并移除本机副本'],defaultId:0,cancelId:0});
      if(result.response!==1)return {cancelled:true};
      return controller.work.removeExported(data.id);
    },
    taskQuery:data=>{
      if(!['hub_list_tasks','hub_get_task','hub_list_worklogs','hub_read_worklog','hub_get_memory_status','hub_search_memory','hub_read_memory_source'].includes(data.tool))throw new HubError('INVALID_DESKTOP_REQUEST');
      return controller.invokeProject(controller.binding(data.bindingId),data.tool,data.args || {});
    },
    chooseWorkspace:async()=>{
      const result=await dialog.showOpenDialog(window,{title:'选择这个项目的工作目录',properties:['openDirectory']});
      selectedWorkspace=result.canceled?null:result.filePaths[0];return selectedWorkspace;
    },
    bind:async data=>{
      if(!selectedWorkspace)throw new HubError('INVALID_WORKSPACE','请先选择项目文件夹。');
      const result=await controller.bind({projectId:data.projectId,repositoryId:data.repositoryId,workspace:selectedWorkspace});
      selectedWorkspace=null;return result;
    },
    repositoryList:data=>controller.invokeProject(controller.binding(data.bindingId),'hub_list_repositories',{}),
    assembleRepositories:data=>controller.invokeProject(controller.binding(data.bindingId),'hub_assemble_project',{
      repository_ids:data.repositoryIds,harness:data.harness,operation_id:data.operationId}),
    fetchRepository:data=>controller.invokeProject(controller.binding(data.bindingId),'hub_fetch_repository',{}),
    disconnect:data=>controller.disconnect(data.id),preview:data=>controller.configurationPreview(data.id),
    managementConfiguration:()=>controller.management.configuration(),
    copyManagementConfiguration:async()=>{
      const configuration=await controller.management.configuration();
      clipboard.writeText(JSON.stringify(configuration,null,2));return {copied:true};
    },
    disconnectManagement:()=>controller.management.disable(),
    enableSetup:()=>controller.setup.enable(),
    disableSetup:()=>controller.setup.disable(),
    resetSetup:async()=>{await controller.setup.disable();return controller.setup.enable();},
    install:data=>controller.installConfiguration(data),
    copyConfiguration:data=>{clipboard.writeText(JSON.stringify(controller.configuration(controller.binding(data.id)),null,2));return {copied:true};},
    copySessionRule:()=>{clipboard.writeText(sessionRule);return {copied:true};},
    copySessionHook:data=>{
      if(!['claude','codex'].includes(data.host))throw new HubError('INVALID_DESKTOP_REQUEST');
      const configuration=controller.configuration(controller.binding(data.id));
      clipboard.writeText(JSON.stringify(sessionHook(Object.values(configuration.mcpServers)[0],data.host),null,2));
      return {copied:true};
    },
    updateInfo:()=>updater?.state || null,
    checkUpdate:()=>updater?.check({fresh:true,retry:true}) || null,
    openAdmin,
    desktopSettings:()=>desktopInfo(),saveDesktopSettings,
    testNotification:()=>notify({title:'Agent Hub 测试通知',
      body:'这是本机测试通知，项目有新进度时也会在此提示。'},{test:true}),
    appInfo:()=>({version:app.getVersion(),deviceName:os.hostname(),platform:process.platform,
      userData:app.getPath('userData'),desktop:desktopInfo()}),
  };
  ipcMain.handle('hub:invoke',async(event,action,data={})=>{
    if(event.sender!==window?.webContents || event.senderFrame?.url!==entry ||
       typeof action!=='string' || !Object.hasOwn(actions,action) || !data || typeof data!=='object' ||
       Array.isArray(data) || JSON.stringify(data).length>64*1024)return {ok:false,code:'INVALID_DESKTOP_REQUEST',message:'请求无效。'};
    const invoke=async()=>{
      try{return {ok:true,result:await actions[action](data)};}
      catch(error){return {ok:false,code:error.code || 'DESKTOP_OPERATION_FAILED',
        message:error instanceof HubError?error.message:'操作未完成，原配置和工作目录已保留。'};}
    };
    if(['state','appInfo','updateInfo','desktopSettings','testNotification','copyConfiguration',
      'copySessionRule','copySessionHook','openAdmin','checkUpdate','taskQuery'].includes(action))return invoke();
    if(restarting || quitting)return {ok:false,code:'CLIENT_UPDATE_REQUIRED',message:'桌面端正在切换版本。'};
    const result=mutation.then(invoke,invoke);mutation=result.then(()=>{});return result;
  });
  if(!isolatedTest){
    tray=new Tray(appIcon.resize({width:32,height:32}));tray.setToolTip('Agent Hub · 项目协作');
    refreshTray();tray.on('double-click',show);
  }
  app.on('second-instance',()=>{
    const action=mutation.then(()=>controller.setup.consumeRequest());
    mutation=action.catch(()=>{});action.then(show,show);
  });app.on('activate',show);
  app.on('before-quit',event=>{
    if(quitting)return;
    event.preventDefault();quitting=true;tray?.destroy();
    Promise.allSettled([updater?.close(),controller.close()]).then(()=>app.quit());
  });
  app.on('window-all-closed',()=>{if(isolatedTest)app.quit();});
  await controller.start();
  await controller.setup.consumeRequest();
  await show();
  if(boot?.pending)await updater.activate(boot.pending);
  updater?.check({fresh:true}).catch(()=>{});
}
