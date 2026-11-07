'use strict';
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
const {spawnSync}=require('node:child_process');
async function main(){
  if(process.platform!=='win32' || process.arch!=='x64')throw new Error('Build this package on Windows x64.');
  const root=path.resolve(__dirname,'..'),manifest=require('../package.json');
  const dist=path.join(root,'dist'),name=`AgentHub-${manifest.version}-win-x64`,target=path.join(dist,name);
  const runtime=path.join(root,'node_modules/electron/dist');
  await fs.access(path.join(runtime,'electron.exe'));
  if(path.relative(dist,target)!==name)throw new Error('Invalid output directory');
  await fs.mkdir(dist,{recursive:true});
  if(!process.argv.includes('--archive-only')){
  try{await fs.access(target);throw new Error('Output already exists; move the previous build before packaging again.');}catch(error){if(error.code!=='ENOENT')throw error;}
  // The unpacked Agent Hub app replaces Electron's default app.
  await fs.cp(runtime,target,{recursive:true,
    filter:source=>path.relative(runtime,source)!==path.join('resources','default_app.asar')});
  await fs.rename(path.join(target,'electron.exe'),path.join(target,'Agent Hub.exe'));
  await require('rcedit')(path.join(target,'Agent Hub.exe'),{
    icon:path.join(root,'assets','agent-hub.ico'),'file-version':manifest.version,
    'product-version':manifest.version,'version-string':{
      ProductName:'Agent Hub',FileDescription:'Agent Hub 项目协作桌面端',
      OriginalFilename:'Agent Hub.exe',CompanyName:'Agent Hub',
    },
  });
  const app=path.join(target,'resources/app');await fs.mkdir(app,{recursive:true});
  for(const item of ['main.cjs','preload.cjs','core','renderer','assets'])await fs.cp(path.join(root,item),path.join(app,item),{recursive:true});
  await fs.mkdir(path.join(app,'scripts'));await fs.copyFile(path.join(root,'scripts/icon.cjs'),path.join(app,'scripts/icon.cjs'));
  await fs.writeFile(path.join(app,'package.json'),JSON.stringify({name:manifest.name,productName:manifest.productName,version:manifest.version,private:true,main:manifest.main},null,2));
  await fs.copyFile(path.join(root,'README.md'),path.join(target,'使用说明.md'));
  const hashes=[];
  async function walk(folder){for(const entry of await fs.readdir(folder,{withFileTypes:true})){
    const file=path.join(folder,entry.name);if(entry.isDirectory())await walk(file);
    else hashes.push({path:path.relative(target,file).split(path.sep).join('/'),sha256:crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex')});
  }}
  await walk(target);await fs.writeFile(path.join(target,'files.sha256.json'),JSON.stringify(hashes,null,2));
  }else{
    const packaged=JSON.parse(await fs.readFile(path.join(target,'resources/app/package.json'),'utf8'));
    if(packaged.name!==manifest.name || packaged.version!==manifest.version)throw new Error('Existing build does not match manifest.');
  }
  const archive=path.join(dist,name+'.zip');
  const result=spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',
    '$ErrorActionPreference="Stop"; Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::CreateFromDirectory($env:AGENT_HUB_PACKAGE_SOURCE, $env:AGENT_HUB_PACKAGE_ZIP, [System.IO.Compression.CompressionLevel]::Optimal, $true)'],
    {env:{...process.env,AGENT_HUB_PACKAGE_SOURCE:target,AGENT_HUB_PACKAGE_ZIP:archive},encoding:'utf8',windowsHide:true,timeout:300000});
  if(result.status!==0)throw new Error('Archive failed: '+(result.stderr || 'unknown error'));
  const digest=crypto.createHash('sha256').update(await fs.readFile(archive)).digest('hex');
  await fs.writeFile(archive+'.sha256',digest+'  '+path.basename(archive)+'\n');
  process.stdout.write(JSON.stringify({directory:target,archive,bytes:(await fs.stat(archive)).size,sha256:digest})+'\n');
}
main().catch(error=>{process.stderr.write(error.message+'\n');process.exitCode=1;});
