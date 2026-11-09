'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const hostInstall=require('./host-install.cjs');
const {HubError}=require('./remote.cjs');
const primary=['project-manager','architect','designer','developer'];
const roleIds=[...primary,'reviewer','tester','publisher'];
const digest=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const cleanText=value=>String(value).replace(/[\r\n\u0000-\u001f]/g,' ').replace(/[<>`]/g,'');
const checker=String.raw`'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
async function check(){
  const root=path.resolve(__dirname,'..');
  const info=await fs.lstat(path.join(root,'doc_index.json'));
  if(!info.isFile()||info.isSymbolicLink()||info.size>262144)throw Error('INVALID_INDEX');
  const index=JSON.parse(await fs.readFile(path.join(root,'doc_index.json'),'utf8'));
  if(index.schema_version!==1||!index.project_id||!Array.isArray(index.files)||index.files.length>64)throw Error('INVALID_INDEX');
  const seen=new Set();
  for(const entry of index.files){
    if(typeof entry.path!=='string'||entry.path.includes('\\')||entry.path.includes(':')||entry.path.startsWith('/')||
      entry.path.split('/').some(p=>!p||p==='.'||p==='..')||seen.has(entry.path)||!/^[a-f0-9]{64}$/.test(entry.sha256))throw Error('INVALID_PATH');
    seen.add(entry.path);let current=root;
    for(const segment of entry.path.split('/')){current=path.join(current,segment);if((await fs.lstat(current)).isSymbolicLink())throw Error('LINK_NOT_ALLOWED');}
    const stat=await fs.lstat(current);if(!stat.isFile()||stat.size>262144)throw Error('INVALID_FILE');
    if(crypto.createHash('sha256').update(await fs.readFile(current)).digest('hex')!==entry.sha256)throw Error('FILE_CHANGED: '+entry.path);
  }
  return {healthy:true,project_id:index.project_id,repository_id:index.repository_id,verified:seen.size,authority:'local-files-only; not remote authorization'};
}
if(require.main===module)check().then(v=>console.log(JSON.stringify(v))).catch(e=>{console.error(e.message);process.exitCode=1;});
module.exports={check};
`;
const smoke=String.raw`'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {check}=require('../scripts/check-framework.cjs');
test('installed framework files retain their declared identity and content',async()=>{const result=await check();assert.equal(result.healthy,true);});
`;

function build(binding,name,catalog,{doc_name_lang='zh',doc_content_lang='zh'}={}){
  if(!catalog || catalog.schema_version!==1 || catalog.catalog_version!==1 || catalog.language!==doc_content_lang ||
    !/^[a-f0-9]{64}$/.test(catalog.content_hash) || !Array.isArray(catalog.roles) || catalog.roles.length!==roleIds.length ||
    Buffer.byteLength(JSON.stringify(catalog))>98304)throw new HubError('FRAMEWORK_CATALOG_UNSUPPORTED');
  const ids=new Set(),files=[],add=(file,text)=>files.push({path:file,bytes:Buffer.from(text)});
  const zh=doc_content_lang==='zh',folder=doc_name_lang==='zh'?'角色':'roles';
  const routes=[];
  for(const role of catalog.roles){
    if(!role || !roleIds.includes(role.id)||ids.has(role.id)||role.number!==String(roleIds.indexOf(role.id)).padStart(2,'0')||
      role.mode!==(primary.includes(role.id)?'primary':'gate')||
      !Array.isArray(role.responsibilities)||!role.responsibilities.length||role.responsibilities.length>6||
      !Array.isArray(role.outputs)||!role.outputs.length||role.outputs.length>6||
      ![role.name_zh,role.name_en,...role.responsibilities,...role.outputs]
        .every(v=>typeof v==='string'&&v.length>0&&v.length<1000)||
      !/^[A-Za-z]+$/.test(role.name_en)||!/^[\u3400-\u9fff]+$/.test(role.name_zh)||role.grants_permissions!==false)throw new HubError('INVALID_RESPONSE');
    ids.add(role.id);
    const file=`${folder}/${role.number}_${doc_name_lang==='zh'?role.name_zh:role.name_en}.md`;
    const title=cleanText(zh?role.name_zh:role.name_en);
    add(file,`# ${title}\n\n${zh?'角色标识':'Role'}: ${role.id}\n\n## ${zh?'职责':'Responsibilities'}\n\n${role.responsibilities.map(v=>'- '+v).join('\n')}\n\n## ${zh?'交接产物':'Handoff outputs'}\n\n${role.outputs.map(v=>'- '+v).join('\n')}\n\n${zh?'角色不授予权限。以当前任务准备包、实际人员设备身份和服务器批准的仓库权限为准。':'Roles grant no permissions. Use the current task context, actual person/device identity and approved repository grants.'}\n`);
    routes.push({id:role.id,mode:role.mode,path:file});
  }
  if(catalog.profiles!==undefined && (!Array.isArray(catalog.profiles) || catalog.profiles.length>32))throw new HubError('INVALID_RESPONSE');
  const profileKeys=new Set();
  for(const profile of catalog.profiles || []){
    if(!profile || !/^[a-z][a-z0-9-]{0,63}$/.test(profile.key || '') || profileKeys.has(profile.key) ||
      !primary.includes(profile.base_role) || typeof profile.enabled!=='boolean' || typeof profile.name!=='string' || !profile.name.trim() || profile.name.length>100 ||
      !['responsibilities','outputs'].every(k=>Array.isArray(profile[k]) && profile[k].length>0 && profile[k].length<=12 &&
        profile[k].every(v=>typeof v==='string' && v.trim() && v.length<=500)))throw new HubError('INVALID_RESPONSE');
    profileKeys.add(profile.key);if(!profile.enabled)continue;
    const file=`profiles/${profile.key}.md`;
    add(file,`# ${cleanText(profile.name)}\n\nprofile: ${profile.key}\nbase_role: ${profile.base_role}\n\n`+
      `${zh?'本岗位补充基础职业职责；不授予仓库或审批权限。使用时以当前开工包为准。':'This profile supplements base duties and grants no repository or approval rights. Use the current prepared context.'}\n\n`+
      profile.responsibilities.map(v=>'- '+cleanText(v)).join('\n')+'\n\n'+profile.outputs.map(v=>'- '+cleanText(v)).join('\n')+'\n');
    routes.push({id:profile.key,base_role:profile.base_role,mode:'primary',path:file});
  }
  add('main.md',`# Agent Hub · ${cleanText(binding.projectName || binding.projectId)}\n\n${zh?'本目录属于当前固定项目和工程；职责与流程按任务加载。':'This directory belongs to the fixed project and workspace; load duties and workflow for the current task.'}\n\n- project_id: ${binding.projectId}\n- repository_id: ${binding.repositoryId || 'null'}\n- binding_id: ${binding.id}\n\n${zh?'开工先检查版本，读取任务、当前角色、规则、专注范围和必要记忆，再确认完整准备包。所有文件修改仍受本次任务与仓库授权限制。':'Check version and read the task, current role, rules, focus and required memory before acknowledging the complete work context. Task and repository authorization still apply to every edit.'}\n\n${zh?'当前角色、专注状态和任务租约按执行实例保存，不在共享 Markdown 中维护全员开关。':'Current role, focus and lease belong to each execution instance; shared Markdown holds no global mode switch.'}\n\n${routes.map(r=>`- [${r.id}](${r.path})`).join('\n')}\n\n- [${zh?'流程与交接':'Workflow and handoff'}](workflow.md)\n- [${zh?'索引':'Index'}](doc_index.json)\n\n${zh?'本地自检：':'Local check:'} node scripts/check-framework.cjs\n${zh?'本地测试：':'Local test:'} node --test tests/framework.test.cjs\n`);
  add('workflow.md',zh?`# 流程与交接\n\n1. 项目经理澄清需求与验收；架构师拆解工程、接口与任务。跨仓库任务分别授权，子任务用 parent_id 关联，前置完成关系用 depends_on；不得形成循环。\n2. 有权人员按任务 workflow_policy 的准入顺序确认并记录依据。标准流程为 requirements、design、feasibility；紧凑流程须项目开放并明确选择原因，保留 requirements、feasibility。所有流程仍须评审、测试与发布；严格流程须核验 Git 交付。职业名称不代替审批权限。\n3. 主执行任务指定 project-manager、architect、designer 或 developer；不同设备和 harness 共用独占认领；前置完成且无同仓库重叠代码占用才可认领。同机并行先用 hub_create_task_worktree 创建独立目录，再准备。\n4. hub_prepare_work 加载本实例职责、规则、范围和获准记忆；实际读取后 hub_acknowledge_work。修改前 hub_check_changes。\n5. 每轮改代码保存 hub_write_worklog；分别确认日志共享、代码备份和 Git 交付。断网保全工作，待服务器回执后才称共享。只有长期结论变化时才引用原文提交 hub_propose_knowledge 候选；先查重，经另一有权身份审核后生效。知识不能覆盖项目规则或保护路径。\n6. 提交最新检查点后依次 review、test、release，绑定当前提交和实际执行身份。自审必须使用明确的超管例外，不能称独立审查。\n7. 交接先保存说明与可恢复检查点；接手人重新获权、读取历史并认领。撤权按 MCP 收尾指令上传并按安装收据卸载。\n\n本目录不自动启动其他 Agent，不提供跨进程文件锁或系统级写入沙箱。worktree 共享 Git 对象，不作为仓库内的保密边界。自定义岗位通过项目设置发布并补充基础职责。使用 hub_preview_hooks、hub_install_hooks、hub_get_hook_status 安装并检查宿主保护；配置已写入不表示宿主已经加载。未核验的宿主能力显示为未覆盖。自动程序执行须另经本人明确批准。\n`:
    `# Workflow and handoff\n\n1. Clarify requirements and acceptance; split design, interfaces and repository-scoped tasks. Use parent_id for hierarchy and depends_on for prerequisite completion; cycles are rejected.\n2. Authorized people follow the task workflow_policy and record actual admission evidence. Standard uses requirements, design and feasibility. Compact requires explicit project opt-in and a recorded reason; it retains requirements and feasibility. All workflows retain review, test and release. Strict additionally requires verified Git delivery. Role labels grant no approval rights.\n3. Assign project-manager, architect, designer or developer to primary tasks. All devices and harnesses share exclusive claims, prerequisite gates and repository path reservations. Create separate local directories with hub_create_task_worktree before preparing parallel tasks.\n4. Read role, rules, focus and authorized memory from hub_prepare_work; acknowledge the complete context, then hub_check_changes before edits.\n5. Record every code-changing turn with hub_write_worklog. Verify log, code backup and Git delivery receipts separately; preserve offline work. Propose source-cited durable knowledge only when lasting conclusions change, after checking existing topics. Another authorized identity reviews it. Knowledge never overrides policy or protected paths.\n6. Submit the latest checkpoint, then review, test and release with current evidence and actual identities. Explicit superadmin self-review is not independent review.\n7. Save recoverable handoff, reauthorize the successor, read history and claim again. Revocation requires limited upload and receipt-based framework removal.\n\nThis directory does not launch agents, lock arbitrary editors or provide an OS write sandbox. Worktrees share Git objects and do not separate repository confidentiality. Registered recipients can receive durable task offers and explicitly accept them. Project-defined occupation profiles supplement base duties. Preview, install and inspect lifecycle protection using hub_preview_hooks, hub_install_hooks and hub_get_hook_status. Written configuration does not prove host activation; unverified hosts report missing coverage. Automatic process execution requires separate explicit local approval.\n`);
  add('scripts/check-framework.cjs',checker);add('tests/framework.test.cjs',smoke);
  const index={schema_version:1,catalog_version:catalog.catalog_version,catalog_hash:catalog.content_hash,
    project_id:binding.projectId,repository_id:binding.repositoryId || null,binding_id:binding.id,
    languages:{doc_name_lang,doc_content_lang},roles:routes,
    files:files.map(f=>({path:f.path,sha256:digest(f.bytes)}))};
  add('doc_index.json',JSON.stringify(index,null,2)+'\n');
  const preview_hash=digest(JSON.stringify({binding:binding.id,workspace:binding.workspace,name,
    files:files.map(f=>({path:f.path,sha256:digest(f.bytes)}))}));
  return {files,preview:{preview_hash,catalog_version:catalog.catalog_version,catalog_hash:catalog.content_hash,
    project_id:binding.projectId,repository_id:binding.repositoryId || null,
    directory:path.join(binding.workspace,'.agent-hub','framework',name),
    files:files.map(f=>({path:f.path,bytes:f.bytes.length,sha256:digest(f.bytes)})),languages:index.languages}};
}

class FrameworkRuntime{
  constructor(controller){this.c=controller;this.operations=new Map();}
  invoke(binding,action,args){
    require('./framework-tools.cjs').validate(action,args);
    const operation=(this.operations.get(binding.id)||Promise.resolve()).catch(()=>{}).then(()=>this.perform(binding,action,args));
    this.operations.set(binding.id,operation);
    return operation.finally(()=>{if(this.operations.get(binding.id)===operation)this.operations.delete(binding.id);});
  }
  async perform(binding,action,args){
    const c=this.c,profile=c.profile,remote=c.remote;
    const guard=()=>{c.ensureCurrent(profile,remote);if(c.closed||c.draining||c.connection!=='active'||c.binding(binding.id)!==binding||binding.status!=='active'||binding.closeout_id)throw new HubError('PROJECT_ACCESS_REVOKED');};
    guard();
    const catalog=await c.invokeProjectRemote(binding,'hub_get_roles',{language:args.doc_content_lang || 'zh'});
    guard();
    const name=Object.keys(c.configuration(binding).mcpServers)[0];
    if(await fs.realpath(binding.workspace)!==binding.workspace)throw new HubError('WORKSPACE_CHANGED');
    const plan=build(binding,name,catalog,args);
    if(action==='hub_preview_framework')return plan.preview;
    if(args.preview_hash!==plan.preview.preview_hash)throw new HubError('FRAMEWORK_PREVIEW_STALE');
    const result=await hostInstall.deployFramework({workspace:binding.workspace,name,files:plan.files,
      receiptDirectory:path.join(c.directory,'framework-deployments',binding.id),guard});
    return {...plan.preview,installed:true,changed:result.changed,receipt_recorded:true,
      entrypoint:path.join(plan.preview.directory,'main.md'),automatic_agent_launch:false};
  }
}
module.exports={FrameworkRuntime,build};
