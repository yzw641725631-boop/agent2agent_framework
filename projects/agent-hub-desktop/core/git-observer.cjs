'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const {HubError}=require('./remote.cjs');
const run=promisify(execFile),sha=data=>crypto.createHash('sha256').update(data).digest('hex');
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value && typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
const digest=value=>sha(Buffer.from(canonical(value)));
const fail=(code,message)=>{throw new HubError(code,message);};
function keyPath(value){
  if(typeof value!=='string' || !value || value.length>800 || value!==value.normalize('NFC') || /[\\:\x00-\x1f\x7f]/.test(value) ||
    value.startsWith('/') || value.split('/').some(p=>!p || p==='.' || p==='..' || /[. ]$/.test(p)))fail('WORK_PATH_UNSAFE','路径不能包含链接逃逸、点段、驱动器或不稳定的名称。');
  return value.toLowerCase();
}
function violations(paths,scope,policy){
  const allowed=scope.paths.map(keyPath),protectedPaths=policy.protected_paths.map(keyPath),result=[];
  for(const original of paths){
    const key=keyPath(original),under=p=>key===p || key.startsWith(p+'/');
    if(key.split('/').some(p=>['.git','.ssh','.env'].includes(p) || p.startsWith('.env.') && p!=='.env.example' || p.includes('明文')) ||
      /\.(pem|key|p12|pfx)$/.test(key) || protectedPaths.some(under))result.push({path:original,code:'WORK_PATH_PROTECTED'});
    else if(!allowed.some(under))result.push({path:original,code:'TASK_PATH_OUT_OF_SCOPE'});
  }
  return result;
}
async function git(workspace,args,{input,maxBuffer=4*1024*1024+65536}={}){
  const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!k.toUpperCase().startsWith('GIT_')));
  Object.assign(env,{GIT_CONFIG_NOSYSTEM:'1',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0'});
  try{
    const job=run('git',['--no-optional-locks','--no-replace-objects','--literal-pathspecs','-c','core.fsmonitor=false',
      '-c','core.untrackedCache=false',...args],{cwd:workspace,env,windowsHide:true,encoding:'buffer',maxBuffer,timeout:15000});
    if(input!==undefined)job.child.stdin.end(input);
    const r=await job;
    return r.stdout;
  }catch{fail('WORK_GIT_UNAVAILABLE','无法读取 Git 工程状态，请检查目录、Git 和仓库所有权；工作文件已保留。');}
}
async function repository(workspace){
  const actual=await fs.realpath(workspace),top=(await git(actual,['rev-parse','--show-toplevel'])).toString('utf8').trim();
  if(await fs.realpath(top)!==actual)fail('WORK_GIT_ROOT_REQUIRED','请绑定这个 Git 工程或独立 worktree 的根目录。');
  const head=(await git(actual,['rev-parse','--verify','HEAD'])).toString('ascii').trim();
  if(!/^[a-f0-9]{40,64}$/.test(head))fail('WORK_GIT_BASE_REQUIRED','工程需要已存在的 Git 基线提交。');
  return {workspace:actual,head};
}
async function initialize(workspace){
  const repo=await repository(workspace);
  const status=await git(repo.workspace,['status','--porcelain=v1','-z','--untracked-files=all']);
  if(status.length)fail('WORKTREE_NOT_CLEAN','首次准备任务需要干净的 Git 工作区，请先保存原有修改或使用独立 worktree。');
  return {workspace:repo.workspace,base_commit:repo.head};
}
async function safeRead(workspace,relative){
  keyPath(relative);
  const file=path.join(workspace,...relative.split('/'));
  for(let current=file;current!==workspace;current=path.dirname(current)){
    const stat=await fs.lstat(current);
    if(stat.isSymbolicLink())fail('WORK_PATH_UNSAFE','检查点不读取符号链接、目录联接或其外部目标。');
    if(current!==file && !stat.isDirectory())fail('WORK_PATH_UNSAFE','文件路径已变化。');
  }
  const real=await fs.realpath(file),rel=path.relative(workspace,real);
  if(rel.startsWith('..'+path.sep) || path.isAbsolute(rel) || !rel)fail('WORK_PATH_UNSAFE','文件超出工程目录。');
  const before=await fs.stat(file);
  if(!before.isFile() || before.size>4194304)fail('WORK_SNAPSHOT_LIMIT','单个检查点文件必须是普通文件且不超过 4 MiB。');
  const data=await fs.readFile(file),after=await fs.stat(file);
  if(before.ino!==after.ino || before.size!==after.size || before.mtimeMs!==after.mtimeMs || data.length!==after.size)
    fail('WORKTREE_CHANGED','文件在采集时变化，请保存编辑后重试。');
  if(/-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----/.test(data.toString('utf8')))
    fail('WORK_SECRET_DETECTED','检测到私钥内容，未建立检查点。请从任务变更中排除凭证。');
  return {data,mode:process.platform==='win32'?null:(after.mode & 0o111?'100755':'100644')};
}
async function captureOnce(workspace,baseCommit,scope,policy,{ignoreUntracked=[]}={}){
  const repo=await repository(workspace);
  if(!/^[a-f0-9]{40,64}$/.test(baseCommit))fail('WORK_GIT_BASE_REQUIRED');
  const raw=(await git(workspace,['diff','--no-ext-diff','--no-textconv','--no-renames','--name-status','-z',baseCommit,'--'])).toString('utf8').split('\0');
  const changes=new Map();
  for(let i=0;i<raw.length && raw[i];i+=2){
    const status=raw[i],file=raw[i+1];
    if(!file || !['A','M','D'].includes(status))fail('WORK_CHANGE_UNSUPPORTED','先解决合并冲突、文件类型变化或子模块变更。');
    changes.set(file,{status:{A:'added',M:'modified',D:'deleted'}[status]});
  }
  for(const file of (await git(workspace,['ls-files','--others','--exclude-standard','-z'])).toString('utf8').split('\0').filter(Boolean))
    if(!ignoreUntracked.includes(file))changes.set(file,{status:'added'});
  if(changes.size>100)fail('WORK_SNAPSHOT_LIMIT','单个检查点最多 100 个变更文件，请拆分任务。');
  const rejected=violations([...changes.keys()],scope,policy);
  if(rejected.length)throw new HubError(rejected[0].code,'实际 Git 改动触及禁止路径或超出任务范围。',{violations:rejected});
  const files=[],contents=[];let bytes=0;
  for(const [relative,entry] of [...changes].sort(([a],[b])=>a<b?-1:a>b?1:0)){
    if(entry.status==='deleted'){
      files.push({path:relative,status:'deleted',sha256:null,bytes:0,mode:'000000'});continue;
    }
    const index=(await git(workspace,['ls-files','--stage','-z','--',relative])).toString('utf8');
    const indexMode=index.slice(0,6);
    if(index && !['100644','100755'].includes(indexMode))fail('WORK_CHANGE_UNSUPPORTED','检查点暂不处理符号链接或 submodule。');
    const {data,mode}=await safeRead(workspace,relative);bytes+=data.length;
    if(bytes>16777216)fail('WORK_SNAPSHOT_LIMIT','单个检查点超过 16 MiB，请拆分任务。');
    const manifest={path:relative,status:entry.status,sha256:sha(data),bytes:data.length,mode:mode || indexMode || '100644'};
    files.push(manifest);contents.push({path:relative,content_base64:data.toString('base64')});
  }
  const evidence={schema_version:1,source:'desktop-git-observer',base_commit:baseCommit,head_commit:repo.head,files};
  return {evidence,evidence_hash:digest(evidence),contents,bytes};
}
async function capture(workspace,baseCommit,scope,policy,options){
  const first=await captureOnce(workspace,baseCommit,scope,policy,options);
  const second=await captureOnce(workspace,baseCommit,scope,policy,options);
  if(first.evidence_hash!==second.evidence_hash)fail('WORKTREE_CHANGED','工程在采集期间发生变化，请保存后重试。');
  return first;
}
module.exports={canonical,digest,keyPath,violations,repository,initialize,capture,safeRead,git};
