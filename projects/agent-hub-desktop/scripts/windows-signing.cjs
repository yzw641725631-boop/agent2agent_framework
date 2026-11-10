'use strict';
const path=require('node:path'),{spawnSync}=require('node:child_process');
function options(env=process.env) {
  const tool=env.HUB_WINDOWS_SIGNTOOL,certificate=env.HUB_WINDOWS_CERT_THUMBPRINT;
  const required=env.HUB_REQUIRE_AUTHENTICODE==='1';
  if(!tool && !certificate) {
    if(required)throw new Error('Windows signing certificate is required for this build');
    return {enabled:false};
  }
  if(!tool || !path.isAbsolute(tool) || !/^[a-f0-9]{40}$/i.test(certificate || ''))throw new Error('Invalid Windows signing configuration');
  const timestamp=env.HUB_WINDOWS_TIMESTAMP_URL || 'https://timestamp.digicert.com';
  const url=new URL(timestamp);
  if(url.protocol!=='https:' || url.username || url.password || url.hash)throw new Error('Invalid timestamp service');
  return {enabled:true,tool,certificate:certificate.toUpperCase(),timestamp};
}
function sign(file,{env=process.env,run=spawnSync}={}) {
  const config=options(env);
  if(!config.enabled)return {authenticode_signed:false,reason:'certificate_not_configured'};
  if(process.platform!=='win32' || !path.isAbsolute(file) || !file.endsWith('.exe'))throw new Error('Invalid Windows signing target');
  const runOptions={windowsHide:true,encoding:'utf8',timeout:180000,env};
  const signed=run(config.tool,['sign','/sha1',config.certificate,'/fd','SHA256','/tr',config.timestamp,'/td','SHA256',file],runOptions);
  if(signed.status!==0)throw new Error('Windows certificate signing failed');
  const verified=run(config.tool,['verify','/pa','/all',file],runOptions);
  if(verified.status!==0)throw new Error('Windows signature verification failed');
  return {authenticode_signed:true,certificate_thumbprint:config.certificate,timestamped:true};
}
module.exports={options,sign};
