'use strict';
const net=require('node:net');
const crypto=require('node:crypto');
const fs=require('node:fs/promises');
const path=require('node:path');
const {HubError}=require('./remote.cjs');

function equalSecret(a,b) {
  if(typeof a!=='string' || typeof b!=='string')return false;
  const left=Buffer.from(a),right=Buffer.from(b);
  return left.length===right.length && crypto.timingSafeEqual(left,right);
}
class Broker {
  constructor(directory,dispatch) { this.directory=directory; this.dispatch=dispatch; this.connections=new Set(); }
  async start() {
    const nonce=crypto.randomBytes(16).toString('hex');
    await fs.mkdir(this.directory,{recursive:true,mode:0o700});
    this.endpoint=process.platform==='win32'?String.raw`\\.\pipe\agent-hub-${nonce}`:path.join(this.directory,'broker.sock');
    if (process.platform!=='win32') await fs.unlink(this.endpoint).catch(error=>{if(error.code!=='ENOENT')throw error;});
    this.server=net.createServer(socket=>{
      this.connections.add(socket);
      socket.setEncoding('utf8'); socket.setTimeout(30000,()=>socket.destroy());
      socket.on('error',()=>{}); socket.on('close',()=>this.connections.delete(socket));
      let input=''; let used=false;
      socket.on('data',chunk=>{
        input+=chunk;
        if (input.length>128*1024) { socket.destroy(); return; }
        if (!input.includes('\n') || used) return;
        used=true;
        (async()=>{
          try {
            const request=JSON.parse(input.split('\n')[0]);
            const result=await this.dispatch(request);
            socket.end(JSON.stringify({ok:true,result})+'\n');
          } catch(error) {
            socket.end(JSON.stringify({ok:false,code:error.code || 'BRIDGE_REQUEST_FAILED',
              message:error instanceof HubError?error.message:'桥接请求未完成，请查看桌面端连接状态。',
              update:error instanceof HubError?error.details?.update:undefined})+'\n');
          }
        })();
      });
    });
    await new Promise((resolve,reject)=>{this.server.once('error',reject);this.server.listen(this.endpoint,resolve);});
    if (process.platform!=='win32') await fs.chmod(this.endpoint,0o600);
  }
  async close() {
    for (const socket of this.connections) socket.destroy();
    if (this.server) await new Promise(resolve=>this.server.close(resolve));
    if (process.platform!=='win32' && this.endpoint) await fs.unlink(this.endpoint).catch(()=>{});
  }
}
async function call(descriptor,payload,{timeout=22000}={}) {
  return new Promise((resolve,reject)=>{
    const socket=net.createConnection(descriptor.endpoint); let input='',bytes=0,finished=false;
    const fail=()=>reject(new HubError('DESKTOP_OFFLINE','桌面后台未运行，请先打开 Agent Hub 并登录。'));
    socket.setEncoding('utf8'); socket.setTimeout(Math.max(250,Math.min(timeout,22000)),()=>{socket.destroy();fail();});
    socket.once('error',fail);
    socket.once('close',()=>{if(!finished)fail();});
    socket.on('connect',()=>socket.write(JSON.stringify({binding:descriptor.binding,secret:descriptor.secret,...payload})+'\n'));
    socket.on('data',chunk=>{
      input+=chunk;
      bytes+=Buffer.byteLength(chunk);
      if(bytes>4*1024*1024){socket.destroy();reject(new HubError('INVALID_BROKER_RESPONSE'));}
    });
    socket.on('end',()=>{
      finished=true;
      try { const result=JSON.parse(input); result.ok?resolve(result.result):reject(new HubError(result.code,result.message,{update:result.update})); }
      catch { reject(new HubError('INVALID_BROKER_RESPONSE')); }
    });
  });
}
module.exports={Broker,call,equalSecret};
