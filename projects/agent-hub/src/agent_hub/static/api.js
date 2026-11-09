// Device private keys stay nonextractable in IndexedDB. Credentials remain in memory.
const encoder = new TextEncoder();
const encode = value => btoa(String.fromCharCode(...new Uint8Array(value)))
  .replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
const json = value => encode(encoder.encode(JSON.stringify(value)));

export const messages = {
  MEMORY_NOT_FOUND:'该记忆或来源当前不可读取。',MEMORY_VERSION_CONFLICT:'记忆处理状态已变化，请刷新后重试。',
  KNOWLEDGE_NOT_FOUND:'该知识或其原文当前不可读取。',KNOWLEDGE_VERSION_CONFLICT:'知识版本或待替换结论已变化，请刷新。',
  KNOWLEDGE_QUOTE_MISMATCH:'引用须逐字出现在来源摘录中，请重新核对。',KNOWLEDGE_SOURCE_HASH_MISMATCH:'来源摘要不一致，请重新读取。',
  KNOWLEDGE_SOURCE_STALE:'来源已换版、退役或存在冲突，请引用当前资料。',KNOWLEDGE_STATE_UNCHANGED:'该知识已经处于所选状态。',
  KNOWLEDGE_INDEPENDENT_REVIEW_REQUIRED:'请由其他有权人员审核；超管本人批准时须填写明确自审原因。',
  KNOWLEDGE_TOPIC_CONFLICT:'同一主题已有生效结论，请核对后显式填写替换 ID 和版本；不可读时联系有权负责人。',
  KNOWLEDGE_REPLACEMENT_MISMATCH:'待替换结论须属于同一项目、资料范围和主题。',KNOWLEDGE_OVERRIDE_NOT_APPLICABLE:'仅超管本人批准自己的知识时填写自审原因。',
  WORK_MEMORY_ACCESS_CHANGED:'已载入记忆的原文权限已变化，请重新准备。',WORK_MEMORY_STALE:'已载入资料已换版、退役或存在冲突，请重新准备。',
  MEMORY_SOURCE_CURSOR_INVALID:'原文读取位置无效，请从来源返回的游标继续。',
  WORK_REMOTE_BACKUP_REQUIRED:'请先在 PC 端完成最新检查点的服务器代码备份，再提交评审。',
  WORK_ARCHIVE_INTEGRITY_FAILED:'服务器代码备份未通过完整性校验，已停止恢复和提交。请保留本机工作并联系管理员。',
  WORK_ARCHIVE_NOT_FOUND:'该日志尚无服务器代码备份。',WORK_ARCHIVE_QUOTA_EXCEEDED:'代码备份容量已达配额，请联系管理员处理。',
  TASK_REPOSITORY_REQUIRED:'请选择任务所属的已接入仓库。',TASK_REPOSITORY_MISMATCH:'任务所属仓库或目标端不匹配。',
  DOCUMENT_SOURCE_NOT_ACCESSIBLE:'当前没有此仓库的资料权限。',DOCUMENT_SOURCE_REPOSITORY_MISMATCH:'文档源须使用登记的仓库、分支和服务器凭证。',
  PROGRESS_REPOSITORY_REQUIRED:'请选择本次进展所属的仓库。',REPOSITORY_ALREADY_REGISTERED:'仓库地址或标识已经接入。',
  REPOSITORY_VERSION_CONFLICT:'仓库配置已更新，请刷新后重试。',DUPLICATE_REPOSITORY_PERMISSION:'同一仓库不能重复填写授权。',
  GITHUB_APP_KEY_REQUIRED:'请填写 GitHub App 私钥。',GITHUB_APP_KEY_INVALID:'私钥格式不正确，请填写该 App 的 RSA PEM 私钥。',
  GITHUB_APP_NOT_CONFIGURED:'请先在后台配置 GitHub App。',GITHUB_APP_AUTHORIZATION_FAILED:'GitHub App 授权失败，请核对 App、安装 ID 及仓库授权。',
  WORK_CONTEXT_REQUIRED:'请在项目 MCP 准备开工规则。', WORK_CONTEXT_ACK_REQUIRED:'请读取并确认全部当前规则。',
  WORK_CONTEXT_STALE:'规则或任务范围已更新，请重新准备。', WORK_CONTEXT_ACK_MISMATCH:'规则哈希或确认列表不匹配。',
  WORK_ROLE_STALE:'当前任务职责已更新，请在工程 MCP 重新准备并确认。',
  WORK_CHANGE_EVIDENCE_REQUIRED:'代码任务需要桌面端采集实际改动。', WORK_EVIDENCE_CHANGED:'检查点已变化，请保存最新日志。',
  WORK_CHANGE_PATHS_MISMATCH:'申报路径与实际改动清单不一致。', WORK_PATH_PROTECTED:'修改了受保护路径。',
  POLICY_VERSION_CONFLICT:'规则已有新版本，请刷新后重试。', WORKLOG_NOT_ACCESSIBLE:'没有这条日志的访问权限。',
  TASK_NOT_FOUND:'任务不存在或不在当前项目。', TASK_ALREADY_CLAIMED:'任务已被认领，请等待释放或租约到期。',
  TASK_NOT_CLAIMABLE:'请先完成准入或当前评审流程。', TASK_VERSION_CONFLICT:'任务已变化，请刷新后重试。',
  TASK_LEASE_EXPIRED:'认领已到期，请重新核验任务。', TASK_LEASE_STALE:'执行凭证已失效，请重新读取任务。',
  TASK_LEASE_NOT_OWNED:'此认领属于其他执行者。', TASK_PATH_OUT_OF_SCOPE:'改动路径超出任务允许范围。',
  TASK_GATE_ORDER_INVALID:'流程关口或提交版本不匹配，请刷新任务。',
  TASK_INDEPENDENT_REVIEW_REQUIRED:'须由其他人员评审；超管自审需要填写至少 10 个字符的原因。',
  TASK_LATEST_CHECKPOINT_REQUIRED:'请先保存本次认领的最新检查点日志。',
  TASK_CHILDREN_INCOMPLETE:'仍有未完成子任务，不能确认父任务完成。',
  TASK_DEPENDENCIES_INCOMPLETE:'前置任务尚未完成，请查看依赖或联系项目负责人。',
  TASK_DEPENDENCY_CYCLE:'前置依赖形成循环（含父任务等待子任务），请重新拆解。',
  TASK_DEPENDENCY_NOT_ACCESSIBLE:'某项前置任务不存在或未获读取权限。',
  TASK_DEPENDENCIES_RESTRICTED:'存在当前不可见的依赖，须由有权负责人调整。',
  TASK_DEPENDENCIES_FROZEN:'任务已开始执行，不能再更改前置依赖。',
  TASK_DEPENDENCY_DUPLICATE:'前置任务不能重复。',
  TASK_SCOPE_CONFLICT:'同仓库重叠代码范围正在开发或等待验收，请等待交接或重新拆分任务。',
  SERVICE_BUSY: '服务正在处理并发请求，请稍后重试。',
  DEVICE_APPLICATION_DENIED: '当前设备申请已被拒绝，请联系超级管理员处理。',
  DOCUMENT_REPOSITORY_NOT_CONFIGURED: '请先保存文档源设置。', DOCUMENT_INDEX_NOT_READY: '尚无可用索引，请先同步文档。',
  DOCUMENT_INDEX_VERSION_CHANGED: '文档索引已更新，请刷新后重新查询。', DOCUMENT_NOT_FOUND: '该文件不在当前文档索引中。',
  REPOSITORY_CREDENTIAL_CONFIRMATION_REQUIRED: '更换仓库时请重新填写凭证，或选择清除凭证。',
  REPOSITORY_NOT_ACCESSIBLE: '无法读取仓库或分支，请核对地址、分支与只读凭证。',
  REPOSITORY_CONNECTION_FAILED: '连接 GitHub 超时或中断，请稍后重新同步。',
  REPOSITORY_RATE_LIMITED_OR_FORBIDDEN: 'GitHub 限流或拒绝访问，请检查凭证权限或稍后重试。',
  REPOSITORY_TOO_LARGE: '仓库压缩包超过 32 MiB 或文件项过多，请使用独立文档仓库。',
  DOCUMENT_LIMIT_EXCEEDED: '文档超出索引上限：单篇 256 KiB、总计 8 MiB、最多 1000 篇。',
  DOCUMENT_NOT_UTF8: '所选 Markdown 需要使用 UTF-8 编码。', DOCUMENT_BINARY_CONTENT: '所选 Markdown 包含二进制内容。',
  SYNC_AUTHORIZATION_CHANGED: '发起同步的设备或项目授权已变化，请由当前管理员重新同步。',
  SYNC_WORKER_INTERRUPTED: '同步进程多次中断，请重新发起同步。',
  DOCUMENT_SYNC_FAILED: '文档同步未完成，已保留同一配置下上次成功的索引。',
  INVALID_CREDENTIALS: '账号或密码不正确。', DEVICE_APPROVAL_REQUIRED: '设备正在等待超级管理员审批。',
  EMAIL_VERIFICATION_REQUIRED: '请先完成这台设备的邮箱验证。', INVALID_EMAIL_CODE: '验证码不正确。',
  EMAIL_CHALLENGE_EXPIRED: '验证码已失效，请重新发送。', RATE_LIMITED: '操作频繁，请稍后再试。',
  MAIL_NOT_CONFIGURED: '请先填写并保存发信服务。', MAIL_DELIVERY_FAILED: '邮件未能发送，请检查发信服务、端口和授权码。',
  MAIL_CONNECTION_FAILED: '服务器连接发信服务超时或中断，请稍后重试或检查 SMTP 网络连接。',
  MAIL_AUTHENTICATION_FAILED: '发信账号认证失败，请核对 SMTP 账号和应用专用密码。',
  MAIL_TLS_FAILED: '发信服务的加密连接未能建立，请核对端口与 TLS / STARTTLS 设置。',
  MAIL_SENDER_REJECTED: '发信邮箱被邮件服务拒绝，请核对发信邮箱与 SMTP 账号。',
  MAIL_RECIPIENT_REJECTED: '收件邮箱被邮件服务拒绝，请核对收件邮箱地址。',
  MAIL_DELIVERY_REJECTED: '邮件服务拒绝接收邮件，请检查发信账号的额度或服务限制。',
  INVALID_SETUP_TOKEN: '初始化码不正确，请从部署服务器获取。', SETUP_DISABLED: '服务器尚未启用初始化，请联系部署人员。',
  SETUP_ALREADY_COMPLETED: '初始化已完成，请使用账号登录。', CONFIGURATION_VERSION_CONFLICT: '设置已发生变化，请刷新后重试。',
  SETUP_CONFIGURATION_CHANGED: '发信设置已修改，请重新发送验证码。', SETUP_CHALLENGE_EXPIRED: '初始化验证码已失效，请重新发送。',
  CONFIGURATION_UNAVAILABLE: '服务器暂时无法读取设置，请联系部署人员。', APPROVAL_PAYLOAD_CHANGED: '申请内容或版本已变化，请刷新后重新审批。',
  APPROVAL_NOT_PENDING: '申请已处理或已过期。', SUBJECT_NOT_ELIGIBLE: '账号、设备或成员状态尚未满足授权条件。',
  VERIFIED_ADMIN_REQUIRED: '请先选择已验证邮箱并处于正常状态的管理员。', ACCOUNT_INACTIVE: '账号未激活或已被停用。',
  SESSION_REVOKED: '会话已撤销，请重新登录。', SESSION_EXPIRED_OR_REVOKED: '会话已失效，请重新登录。',
  PROJECT_ACCESS_REVOKED: '项目访问已撤销。请停止常规操作并保留未交付工作。',
  DEVICE_NOT_ALLOWED: '设备已撤销或无法使用，请联系超级管理员。', PROJECT_NOT_ACCESSIBLE: '当前没有该项目的访问权限。',
  SUPERADMIN_REQUIRED: '此操作需要超级管理员权限。', USER_ALREADY_EXISTS: '登录名或邮箱已被使用。',
  PROJECT_ALREADY_EXISTS: '项目标识已被使用。', MEMBERSHIP_ALREADY_EXISTS: '该成员关系已存在。',
  SMTP_TLS_REQUIRED: '远程发信服务必须使用 TLS 或 STARTTLS。', INVALID_REQUEST: '请检查输入内容与格式。',
};

export class ApiError extends Error {
  constructor(data, status) {
    const rules = {
      login:'登录名须为 3–80 位小写字母或数字，可含 . _ -，并以字母或数字开头',
      email:'邮箱格式不正确，请填写完整邮箱地址', password:'账号密码须为 12–256 个字符',
      device_name:'设备名称须为 1–100 个字符', os:'请选择 Windows 或 macOS',
      smtp_from:'发信邮箱格式不正确，请填写完整邮箱地址', smtp_host:'SMTP 服务器格式不正确',
      smtp_port:'SMTP 端口须为 1–65535 的整数', smtp_username:'SMTP 账号格式不正确',
      smtp_password:'SMTP 密码不能超过 2048 个字符', smtp_transport:'请选择 TLS 或 STARTTLS',
      public_url:'服务地址须为 https:// 开头的域名，不包含路径；也可以留空',
      repository_url:'仓库地址须为 https://github.com/组织/仓库，不包含凭证或参数',
      branch:'请填写有效 Git 分支名称', include_paths:'请逐行填写相对路径，不能使用整个仓库根目录或 ..',
      token:'请填写有效的仓库只读凭证，或留空',
      code:'邮箱验证码须为 8 位数字', slug:'项目标识须为 2–80 位小写字母、数字或短横线，并以字母开头',
    };
    const issues = data.code === 'INVALID_REQUEST' && Array.isArray(data.errors) ? data.errors.map(issue => {
      const field = issue.location?.at(-1);
      return {field, message:rules[field] || '部分输入格式不正确，请核对必填项'};
    }) : [];
    super(issues.length ? [...new Set(issues.map(issue => issue.message))].join('；') + '。' : messages[data.code] || `操作未完成（${data.code || status}）`);
    this.code = data.code; this.data = data; this.issues = issues;
  }
}

async function deviceKey() {
  if (!globalThis.crypto?.subtle) throw new Error('请使用 HTTPS 或通过服务器 SSH 转发的 localhost 地址打开后台。');
  const db = await new Promise((resolve, reject) => {
    const request = indexedDB.open('agent-hub-device', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('keys');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error('浏览器无法保存设备密钥，请检查网站存储权限。'));
  });
  const existing = await new Promise((resolve, reject) => {
    const request = db.transaction('keys').objectStore('keys').get('device-v1');
    request.onsuccess = () => resolve(request.result); request.onerror = reject;
  });
  if (existing) { db.close(); return existing; }
  const pair = await crypto.subtle.generateKey({name: 'ECDSA', namedCurve: 'P-256'}, false, ['sign', 'verify']);
  const exported = await crypto.subtle.exportKey('jwk', pair.publicKey);
  let value = {privateKey: pair.privateKey, jwk: {kty: exported.kty, crv: exported.crv, x: exported.x, y: exported.y}};
  await new Promise((resolve, reject) => {
    const transaction = db.transaction('keys', 'readwrite');
    const store = transaction.objectStore('keys'), read = store.get('device-v1');
    read.onsuccess = () => {
      if (read.result) value = read.result;
      else store.add(value, 'device-v1');
    };
    transaction.oncomplete = resolve; transaction.onerror = reject;
  });
  db.close(); return value;
}

export class Client {
  constructor() { this.session = null; this.enrollment = null; this.refreshing = null; }
  async init() {
    const response = await fetch('/api/setup/status', {cache: 'no-store'});
    if (!response.ok) throw new Error('暂时无法连接服务，请检查部署状态。');
    this.status = await response.json(); this.origin = this.status.active_origin.replace(/\/$/, '');
    this.key = await deviceKey(); return this.status;
  }
  async proof(method, path, token) {
    const claims = {htm: method, htu: this.origin + path.split('?')[0], iat: Math.floor(Date.now() / 1000), jti: crypto.randomUUID()};
    if (token) claims.ath = encode(await crypto.subtle.digest('SHA-256', encoder.encode(token)));
    const head = json({typ: 'dpop+jwt', alg: 'ES256', jwk: this.key.jwk}), body = json(claims);
    const signature = await crypto.subtle.sign({name: 'ECDSA', hash: 'SHA-256'}, this.key.privateKey, encoder.encode(`${head}.${body}`));
    return `${head}.${body}.${encode(signature)}`;
  }
  async request(method, path, body, options = {}) {
    const token = options.setup || options.enrollment || options.token || this.session?.access_token;
    const headers = {'DPoP': await this.proof(method, path, token)};
    if (token) headers[options.setup ? 'X-Setup-Token' : 'Authorization'] = options.setup ? token : `DPoP ${token}`;
    const idempotency = options.idempotency || crypto.randomUUID();
    if (!['GET', 'HEAD'].includes(method)) headers['Idempotency-Key'] = idempotency;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try { response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store'}); }
    catch { throw new Error('连接中断。请保留输入，确认网络恢复后再操作。'); }
    const data = await response.json();
    if (!response.ok) {
      if (data.code === 'SESSION_EXPIRED_OR_REVOKED' && this.session && !options.noRefresh && !options.setup && !options.enrollment && !options.token) {
        if (!this.refreshing) this.refreshing = this.request('POST', '/api/auth/refresh', {}, {token: this.session.refresh_token, noRefresh: true})
          .then(result => { this.session = result; }).finally(() => { this.refreshing = null; });
        await this.refreshing;
        return this.request(method, path, body, {...options, idempotency, noRefresh: true});
      }
      throw new ApiError(data, response.status);
    }
    return data;
  }
  setSession(session) { this.session = session; }
  clear() { this.session = null; this.enrollment = null; this.refreshing = null; }
}
