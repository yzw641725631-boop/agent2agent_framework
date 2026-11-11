'use strict';
const crypto = require('node:crypto');

const messages = {
  DISTILLATION_NOT_ACCESSIBLE:'该批次或其中部分来源当前不可读取。',DISTILLATION_POLICY_VERSION_CONFLICT:'记忆整理设置已变化，请刷新。',
  DISTILLATION_VERSION_CONFLICT:'提炼批次已被其他实例处理，请刷新。',DISTILLATION_NOT_CLAIMABLE:'该批次尚有有效认领，或已结束，请查看当前状态。',
  DISTILLATION_LEASE_STALE:'提炼认领已过期或属于其他实例，请重新核验。',DISTILLATION_SOURCE_CHANGED:'来源或整理设置已变化，旧批次不能继续提交，请读取新批次。',
  DISTILLATION_CITATION_OUTSIDE_BATCH:'候选引用必须全部来自本批固定来源，请逐条核对。',DISTILLATION_INPUT_BUDGET_EXCEEDED:'本批资料超出输入上限，请由负责人调整整理设置。',
  CODE_AUDIT_REQUIRED:'当前提交的代码规范审查或必需 CI 尚未满足要求，请查看“代码规范审计”。',
  CODE_AUDIT_CI_POLICY_REQUIRED:'请先在交付要求中开启必需自动测试、列出工作流，并选择核验推送的工作提交。',
  CODE_AUDIT_POLICY_CONFLICT:'代码规范版本已变化，请刷新后重试。',CODE_AUDIT_VERSION_CONFLICT:'审计状态已变化，请刷新。',
  CODE_AUDIT_NOT_ACCESSIBLE:'当前没有这项审计的阅读权限。',CODE_AUDIT_STALE:'提交、范围或规则已变化，请处理新审计。',
  CODE_AUDIT_NOT_CLAIMABLE:'审计已被认领或已结束，请核对当前状态。',CODE_AUDIT_LEASE_EXPIRED:'审计租约已失效或属于其他执行者，请重新核验。',
  CODE_AUDIT_LEASE_ACTIVE:'另一位审查者仍在处理，请等待或联系对方释放。',
  CODE_AUDIT_INDEPENDENT_REVIEW_REQUIRED:'请由未参与开发的有权人员审查；超管自审必须记录至少 10 字的例外原因。',
  CODE_AUDIT_OVERRIDE_NOT_APPLICABLE:'仅超管审查本人参与的工作时填写自审例外原因。',
  CODE_AUDIT_RULE_COVERAGE_REQUIRED:'请覆盖所有内置与仓库规则，并使用有效规则 ID。',
  CODE_AUDIT_SOURCE_MISMATCH:'审查来源哈希或提交号不一致，不能提交当前报告。',
  CODE_AUDIT_INCOMPLETE_CANNOT_PASS:'有未覆盖的规则、文件、源码缺口或阻断问题，不能标记通过。',
  CODE_AUDIT_SNAPSHOT_UNAVAILABLE:'当前没有可读取的工作树备份，请核对获准的确切 Git 提交。',
  CODE_AUDIT_SOURCE_NOT_IN_SNAPSHOT:'该文件不在所选快照内容中。',CODE_AUDIT_SOURCE_NOT_TEXT:'该文件不能作为 UTF-8 文本读取。',
  DELIVERY_EXTERNAL_EVIDENCE_REQUIRED:'当前提交缺少有效的外部交付凭据，请查看任务的“交付核验”。',
  DELIVERY_POLICY_VERSION_CONFLICT:'交付规则已变化，请刷新后重新配置。',DELIVERY_CHECKPOINT_NOT_PUSHED:'此工作检查点尚无 Git 推送核验回执。',
  DELIVERY_PROVIDER_UNAVAILABLE:'暂时无法读取 GitHub 结果，请检查仓库凭证和服务器网络后重试。',
  DELIVERY_PROVIDER_INVALID:'GitHub 返回的交付资料不完整或格式不符，本次未保存成功凭据。',
  DELIVERY_PROVIDER_COMMIT_MISMATCH:'GitHub 记录的仓库、分支或提交与此工作检查点不一致。',
  DELIVERY_PROVIDER_NOT_LATEST:'该编号已有更新的测试运行或环境部署，请核验最新记录。',
  DELIVERY_WORKFLOW_NOT_APPROVED:'此测试文件未列入本仓库批准的交付规则。',DELIVERY_ENVIRONMENT_NOT_APPROVED:'此环境未列入本仓库批准的交付规则。',
  DELIVERY_VERIFICATION_STALE:'核验期间的规则或仓库已变化，请刷新后用新的操作重新核验。',
  DELIVERY_MERGE_EVIDENCE_REQUIRED:'请先取得本检查点当前有效的合并凭据，并按规则选择核验的提交。',
  DELIVERY_ROLLBACK_SOURCE_MISMATCH:'回滚须关联同仓库同环境中被替换的部署，以及更早的成功恢复目标；请核对凭据。',
  INVITATION_NOT_ACCESSIBLE:'邀请不存在，或当前没有管理权限。',INVITATION_NOT_PENDING:'邀请已关闭或账号已领取，请刷新状态。',
  GUIDES_NOT_APPROVED:'当前提交仍有操作说明未完成产品和测试审核。',GUIDE_NOT_ACCESSIBLE:'当前没有这份说明的阅读权限。',
  GUIDE_TARGET_MISMATCH:'请由待办指定的本人设备与执行实例处理；负责人可明确移交。',GUIDE_NOT_CLAIMABLE:'说明已被认领或正在审核，请刷新。',
  GUIDE_LEASE_EXPIRED:'说明生成租约已失效，请重新认领。',GUIDE_VERSION_CONFLICT:'说明状态已变化，请刷新后重试。',
  GUIDE_SOURCE_CHANGED:'冻结来源不一致，请保留草稿并联系负责人核对。',GUIDE_SOURCE_NOT_ACCESSIBLE:'当前无法读取该说明的原始工作来源。',
  GUIDE_REVIEW_STALE:'草稿或审核状态已变化，请重新展开当前版本。',GUIDE_GATE_ALREADY_DECIDED:'本版说明已经完成该项审核。',
  GUIDE_PUBLISHED_FROZEN:'正式说明已冻结，后续变更须通过新任务留下记录。',GUIDE_INDEPENDENT_REVIEW_REQUIRED:'请由其他有权人员核对说明；超管本人审核须填写明确例外原因。',
  GUIDE_OVERRIDE_NOT_APPLICABLE:'仅超管审核本人草稿时填写例外原因。',GUIDE_REPOSITORY_MISMATCH:'只能移交给同一仓库中仍获准的执行者。',
  GUIDE_PLAN_REQUIRED:'请安排操作说明；确实无需说明时，请填写至少 10 字的原因。',
  USAGE_BUDGET_EXCEEDED:'已上报用量达到预算，新受管修改已暂停；仍可保存日志和交接。',USAGE_POLICY_VERSION_CONFLICT:'预算配置已变化，请刷新。',
  USAGE_RECEIPT_CONFLICT:'同一用量回执内容不一致，请核对原始记录。',USAGE_RUN_NOT_ACCESSIBLE:'当前无法为该执行记录上报用量。',
  EVALUATION_ACTIVE_SOURCE_REQUIRED:'检索评测须引用当前获准且有效的来源。',
  COLLABORATION_VERSION_CONFLICT:'岗位与流程配置已变化，请刷新。',COMPACT_WORKFLOW_DISABLED:'本项目尚未开放紧凑流程。',
  ROLE_PROFILE_BASE_FROZEN:'同一岗位标识的基础职业不可更改，需要新职业时请创建新标识。',ROLE_PROFILE_UNAVAILABLE:'岗位已停用或不存在。',
  ROLE_PROFILE_MISMATCH:'岗位与任务的基础职业不一致。',AGENT_ROLE_PROFILE_MISMATCH:'执行者未登记任务所需岗位。',
  WORKFLOW_CHANGE_REQUIRES_RELEASE:'请先保存并释放当前工作，再调整流程。',WORKFLOW_CODE_REPOSITORY_REQUIRED:'严格流程须使用已登记仓库中的受管代码任务。',
  WORKFLOW_DOWNGRADE_FORBIDDEN:'任务执行后只能升级流程。',WORKFLOW_REASON_REQUIRED:'请填写至少 10 字的流程选择原因。',
  WORKFLOW_VERIFIED_DELIVERY_REQUIRED:'请先取得当前检查点的 Git 交付核验回执。',
  EXECUTION_WORKSPACE_BUSY:'此目录已有本机执行正在启动或运行。',EXECUTION_PREVIOUS_STOP_UNCONFIRMED:'上次执行尚未确认停止，请在桌面端核查旧进程。',
  EXECUTION_PROCESS_STOP_UNCONFIRMED:'未收到开发工具退出回执，请本人核查旧进程后再启动。',EXECUTION_RUN_MISMATCH:'请使用匹配的已准备受管运行和开发工具。',
  EXECUTION_APPROVAL_EXPIRED:'本次执行预览已失效，请重新预览并由本人批准。',EXECUTION_PREVIEW_CHANGED:'运行规则、记忆或预览已变化，请重新核对。',
  EXECUTION_DESKTOP_APPROVAL_REQUIRED:'请先在桌面端核对并批准这一次本机执行。',EXECUTION_START_CANCELLED:'本次启动已取消，没有授权继续执行。',
  EXECUTION_SPAWN_FAILED:'原生开发工具未能启动，请核对安装及宿主登录状态。',EXECUTION_NOT_ACCESSIBLE:'该执行记录当前不可处理。',
  EXECUTABLE_NOT_SUPPORTED:'请选择真实、未经过链接的受支持原生程序；Windows 使用 exe。',EXECUTABLE_CHANGED:'程序已变化，请在桌面端重新登记并核对。',
  HOOK_PREVIEW_STALE:'钩子预览已变化，请重新预览。',HOOK_CONFIGURATION_CHANGED:'宿主保护配置已变化，请先核对安装状态。',
  PROJECT_CLOSEOUT_REQUIRED:'权限已撤销。必须停止本项目开发，调用 hub_run_closeout 上传工作并按安装收据卸载框架；失败时保留原工作。',
  CLOSEOUT_EXPIRED:'撤权收尾窗口已过期，请超管延长收尾授权；原工作继续保留。',
  CLOSEOUT_UNREGISTERED_WORK:'存在无法关联原执行记录的工作，请联系管理员处理交接；未卸载、未删除原工作。',
  CLOSEOUT_LOCAL_STATE_MISSING:'服务器已记录完成，但本机缺少对应收尾凭据；已保留工作，请联系管理员核对。',
  WORK_REMOTE_BACKUP_REQUIRED:'请先完成最新检查点的服务器代码备份，再提交评审。',
  WORK_ARCHIVE_INTEGRITY_FAILED:'服务器代码备份未通过完整性校验。请保留本机工作并联系管理员恢复制品。',
  WORK_ARCHIVE_NOT_CONFIGURED:'服务器尚未配置代码备份存储，本机检查点已保留。',
  WORK_ARCHIVE_STORAGE_UNAVAILABLE:'服务器代码存储暂时不可用，本机检查点已保留。',
  WORK_ARCHIVE_STORAGE_FULL:'服务器代码存储空间不足，请联系管理员处理。',
  WORK_ARCHIVE_QUOTA_EXCEEDED:'代码备份达到容量配额，本机检查点已保留，请联系管理员处理。',
  WORK_ARCHIVE_LIMIT:'代码快照超过支持的文件或容量上限，请保留工作并联系管理员。',
  WORK_ARCHIVE_SECRET_PATH:'基线或改动含凭证文件路径，请先整理仓库；原工作保留。',
  WORK_ARCHIVE_SECRET_CONTENT:'基线或改动检测到凭证内容，未上传代码备份，请先清理凭证。',
  WORK_ARCHIVE_HASH_MISMATCH:'代码快照摘要不匹配，未确认备份，请重试。',
  WORK_ARCHIVE_NOT_FOUND:'这条日志尚无服务器代码备份。',
  REPOSITORY_NOT_ACCESSIBLE:'当前没有该仓库所需的代码或资料权限。',TASK_REPOSITORY_MISMATCH:'请使用任务所属仓库的连接。',
  TASK_REPOSITORY_REQUIRED:'任务必须绑定已接入的仓库。',DOCUMENT_SOURCE_NOT_ACCESSIBLE:'当前没有该仓库的资料权限。',
  GIT_RECORDED_CHECKPOINT_REQUIRED:'请先提交代码到本机 Git，再保存含该提交号的最新 checkpoint 工作日志。',
  GIT_PUSH_SCOPE_DENIED:'只允许推送当前任务已记录的提交到本任务分支。',
  GIT_TICKET_EXPIRED_OR_INSUFFICIENT:'本次 Git 访问凭证已失效，请重新验证授权后重试。',
  REPOSITORY_UPSTREAM_REJECTED:'GitHub 拒绝本次仓库访问，请核对服务器凭证与仓库设置。',
  GITHUB_APP_NOT_CONFIGURED:'请先在后台配置 GitHub App。',GITHUB_APP_AUTHORIZATION_FAILED:'GitHub App 授权失败，请核对 App 安装及仓库权限。',
  MEMORY_NOT_FOUND:'该记忆或来源当前不可读取。',MEMORY_VERSION_CONFLICT:'记忆状态已更新，请刷新后重试。',
  KNOWLEDGE_NOT_FOUND:'该知识或其原文当前不可读取。',KNOWLEDGE_VERSION_CONFLICT:'知识版本或待替换的结论已变化，请刷新。',
  KNOWLEDGE_QUOTE_MISMATCH:'引用须逐字出现在来源摘录中，请重新核对。',KNOWLEDGE_SOURCE_HASH_MISMATCH:'来源摘要不一致，请重新读取。',
  KNOWLEDGE_SOURCE_STALE:'引用来源已换版、退役或存在冲突，请引用当前资料。',KNOWLEDGE_STATE_UNCHANGED:'该知识已经处于所选状态。',
  KNOWLEDGE_INDEPENDENT_REVIEW_REQUIRED:'请由其他有权人员审核；超管本人批准时须填写明确自审原因。',
  KNOWLEDGE_TOPIC_CONFLICT:'同一主题已有生效结论。核对当前结论后，显式填写替换 ID 和版本；不可读时联系有权负责人。',
  KNOWLEDGE_REPLACEMENT_MISMATCH:'待替换结论须属于同一项目、资料范围和主题。',KNOWLEDGE_OVERRIDE_NOT_APPLICABLE:'仅超管本人批准自己的知识时填写自审原因。',
  WORK_MEMORY_ACCESS_CHANGED:'已载入记忆的原文权限已变化，请重新准备。',WORK_MEMORY_STALE:'已载入资料已换版、退役或存在冲突，请重新准备。',
  WORK_CONTEXT_REQUIRED:'请先准备本执行实例的开工规则。',WORK_CONTEXT_ACK_REQUIRED:'请实际读取并确认全部当前规则。',
  WORK_CONTEXT_STALE:'规则或任务范围已变化，请重新准备并确认。',WORK_CONTEXT_ACK_MISMATCH:'规则哈希或确认列表不匹配。',
  WORK_ROLE_STALE:'当前任务职责已变化，请重新准备并确认。',
  FRAMEWORK_PREVIEW_STALE:'职责预览已失效，请重新预览文件后部署。',
  FRAMEWORK_CATALOG_UNSUPPORTED:'服务器职业目录版本不兼容，请检查桌面端更新。',
  WORK_CHANGE_EVIDENCE_REQUIRED:'代码任务需要桌面端采集的实际改动清单。',WORK_EVIDENCE_CHANGED:'检查点之后代码已变化，请重新记录。',
  WORK_CHANGE_PATHS_MISMATCH:'申报路径与实际改动不一致。',WORK_PATH_PROTECTED:'改动触及受保护路径。',
  WORK_RUNTIME_REQUIRED:'请在新版桌面端使用本机工作运行组件。',WORKLOG_NOT_ACCESSIBLE:'日志不存在或不在当前资料权限内。',
  POLICY_VERSION_CONFLICT:'规则已有新版本，请刷新后再发布。',
  TASK_NOT_FOUND:'任务不存在或不在当前项目。',TASK_ALREADY_CLAIMED:'任务已被认领，请等待释放或租约过期。',
  TASK_NOT_CLAIMABLE:'任务还不能认领，请先完成准入确认或当前评审流程。',
  TASK_MANUAL_RESOLUTION_REQUIRED:'任务多次不通过或已超时，须由负责人处理。请停止开发并保存工作日志与代码备份；项目无需卸载。',
  WORKFLOW_RESOLUTION_CONFLICT:'处理状态或任务版本已变化，请刷新任务后核对。',
  TASK_LEASE_STALE:'任务已交接或提交，当前执行凭证失效，请重新读取任务。',
  TASK_LEASE_EXPIRED:'认领已过期，请重新核验和认领任务。',TASK_LEASE_NOT_OWNED:'这个开发租约属于其他执行者。',
  TASK_VERSION_CONFLICT:'任务状态已变化，请刷新后重试。',TASK_GATE_ORDER_INVALID:'流程关口或提交版本不匹配，请刷新任务。',
  TASK_INDEPENDENT_REVIEW_REQUIRED:'须由其他人员评审。单人项目需超管填写明确的自审原因。',
  TASK_LATEST_CHECKPOINT_REQUIRED:'请先保存本次认领的最新检查点日志。',TASK_PATH_OUT_OF_SCOPE:'变更路径超出任务焦点范围。',
  TASK_CHILDREN_INCOMPLETE:'还有未完成的子任务，不能确认父任务完成。',
  TASK_DEPENDENCIES_INCOMPLETE:'前置任务尚未完成，请查看依赖或联系项目负责人。',
  TASK_DEPENDENCY_CYCLE:'前置依赖形成循环（含父任务等待子任务），请重新拆解。',
  TASK_DEPENDENCY_NOT_ACCESSIBLE:'某项前置任务不存在或未获读取权限。',
  TASK_DEPENDENCIES_RESTRICTED:'存在当前不可见的依赖，须由有权负责人调整。',
  TASK_DEPENDENCIES_FROZEN:'任务已开始执行，不能再更改前置依赖。',
  TASK_DEPENDENCY_DUPLICATE:'前置任务不能重复。',
  TASK_SCOPE_CONFLICT:'同仓库重叠代码范围正在开发或等待验收，请等待交接或重新拆分任务。',
  TASK_WORKTREE_MISMATCH:'此工作目录只用于绑定的任务和执行实例，请进入对应连接。',
  SERVICE_BUSY:'服务正在处理并发请求，请稍后重试。',
  DEVICE_APPLICATION_DENIED:'当前设备申请已被拒绝，请联系超级管理员处理。',
  DOCUMENT_INDEX_NOT_READY:'文档索引尚未建立，请联系项目管理员同步。',
  DOCUMENT_INDEX_VERSION_CHANGED:'文档索引已更新，请重新搜索并使用新的 snapshot_id。',
  DOCUMENT_NOT_FOUND:'该路径不在当前项目文档索引中。',
  INVALID_CREDENTIALS:'账号或密码不正确。', DEVICE_APPROVAL_REQUIRED:'请等待超级管理员批准本设备。',
  EMAIL_VERIFICATION_REQUIRED:'请先验证本设备邮箱。', INVALID_EMAIL_CODE:'验证码不正确。',
  RATE_LIMITED:'操作过于频繁，请稍后重试。', PROJECT_NOT_ACCESSIBLE:'当前没有这个项目的访问权限。',
  PROJECT_ACCESS_REVOKED:'项目权限已撤销，已停止对应 MCP 操作，请保留工作等待收尾。',
  ACCOUNT_INACTIVE:'账号已冻结或停用。', DEVICE_NOT_ALLOWED:'设备已撤销，或不能用于此账号。',
  ENROLLMENT_EXPIRED:'本次设备登记已过期，请重新登录。', INVALID_REQUEST:'输入格式不正确，请检查必填项。',
  INVALID_SESSION:'会话已失效，请重新登录。', SESSION_REVOKED:'会话已撤销，请重新登录。',
  SESSION_EXPIRED_OR_REVOKED:'会话已过期或撤销，请重新登录。',
  DEVICE_KEY_MISMATCH:'设备身份不匹配，请重新登录。',
  REFRESH_REPLAY_FAMILY_REVOKED:'会话已撤销，请重新登录。',
  MAIL_CONNECTION_FAILED:'邮件连接中断，请稍后重新发送。',
  MAIL_AUTHENTICATION_FAILED:'发信账号认证失败，请在后台核对 SMTP 设置。',
  MAIL_DELIVERY_FAILED:'邮件未能发送，请检查后台的发信设置。',
  EMAIL_CHALLENGE_EXPIRED:'验证码已过期，请重新发送。',
};
class HubError extends Error {
  constructor(code, message, details={}) {
    super(message || messages[code] || `操作未完成（${code}）`);
    this.code=code; this.details=details;
  }
}
function origin(value) {
  let url;
  try { url=new URL(value); } catch { throw new HubError('INVALID_ORIGIN','请填写完整的 HTTPS 服务器地址。'); }
  if (url.username || url.password || url.search || url.hash || url.pathname!=='/' ||
      (url.protocol!=='https:' && !(url.protocol==='http:' && ['127.0.0.1','localhost'].includes(url.hostname)))) {
    throw new HubError('INVALID_ORIGIN','远程服务器使用 HTTPS；地址不能包含路径、密码或参数。');
  }
  return url.origin;
}
function identity() {
  const pair=crypto.generateKeyPairSync('ec',{namedCurve:'prime256v1'});
  const publicKey=pair.publicKey.export({format:'jwk'});
  return {privateKey:pair.privateKey.export({format:'pem',type:'pkcs8'}),
    jwk:{kty:publicKey.kty,crv:publicKey.crv,x:publicKey.x,y:publicKey.y}};
}
function proof(key, canonical, method, path, token) {
  const head=Buffer.from(JSON.stringify({typ:'dpop+jwt',alg:'ES256',jwk:key.jwk})).toString('base64url');
  const claims={htm:method,htu:canonical+path.split('?')[0],iat:Math.floor(Date.now()/1000),jti:crypto.randomUUID()};
  if (token) claims.ath=crypto.createHash('sha256').update(token).digest('base64url');
  const payload=Buffer.from(JSON.stringify(claims)).toString('base64url');
  const signature=crypto.sign('sha256',Buffer.from(`${head}.${payload}`),
    {key:key.privateKey,dsaEncoding:'ieee-p1363'}).toString('base64url');
  return `${head}.${payload}.${signature}`;
}
class Remote {
  constructor(profile, persist, transport=fetch) {
    this.profile=profile; this.persist=persist; this.transport=transport; this.refreshing=new Map();
    this.base=origin(profile.origin); this.canonical=this.base;this.lifetime=new AbortController();
  }
  close(){this.lifetime.abort();}
  checkOpen(){if(this.lifetime.signal.aborted)throw new HubError('LOGIN_REQUIRED','本次登录已结束，请重新登录。');}
  async init() {
    this.checkOpen();
    let response;
    try{
      response=await this.transport(this.base+'/api/setup/status',{
        redirect:'error',signal:AbortSignal.any([this.lifetime.signal,AbortSignal.timeout(12000)]),headers:{Accept:'application/json'},
      });
    }catch{this.checkOpen();throw new HubError('NETWORK_UNAVAILABLE','服务器连接中断，请检查网络后重试。');}
    this.checkOpen();
    if (!response.ok) throw new HubError('UNAVAILABLE','服务器暂时不可用。');
    const data=await response.json(); this.canonical=origin(data.active_origin);
    return data;
  }
  async raw(method,path,body,token,{idempotency,signal,stream=false,binary=false,contentHash}={}) {
    this.checkOpen();
    const [pathname,query,...extra]=typeof path==='string'?path.split('?'):[];
    if (!pathname || !/^\/api\/[A-Za-z0-9_/-]+$/.test(pathname) || pathname.includes('..') || extra.length ||
        (query!==undefined && (!/^[A-Za-z0-9_=&.%:+*~-]*$/.test(query) || /%(?![a-fA-F0-9]{2})/.test(query)))) {
      throw new HubError('INVALID_API_PATH');
    }
    const headers={DPoP:proof(this.profile.identity,this.canonical,method,path,token),
      Accept:stream?'text/event-stream':binary?'application/gzip':'application/json'};
    if (token) headers.Authorization=`DPoP ${token}`;
    if (!['GET','HEAD'].includes(method)) headers['Idempotency-Key']=idempotency || crypto.randomUUID();
    if (body!==undefined) headers['Content-Type']=binary?'application/gzip':'application/json';
    if(binary && body!==undefined)headers['X-Content-SHA256']=contentHash;
    let response;
    try {
      response=await this.transport(this.base+path,{method,headers,redirect:'error',
        body:body===undefined?undefined:binary?body:JSON.stringify(body),signal:AbortSignal.any([this.lifetime.signal,signal || AbortSignal.timeout(binary?360000:18000)])});
    } catch (error) {
      this.checkOpen();
      if (signal?.aborted) throw error;
      throw new HubError('NETWORK_UNAVAILABLE','服务器连接中断。已保留本机配置，请恢复网络后重试。');
    }
    this.checkOpen();
    if (response.ok && stream) return response;
    if(response.ok && binary && method==='GET'){
      const max=require('./backup-limits.cjs').ARCHIVE_BYTES,parts=[];let total=0;
      if(Number(response.headers.get('content-length'))>max){await response.body?.cancel();throw new HubError('WORK_ARCHIVE_LIMIT');}
      try{
        for await(const part of response.body){total+=part.length;if(total>max)throw new HubError('WORK_ARCHIVE_LIMIT');parts.push(Buffer.from(part));}
      }catch(error){
        this.checkOpen();if(error instanceof HubError || signal?.aborted)throw error;
        throw new HubError('NETWORK_UNAVAILABLE','代码备份下载中断，原检查点仍保留，请恢复网络后重试。');
      }
      this.checkOpen();const bytes=Buffer.concat(parts);
      if(crypto.createHash('sha256').update(bytes).digest('hex')!==response.headers.get('x-content-sha256'))throw new HubError('WORK_ARCHIVE_HASH_MISMATCH');
      return bytes;
    }
    let data;
    try { data=await response.json(); } catch { throw new HubError('INVALID_RESPONSE','服务器返回了无法识别的响应。'); }
    this.checkOpen();
    if (!response.ok) throw new HubError(data.code || 'REQUEST_FAILED',undefined,{
      normal_operations_allowed:data.normal_operations_allowed,
      required_action:data.required_action,closeout_available:data.closeout_available,
      current_version:data.current_version,task_status:data.task_status,run_id:data.run_id,
      lease_until:data.lease_until,path:data.path,
    });
    return data;
  }
  async refresh(slot='platform') {
    if (this.refreshing.has(slot)) return this.refreshing.get(slot);
    const action=(async()=>{
      const session=this.profile.sessions[slot];
      if (!session) throw new HubError('LOGIN_REQUIRED','请先在桌面端登录。');
      const updated=await this.raw('POST','/api/auth/refresh',{},session.refresh_token);
      this.checkOpen();
      if(this.profile.sessions[slot]!==session)throw new HubError('LOGIN_REQUIRED','此会话已更换，请重新检查授权。');
      this.profile.sessions[slot]=updated;
      await this.persist(); return updated;
    })();
    this.refreshing.set(slot,action);
    try { return await action; } finally { this.refreshing.delete(slot); }
  }
  async request(method,path,body,{slot='platform',...options}={}) {
    this.checkOpen();
    let session=this.profile.sessions[slot];
    if (!session) throw new HubError('LOGIN_REQUIRED','请先在桌面端登录。');
    if (Date.parse(session.expires_at)<Date.now()+15000) session=await this.refresh(slot);
    const idempotency=options.idempotency || crypto.randomUUID();
    try { return await this.raw(method,path,body,session.access_token,{...options,idempotency}); }
    catch (error) {
      if (error.code!=='SESSION_EXPIRED_OR_REVOKED') throw error;
      const latest=this.profile.sessions[slot];
      if(!latest)throw new HubError('LOGIN_REQUIRED','此会话已结束，请重新登录。');
      session=latest!==session?latest:await this.refresh(slot);
      return this.raw(method,path,body,session.access_token,{...options,idempotency});
    }
  }
}
async function* parseEvents(body,{onActivity=()=>{}}={}) {
  const decoder=new TextDecoder(); let buffered='';
  for await (const chunk of body) {
    onActivity();
    buffered+=decoder.decode(chunk,{stream:true}).replaceAll('\r','');
    if (buffered.length>512*1024) throw new HubError('INVALID_EVENT_STREAM');
    let end;
    while ((end=buffered.indexOf('\n\n'))!==-1) {
      const block=buffered.slice(0,end); buffered=buffered.slice(end+2);
      let event='message',id,data=[];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event=line.slice(6).trim();
        if (line.startsWith('id:')) id=line.slice(3).trim();
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
      }
      if (data.length) yield {event,id,data:JSON.parse(data.join('\n'))};
    }
  }
}
module.exports={Remote,HubError,origin,identity,proof,parseEvents};
