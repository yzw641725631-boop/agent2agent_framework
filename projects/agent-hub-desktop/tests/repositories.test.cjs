'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const http=require('node:http'),{spawn,execFile}=require('node:child_process'),{promisify}=require('node:util');
const execute=promisify(execFile),{RepositoryRuntime}=require('../core/repository-runtime.cjs');
const hostInstall=require('../core/host-install.cjs');
const {HubError}=require('../core/remote.cjs'),{validate}=require('../core/repository-tools.cjs');

async function fixture(t){
  const base=await fs.realpath(os.tmpdir()),temporary=await fs.mkdtemp(path.join(base,'hub-repositories-'));
  t.after(async()=>{const rel=path.relative(base,temporary);assert.ok(rel.startsWith('hub-repositories-') && !rel.includes(path.sep));await fs.rm(temporary,{recursive:true,force:true});});
  const source=path.join(temporary,'source'),upstream=path.join(temporary,'upstream');
  await fs.mkdir(source);await fs.mkdir(upstream);
  const git=(args,cwd=source)=>execute('git',args,{cwd,windowsHide:true});
  await git(['init','-q','--initial-branch=main']);await fs.mkdir(path.join(source,'src'));
  await fs.writeFile(path.join(source,'src/main.ts'),'baseline\n');
  await git(['add','.']);await git(['-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','baseline']);
  await git(['clone','--bare','-q',source,path.join(upstream,'web.git')]);
  await git(['--git-dir='+path.join(upstream,'web.git'),'config','http.receivepack','true']);
  const tickets=new Map(),requests=[];
  const server=http.createServer((req,res)=>{
    const access=tickets.get(req.headers.authorization),parsed=new URL(req.url,'http://localhost');
    if(!access){res.writeHead(401);res.end();return;}
    const write=parsed.pathname.endsWith('git-receive-pack') || parsed.searchParams.get('service')==='git-receive-pack';
    if(write && access.action!=='write'){res.writeHead(403);res.end();return;}
    requests.push({url:req.url,action:access.action});
    const service=write?'git-receive-pack':'git-upload-pack';
    const command=write?'receive-pack':'upload-pack';
    const child=spawn('git',[command,'--stateless-rpc',...(req.method==='GET'?['--advertise-refs']:[]),path.join(upstream,'web.git')],{windowsHide:true});
    const chunks=[],errors=[];child.stdout.on('data',data=>chunks.push(data));child.stderr.on('data',data=>errors.push(data));
    if(req.method==='GET')child.stdin.end();else req.pipe(child.stdin);
    child.stdin.on('error',()=>{});child.on('error',()=>{res.writeHead(500);res.end();});
    child.on('close',code=>{
      if(code){t.diagnostic(Buffer.concat(errors).toString());res.writeHead(500);res.end();return;}
      res.writeHead(200,{'content-type':`application/x-${service}-`+(req.method==='GET'?'advertisement':'result')});
      if(req.method==='GET'){
        const line=Buffer.from('# service='+service+'\n');res.write(Buffer.from((line.length+4).toString(16).padStart(4,'0')));res.write(line);res.write('0000');
      }
      res.end(Buffer.concat(chunks));
    });
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>new Promise(resolve=>server.close(resolve)));
  const origin='http://127.0.0.1:'+server.address().port,projectId=crypto.randomUUID(),webId=crypto.randomUUID(),pcId=crypto.randomUUID();
  const root=path.join(temporary,'workspace'),data=path.join(temporary,'data');await fs.mkdir(root);await fs.mkdir(data);
  const binding={id:crypto.randomUUID(),projectId,workspace:root,status:'active'};
  const profile={bindings:[binding]},remote={base:origin,lifetime:new AbortController()},worklogId=crypto.randomUUID();
  let allowed=true;
  const repos=[{id:webId,slug:'web',name:'Web',platform:'web',repository_url:'https://github.com/example/web',branch:'main',status:'active',capabilities:['code:read','code:write']},
    {id:pcId,slug:'pc',name:'PC',platform:'pc',repository_url:'https://github.com/example/pc',branch:'main',status:'active',capabilities:['documents:read']}];
  const c={profile,remote,directory:data,connection:'active',persist:async()=>{},emitState(){},
    ensureCurrent(p,r){assert.equal(p,this.profile);assert.equal(r,this.remote);},binding(id){return profile.bindings.find(b=>b.id===id);},
    projectSlot:async()=>projectId,
    async bind({projectId,workspace,repositoryId}){profile.bindings.push({id:crypto.randomUUID(),projectId,workspace,repositoryId,status:'active'});},
    configuration(child){return {mcpServers:{['agent-hub-'+child.id]:{command:'test-desktop',args:['binding'],env:{ELECTRON_RUN_AS_NODE:'1'}}}};},
    async invokeProjectRemote(b,action){
      if(!allowed)throw new HubError('PROJECT_ACCESS_REVOKED');
      if(action==='hub_list_repositories')return {repository_mode:'scoped',items:repos};
      if(action==='hub_get_task')return {task:{repository_id:webId,execution_mode:'observed'}};
      if(action==='hub_heartbeat_task')return {heartbeat_seconds:120};
      if(action==='hub_list_worklogs')return {items:[{id:worklogId}]};
      throw new Error(action);
    }};
  remote.request=async(method,url,body)=>{
    assert.equal(method,'POST');
    if(url.endsWith('/deliveries')){
      assert.deepEqual(Object.keys(body).sort(),['lease_epoch','run_id','worklog_id']);
      const taskId=url.split('/').at(-2),ref=`refs/heads/hub/${taskId}/${body.run_id}`;
      const actual=(await git(['--git-dir='+path.join(upstream,'web.git'),'rev-parse',ref])).stdout.trim();
      return {id:crypto.randomUUID(),worklog_id:worklogId,git_commit:actual,ref,receipt_hash:'a'.repeat(64)};
    }
    assert.equal(url,`/api/projects/${projectId}/repositories/${webId}/git-ticket`);
    if(!allowed)throw new HubError('REPOSITORY_NOT_ACCESSIBLE');
    const secret=crypto.randomBytes(32).toString('base64url');
    tickets.set('Basic '+Buffer.from('agent-hub:'+secret).toString('base64'),body);
    return {ticket:secret,expires_at:new Date(Date.now()+120000).toISOString(),git_path:`/api/git/${webId}.git`,branch:'main',
      write_ref:body.action==='write'?`refs/heads/hub/${body.task_id}/${body.run_id}`:null};
  };
  c.closeouts={options(b){return {workspace:b.workspace,name:Object.keys(c.configuration(b).mcpServers)[0],
    receiptDirectories:['assembled-projects','task-worktrees','framework-deployments'].map(v=>path.join(data,v,b.id))};}};
  const runtime=new RepositoryRuntime(c);
  return {temporary,source,upstream,root,data,binding,profile,remote,c,runtime,webId,pcId,requests,git,
    revoke(){allowed=false;},repos};
}

test('real Git assembly clones only authorized repositories, installs fixed connections and retries without overwriting work',async t=>{
  const f=await fixture(t),args={operation_id:crypto.randomUUID(),harness:'codex'};
  const first=await f.runtime.invoke(f.binding,'hub_assemble_project',args);
  assert.equal(first.items.length,1);assert.equal(first.items[0].repository_id,f.webId);
  assert.equal(await fs.readFile(path.join(f.root,'web/src/main.ts'),'utf8'),'baseline\n');
  await assert.rejects(fs.stat(path.join(f.root,'pc')),{code:'ENOENT'});
  const config=await fs.readFile(path.join(f.root,'web/.codex/config.toml'),'utf8');assert.ok(config.includes('test-desktop'));
  await fs.writeFile(path.join(f.root,'web/src/main.ts'),'unfinished work\n');
  const count=f.requests.length;
  assert.deepEqual((await f.runtime.invoke(f.binding,'hub_assemble_project',args)).items,first.items);
  assert.equal(f.requests.length,count);
  assert.equal(await fs.readFile(path.join(f.root,'web/src/main.ts'),'utf8'),'unfinished work\n');
  f.revoke();await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',args),{code:'PROJECT_ACCESS_REVOKED'});
});

test('unapproved selection, existing directories and junctions are refused before cloning',async t=>{
  const f=await fixture(t);
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID(),repository_ids:[f.pcId]}),{code:'REPOSITORY_NOT_ACCESSIBLE'});
  await fs.mkdir(path.join(f.root,'web'));await fs.writeFile(path.join(f.root,'web/user.txt'),'keep');
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()}),{code:'ASSEMBLY_DIRECTORY_EXISTS'});
  assert.equal(await fs.readFile(path.join(f.root,'web/user.txt'),'utf8'),'keep');assert.equal(f.requests.length,0);
});

test('real Git fetch preserves dirty files and push verifies the exact task branch without changing main',async t=>{
  const f=await fixture(t);
  await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()});
  const child=f.profile.bindings.find(b=>b.repositoryId===f.webId),baseline=(await f.git(['rev-parse','HEAD'])).stdout.trim();
  await fs.writeFile(path.join(child.workspace,'src/main.ts'),'task code\n');
  const fetched=await f.runtime.invoke(child,'hub_fetch_repository',{});
  assert.equal(fetched.fetched_commit,baseline);
  assert.equal(await fs.readFile(path.join(child.workspace,'src/main.ts'),'utf8'),'task code\n');
  const taskId=crypto.randomUUID(),runId=crypto.randomUUID(),lease={task_id:taskId,run_id:runId,lease_epoch:1};
  await assert.rejects(f.runtime.invoke(child,'hub_push_task',lease),{code:'WORKTREE_NOT_CLEAN'});
  await f.git(['add','src/main.ts'],child.workspace);
  await f.git(['-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','task'],child.workspace);
  const pushed=await f.runtime.invoke(child,'hub_push_task',lease);
  assert.equal(pushed.remote_verified,true);assert.equal(pushed.merged,false);
  assert.equal(pushed.delivery_receipt.git_commit,pushed.git_commit);
  assert.equal((await f.git(['--git-dir='+path.join(f.upstream,'web.git'),'rev-parse',pushed.ref])).stdout.trim(),pushed.git_commit);
  assert.equal((await f.git(['--git-dir='+path.join(f.upstream,'web.git'),'rev-parse','main'])).stdout.trim(),baseline);
});

test('wrong repository bindings and changed Git origins stop authenticated transfers',async t=>{
  const f=await fixture(t);await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()});
  const child=f.profile.bindings.find(b=>b.repositoryId===f.webId),before=f.requests.length;
  await assert.rejects(f.runtime.verifyTaskBinding(child,{repository_id:f.pcId}),{code:'TASK_REPOSITORY_MISMATCH'});
  await f.git(['remote','set-url','origin','https://unapproved.example/repo'],child.workspace);
  await assert.rejects(f.runtime.invoke(child,'hub_fetch_repository',{}),{code:'TASK_REPOSITORY_MISMATCH'});
  assert.equal(f.requests.length,before);
  assert.throws(()=>validate('hub_assemble_project',{operation_id:crypto.randomUUID(),command:'arbitrary'}),{code:'INVALID_REQUEST'});
});

test('interrupted assembly resumes a complete clean clone without another clone or duplicate binding',async t=>{
  const f=await fixture(t),args={operation_id:crypto.randomUUID(),harness:'codex'},bind=f.c.bind;
  f.c.bind=async()=>{throw new HubError('NETWORK_UNAVAILABLE');};
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',args),{code:'NETWORK_UNAVAILABLE'});
  const count=f.requests.length;assert.equal(f.profile.assemblyOperations[args.operation_id].items[f.webId].state,'interrupted');
  f.c.bind=bind;
  const result=await f.runtime.invoke(f.binding,'hub_assemble_project',args);
  assert.equal(result.items[0].state,'ready');assert.equal(f.requests.length,count);
  assert.equal(f.profile.bindings.filter(b=>b.repositoryId===f.webId).length,1);
  const exclude=await fs.readFile(path.join(f.root,'web/.git/info/exclude'),'utf8');
  assert.equal(exclude.split('# Agent Hub local connection').length,2);
});

test('interrupted directories with user edits and intentionally removed connections are preserved',async t=>{
  const f=await fixture(t),args={operation_id:crypto.randomUUID()},bind=f.c.bind;
  f.c.bind=async()=>{throw new HubError('NETWORK_UNAVAILABLE');};
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',args));f.c.bind=bind;
  const file=path.join(f.root,'web/src/main.ts');await fs.writeFile(file,'unfinished user edits\n');
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',args),{code:'ASSEMBLY_RECOVERY_REQUIRED'});
  assert.equal(await fs.readFile(file,'utf8'),'unfinished user edits\n');
  await fs.writeFile(file,'baseline\n');await f.runtime.invoke(f.binding,'hub_assemble_project',args);
  f.profile.bindings=f.profile.bindings.filter(b=>!b.repositoryId);
  const count=f.requests.length;
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',args),{code:'ASSEMBLY_CONNECTION_UNAVAILABLE'});
  assert.equal(f.requests.length,count);assert.equal(await fs.readFile(file,'utf8'),'baseline\n');
});

test('an empty directory owned by an interrupted assembly can retry its first network request',async t=>{
  const f=await fixture(t),args={operation_id:crypto.randomUUID()},request=f.remote.request;
  f.remote.request=async()=>{throw new HubError('NETWORK_UNAVAILABLE');};
  await assert.rejects(f.runtime.invoke(f.binding,'hub_assemble_project',args),{code:'NETWORK_UNAVAILABLE'});
  assert.deepEqual(await fs.readdir(path.join(f.root,'web')),[]);
  f.remote.request=request;
  assert.equal((await f.runtime.invoke(f.binding,'hub_assemble_project',args)).items[0].state,'ready');
});

test('task worktrees isolate dirty original files, persist task connections and retry without discarding work',async t=>{
  const f=await fixture(t);await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()});
  const source=f.profile.bindings.find(b=>b.repositoryId===f.webId);
  await fs.writeFile(path.join(source.workspace,'src/main.ts'),'original unfinished work\n');
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1,operation_id:crypto.randomUUID(),harness:'codex'};
  const result=await f.runtime.invoke(source,'hub_create_task_worktree',args),child=f.c.binding(result.binding_id);
  assert.equal(await fs.readFile(path.join(result.workspace,'src/main.ts'),'utf8'),'baseline\n');
  assert.equal(await fs.readFile(path.join(source.workspace,'src/main.ts'),'utf8'),'original unfinished work\n');
  assert.equal((await f.git(['branch','--show-current'],source.workspace)).stdout.trim(),'main');
  assert.equal(child.taskId,args.task_id);assert.equal(child.worktreeRunId,args.run_id);
  assert.equal(result.code_confidentiality_boundary,false);assert.equal(result.agent_started,false);
  await fs.writeFile(path.join(child.workspace,'src/main.ts'),'parallel task work\n');
  assert.equal((await f.runtime.invoke(source,'hub_create_task_worktree',args)).binding_id,child.id);
  assert.equal(await fs.readFile(path.join(child.workspace,'src/main.ts'),'utf8'),'parallel task work\n');
  const {Controller}=require('../core/controller.cjs');
  await assert.rejects(Controller.prototype.projectRequest.call(f.c,child,'hub_get_task',{task_id:crypto.randomUUID()}),{code:'TASK_WORKTREE_MISMATCH'});
  await assert.rejects(Controller.prototype.projectRequest.call(f.c,child,'hub_prepare_work',{task_id:args.task_id,run_id:crypto.randomUUID()}),{code:'TASK_WORKTREE_MISMATCH'});
  const cleanup=await hostInstall.uninstallProject(f.c.closeouts.options(child));
  assert.equal(cleanup.state,'complete');assert.equal(cleanup.business_files_deleted,false);
  assert.equal(await fs.readFile(path.join(child.workspace,'src/main.ts'),'utf8'),'parallel task work\n');
  const config=await fs.readFile(path.join(child.workspace,'.codex/config.toml'),'utf8');assert.ok(!config.includes('mcp_servers.'));
});

test('worktree partial binding resumes, preexisting directories and changed retry identities remain protected',async t=>{
  const f=await fixture(t);await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()});
  const source=f.profile.bindings.find(b=>b.repositoryId===f.webId),bind=f.c.bind;
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1,operation_id:crypto.randomUUID()};
  f.c.bind=async()=>{throw new HubError('NETWORK_UNAVAILABLE');};
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',args),{code:'NETWORK_UNAVAILABLE'});
  const op=f.profile.worktreeOperations[args.operation_id];assert.equal(op.state,'interrupted');
  await fs.writeFile(path.join(op.workspace,'src/main.ts'),'preserve interrupted work\n');
  f.c.bind=bind;
  const result=await f.runtime.invoke(source,'hub_create_task_worktree',args);
  assert.equal(await fs.readFile(path.join(result.workspace,'src/main.ts'),'utf8'),'preserve interrupted work\n');
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',{...args,lease_epoch:2}),{code:'IDEMPOTENCY_PAYLOAD_CHANGED'});
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',{...args,operation_id:crypto.randomUUID()}),{code:'TASK_WORKTREE_EXISTS'});
  const conflict={...args,run_id:crypto.randomUUID(),operation_id:crypto.randomUUID()};
  const existing=path.join(path.dirname(source.workspace),'.agent-hub-worktrees',conflict.task_id,conflict.run_id);
  await fs.mkdir(existing,{recursive:true});await fs.writeFile(path.join(existing,'keep.txt'),'user data');
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',conflict),{code:'WORKTREE_DIRECTORY_EXISTS'});
  assert.equal(await fs.readFile(path.join(existing,'keep.txt'),'utf8'),'user data');
});

test('worktree creation refuses revoked authorization, unsafe parent links and a run already prepared elsewhere',async t=>{
  const f=await fixture(t);await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()});
  const source=f.profile.bindings.find(b=>b.repositoryId===f.webId);
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1,operation_id:crypto.randomUUID()};
  f.profile.workRuns={[args.run_id]:{binding_id:source.id}};
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',args),{code:'TASK_WORKTREE_ALREADY_PREPARED'});
  delete f.profile.workRuns;
  const outside=path.join(f.temporary,'outside');await fs.mkdir(outside);
  const link=path.join(path.dirname(source.workspace),'.agent-hub-worktrees');await fs.symlink(outside,link,'junction');
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',args),{code:'WORK_PATH_UNSAFE'});
  assert.deepEqual(await fs.readdir(outside),[]);
  f.revoke();await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',args),{code:'PROJECT_ACCESS_REVOKED'});
});

test('Git delivery ignores only receipted untracked framework files and still blocks unknown files or tracked changes',async t=>{
  const f=await fixture(t);await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID(),harness:'codex'});
  const child=f.profile.bindings.find(b=>b.repositoryId===f.webId),name=Object.keys(f.c.configuration(child).mcpServers)[0];
  await hostInstall.deployFramework({workspace:child.workspace,name,receiptDirectory:path.join(f.data,'framework-deployments',child.id),
    files:[{path:'main.md',bytes:Buffer.from('managed rules\n')}]});
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1};
  assert.equal((await f.runtime.invoke(child,'hub_push_task',args)).remote_verified,true);
  const unknown=path.join(child.workspace,'.agent-hub/unknown.md');await fs.writeFile(unknown,'user data');
  await assert.rejects(f.runtime.invoke(child,'hub_push_task',args),{code:'WORKTREE_NOT_CLEAN'});await fs.unlink(unknown);
  await fs.writeFile(path.join(child.workspace,'src/main.ts'),'tracked dirty');
  await assert.rejects(f.runtime.invoke(child,'hub_push_task',args),{code:'WORKTREE_NOT_CLEAN'});
});

test('managed checkout does not execute repository filters and worktree-specific includes cannot bypass origin checks',async t=>{
  const f=await fixture(t);await f.runtime.invoke(f.binding,'hub_assemble_project',{operation_id:crypto.randomUUID()});
  const source=f.profile.bindings.find(b=>b.repositoryId===f.webId);
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1,operation_id:crypto.randomUUID()};
  await f.git(['config','filter.unknown.smudge','should-never-execute'],source.workspace);
  await assert.rejects(f.runtime.invoke(source,'hub_create_task_worktree',args),{code:'WORKTREE_FILTER_UNSUPPORTED'});
  assert.equal(f.profile.worktreeOperations,undefined);
  await f.git(['config','--remove-section','filter.unknown'],source.workspace);
  const created=await f.runtime.invoke(source,'hub_create_task_worktree',args);
  await f.git(['config','extensions.worktreeConfig','true'],source.workspace);
  await f.git(['config','--worktree','include.path','unknown.config'],created.workspace);
  await assert.rejects(f.runtime.verifyTaskBinding(f.c.binding(created.binding_id),{repository_id:f.webId}),{code:'REPOSITORY_CONFIGURATION_UNSAFE'});
});
