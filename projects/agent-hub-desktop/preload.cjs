'use strict';
const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('hub',{
  invoke:async(action,data={})=>{
    const response=await ipcRenderer.invoke('hub:invoke',action,data);
    if(!response.ok){const error=new Error(response.message);error.code=response.code;throw error;}
    return response.result;
  },
  onState:callback=>{
    if(typeof callback!=='function')return ()=>{};
    const listener=(_event,state)=>callback(state);
    ipcRenderer.on('hub:state',listener);return ()=>ipcRenderer.removeListener('hub:state',listener);
  },
  onUpdate:callback=>{
    if(typeof callback!=='function')return ()=>{};
    const listener=(_event,status)=>callback(status);
    ipcRenderer.on('hub:update',listener);return ()=>ipcRenderer.removeListener('hub:update',listener);
  },
  onDesktop:callback=>{
    if(typeof callback!=='function')return ()=>{};
    const listener=(_event,value)=>callback(value);
    ipcRenderer.on('hub:desktop',listener);return ()=>ipcRenderer.removeListener('hub:desktop',listener);
  },
  onNavigate:callback=>{
    if(typeof callback!=='function')return ()=>{};
    const listener=(_event,value)=>callback(value);
    ipcRenderer.on('hub:navigate',listener);return ()=>ipcRenderer.removeListener('hub:navigate',listener);
  },
});
