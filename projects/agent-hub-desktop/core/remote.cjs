'use strict';
const crypto = require('node:crypto');

const messages = {
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
  WORK_CONTEXT_REQUIRED:'请先准备本执行实例的开工规则。',WORK_CONTEXT_ACK_REQUIRED:'请实际读取并确认全部当前规则。',
  WORK_CONTEXT_STALE:'规则或任务范围已变化，请重新准备并确认。',WORK_CONTEXT_ACK_MISMATCH:'规则哈希或确认列表不匹配。',
  WORK_CHANGE_EVIDENCE_REQUIRED:'代码任务需要桌面端采集的实际改动清单。',WORK_EVIDENCE_CHANGED:'检查点之后代码已变化，请重新记录。',
  WORK_CHANGE_PATHS_MISMATCH:'申报路径与实际改动不一致。',WORK_PATH_PROTECTED:'改动触及受保护路径。',
  WORK_RUNTIME_REQUIRED:'请在新版桌面端使用本机工作运行组件。',WORKLOG_NOT_ACCESSIBLE:'日志不存在或不在当前资料权限内。',
  POLICY_VERSION_CONFLICT:'规则已有新版本，请刷新后再发布。',
  TASK_NOT_FOUND:'任务不存在或不在当前项目。',TASK_ALREADY_CLAIMED:'任务已被认领，请等待释放或租约过期。',
  TASK_NOT_CLAIMABLE:'任务还不能认领，请先完成准入确认或当前评审流程。',
  TASK_LEASE_STALE:'任务已交接或提交，当前执行凭证失效，请重新读取任务。',
  TASK_LEASE_EXPIRED:'认领已过期，请重新核验和认领任务。',TASK_LEASE_NOT_OWNED:'这个开发租约属于其他执行者。',
  TASK_VERSION_CONFLICT:'任务状态已变化，请刷新后重试。',TASK_GATE_ORDER_INVALID:'流程关口或提交版本不匹配，请刷新任务。',
  TASK_INDEPENDENT_REVIEW_REQUIRED:'须由其他人员评审。单人项目需超管填写明确的自审原因。',
  TASK_LATEST_CHECKPOINT_REQUIRED:'请先保存本次认领的最新检查点日志。',TASK_PATH_OUT_OF_SCOPE:'变更路径超出任务焦点范围。',
  TASK_CHILDREN_INCOMPLETE:'还有未完成的子任务，不能确认父任务完成。',
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
    if (!/^\/api\/[A-Za-z0-9_/?=&.%:-]+$/.test(path) || path.includes('..') || path.includes('%')) {
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
        body:body===undefined?undefined:binary?body:JSON.stringify(body),signal:AbortSignal.any([this.lifetime.signal,signal || AbortSignal.timeout(binary?150000:18000)])});
    } catch (error) {
      this.checkOpen();
      if (signal?.aborted) throw error;
      throw new HubError('NETWORK_UNAVAILABLE','服务器连接中断。已保留本机配置，请恢复网络后重试。');
    }
    this.checkOpen();
    if (response.ok && stream) return response;
    if(response.ok && binary && method==='GET'){
      const max=36*1024*1024,parts=[];let total=0;
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
