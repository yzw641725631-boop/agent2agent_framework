import {Client, messages} from './api.js';

const root = document.querySelector('#app');
const modal = document.querySelector('#modal');
const notice = document.querySelector('#notice');
const names = {active:'正常', approved:'已批准', pending:'待审批', pending_email:'待验证邮箱',
  pending_approval:'待审批', revoked:'已撤销', suspended:'已冻结', disabled:'已停用', issued:'已签发', denied:'已拒绝'};
const caps = {'project:read':'项目资料', 'documents:read':'读取文档', 'documents:propose':'文档提案',
  'tasks:claim':'认领任务', 'tasks:write':'提交进展', 'tasks:publish':'发布任务', 'documents:manage':'管理文档源',
  'tasks:review':'代码评审', 'tasks:test':'测试确认', 'logs:read':'读取共享日志',
  'repositories:manage':'管理仓库接入', 'guides:read':'查阅已发布功能说明',
  'members:manage':'成员管理', 'grants:request':'授权申请', 'audit:read':'项目审计'};
const osDefault = /Mac/i.test(navigator.platform) ? 'macos' : 'windows';
const messagesForDocument = code => messages[code] || code;
const taskStates = {draft:'待准入',ready:'待认领',in_progress:'开发中',review:'待评审',testing:'待测试',release_ready:'待发布确认',completed:'已完成'};
const dispatchStates = {queued:'等待接收',received:'已接收，待决定',accepted:'已接受并认领',declined:'已拒绝',cancelled:'已取消',expired:'已过期'};
const gateNames = {requirements:'需求确认',design:'设计审查',feasibility:'可行性确认',review:'代码评审',test:'测试确认',release:'发布确认'};
const roleNames = {'project-manager':'项目经理',architect:'架构师',designer:'产品与交互设计',developer:'开发工程师',reviewer:'代码审查',tester:'测试工程师',publisher:'发布负责人'};
const loginOptions = {required:true,pattern:'[a-z0-9][a-z0-9_.\\-]{2,79}',minlength:3,maxlength:80,
  title:'3–80 位小写字母或数字，可含 . _ -，以字母或数字开头，支持纯数字。',
  hint:'3–80 位小写字母或数字，可含 . _ -，支持纯数字。'};

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = value;
    else if (key === 'value') node.value = value;
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false)
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const pill = value => h('span', {class:`pill ${value}`}, names[value] || value);
const textPair = (title, detail) => h('div', {}, h('strong', {}, title), detail && h('small', {class:'mono'}, detail));
const values = form => Object.fromEntries(new FormData(form));
const short = value => value ? value.slice(0, 8) : '—';
const date = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—';

function field(name, title, options = {}) {
  const {full, hint, items, multiline, ...attrs} = options;
  const control = items ? h('select', {name, ...attrs}, items.map(([value, label]) => h('option', {value}, label)))
    : multiline ? h('textarea', {name, rows:4, ...attrs}) : h('input', {name, ...attrs});
  if (attrs.value !== undefined) control.value = attrs.value;
  return h('label', {class:full ? 'full' : ''}, title, control, hint && h('small', {}, hint));
}
function toast(text, error = false) {
  notice.textContent = text; notice.className = error ? 'error' : ''; notice.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { notice.hidden = true; }, error ? 11000 : 6500);
}
async function run(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { app.report(error); }
  finally { button.disabled = false; }
}
function button(label, action, variant = '') {
  const node = h('button', {type:'button', class:variant}, label);
  node.addEventListener('click', () => run(node, action)); return node;
}
function submit(form, label, action, variant = 'primary') {
  const node = h('button', {type:'submit', class:variant}, label);
  const feedback = h('p',{class:'danger-note',role:'alert',hidden:true});
  form.append(feedback);
  form.addEventListener('input', () => {
    feedback.hidden = true;
    form.querySelectorAll('[aria-invalid="true"]').forEach(control => control.removeAttribute('aria-invalid'));
  });
  form.addEventListener('submit', event => {
    event.preventDefault(); feedback.hidden = true;
    run(node, async () => {
      try { await action(values(form)); }
      catch (error) {
        feedback.textContent = error.message; feedback.hidden = false;
        for (const issue of error.issues || []) {
          const control = form.elements.namedItem(issue.field);
          if (control instanceof HTMLElement) control.setAttribute('aria-invalid','true');
        }
        const invalid = form.querySelector('[aria-invalid="true"]');
        if (invalid) invalid.focus();
        throw error;
      }
    });
  });
  return node;
}
function table(title, headers, rows, actions = []) {
  return h('section', {class:'table-card'}, h('div', {class:'table-title'}, h('h2', {}, title), h('div', {class:'row-actions'}, actions)),
    rows.length ? h('div', {class:'table-wrap'}, h('table', {}, h('thead', {}, h('tr', {}, headers.map(title => h('th', {}, title)))),
      h('tbody', {}, rows.map(row => h('tr', {}, row.map(cell => h('td', {}, cell)))))))
      : h('div', {class:'empty'}, '这里暂时没有记录。', h('br'), '完成相应配置后，记录会显示在这里。'));
}
function dialog(title, description, form, actionLabel, action) {
  if (!modal.querySelector('#modal-body')) modal.replaceChildren(h('div',{id:'modal-body'}));
  modal.querySelector('#modal-body').replaceChildren(h('h2', {}, title), h('p', {}, description), form);
  form.append(h('div', {class:'actions end'}, button('取消', () => modal.close()), submit(form, actionLabel, async data => {
    await action(data); modal.close(); toast('已保存'); await app.renderView();
  })));
  modal.showModal();
}

class Console {
  constructor() { this.client = new Client(); this.currentView = 'overview'; this.project = null; this.user = null; this.offset = 0; this.renderVersion = 0; }
  api(method, path, data, options) { return this.client.request(method, path, data, options); }
  get isRoot() { return this.user?.platform_role === 'superadmin'; }
  report(error) {
    if (['PROJECT_ACCESS_REVOKED','ACCOUNT_INACTIVE','SESSION_REVOKED','SESSION_EXPIRED_OR_REVOKED','REFRESH_REPLAY_FAMILY_REVOKED','DEVICE_APPROVAL_REQUIRED'].includes(error.code) && this.user) {
      this.client.clear(); this.user = null; this.project = null; modal.close(); this.login(error.message);
    }
    toast(error.message || '操作暂时未完成，请稍后重试。', true);
  }
  async start() {
    try { const status = await this.client.init(); status.setup_required ? this.setup() : this.login(); }
    catch (error) { root.replaceChildren(h('main', {class:'loading'}, h('h1', {}, '暂时无法打开后台'), h('p', {}, error.message))); }
  }
  authLayout(content) {
    root.replaceChildren(h('main', {class:'auth-layout'}, h('aside', {class:'auth-aside'},
      h('div', {class:'brand'}, h('span', {class:'brand-mark'}, 'A'), h('div', {}, 'Agent Hub', h('small', {}, 'PROJECT COLLABORATION'))),
      h('div', {}, h('div', {class:'eyebrow'}, 'YOUR WORK, CONNECTED'), h('h1', {}, '让项目知识与协作，', h('br'), '跟随每一位执行者。'),
        h('p', {}, '统一管理项目、人员和设备，明确每一次授权，让不同设备上的工作有序衔接。')),
      h('div', {class:'footnote'}, '项目有边界 · 授权可追溯 · 工作可交接')),
      h('section', {class:'auth-main'}, content)));
  }
  configFields(data = {}) {
    return h('div', {class:'fields'},
      field('public_url','服务地址', {type:'url', value:data.public_url || '', placeholder:'https://agent.example.com', full:true,
        hint:'可以稍后填写；HTTPS 入口就绪后，由部署流程启用该地址。'}),
      field('smtp_host','SMTP 服务器', {required:true, value:data.smtp_host || '', placeholder:'smtp.example.com'}),
      field('smtp_port','端口', {type:'number', required:true, min:1, max:65535, value:data.smtp_port || 587}),
      field('smtp_from','发信邮箱', {type:'email', required:true, value:data.smtp_from || '', placeholder:'noreply@example.com'}),
      field('smtp_transport','连接加密', {items:[['starttls','STARTTLS（通常为 587）'],['tls','TLS（通常为 465）'],['local','仅本机邮件中继']], value:data.smtp_transport || 'starttls'}),
      field('smtp_username','SMTP 账号', {value:data.smtp_username || '', autocomplete:'off'}),
      field('smtp_password','SMTP 密码 / 授权码', {type:'password', autocomplete:'new-password',
        placeholder:data.smtp_password_set ? '已保存；留空保留' : '请输入发信授权码', hint:'加密保存，后台不会回显。'}));
  }
  configBody(data, version) {
    return {...data, expected_version:version, public_url:data.public_url || null, smtp_port:Number(data.smtp_port),
      smtp_password:data.smtp_password || null};
  }
  setup() {
    const content = h('div', {class:'auth-form'}, h('div', {class:'eyebrow'}, 'INITIAL SETUP'),
      h('div', {class:'intro'}, h('h1', {}, '配置你的协作空间'), h('p', {}, '连接发信服务，验证邮箱并建立第一个超级管理员账号。')));
    let configuration = {version:0}, attempt = null;
    const tokenInput = field('setup_token','一次性初始化码', {type:'password', required:true, autocomplete:'off', full:true,
      hint:'从部署服务器 /opt/agent-hub/application/setup-token 获取。'});
    const token = () => {
      const value = tokenInput.querySelector('input').value.trim();
      if (!value) throw new Error('请先输入服务器上的一次性初始化码。');
      return value;
    };
    const config = h('form', {}, this.configFields(configuration));
    const configNotice = h('div', {class:'hint'}, '填写并保存后，下一步将向超管邮箱发送验证码。');
    const load = button('读取已保存设置', async () => {
      configuration = await this.api('GET','/api/setup/configuration',undefined,{setup:token()});
      config.querySelector('.fields').replaceWith(this.configFields(configuration));
      configNotice.textContent = configuration.mail_configured ? '发信设置已读取，可以继续验证超管邮箱。' : '请补充发信设置。';
    });
    config.append(h('div', {class:'actions'}, submit(config,'保存发信设置',async data => {
      configuration = await this.api('PUT','/api/setup/configuration',this.configBody(data,configuration.version),{setup:token()});
      config.elements.smtp_password.value = ''; configNotice.textContent = '发信设置已保存，请继续验证超管邮箱。'; toast('发信设置已加密保存');
    }),load),configNotice);
    const signup = h('form', {}, h('div', {class:'fields'},
      field('login','超管登录名',{...loginOptions,placeholder:'例如 root 或 641725631',autocomplete:'username'}),
      field('email','超管邮箱',{type:'email',required:true,autocomplete:'email'}),
      field('password','超管密码',{type:'password',required:true,minlength:12,maxlength:256,autocomplete:'new-password',full:true,hint:'至少 12 个字符。'}),
      field('device_name','本设备名称',{required:true,value:'管理工作站'}),
      field('os','设备系统',{items:[['windows','Windows'],['macos','macOS']],value:osDefault})));
    const codeForm = h('form', {hidden:true}, h('div',{class:'fields single'},field('code','邮箱验证码',{required:true,pattern:'[0-9]{8}',inputmode:'numeric',autocomplete:'one-time-code'})));
    signup.append(h('div',{class:'actions'},submit(signup,'发送邮箱验证码',async data => {
      if (!configuration.mail_configured) throw new Error('请先保存或读取发信设置。');
      attempt = await this.api('POST','/api/setup/challenges',data,{setup:token()});
      codeForm.hidden = false; toast('验证码已发送，请查看超管邮箱。'); codeForm.elements.code.focus();
    })));
    codeForm.append(h('div',{class:'actions'},submit(codeForm,'验证邮箱并建立超管',async data => {
      const session = await this.api('POST','/api/setup/complete',{attempt_id:attempt.attempt_id,code:data.code},{setup:token()});
      tokenInput.querySelector('input').value = ''; signup.elements.password.value = ''; codeForm.elements.code.value = '';
      this.client.setSession(session); await this.enter(); toast('超级管理员已建立，当前设备已批准。');
    })));
    content.append(h('section',{class:'card'},tokenInput), h('section',{class:'card'},h('div',{class:'card-title'},h('span',{class:'step'},'1'),h('h2',{},'发信服务与服务地址')),config),
      h('section',{class:'card'},h('div',{class:'card-title'},h('span',{class:'step'},'2'),h('h2',{},'建立超级管理员')),signup,codeForm));
    if (!this.client.status.setup_enabled) content.prepend(h('p',{class:'danger-note'},'部署端尚未启用初始化码，请先完成服务器应用配置。'));
    this.authLayout(content);
  }
  login(message = '') {
    const content = h('div',{class:'auth-form narrow'},h('div',{class:'eyebrow'},'WELCOME BACK'),h('h1',{},'登录协作后台'),
      h('p',{},'超级管理员的新设备通过密码和邮箱验证码确认。其他账号的新设备按审批流程登录。'));
    if (message) content.append(h('p',{class:'danger-note'},message));
    const form = h('form',{},h('div',{class:'fields single'},field('login','登录名',{required:true,autocomplete:'username'}),
      field('password','密码',{type:'password',required:true,autocomplete:'current-password'}),
      field('device_name','设备名称',{required:true,value:'管理工作站'}),field('os','设备系统',{items:[['windows','Windows'],['macos','macOS']],value:osDefault})));
    form.append(h('div',{class:'actions'},submit(form,'登录',async data => {
      this.client.enrollment = await this.api('POST','/api/auth/enrollments',data);
      form.elements.password.value = '';
      if (this.client.enrollment.device_status === 'approved' && !this.client.enrollment.email_verification_required) {
        this.client.setSession(await this.api('POST','/api/auth/sessions',{}, {enrollment:this.client.enrollment.enrollment_token}));
        await this.enter();
      } else this.pendingDevice();
    })));
    content.append(h('section',{class:'card'},form),h('div',{class:'actions'},
      button('忘记密码',() => this.recoverAccount('reset')),button('领取邮箱邀请',() => this.recoverAccount('invitation'))),
      h('p',{class:'footer-note'},'浏览器保存本设备的签名密钥。访问凭证仅保留在当前页面内存中。'));
    this.authLayout(content);
  }
  recoverAccount(purpose) {
    this.client.clear();
    let challenge;
    const content = h('div',{class:'auth-form narrow'},h('h1',{},purpose === 'reset' ? '找回账号密码' : '领取邮箱邀请'),
      h('p',{},purpose === 'reset' ? '通过已验证的绑定邮箱重置。完成后旧会话和项目授权失效，需要重新登录与签发授权；旧设备上的工作按交接流程保留。' :
        '填写管理员为你创建的登录名和邮箱。设置自己的密码后，再登录并完成设备审批。'));
    const status = h('p',{class:'hint',role:'status'});
    const start = h('form',{},field('login','登录名',{...loginOptions}),field('email','绑定邮箱',{type:'email',required:true}));
    const finish = h('form',{hidden:true},field('code','邮箱验证码',{required:true,pattern:'[0-9]{8}',inputmode:'numeric',autocomplete:'one-time-code'}),
      field('new_password','新密码',{type:'password',required:true,minlength:12,maxlength:256,autocomplete:'new-password'}));
    start.append(submit(start,'发送验证码',async data => {
      challenge = await this.api('POST','/api/auth/account-challenges',{...data,purpose});
      finish.hidden = false; finish.elements.code.value = '';
      status.textContent = '如果账号、邮箱及状态符合条件，验证码将发送到绑定邮箱，请等待约 30 秒。有效期至 '+date(challenge.expires_at)+'。';
    }));
    finish.append(submit(finish,'验证并设置密码',async data => {
      if (!challenge) throw new Error('请先发送验证码。');
      await this.api('POST','/api/auth/account-recovery',{...data,challenge_id:challenge.challenge_id});
      finish.reset(); this.login('密码已设置，请登录并按提示完成设备验证和授权。');
    }));
    content.append(h('section',{class:'card'},start,status,finish),button('返回登录',() => this.login()));
    this.authLayout(content);
  }
  changePassword() {
    const form = h('form',{},field('current_password','当前密码',{type:'password',required:true,maxlength:256,autocomplete:'current-password'}),
      field('new_password','新密码',{type:'password',required:true,minlength:12,maxlength:256,autocomplete:'new-password'}));
    dialog('修改密码','保留当前页面的会话，其他旧会话将退出。',form,'修改密码',async data => {
      await this.api('POST','/api/me/password',data); form.reset();
    });
  }
  inviteByEmail(administrator = false) {
    const projectId = this.project?.id;
    const form = h('form',{},field('login','登录名',{...loginOptions}),field('email','领取邮箱',{type:'email',required:true}));
    dialog(administrator ? '邮箱邀请管理员' : '邮箱邀请成员',
      '创建后请告知对方登录名和后台地址，让其点击“领取邮箱邀请”自行设置密码。邀请保留 3 天；设备与项目权限仍需审批。',form,'创建邀请',
      data => this.api('POST',administrator ? '/api/admin/account-invitations' : `/api/projects/${projectId}/account-invitations`,data));
  }
  async invitations(administrator = false) {
    const base = administrator ? '/api/admin/account-invitations' : `/api/projects/${this.project.id}/account-invitations`;
    const items = (await this.api('GET',base)).items;
    const states = {pending:'等待领取',expired:'已过期',closed:'已关闭或领取'};
    const act = (entry,action) => {
      const form = h('form',{},field('reason','处理原因',{required:true,minlength:10,maxlength:1000,multiline:true}));
      dialog(action === 'renew' ? '续发邀请' : '取消邀请','旧验证码将立即失效。续发后仍需本人验证邮箱并完成设备和权限审批。',form,'确认',async data => {
        await this.api('POST',`${base}/${entry.invitation_id}/actions`,{...data,action}); toast('邀请已更新。');
      });
    };
    const content = table('最近 100 条邀请',['账号','状态','有效期','操作'],items.map(entry => [textPair(entry.login,entry.email),states[entry.state] || entry.state,date(entry.expires_at),
      entry.can_manage ? h('div',{class:'row-actions'},button('续发',() => act(entry,'renew'),'small'),
        entry.state !== 'closed' ? button('取消',() => act(entry,'cancel'),'danger small') : null) : '账号已领取或状态不可处理']));
    dialog('邮箱邀请管理','仅管理当前权限范围内的邀请。',h('form',{},content),'关闭',async () => {});
  }
  pendingDevice() {
    const enrolled = this.client.enrollment;
    const options = {enrollment:enrolled.enrollment_token};
    let challenge = null;
    const selfConfirm = enrolled.device_approval_mode === 'root_email';
    const state = h('p',{},selfConfirm ? '这是超级管理员账号。完成本设备的邮箱验证后即可登录。' :
      enrolled.email_verification_required ? '请先验证邮箱，再等待超级管理员批准这台设备。' : '请等待超级管理员批准这台设备。');
    const content = h('div',{class:'auth-form narrow'},h('div',{class:'eyebrow'},'DEVICE VERIFICATION'),h('h1',{},'验证当前设备'),
      h('section',{class:'card'},state,h('p',{class:'mono'},`设备：${enrolled.device_id}`)));
    const card = content.querySelector('.card');
    if (enrolled.email_verification_required) {
      const code = h('form',{},field('code','邮箱验证码',{required:true,pattern:'[0-9]{8}',inputmode:'numeric',autocomplete:'one-time-code'}));
      code.append(h('div',{class:'actions'},button('发送验证码',async () => {
        challenge = await this.api('POST','/api/devices/email-challenges',{},options); toast('验证码已发送至账号绑定邮箱。');
      }),submit(code,'验证邮箱',async data => {
        if (!challenge) throw new Error('请先发送验证码。');
        const result = await this.api('POST','/api/devices/email-verifications',{challenge_id:challenge.challenge_id,code:data.code},options);
        if (!result.device_approval_required) {
          this.client.setSession(await this.api('POST','/api/auth/sessions',{},options)); await this.enter(); return;
        }
        state.textContent = '邮箱验证通过，请等待超级管理员批准设备。'; toast('邮箱验证通过');
      })));
      card.append(code);
    }
    card.append(h('div',{class:'actions'},button(selfConfirm ? '检查验证并登录' : '检查审批并继续',async () => {
      const status = await this.api('GET','/api/devices/enrollment',undefined,options);
      if (status.email_verification_required) throw new Error('邮箱验证尚未完成。');
      if (status.status !== 'approved') throw new Error('设备仍在等待超级管理员审批。');
      this.client.setSession(await this.api('POST','/api/auth/sessions',{},options)); await this.enter();
    },'primary'),button('返回登录',() => { this.client.clear(); this.login(); })));
    this.authLayout(content);
  }
  async enter() {
    const info = await this.api('GET','/api/me'); this.user = info.user; this.device = info.device_id;
    this.grants = (await this.api('GET','/api/me/grants')).items;
    if (!this.isRoot && !info.grant_id && this.grants.length) {
      this.client.setSession(await this.api('POST','/api/auth/select-project',{grant_id:this.grants[0].id}));
    }
    this.projects = (await this.api('GET','/api/projects')).items;
    this.project = this.projects.find(p => p.id === this.project?.id) || this.projects[0] || null;
    this.currentView = 'overview'; this.shell(); await this.renderView();
  }
  shell() {
    modal.close();
    const navigation = [['overview','工作台'],['projects','项目组与成员'],['repositories','仓库与资料'],['tasks','任务与日志'],['guides','功能操作说明'],['documents','项目文档'],['memory','共享记忆'],['usage','用量与预算'],
      ...(this.can('members:manage')?[['closeouts','撤权交接']]:[]),...(this.isRoot ? [['users','人员管理'],['devices','设备管理'],['approvals','授权审批'],['settings','系统设置']] : [['devices','我的设备']]),['audit','操作记录']];
    const nav = h('nav',{class:'nav','aria-label':'主要导航'},navigation.map(([key,label]) => button(label,async () => {
      this.currentView = key; this.offset = 0; await this.renderView();
    })));
    for (let i = 0; i < navigation.length; i++) nav.children[i].dataset.view = navigation[i][0];
    const picker = h('select',{class:'project-picker','aria-label':'当前项目'},h('option',{value:''},'选择项目'));
    if (this.isRoot) this.projects.forEach(p => picker.append(h('option',{value:p.id},p.name)));
    else this.grants.forEach(g => picker.append(h('option',{value:g.project_id,'data-grant':g.id},g.project_name)));
    picker.value = this.project?.id || '';
    picker.addEventListener('change',() => run(picker,async () => {
      if (!picker.value) return;
      modal.close();
      if (!this.isRoot) {
        const grant = this.grants.find(g => g.project_id === picker.value);
        this.client.setSession(await this.api('POST','/api/auth/select-project',{grant_id:grant.id}));
        this.projects = (await this.api('GET','/api/projects')).items;
      }
      this.project = this.projects.find(p => p.id === picker.value); this.offset = 0; await this.renderView();
    }));
    this.body = h('main',{class:'workspace-body'});
    root.replaceChildren(h('div',{class:'shell'},h('aside',{class:'sidebar'},
      h('div',{class:'brand'},h('span',{class:'brand-mark'},'A'),h('div',{},'Agent Hub',h('small',{},'协作管理'))),
      h('div',{},h('div',{class:'nav-label'},'工作空间'),nav),
      h('div',{class:'sidebar-bottom'},h('strong',{},this.isRoot ? '超级管理员' : this.user.admin_eligible ? '项目管理员' : '项目成员'),h('br'),this.user.login,h('br'),'每次操作都会校验当前授权。')),
      h('section',{class:'workspace'},h('header',{class:'topbar'},h('span',{class:'muted'},'项目协作 / 管理控制台'),h('div',{class:'topbar-right'},picker,
        h('span',{class:'badge'},this.user.login.slice(0,2).toUpperCase()),button('修改密码',() => this.changePassword(),'quiet small'),button('退出',async () => {
          await this.api('POST','/api/auth/logout',{}); clearTimeout(this.viewTimer); this.renderVersion++; modal.close();
          this.client.clear(); this.user = null; this.project = null; this.login();
        },'quiet small'))),this.body)));
  }
  heading(title, description, actions = []) {
    return h('div',{class:'page-heading'},h('div',{},h('div',{class:'eyebrow'},'AGENT HUB / CONSOLE'),h('h1',{},title),h('p',{},description)),h('div',{class:'row-actions'},actions));
  }
  async renderView() {
    clearTimeout(this.viewTimer);
    const version = ++this.renderVersion, view = this.currentView, body = this.body;
    document.querySelectorAll('[data-view]').forEach(node => node.classList.toggle('active',node.dataset.view === view));
    body.replaceChildren(h('p',{class:'muted'},'正在读取…'));
    try {
      const content = await this[view === 'projects' ? 'projectView' : view]();
      if (version === this.renderVersion && body === this.body) body.replaceChildren(...content);
    } catch (error) {
      if (version === this.renderVersion && body === this.body) body.replaceChildren(h('p',{class:'danger-note'},error.message));
      throw error;
    }
  }
  async overview() {
    const approvals = this.isRoot ? (await this.api('GET','/api/admin/approvals')).items : [];
    const devices = (await this.api('GET',this.isRoot ? '/api/admin/devices' : '/api/me/devices')).items;
    const stat = (label, value, detail) => h('article',{class:'stat'},h('div',{class:'stat-label'},label),h('strong',{},value),h('span',{class:'hint'},detail));
    return [this.heading('工作台','管理项目访问，处理设备和授权申请。'),
      h('div',{class:'stats'},stat('当前可见项目',this.projects.length,this.isRoot ? '由超级管理员创建与分配' : '仅显示当前授权范围'),
        stat('待处理申请',approvals.length,this.isRoot ? '设备批准与项目授权' : '由超级管理员审批'),stat('已批准设备',devices.filter(d => d.status === 'approved').length,'设备密钥与登录凭证共同校验')),
      h('div',{class:'split'},table('项目一览',['项目','状态'],this.projects.map(p => [textPair(p.name,p.slug),pill('active')])),
        h('section',{class:'card'},h('h2',{},'当前账号'),h('div',{class:'detail-list'},
          h('div',{},h('span',{},'登录名'),h('strong',{},this.user.login)),h('div',{},h('span',{},'账号角色'),h('span',{},this.isRoot ? '超级管理员' : '项目成员 / 管理员')),
          h('div',{},h('span',{},'邮箱'),h('span',{},this.user.email || '未绑定')),h('div',{},h('span',{},'邮箱验证'),pill(this.user.email_verified ? 'approved' : 'pending_email')),
          h('div',{},h('span',{},'当前设备'),h('span',{class:'mono'},short(this.device)))))),
      h('div',{class:'help-block'},h('strong',{},'开始协作：'), ' 创建项目组，接入独立仓库，为成员设备签发代码与资料权限，再拆解任务。工作日志与代码备份分别确认；撤权后桌面限定交接并按安装收据卸载项目框架。')];
  }
  can(cap, projectId = this.project?.id) {
    return this.isRoot || this.grants.some(g => g.project_id === projectId && g.capabilities.includes(cap));
  }
  async closeouts() {
    if(!this.project)return [this.heading('撤权交接','先选择项目组。')];
    const projectId=this.project.id,revision=this.renderVersion;
    const data=await this.api('GET',`/api/projects/${projectId}/closeouts`);
    this.viewTimer=setTimeout(()=>{
      if(this.currentView==='closeouts' && this.project?.id===projectId && this.renderVersion===revision)this.renderView().catch(error=>this.report(error));
    },5000);
    const labels={pending:'待上传',uploaded:'上传已确认',conflicts:'清理冲突',complete:'客户端报告清理完成'};
    return [this.heading('撤权交接','已撤权设备只能上传原工作。界面分别显示服务器保存状态与客户端卸载报告。'),
      table('最近 100 条收尾记录',['人员 / 设备','原因','工作上传','卸载报告','有效期 / 操作'],data.items.map(job=>[
        textPair(short(job.user_id),short(job.device_id)),job.reason,
        job.server_upload_confirmed?'服务器已确认':'待确认',
        h('div',{},labels[job.state] || job.state,job.cleanup_report?h('small',{},`${job.cleanup_report.removed_files} 文件，${job.cleanup_report.conflicts} 冲突`):null),
        h('div',{},date(job.expires_at),this.isRoot && job.state!=='complete'?button('延长收尾 7 天',async()=>{
          await this.api('POST',`/api/admin/closeouts/${job.id}/extend`,{days:7});await this.renderView();
        },'small'):null)])),
      h('p',{class:'hint'},'卸载结果来自设备报告，不是远端擦除证明。上传失败、断网、文件被修改时保留工作，普通开发权限保持关闭。')];
  }
  async tasks() {
    const projectId = this.project?.id, revision = this.renderVersion;
    const title = this.heading('任务与工作日志','任务状态、认领和流程决定由服务器保存。当前页面每 5 秒刷新。',
      [button('刷新',() => this.renderView()),...(projectId ? [button('项目规则',() => this.openPolicy(projectId)),button('职业职责',() => this.openRoles(projectId))] : []),
        ...(projectId && this.can('tasks:publish') ? [button('岗位与流程',() => this.openCollaboration(projectId)),button('创建任务',() => this.createTask(),'primary')] : [])]);
    if (!projectId) return [title,h('section',{class:'card empty'},'请先选择一个项目。')];
    const base = `/api/projects/${projectId}`, list = h('div',{}), feed = h('div',{}), dispatches = h('div',{}), status = h('p',{class:'hint'});
    let offset = 0, before = '0', busy = false;
    const current = () => this.project?.id === projectId && this.currentView === 'tasks' && this.renderVersion === revision;
    const refresh = async () => {
      if (!current() || busy) return;
      busy = true;
      try {
        const tasks = await this.api('GET',`${base}/tasks?offset=${offset}&limit=50`);
        const activity = await this.api('GET',`${base}/activity?before=${before}&limit=20`);
        const offers = await this.api('GET',`${base}/agent-dispatches?limit=20`);
        const agents = await this.api('GET',`${base}/agents?limit=100`);
        if (!current()) return;
        list.replaceChildren(table('协作任务',['任务 / 父任务','当前阶段','专注范围','开发租约','操作'],tasks.items.map(task => [
          textPair(task.title,`${short(task.id)}${task.parent_id ? ` · 父 ${short(task.parent_id)}` : ''}`),
          textPair(taskStates[task.status],task.dependencies_satisfied === false ? '前置任务未完成' : ''),
          textPair(`${task.scope.repository} · ${task.scope.platform}`,task.scope.paths.join('、')),
          task.active_run_id ? textPair(short(task.active_run_id),`到期 ${date(task.lease_until)}`) : '—',
          button('查看 / 推进',() => this.openTask(projectId,task.id),'small')])));
        list.append(h('div',{class:'row-actions'},offset > 0 && button('上一页',async () => { offset = Math.max(0,offset-50); await refresh(); }),
          tasks.has_more && button('下一页',async () => { offset = tasks.next_offset; await refresh(); })));
        feed.replaceChildren(table('共享动态',['时间','内容','任务','Git 引用'],activity.items.map(event => [date(event.created_at),
          textPair(event.summary,event.kind || 'report'),event.task_id ? button(short(event.task_id),() => this.openTask(projectId,event.task_id),'small') : event.task_reference || '—',
          event.git_commit || '—'])));
        feed.append(h('div',{class:'row-actions'},before !== '0' && button('返回最新',async () => { before = '0'; await refresh(); }),
          activity.has_more && button('更早动态',async () => { before = activity.before; await refresh(); })));
        dispatches.replaceChildren(table('执行者登记',['名称','工具与职业','最近活动','版本'],agents.items.map(a=>[
          a.name,textPair(a.harness,a.roles.map(r=>roleNames[r] || r).join('、')),
          textPair(date(a.last_seen_at),a.recent_agent_activity?'最近有 Agent 活动':'暂无近期活动'),String(a.version)])),
          h('p',{class:'hint'},'登记与桌面在线不代表模型正在执行。投递不启动外部程序，接收不占租约，接受才认领。'),
          table('最近任务投递',['任务','回执状态','说明','时间','操作'],offers.items.map(o=>[
            o.task_title,textPair(dispatchStates[o.state],o.run_id?`运行 ${short(o.run_id)}`:o.expired?'已超时':'未取得运行租约'),
            textPair(o.note,o.reason),date(o.created_at),this.can('tasks:publish') && ['queued','received'].includes(o.state)?button('取消投递',()=>{
              const form=h('form',{},field('reason','取消原因',{required:true,maxlength:1000}));
              dialog('取消投递','取消不终止已接受的开发运行。',form,'取消待办',v=>this.api('POST',`${base}/agent-dispatches/${o.id}/decision`,
                {action:'cancel',expected_version:o.version,reason:v.reason}));
            },'small'):'—'])),offers.has_more?h('p',{class:'hint'},'这里展示最近 20 条；完整历史可由项目 MCP 按任务翻页。'):h('span',{}));
        status.textContent = `最近同步 ${date(new Date().toISOString())} · 完整日志需要单独授权，动态不包含日志原文。`;
      } catch (error) {
        if (current()) { status.textContent = error.message; this.report(error); }
      } finally {
        busy = false;
        if (current()) { clearTimeout(this.viewTimer); this.viewTimer = setTimeout(refresh,5000); }
      }
    };
    await refresh();
    return [title,status,list,dispatches,feed,h('p',{class:'help-block'},'前置任务完成后才能认领；同仓库重叠代码范围在开发及评审测试期间保留占用。日志、代码备份、Git 交付分别核验回执。')];
  }
  async dispatchTask(projectId,task){
    const revision=this.renderVersion;
    const agents=await this.api('GET',`/api/projects/${projectId}/agents?limit=100${task.repository_id?'&repository_id='+task.repository_id:''}`);
    if(this.project?.id!==projectId || this.currentView!=='tasks' || this.renderVersion!==revision)return;
    const eligible=agents.items.filter(a=>a.enabled && a.repository_id===task.repository_id && a.roles.includes(task.execution_role) &&
      (!task.role_profile || a.role_profiles?.includes(task.role_profile)));
    if(!eligible.length){toast('没有同仓库、同职业的可投递执行者。请成员在开发工具中登记实例。');return;}
    const form=h('form',{},field('endpoint_id','执行者',{required:true,items:eligible.map(a=>[a.id,`${a.name} · ${a.harness} · ${short(a.device_id)}`])}),
      field('note','投递说明',{required:true,maxlength:1000}));
    dialog('投递任务','待办保留 24 小时。接收不认领；执行者接受时重新检查权限、版本、依赖和占用。',form,'投递待办',values=>{
      const endpoint=eligible.find(a=>a.id===values.endpoint_id);
      return this.api('POST',`/api/projects/${projectId}/agent-dispatches`,{task_id:task.id,endpoint_id:endpoint.id,
        expected_task_version:task.version,expected_endpoint_version:endpoint.version,note:values.note});
    });
  }
  async createTask(parentId = null) {
    const projectId = this.project.id;
    const [registry,collaboration] = await Promise.all([this.api('GET',`/api/projects/${projectId}/repositories`),this.api('GET',`/api/projects/${projectId}/collaboration`)]);
    const repositories = registry.items.filter(repo => repo.status === 'active' && repo.capabilities.includes('code:read'));
    const scoped = registry.repository_mode === 'scoped';
    if (scoped && !repositories.length) throw new Error('请先由超级管理员批准要开发的仓库权限。');
    if (this.project?.id !== projectId) return;
    const form = h('form',{},h('div',{class:'fields single'},field('title','任务名称',{required:true,maxlength:200}),
      h('label',{},'需求描述',h('textarea',{name:'description',required:true,maxlength:8000,rows:3})),
      h('label',{},'验收标准',h('textarea',{name:'acceptance',required:true,maxlength:8000,rows:3})),
      scoped ? field('repository_id','任务所属仓库',{required:true,items:repositories.map(repo => [repo.id,`${repo.name} · ${repo.platform}`])}) :
        field('repository','仓库标识',{required:true,maxlength:200}),
      !scoped && field('platform','目标端',{required:true,maxlength:80,placeholder:'例如 web'}),
      field('execution_mode','记录类型',{items:[['observed','代码任务：须确认规则并采集真实改动'],['declaration','人工资料记录：不提供代码证据保证']]}),
      field('execution_role','任务负责角色',{items:[['developer','开发工程师'],['project-manager','项目经理'],['architect','架构师'],['designer','产品与交互设计']]}),
      field('role_profile','自定义岗位（须与负责角色对应）',{items:[['','使用基础职业职责'],...collaboration.profiles.filter(p=>p.enabled).map(p=>[p.key,p.name+' · '+p.base_role])]}),
      field('workflow','任务流程',{items:[['standard','标准：需求 → 设计 → 可行性 → 评审 → 测试 → 发布'],
        ...(collaboration.allow_compact?[['compact','紧凑：需求 → 可行性 → 评审 → 测试 → 发布']]:[]),['strict','严格：标准流程，另须核验 Git 交付']]}),
      field('workflow_reason','非标准流程的选择原因（至少 10 字）',{maxlength:1000}),
      field('guide_kind','是否需要使用说明',{items:[['none','无操作变化，不生成'],['user','界面操作说明'],['api','接口使用说明'],['operations','配置与运维说明']]}),
      field('guide_module','说明负责的功能或模块（需要说明时必填）',{maxlength:200}),
      field('guide_reason','不生成说明的原因（项目要求时至少 10 字）',{maxlength:1000}),
      h('label',{},'须先完成的任务 ID（可选，每行一项）',h('textarea',{name:'depends_on',rows:3})),
      h('label',{},'允许修改的相对目录或文件（每行一项）',h('textarea',{name:'paths',required:true,rows:3,placeholder:'src\ntests'}))));
    dialog(parentId ? '拆解子任务' : '创建任务','任务绑定具体仓库；成员还须具有该仓库的开发权限才能认领。创建后依次完成需求、设计和可行性确认。',form,'创建草稿',data => {
      const repo = repositories.find(repo => repo.id === data.repository_id);
      return this.api('POST',`/api/projects/${projectId}/tasks`,{title:data.title,description:data.description,acceptance:data.acceptance,
        repository_id:repo?.id || null,parent_id:parentId,execution_mode:data.execution_mode,execution_role:data.execution_role,
        role_profile:data.role_profile || null,workflow:data.workflow,workflow_reason:data.workflow_reason,
        documentation:{kind:data.guide_kind,module:data.guide_module,reason:data.guide_reason},
        depends_on:data.depends_on.split(/\r?\n/).map(p => p.trim()).filter(Boolean),
        scope:{repository:repo?.slug || data.repository,platform:repo?.platform || data.platform,
          paths:data.paths.split(/\r?\n/).map(p => p.trim()).filter(Boolean)}});
    });
  }

  async guides() {
    const heading = this.heading('功能操作说明','开发提交后为每位参与者生成独立待办；产品和测试核对通过后随功能发布。说明阅读权与代码权分别管理。');
    if (!this.project) return [heading,h('section',{class:'card empty'},'请先选择项目。')];
    const projectId=this.project.id,base=`/api/projects/${projectId}/guides`,version=this.renderVersion;
    const query=this.guideQuery || '';
    const data=await this.api('GET',`${base}?offset=${this.offset}&limit=100&query=${encodeURIComponent(query)}`);
    if(this.project?.id!==projectId || this.renderVersion!==version)return [heading];
    const names={queued:'待生成 / 待重试',running:'正在生成',review:'待核对',approved:'已核对，待功能发布',published:'已发布'};
    const form=h('form',{},field('query','查找功能或正文',{value:query,maxlength:200}));
    form.append(submit(form,'查找',async values=>{this.guideQuery=values.query;this.offset=0;await this.renderView();}));
    const listing=table('说明目录与生成进度',['功能','适用版本','状态','负责实例','操作'],data.items.map(item=>[
      item.title,item.product_version || '待生成',names[item.state] || item.state,item.instance_id ? short(item.instance_id) : '—',
      button('查看',() => this.openGuide(projectId,item.id),'small')]));
    if(data.has_more || this.offset)listing.append(this.pagination(data.has_more?100:0));
    return [heading,form,listing];
  }
  async openGuide(projectId,id) {
    const base=`/api/projects/${projectId}/guides/${id}`,item=await this.api('GET',base);
    if(this.project?.id!==projectId)return;
    const content=h('div',{},item.content?.user_roles?h('p',{},'适用角色：'+item.content.user_roles.join('、')):null,
      item.reason?h('p',{class:'danger-note'},item.reason):null);
    const names={purpose:'用途',prerequisites:'使用前提',entry:'功能入口',steps:'操作步骤',inputs:'输入与校验',results:'操作结果',
      recovery:'异常与恢复',cautions:'操作提示',examples:'完整示例',changes:'版本变化',limitations:'限制与反馈',verification:'核验依据'};
    if(item.content)for(const [key,label] of Object.entries(names))content.append(h('h3',{},label),Array.isArray(item.content[key])?
      h('ol',{},item.content[key].map(step=>h('li',{},step))):h('p',{class:'prewrap'},item.content[key]));
    else content.append(h('p',{},'生成待办已保存在服务器。请由负责实例通过 MCP 领取，离线恢复后可继续；原开发租约已经结束也能独立办理。'));
    if(item.source_worklog_id)content.append(h('details',{},h('summary',{},'来源与处理记录'),h('pre',{},JSON.stringify({source_worklog_id:item.source_worklog_id,
      source_hash:item.source_hash,git_commit:item.git_commit,instance_id:item.instance_id,events:item.events},null,2))));
    const controls=h('div',{class:'row-actions'},button('关闭',()=>modal.close()));
    const decide=(gate)=>{
      const form=h('form',{},field('approved','核对结果',{items:[['yes','通过'],['no','退回修改']]}),
        field('reason','核对依据 / 修改意见',{required:true,minlength:10,maxlength:2000,multiline:true}),
        gate==='product'?field('audience','正式阅读范围',{items:[['repository','仅拥有来源仓库代码读取权的成员'],['project','本项目有功能说明阅读权的成员（批准前移除内部敏感资料）']]}):null,
        field('override_reason','超管自审例外原因（独立审核留空）',{maxlength:2000}));
      dialog('功能说明核对','必须针对当前草稿和对应代码版本核验；退回会保留历史草稿和意见。',form,'提交',async data=>{
        await this.api('POST',base+'/decisions',{...data,approved:data.approved==='yes',gate,expected_version:item.version,draft_version:item.draft_version});
      });
    };
    if(item.state==='review'){
      if(this.can('tasks:publish'))controls.append(button('产品核对',()=>decide('product')));
      if(this.can('tasks:test'))controls.append(button('测试核对',()=>decide('test')));
    }
    if(!modal.querySelector('#modal-body'))modal.replaceChildren(h('div',{id:'modal-body'}));
    modal.querySelector('#modal-body').replaceChildren(h('h2',{},item.title),h('p',{},item.product_version || '待生成'),content,controls);
    if(!modal.open)modal.showModal();
  }
  async repositories() {
    const title = this.heading('仓库与资料','一个项目组可以接入 Web、PC、API 和共享资料等独立仓库。成员只获得明确批准的资源。');
    if (!this.project) return [title,h('section',{class:'card empty'},'请先选择项目组。')];
    const projectId = this.project.id, base = `/api/projects/${projectId}/repositories`;
    const data = await this.api('GET',base);
    const permissionNames = {'code:read':'读取代码','code:write':'开发代码','documents:read':'读取文档'};
    const parts = [title,table('已接入仓库',['仓库','目标端','本人权限','状态','操作'],data.items.map(repo => [
      textPair(repo.name,repo.repository_url),repo.platform,
      repo.capabilities.map(cap => permissionNames[cap]).join('、') || '尚未获得代码或资料权限',
      repo.status === 'active' ? '可用' : '已归档',this.isRoot ? h('div',{class:'row-actions'},button(repo.status === 'active' ? '归档' : '恢复',() => {
        const status = repo.status === 'active' ? 'archived' : 'active';
        dialog(status === 'archived' ? '归档仓库' : '恢复仓库','归档会停止本平台的仓库访问。恢复后仍在有效期内的原授权可以再次使用。',h('form'),
          '确认',() => this.api('POST',`${base}/${repo.id}/status`,{expected_version:repo.version,status}));
      },'small'),repo.auth_mode === 'token' && repo.status === 'active' && button('更换仓库凭证',() => {
        const form = h('form',{},field('token','新的仓库专用凭证',{type:'password',required:true,maxlength:2048,autocomplete:'new-password'}));
        dialog('更换仓库凭证','新凭证加密保存在服务器。成员下次访问会使用新凭证，已有代码不会被覆盖。',form,'保存新凭证',data =>
          this.api('PUT',`${base}/${repo.id}/credential`,{expected_version:repo.version,token:data.token}));
      },'small')) : '—'
    ]))];
    if (data.can_manage) parts.push(h('section',{class:'card'},h('h2',{},'接入独立仓库'),
      h('p',{},'接入后按仓库审批代码和文档权限。原有未归类记录继续保留，由超级管理员查看。'),
      button('接入仓库',() => {
        const form = h('form',{},h('div',{class:'fields single'},
          field('slug','仓库标识',{required:true,pattern:'[a-z][a-z0-9\\-]{1,79}',placeholder:'web 或 api'}),
          field('name','显示名称',{required:true,maxlength:200}),
          field('platform','目标端',{items:[['web','Web'],['pc','PC'],['api','API'],['mobile','移动端'],['shared','共享资料'],['other','其他']]}),
          field('repository_url','GitHub 仓库地址',{type:'url',required:true,placeholder:'https://github.com/组织/仓库'}),
          field('branch','分支',{required:true,value:'main'}),
          field('auth_mode','服务器访问方式',{items:[['github_app','GitHub App'],['token','该仓库的专用凭证'],['public','公共只读仓库']]}),
          field('installation_id','GitHub App 安装 ID（使用 App 时填写）',{type:'number',min:1}),
          field('token','仓库专用凭证（选择专用凭证时填写）',{type:'password',autocomplete:'new-password',maxlength:2048,
            hint:'仅授予所需仓库的权限；凭证加密保存在服务器，成员和 Agent 不会获得它。'})));
        dialog('接入仓库','仓库地址和标识创建后固定。代码权限、文档权限仍需单独审批。',form,'接入',data => {
          const body = {...data};
          if (body.auth_mode !== 'token') delete body.token;
          if (body.auth_mode === 'github_app') body.installation_id = Number(body.installation_id); else delete body.installation_id;
          return this.api('POST',base,body);
        });
      },'primary')));
    if (this.isRoot) {
      const app = await this.api('GET','/api/admin/integrations/github');
      parts.push(h('section',{class:'card'},h('h2',{},'GitHub App'),
        h('p',{},app.private_key_set ? `已配置 App ${app.app_id}。请在 GitHub 上将 App 安装到允许接入的仓库。` : '先创建并安装 GitHub App，再在这里保存 App ID 与私钥。'),
        button('配置 GitHub App',() => {
          const form = h('form',{},field('app_id','App ID',{required:true,type:'number',min:1,value:app.app_id || ''}),
            h('label',{},'PEM 私钥（已配置时可留空保留）',h('textarea',{name:'private_key',rows:6,maxlength:16384,autocomplete:'off'})));
          dialog('GitHub App 配置','私钥仅保存在服务器，用于取得指定仓库的短期访问凭证。',form,'保存',data =>
            this.api('PUT','/api/admin/integrations/github',{expected_version:app.version,app_id:Number(data.app_id),
              ...(data.private_key.trim() ? {private_key:data.private_key.trim()} : {})}));
        })));
    }
    return parts;
  }
  async openRoles(projectId) {
    const catalog = await this.api('GET',`/api/projects/${projectId}/roles`);
    if (this.project?.id !== projectId || this.currentView !== 'tasks') return;
    const content = h('div',{id:'modal-body'},h('h2',{},'职业职责'),
      h('p',{class:'hint'},'任务指定负责角色；开工包只加载当前职责。人员权限、仓库授权和独立评审仍由服务器校验。'),
      ...catalog.roles.map(role => h('details',{},h('summary',{},role.name),
        ...role.responsibilities.map(item => h('p',{},item)),h('p',{},'交接产物：'+role.outputs.join('、')))),
      ...(catalog.profiles || []).filter(p=>p.enabled).map(p=>h('details',{},h('summary',{},p.name+' · '+p.base_role),
        ...p.responsibilities.map(item=>h('p',{},item)),h('p',{},'交接产物：'+p.outputs.join('、')))),
      h('p',{class:'hint'},'在 PC“项目与 MCP”选择“部署职责”，可将角色文档、流程和自检脚本安装到本地工程。'),
      h('div',{class:'row-actions'},button('关闭',() => modal.close())));
    modal.replaceChildren(content); if (!modal.open) modal.showModal();
  }
  async openCollaboration(projectId) {
    const config=await this.api('GET',`/api/projects/${projectId}/collaboration`);
    if(this.project?.id!==projectId)return;
    const save=(profiles,allow_compact,change_reason,require_guides=config.require_guides)=>this.api('PUT',`/api/projects/${projectId}/collaboration`,
      {expected_version:config.version,profiles,allow_compact,require_guides,change_reason});
    const edit=profile=>{
      const form=h('form',{},field('key','岗位标识',{value:profile?.key || '',required:true,pattern:'[a-z][a-z0-9-]{0,63}',readonly:!!profile}),
        field('name','岗位名称',{value:profile?.name || '',required:true,maxlength:100}),
        field('base_role','基础职业',{value:profile?.base_role || 'developer',items:[['developer','开发工程师'],['architect','架构师'],['designer','设计师'],['project-manager','项目经理']]}),
        h('label',{},'职责（每行一项，最多 12 项）',h('textarea',{name:'responsibilities',required:true,rows:4},profile?.responsibilities.join('\n') || '')),
        h('label',{},'交接产物（每行一项，最多 12 项）',h('textarea',{name:'outputs',required:true,rows:3},profile?.outputs.join('\n') || '')),
        field('enabled','状态',{value:profile?.enabled===false?'no':'yes',items:[['yes','启用'],['no','停用']]}),
        field('change_reason','变更原因',{required:true,minlength:10,maxlength:1000}));
      dialog(profile?'编辑岗位':'创建岗位','岗位只补充职责。调整后相关执行实例须重新准备上下文；原修订保留。',form,'保存岗位',data=>{
        const lines=value=>value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
        const entry={key:data.key,name:data.name,base_role:data.base_role,enabled:data.enabled==='yes',responsibilities:lines(data.responsibilities),outputs:lines(data.outputs)};
        return save([...config.profiles.filter(p=>p.key!==entry.key),entry],config.allow_compact,data.change_reason);
      });
    };
    const content=h('div',{id:'modal-body'},h('h2',{},`岗位与流程 · 版本 ${config.version}`),
      h('p',{},'岗位不授予仓库或审批权限。紧凑流程省略单独的设计确认，仍保留需求、可行性、独立评审、测试和发布。'),
      ...config.profiles.map(p=>h('p',{},p.name+' · '+p.base_role+' · '+(p.enabled?'启用':'停用')+' ',button('编辑',()=>edit(p),'small'))),
      h('p',{},'紧凑流程：'+(config.allow_compact?'允许负责人明确选择':'未开放')),
      h('p',{},'操作说明：'+(config.require_guides?'代码任务须安排说明或明确说明豁免原因':'由任务负责人选择')),
      button(config.require_guides?'关闭说明计划要求':'要求代码任务安排说明',()=>{
        const form=h('form',{},field('change_reason','调整原因',{required:true,minlength:10,maxlength:1000}));
        dialog('操作说明要求','之后创建的代码任务必须安排说明；确实没有操作变化时，由有权负责人填写明确豁免原因。',form,'保存设置',
          data=>save(config.profiles,config.allow_compact,data.change_reason,!config.require_guides));
      }),
      h('div',{class:'row-actions'},button('新增岗位',()=>edit(null)),button(config.allow_compact?'关闭紧凑流程':'允许紧凑流程',()=>{
        const form=h('form',{},field('change_reason','调整原因',{required:true,minlength:10,maxlength:1000}));
        dialog('调整流程选项','只影响之后的明确选择；已有任务保留创建时的流程与原因。',form,'保存设置',data=>save(config.profiles,!config.allow_compact,data.change_reason));
      }),button('关闭',()=>modal.close())));
    modal.replaceChildren(content);if(!modal.open)modal.showModal();
  }
  async openPolicy(projectId) {
    const policy = await this.api('GET',`/api/projects/${projectId}/policy`);
    if (this.project?.id !== projectId || this.currentView !== 'tasks') return;
    const content = h('div',{id:'modal-body'},h('h2',{},`项目规则 · 版本 ${policy.version}`),
      h('p',{class:'hint'},'基础规则不能由项目规则覆盖。新增版本使旧开工确认失效；规则不直接设置本机文件权限。'),
      ...policy.rules.map(rule => h('p',{},h('strong',{},rule.id+' '),rule.text)),
      h('p',{},'保护路径：'+(policy.protected_paths.join('、') || '尚无自定义路径')),
      h('p',{},'禁止功能：'+(policy.forbidden_features.join('、') || '尚无自定义功能')),
      h('div',{class:'row-actions'},button('关闭',() => modal.close())));
    if (this.can('tasks:publish',projectId)) content.append(button('发布新版本',() => {
      const form = h('form',{},
        h('label',{},'禁止修改的相对路径（每行一项，按目录前缀匹配）',h('textarea',{name:'protected_paths',rows:4},policy.protected_paths.join('\n'))),
        h('label',{},'禁止扩展的功能（每行一项）',h('textarea',{name:'forbidden_features',rows:3},policy.forbidden_features.join('\n'))),
        h('label',{},'项目规章（每行 P-规则编号|规则说明）',h('textarea',{name:'rules',rows:5},policy.rules.filter(r => r.id.startsWith('P-')).map(r => r.id+'|'+r.text).join('\n'))),
        field('change_reason','变更原因',{required:true,minlength:10,maxlength:1000,full:true}));
      dialog('发布规则版本','所有代码执行实例下次记录或提交时须使用当前规则确认。原版本保留。',form,'发布版本',data => {
        const lines = value => value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
        const rules = lines(data.rules).map(line => { const separator = line.indexOf('|');
          if (separator < 0) throw new Error('项目规章应为 P-编号|说明。'); return {id:line.slice(0,separator).trim(),text:line.slice(separator+1).trim()}; });
        return this.api('PUT',`/api/projects/${projectId}/policy`,{expected_version:policy.version,protected_paths:lines(data.protected_paths),
          forbidden_features:lines(data.forbidden_features),rules,change_reason:data.change_reason});
      });
    },'primary'));
    modal.replaceChildren(content); if (!modal.open) modal.showModal();
  }
  async openTask(projectId, taskId, offset = 0) {
    const revision = this.renderVersion, base = `/api/projects/${projectId}/tasks/${taskId}`;
    const data = await this.api('GET',base), logs = await this.api('GET',`${base}/worklogs?offset=${offset}&limit=20`);
    if (this.project?.id !== projectId || this.currentView !== 'tasks' || this.renderVersion !== revision) return;
    const task = data.task, run = data.runs.find(r => r.id === task.active_run_id);
    const owned = run?.user_id === this.user.id && run?.device_id === this.device;
    const lease = {run_id:task.active_run_id,lease_epoch:task.lease_epoch};
    const reload = () => this.openTask(projectId,taskId,offset);
    const act = async (path,body) => { await this.api('POST',base+path,body); toast('服务器已保存'); await reload(); };
    const actions = [button('关闭',() => modal.close()),button('刷新',reload)];
    if(this.can('tasks:publish') && task.status==='ready')actions.push(button('投递给执行者',()=>this.dispatchTask(projectId,task)));
    const coordination = data.coordination;
    if(this.can('tasks:publish') && !task.lease_epoch && ['draft','ready'].includes(task.status) && !coordination?.has_restricted_dependencies)
      actions.push(button('设置前置任务',() => {
        const form=h('form',{},h('label',{},'前置任务 ID（每行一项）',h('textarea',{name:'depends_on',rows:4},
          (coordination?.dependencies || []).map(t=>t.id).join('\n'))),field('reason','调整原因',{required:true,minlength:10,maxlength:1000,full:true}));
        dialog('设置前置任务','调整后重新进行准入确认；首次认领后依赖冻结。父任务等待子任务的关系也参与循环检查。',form,'保存并重新准入',values=>
          this.api('POST',base+'/dependencies',{expected_version:task.version,reason:values.reason,
            depends_on:values.depends_on.split(/\r?\n/).map(v=>v.trim()).filter(Boolean)}));
      }));
    if (this.can('tasks:publish') && task.status !== 'completed') actions.push(button('拆解子任务',() => this.createTask(taskId)));
    if (this.can('tasks:publish') && ['draft','ready'].includes(task.status)) actions.push(button('调整流程',()=>{
      const form=h('form',{},field('workflow','新流程',{items:[['standard','标准'],['compact','紧凑（须项目开放）'],['strict','严格（须代码仓库与核验交付）']]}),
        field('reason','调整原因',{required:true,minlength:10,maxlength:1000}));
      dialog('调整任务流程','执行过的任务只允许升级。保存后重新准入，旧决定保留。',form,'保存并重新准入',data=>this.api('POST',base+'/workflow',
        {expected_version:task.version,workflow:data.workflow,reason:data.reason}));
    }));
    const expired = !task.lease_until || new Date(task.lease_until).getTime() <= Date.now();
    if (task.execution_mode !== 'observed' && ['ready','in_progress'].includes(task.status) && expired && coordination?.claimable !== false && this.can('tasks:claim') && this.can('tasks:write'))
      actions.push(button('本人认领',() => act('/claim',{instance_id:crypto.randomUUID(),harness:'human'}),'primary'));
    if (owned && !expired && task.status === 'in_progress') {
      actions.push(button('续租 10 分钟',() => act('/heartbeat',lease)),
        task.execution_mode !== 'observed' && button('记录本轮工作',() => this.writeTaskLog(base,lease)),
        button('释放认领',() => {
          const form = h('form',{},field('reason','交接 / 释放原因',{required:true,maxlength:1000,full:true}));
          dialog('释放认领','先保存工作日志。释放后，其他人可以立即认领。',form,'释放',data => this.api('POST',base+'/release',{...lease,reason:data.reason}));
        }));
      const latest = logs.items.find(log => log.run_id === task.active_run_id);
      if (task.execution_mode !== 'observed' && !offset && latest?.outcome === 'checkpoint') actions.push(button('提交最新日志待评审',() => act('/submit',{
        ...lease,worklog_id:latest.id,expected_version:task.version}),'primary'));
    }
    const nextGate = task.status === 'draft' ? (task.workflow_policy?.admission || ['requirements','design','feasibility'])[task.admission_step] :
      {review:'review',testing:'test',release_ready:'release'}[task.status];
    const gateCap = {review:'tasks:review',test:'tasks:test'}[nextGate] || 'tasks:publish';
    if (nextGate && this.can(gateCap) && (!task.submission_id || this.can('logs:read'))) actions.push(button(gateNames[nextGate],() => {
      const form = h('form',{},h('label',{},'决定依据 / 验证证据',h('textarea',{name:'evidence',required:true,rows:5,maxlength:8000})),
        field('approved','决定',{items:[['yes','通过'],['no','退回 / 不通过']]}),
        this.isRoot && ['review','test'].includes(nextGate) ? field('override_reason','超管自审原因（仅评审本人工作时填写）',{maxlength:1000}) : null);
      dialog(gateNames[nextGate],'该决定只针对当前任务版本和提交日志。退回后需要重新认领、提交与评审。',form,'保存决定',values => this.api('POST',base+'/decisions',{
        gate:nextGate,approved:values.approved === 'yes',expected_version:task.version,worklog_id:task.submission_id,
        evidence:values.evidence,override_reason:values.override_reason || '',instance_id:crypto.randomUUID(),harness:'human'}));
    },'primary'));
    const content = h('div',{id:'modal-body'},h('h2',{},task.title),h('p',{},`${taskStates[task.status]} · 版本 ${task.version} · ${task.id}`),
      task.execution_mode === 'observed' && h('p',{class:'help-block'},'代码任务：在工程 MCP 调用 hub_claim_task 认领，再调用 hub_prepare_work、hub_acknowledge_work。保存日志时采集本机实际改动。网页用于查看与流程决定。'),
      h('p',{},`${roleNames[task.execution_role] || task.execution_role} · ${task.scope.repository} / ${task.scope.platform} · ${task.scope.paths.join('、')}`),
      h('p',{},`岗位：${task.role_profile || '基础职责'} · 流程：${{standard:'标准',compact:'紧凑',strict:'严格'}[task.workflow] || '标准'}${task.workflow_reason?' · '+task.workflow_reason:''}`),
      h('h3',{},'需求与验收'),h('pre',{class:'document-content'},task.description+'\n\n验收：'+task.acceptance),
      coordination && h('section',{},h('h3',{},'前置任务与范围占用'),
        h('p',{},coordination.dependencies_satisfied ? '前置任务已满足。' : '前置任务未完成，当前不能认领。'),
        coordination.dependencies.map(t=>h('p',{},button(t.title,()=>this.openTask(projectId,t.id),'small'),` · ${taskStates[t.status]} · ${t.id}`)),
        coordination.has_restricted_dependencies && h('p',{class:'hint'},'另有当前不可读取的前置关系，请由项目负责人协调。'),
        coordination.scope_conflicts.map(conflict=>h('p',{class:'danger-note'},`范围占用：${conflict.paths.join('、')} · ${taskStates[conflict.status]} · ${conflict.task_id}`))),
      task.lease_until && h('p',{},`租约到期 ${date(task.lease_until)}。网页认领需手动续租；Agent 使用 MCP 心跳。`),h('div',{class:'row-actions'},actions),
      h('h3',{},`工作日志 · ${logs.visibility === 'project' ? '共享原文' : '仅本人记录'}`),
      logs.items.length ? logs.items.map(log => h('details',{},h('summary',{},`${date(log.created_at)} · ${log.summary} · ${log.outcome}`),
        h('pre',{class:'document-content'},`${log.notes}\n\n变更：${log.changed_paths.join('、')}\n验证：${log.tests}\n下一步：${log.next_steps}`),
        h('p',{class:'mono'},`Git 引用 ${log.git_commit || '未提供'}${log.delivery ? '（服务器已核验任务分支）' : '（未核验）'}\n日志回执 ${log.receipt_hash}`),
        log.snapshot ? h('div',{},h('p',{class:'hint'},`代码工作树已备份：${log.snapshot.baseline_files} 个基线文件，${log.snapshot.changed_files} 个新增或修改文件。不含 Git 历史。请在 PC 端或工程 MCP 取回。`),
          h('p',{class:'mono'},`备份回执 ${log.snapshot.receipt_hash}`)) : h('p',{class:'hint'},'尚无服务器代码备份回执。'),
        log.delivery && h('p',{class:'mono'},`Git 交付回执 ${log.delivery.receipt_hash}\n${log.delivery.ref}（未合并、未部署）`))) : h('p',{class:'hint'},'没有当前账号可见的日志。'),
      h('div',{class:'row-actions'},offset > 0 && button('较新日志',() => this.openTask(projectId,taskId,Math.max(0,offset-20))),
        logs.has_more && button('较早日志',() => this.openTask(projectId,taskId,logs.next_offset))),
      table('执行身份',['角色','人员 / 设备','工具','状态'],data.runs.map(r => [roleNames[r.role] || r.role,textPair(short(r.user_id),short(r.device_id)),r.harness,r.state])),
      table('可见流程证据',['关口','结果','依据','记录时间'],data.decisions.map(d => [gateNames[d.gate],d.approved ? '通过' : '退回',
        textPair(d.evidence,d.override_reason),date(d.created_at)])),h('p',{class:'hint'},'日志、代码备份和 Git 交付独立记录。请核对各自回执；代码恢复通过 PC 端或工程 MCP。'));
    modal.replaceChildren(content); if (!modal.open) modal.showModal();
  }
  writeTaskLog(base, lease) {
    const form = h('form',{},field('summary','本轮摘要',{required:true,maxlength:300,full:true}),
      h('label',{},'做了什么 / 遇到什么问题',h('textarea',{name:'notes',required:true,rows:5,maxlength:16000})),
      h('label',{},'改动路径（每行一项）',h('textarea',{name:'changed_paths',rows:3})),
      field('tests','实际验证结果（未执行也要如实填写）',{required:true,maxlength:8000,full:true}),
      field('next_steps','下一步 / 交接事项',{required:true,maxlength:4000,full:true}),
      field('outcome','本轮状态',{items:[['checkpoint','可继续的检查点'],['blocked','阻塞'],['failed','失败']]}),
      field('git_commit','Git 提交号（可选，仅引用）',{pattern:'[a-fA-F0-9]{40,64}',maxlength:64}));
    dialog('记录本轮工作','日志不可覆盖；不包含代码备份，请保留本地修改。',form,'保存日志',values => this.api('POST',base+'/worklogs',{
      ...lease,...values,git_commit:values.git_commit || null,changed_paths:values.changed_paths.split(/\r?\n/).map(v => v.trim()).filter(Boolean)}));
  }
  async memory() {
    const title=this.heading('共享记忆','日志交接与文档章节按原文权限共享；旧版本留档，默认加载当前内容。',[button('刷新',()=>this.renderView())]);
    if(!this.project)return [title,h('section',{class:'card empty'},'请先选择项目组。')];
    const projectId=this.project.id,revision=this.renderVersion;
    const filter=this.memoryFilter?.projectId===projectId?this.memoryFilter:{projectId,query:'',include_history:false,repository_id:'',layer:'extracts'};
    const isKnowledge=filter.layer==='knowledge';
    const registry=await this.api('GET',`/api/projects/${projectId}/repositories`);
    const query=new URLSearchParams({query:filter.query,search_mode:filter.search_mode || 'hybrid',include_history:String(filter.include_history),limit:'12'});
    if(filter.repository_id)query.set('repository_id',filter.repository_id);
    const base=`/api/projects/${projectId}/memory`;
    const data=await this.api('GET',base+(isKnowledge?'/knowledge':'')+'?'+query);
    const status=await this.api('GET',base+'/status'+(filter.repository_id?'?repository_id='+filter.repository_id:''));
    const statusText=s=>`当前摘录 ${s.visible_cards.active || 0} · 长期知识 ${s.knowledge?.active || 0} · 知识待审 ${s.knowledge?.pending || 0} · 过时 ${s.visible_cards.superseded || 0} · 冲突 ${s.visible_cards.conflicted || 0} · 待提取 ${s.visible_jobs.queued || 0} · 失败 ${s.visible_jobs.failed || 0}`;
    const semanticText=s=>s.embeddings?` · 语义索引 ${s.embeddings.cards.indexed+s.embeddings.knowledge.indexed}/${s.embeddings.cards.visible+s.embeddings.knowledge.visible} · 索引失败 ${s.embeddings.visible_jobs.failed || 0}`:'';
    const counts=h('p',{class:'mono'},statusText(status)+semanticText(status));
    const poll=async()=>{
      if(this.currentView!=='memory' || this.project?.id!==projectId || this.renderVersion!==revision)return;
      try{const next=await this.api('GET',base+'/status'+(filter.repository_id?'?repository_id='+filter.repository_id:''));counts.textContent=statusText(next)+semanticText(next);}
      catch(error){counts.textContent=error.message;return;}
      this.viewTimer=setTimeout(poll,5000);
    };
    this.viewTimer=setTimeout(poll,5000);
    const form=h('form',{},h('div',{class:'fields'},
      field('layer','资料类型',{value:filter.layer || 'extracts',items:[['extracts','日志交接与文档摘录'],['knowledge','长期知识与审核候选']]}),
      field('query','问题或关键词',{value:filter.query,maxlength:200,placeholder:'例如：换电脑怎样验证身份'}),
      field('search_mode','检索方式',{value:filter.search_mode || 'hybrid',items:[['hybrid','语义与关键词'],['keyword','仅关键词']]}),
      field('repository_id','仓库范围',{value:filter.repository_id,items:[['','全部获准资料'],...registry.items.map(r=>[r.id,r.name])]}),
      field('history','版本范围',{value:filter.include_history?'all':'current',items:[['current','当前有效卡片'],['all','含候选、过时、冲突和退役卡片']]})));
    form.append(h('div',{class:'actions'},submit(form,'检索',async values=>{
      this.memoryFilter={projectId,query:values.query,search_mode:values.search_mode,repository_id:values.repository_id,include_history:values.history==='all',layer:values.layer};
      await this.renderView();
    })));
    const labels={active:'当前生效',pending:'候选待审核',superseded:'已被新版本替代',conflicted:'存在冲突',retired:'已退役',rejected:'已驳回',source_stale:'来源已失效，需重新核对'};
    const cards=data.items.map(card=>{
      const source=card.source;
      const original=h('div');
      const actions=[button('展开来源原文',async()=>{
        const raw=await this.api('GET',`${base}/${isKnowledge?card.source_card_id:card.id}/original?start_line=${card.line_start || 1}&limit=100`);
        if(this.renderVersion!==revision || this.project?.id!==projectId)return;
        original.replaceChildren(h('p',{class:'hint'},`原文共 ${raw.line_count} 行${raw.has_more?' · 此处仅显示一段，可通过 MCP 继续读取':''}`),h('pre',{class:'document-content'},raw.content));
      },'small')];
      if(!isKnowledge && this.can('tasks:write'))actions.push(button('提炼为长期知识候选',()=>{
        const candidate=h('form',{},h('div',{class:'fields'},
          field('topic','主题标识',{required:true,pattern:'[a-z0-9][a-z0-9._/-]{0,119}',maxlength:120,placeholder:'例如 web.auth.device-approval'}),
          field('kind','知识类型',{items:[['constraint','约束'],['decision','决定'],['interface','接口'],['lesson','经验'],['pitfall','错误预防']]}),
          field('title','标题',{required:true,maxlength:200,value:card.title.slice(0,200)}),
          field('body','长期结论',{required:true,minlength:10,maxlength:2000,full:true,multiline:true}),
          field('source_quote','逐字原文引用',{required:true,minlength:8,maxlength:1200,value:card.body.slice(0,800),full:true,multiline:true}),
          field('reason','提炼原因',{required:true,minlength:10,maxlength:1000,full:true,multiline:true})));
        dialog('提交长期知识候选','每条绑定一个获准来源。候选经独立审核后生效，原文继续保留。',candidate,'提交候选',values=>
          this.api('POST',base+'/knowledge',{...values,source_card_id:card.id,source_hash:card.source_hash,
            generation:{method:'human'},...(filter.repository_id?{repository_id:filter.repository_id}:{})}));
      },'small'));
      const decisions=isKnowledge?[['active','审核生效'],['conflicted','标记冲突'],['retired','退役'],['rejected','驳回']]:[['conflicted','标记冲突'],['retired','退役'],['active','恢复使用']];
      if(this.can('tasks:publish'))for(const [state,label] of decisions){
        actions.push(button(label,()=>{
          const reason=h('form',{},h('label',{},'处理原因',h('textarea',{name:'reason',required:true,minlength:isKnowledge?10:1,maxlength:2000,rows:3})));
          if(isKnowledge && state==='active')reason.append(h('div',{class:'fields'},
            field('override_reason','超管本人批准时填写自审原因',{maxlength:2000,full:true}),
            field('replaces_id','替换的生效知识 ID（可选）',{maxlength:36}),
            field('replaces_version','替换知识的当前版本（可选）',{type:'number',min:1})));
          dialog(label,isKnowledge?'作者需由其他有权人员审核；超管自审记录原因。同主题替换须核对旧知识 ID 和版本。':'原文与卡片保留。恢复不会让已经被替代的历史文档或日志成为当前版本。',reason,'保存',values=>{
            const body={expected_version:isKnowledge?card.version:card.state_version,state,reason:values.reason};
            if(isKnowledge){
              if(filter.repository_id)body.repository_id=filter.repository_id;
              if(values.override_reason)body.override_reason=values.override_reason;
              if(values.replaces_id){body.replaces_id=values.replaces_id;body.replaces_version=Number(values.replaces_version);}
            }
            return this.api('POST',`${base}/${isKnowledge?'knowledge/':''}${card.id}/decisions`,body);
          });
        },'quiet small'));
      }
      return h('article',{class:'card'},h('h2',{},card.title),h('p',{class:'hint'},labels[card.state] || card.state),
        isKnowledge?h('p',{class:'mono'},`${card.topic} · ${card.kind} · 知识 ${card.id} · 版本 ${card.version}`):null,
        h('p',{class:'mono'},source.kind==='document'?`${source.path} · ${short(source.git_commit)} · 行 ${card.line_start}–${card.line_end}`:`日志 ${short(source.id)} · 任务 ${short(source.task_id)}`),
        h('pre',{class:'document-content'},card.body),card.truncated?h('p',{class:'hint'},'摘录已截短；完整内容保存在来源原文。'):null,
        isKnowledge?h('details',{},h('summary',{},'引用与生成记录'),h('pre',{class:'document-content'},card.source_quote),
          h('p',{class:'hint'},`生成方式：${card.generation.method}；模型及用量为作者报告。${card.generation.model_reported || ''}`),
          card.review_override_reason?h('p',{},'超管自审原因：'+card.review_override_reason):null):null,
        card.state_reason?h('p',{},card.state_reason):null,h('div',{class:'row-actions'},actions),original);
    });
    return [title,h('section',{class:'card'},h('h2',{},'增量处理状态'),counts,
      this.can('tasks:publish')?button('检索验证记录',()=>this.openMemoryEvaluations(projectId,filter.repository_id),'small'):null,
      this.can('tasks:publish') && (status.visible_jobs.failed || status.embeddings?.visible_jobs.failed)?button('重试失败处理',async()=>{await this.api('POST',base+'/retry',{});await this.renderView();},'small'):null,
      h('p',{class:'hint'},'日志和文档增量摘录；长期知识经审核后进入开工记忆。语义检索在授权范围内与关键词融合，来源变化即时重查。开工记忆共用 12,000 UTF-8 字节预算。')),
      h('section',{class:'card'},form,h('p',{class:'hint'},data.semantic_search==='ready'?'本次结合语义和关键词排序；请核对来源。':data.semantic_search==='unavailable-keyword-fallback'?'语义服务暂不可用，本次已退回关键词。':data.semantic_search==='keyword-requested'?'本次仅按关键词匹配。':'当前列出获准资料或按关键词匹配。'),
        data.candidate_limit_reached?h('p',{class:'hint'},'达到检索候选上限，请缩小问题或仓库范围。'):null),...cards,
      !cards.length?h('p',{class:'hint'},'当前检索范围没有匹配卡片。请查看文档同步、队列状态或调整关键词；这不代表没有其他历史记录。'):null,
      data.has_more?h('p',{class:'hint'},'这里只显示前 12 条，缩小检索范围或通过 MCP 翻页。'):null].filter(Boolean);
  }
  async usage() {
    const title=this.heading('用量与预算','程序测量与工具上报分别统计；目前按月统计本人可见的项目资料范围。');
    if(!this.project)return [title,h('section',{class:'card empty'},'请先选择项目。')];
    const projectId=this.project.id,base=`/api/projects/${projectId}/usage`,data=await this.api('GET',base);
    const content=h('section',{class:'card'},h('h2',{},'本月记录'),
      h('p',{},`输入 Token（上报）：${data.reported_input_tokens} · 输出 Token（上报）：${data.reported_output_tokens}`),
      h('p',{},`缓存输入（已包含在输入中）：${data.reported_cached_input_tokens} · 开工包大小（程序测得）：${data.measured_context_bytes.toLocaleString()} 字节`),
      h('p',{},'上报费用：'+(data.reported_cost_micro_usd===null?'未提供':(data.reported_cost_micro_usd/1000000).toFixed(6)+' USD')),
      h('p',{class:'hint'},'上报尚未核对供应商账单。工具未提供的 Token 消耗无法计入；未收到报告不表示没有消耗。开工包字节数不是 Token 计费量。'),
      h('h3',{},'项目额度'),h('p',{},`${data.budget.mode==='observe'?'只观察':'达到额度暂停受管开发'} · 月度上报 Token 额度：${data.budget.monthly_reported_token_limit || '未设上限'}`),
      h('p',{},data.budget.managed_work_available?'当前允许开始受管工作':'已暂停新的受管修改；可以继续保存日志、上传检查点和交接。'),
      h('p',{class:'hint'},'额度基于已收到的全项目报告判断，不能拦截独立外部程序或未上报的调用。上方用量只展示你有权查看的记录。'));
    if(this.can('tasks:publish'))content.append(button('调整额度',()=>{
      const form=h('form',{},field('mode','达到额度时',{value:data.budget.mode,items:[['observe','只观察'],['pause-managed-work','暂停受管开发']]}),
        field('limit','月度上报 Token 额度（0 表示不设上限）',{type:'number',min:0,max:1000000000000,value:data.budget.monthly_reported_token_limit,required:true}),
        field('change_reason','调整原因',{required:true,minlength:10,maxlength:1000}));
      dialog('调整项目额度','额度只对平台收到的宿主用量报告生效，日志和交接继续可用。',form,'保存额度',values=>this.api('PUT',base+'/budget',{
        expected_version:data.budget.version,mode:values.mode,monthly_reported_token_limit:Number(values.limit),change_reason:values.change_reason}));
    }));
    return [title,content];
  }
  async openMemoryEvaluations(projectId,repositoryId) {
    const base=`/api/projects/${projectId}/memory/evaluations`;
    const data=await this.api('GET',base+(repositoryId?'?repository_id='+repositoryId:''));
    if(this.project?.id!==projectId)return;
    const content=h('div',{id:'modal-body'},h('h2',{},'检索验证记录'),
      h('p',{},'用你已确认应找到的原文摘录进行实际检索，比较命中与排名。记录保留当时结果，不能代表完整项目理解。'),
      ...data.items.map(item=>h('p',{},`${date(item.created_at)} · ${item.payload.case_count} 题 · 命中率 ${(item.payload.mean_recall_at_k*100).toFixed(1)}% · 平均倒数排名 ${item.payload.mean_reciprocal_rank.toFixed(3)} · 前 ${item.payload.k} 条`)),
      h('div',{class:'row-actions'},button('新增验证',()=>{
        const form=h('form',{},field('query','验证问题',{required:true,maxlength:200}),
          h('label',{},'预期应找到的摘录 ID（每行一项）',h('textarea',{name:'expected',required:true,rows:3})),
          field('search_mode','检索方式',{items:[['hybrid','语义与关键词'],['keyword','只用关键词']]}),
          field('k','检查前几条',{type:'number',min:1,max:20,value:5,required:true}));
        dialog('新增检索验证','只使用当前可读且有效的原文摘录 ID；长期知识候选 ID 不适用。结果会记录真实使用的检索方式与降级情况。',form,'执行并保存',values=>this.api('POST',base,{
          repository_id:repositoryId || null,k:Number(values.k),search_mode:values.search_mode,
          cases:[{query:values.query,expected_card_ids:values.expected.split(/\r?\n/).map(v=>v.trim()).filter(Boolean)}]}));
      }),button('关闭',()=>modal.close())));
    modal.replaceChildren(content);if(!modal.open)modal.showModal();
  }
  async documents() {
    const title = this.heading('项目文档','从指定 GitHub 分支建立 Markdown 索引；读取结果带文件路径和提交号。',[button('刷新状态',() => this.renderView())]);
    if (!this.project) return [title,h('section',{class:'card empty'},'请先选择项目；项目只能由超级管理员创建。')];
    const revision = this.renderVersion;
    const projectId = this.project.id, registry = await this.api('GET',`/api/projects/${projectId}/repositories`);
    const scoped = registry.repository_mode === 'scoped';
    const accessible = registry.items.filter(repo => repo.status === 'active' &&
      (repo.capabilities.includes('documents:read') || this.can('documents:manage') && repo.capabilities.includes('code:read')));
    const parts = [title];
    let historical = false;
    if (scoped && this.isRoot) {
      const sources = await this.api('GET',`/api/projects/${projectId}/documents/sources`);
      historical = sources.items.some(source => !source.repository_id);
    }
    if (scoped && !accessible.length && !historical) return [...parts,h('section',{class:'card empty'},'当前没有获准的文档仓库，请联系超级管理员审批资料权限。')];
    const previous = this.documentSource?.projectId === projectId ? this.documentSource.repositoryId : null;
    const selected = scoped ? (historical && previous === '' ? null : accessible.find(repo => repo.id === previous) || accessible[0]) : null;
    if (scoped) {
      this.documentSource = {projectId,repositoryId:selected?.id || ''};
      const picker = field('document_source','文档所属仓库',{value:selected?.id || '',items:[...accessible.map(repo => [repo.id,repo.name]),
        ...(historical ? [['','历史文档源（仅超管）']] : [])]});
      picker.querySelector('select').addEventListener('change',event => {
        this.documentSource = {projectId,repositoryId:event.target.value}; this.renderView().catch(error => this.report(error));
      });
      parts.push(h('section',{class:'card'},picker));
    }
    const base = selected ? `/api/projects/${projectId}/repositories/${selected.id}/documents` : `/api/projects/${projectId}/documents`;
    const data = await this.api('GET',`${base}/repository`);
    const status = data.latest_sync;
    const states = {queued:'排队中',running:'同步中',succeeded:'同步成功',failed:'同步失败',cancelled:'已取消'};
    parts.push(h('section',{class:'card'},h('h2',{},'当前索引'),
      h('p',{},data.configured ? `${data.repository_url} · ${data.branch}` : '尚未配置文档仓库。'),
      h('p',{class:'mono'},data.git_commit ? `提交 ${data.git_commit}` : '尚无可用索引'),
      h('p',{class:'hint'},`文档 ${data.document_count || 0} 篇 · 跳过 ${data.skipped_count || 0} 篇 · 最近同步 ${date(data.indexed_at)}`),
      status && h('p',{},`同步任务：${states[status.status] || status.status}${status.error_code ? ` · ${messagesForDocument(status.error_code)}` : ''}`),
      data.can_manage && data.configured && button('同步文档',async () => {
        await this.api('POST',`${base}/syncs`,{});toast('同步任务已排队，点击“刷新状态”查看结果。');await this.renderView();
      },'primary')));
    if (data.can_manage) {
      const form = h('form',{},h('div',{class:'fields single'},
        field('repository_url','GitHub 仓库地址',{type:'url',required:true,readonly:!!selected,value:data.repository_url || selected?.repository_url || '',placeholder:'https://github.com/你的组织/你的仓库'}),
        field('branch','分支',{required:true,readonly:!!selected,maxlength:200,value:data.branch || selected?.branch || 'main'}),
        h('label',{class:'full'},'允许索引的目录或 Markdown 文件（每行一项）',h('textarea',{name:'include_paths',required:true,rows:4},(data.include_paths || ['docs','AGENTS.md','install.md']).join('\n'))),
        !selected && field('token','GitHub 只读凭证（公共仓库可留空）',{type:'password',autocomplete:'new-password',maxlength:2048,
          hint:data.token_set ? '已有加密凭证；留空保留。更换仓库时需重新填写凭证或选择清除。' : '私有仓库使用仅授权该仓库 Contents: read 的凭证。'}),
        !selected && field('clear_token','凭证处理',{items:[['keep','保留现有凭证'],['clear','清除凭证，使用公共访问']]})));
      form.append(h('div',{class:'actions'},submit(form,'保存文档源',async values => {
        const body = {expected_version:data.version,repository_url:values.repository_url.trim(),branch:values.branch.trim(),
          include_paths:values.include_paths.split(/\r?\n/).map(p => p.trim()).filter(Boolean),clear_token:values.clear_token === 'clear'};
        if (values.token) body.token = values.token;
        await this.api('PUT',`${base}/repository`,body);toast('文档源已保存，请点击“同步文档”建立索引。');await this.renderView();
      })));
      parts.push(h('section',{class:'card'},h('h2',{},'文档源设置'),form,h('p',{class:'hint'},'保存配置会清除旧索引。仅处理指定范围内的 UTF-8 Markdown；不执行仓库脚本。')));
    }
    const canRead = this.isRoot || this.grants.some(g => g.project_id === projectId && g.capabilities.includes('documents:read'));
    if (!data.snapshot_id || !canRead) return parts;
    const resultBox = h('div',{}), searchForm = h('form',{},field('query','关键词（留空列出文档）',{maxlength:200,placeholder:'支持中文、路径和正文片段'}));
    let requestNumber = 0;
    const find = async (query,offset=0) => {
      const request = ++requestNumber;
      const result = await this.api('POST',`${base}/search`,{query,offset,limit:20,snapshot_id:data.snapshot_id});
      if (request !== requestNumber || this.project?.id !== projectId || this.currentView !== 'documents' || this.renderVersion !== revision) return;
      resultBox.replaceChildren(table('匹配文档',['文档','正文片段','操作'],result.items.map(doc => [textPair(doc.title,doc.path),
        h('p',{class:'document-excerpt'},doc.excerpt),button('读取',async () => {
          const document = await this.api('POST',`${base}/read`,{path:doc.path,snapshot_id:doc.snapshot_id});
          if (this.project?.id !== projectId || this.currentView !== 'documents' || this.renderVersion !== revision) return;
          modal.replaceChildren(h('div',{class:'modal-heading'},h('h2',{},document.title),button('关闭',() => modal.close(),'quiet')),
            h('p',{class:'mono'},`${document.path} · ${document.git_commit}`),h('pre',{class:'document-content'},document.content));
          modal.showModal();
        },'small')])));
      if (result.has_more || offset) resultBox.append(h('div',{class:'actions'},
        offset > 0 && button('上一页',() => find(query,Math.max(0,offset-20)),'small'),
        result.has_more && button('下一页',() => find(query,result.next_offset),'small')));
    };
    searchForm.append(submit(searchForm,'搜索文档',values => find(values.query)));
    parts.push(h('section',{class:'card'},h('h2',{},'检索文档'),h('p',{class:'hint'},'按文字片段匹配，结果按路径排序。'),searchForm,resultBox));
    await find('');return parts;
  }
  async projectView() {
    const actions = this.isRoot ? [button('新建项目',() => this.createProject(),'primary')] : [];
    const parts = [this.heading('项目管理','项目由超级管理员创建，管理员在分配范围内管理成员。',actions),
      table('项目列表',['项目','项目 ID','操作'],this.projects.map(p => [textPair(p.name,p.slug),h('span',{class:'mono'},p.id),
        button('查看成员',async () => { this.project = p; this.shell(); await this.renderView(); },'small')]))];
    if (!this.project) return [...parts,h('p',{class:'help-block'},this.projects.length ? '请从列表选择一个项目。' : this.isRoot ? '点击“新建项目”创建第一个项目。' : '当前没有获授权项目。请联系超级管理员创建项目并签发授权。')];
    const canManage = this.isRoot || this.grants.some(g => g.project_id === this.project.id && g.capabilities.includes('members:manage'));
    if (!canManage) return [...parts,h('p',{class:'help-block'},`当前项目：${this.project.name}。当前账号没有成员管理权限。`)];
    const members = (await this.api('GET',`/api/projects/${this.project.id}/members?offset=${this.offset}`)).items;
    const actions2 = [button('添加成员',() => this.inviteMember(),'primary small'),button('邮箱邀请',() => this.inviteByEmail(),'small'),button('管理邀请',() => this.invitations(),'small'),button('申请项目授权',() => this.requestGrant(),'small')];
    if (this.isRoot) actions2.push(button('分配管理员',() => this.assignAdmin(),'small'));
    const memberTable = table(`${this.project.name} · 成员`,['人员','项目角色','成员状态','账号状态','操作'],members.map(u => [
      textPair(u.login,u.email || u.id),u.membership_role === 'administrator' ? '项目管理员' : '普通成员',pill(u.membership_status),pill(u.status),
      h('div',{class:'row-actions'},u.membership_status === 'active' && (this.isRoot || u.membership_role !== 'administrator') ? button('移出项目',async () => {
        if (!confirm(`移出 ${u.login} 后，该项目现有授权将立即失效。继续？`)) return;
        await this.api('POST',`/api/projects/${this.project.id}/members/${u.id}/revoke`,{}); toast('成员已移出，项目授权已撤销。'); await this.renderView();
      },'danger small') : this.isRoot && u.membership_status === 'revoked' ? button('恢复成员关系',async () => {
        await this.api('POST',`/api/admin/projects/${this.project.id}/members/${u.id}/restore`,{}); toast('成员关系已恢复，仍需重新签发授权。'); await this.renderView();
      },'small') : '—')]),actions2);
    if (members.length === 100 || this.offset) memberTable.append(this.pagination(members.length));
    parts.push(memberTable);
    const grants = (await this.api('GET',`/api/projects/${this.project.id}/grants`)).items;
    parts.push(table('近期项目授权',['人员 / 设备','能力','状态','到期'],grants.map(g => [textPair(short(g.user_id),short(g.device_id)),g.capabilities.map(c => caps[c] || c).join('、'),pill(g.status),date(g.expires_at)])));
    return parts;
  }
  pagination(count) {
    return h('div',{class:'pagination'},h('div',{class:'row-actions'},this.offset > 0 && button('上一页',async () => { this.offset -= 100; await this.renderView(); },'small'),
      h('span',{class:'hint'},`第 ${Math.floor(this.offset / 100) + 1} 页`),count === 100 && button('下一页',async () => { this.offset += 100; await this.renderView(); },'small')));
  }
  async allPages(path) {
    const items = [];
    for (let offset = 0; ; offset += 100) {
      const page = (await this.api('GET',`${path}?offset=${offset}&limit=100`)).items;
      items.push(...page);
      if (page.length < 100) return items;
    }
  }
  async users() {
    const users = (await this.api('GET',`/api/admin/users?offset=${this.offset}`)).items;
    const view = table('账号列表',['人员','账号角色','邮箱验证','状态','操作'],users.map(u => [textPair(u.login,u.email || u.id),
      u.platform_role === 'superadmin' ? '超级管理员' : u.admin_eligible ? '管理员账号' : '普通成员',pill(u.email_verified ? 'approved' : 'pending_email'),pill(u.status),
      u.platform_role === 'superadmin' ? '—' : u.status === 'pending_email' ? '等待邮箱验证' : h('div',{class:'row-actions'},...(u.status === 'active' ? [['suspended','冻结'],['disabled','停用']] : [['active','恢复']]).map(([status,label]) => button(label,async () => {
        if (!confirm(`${label} ${u.login}？已有会话与授权会失效，恢复后也需重新签发授权。`)) return;
        await this.api('POST',`/api/admin/users/${u.id}/status`,{status}); toast('账号状态已更新'); await this.renderView();
      },status === 'active' ? 'small' : 'danger small')))]));
    if (users.length === 100 || this.offset) view.append(this.pagination(users.length));
    return [this.heading('人员管理','管理全局人员状态；管理员账号必须绑定并验证邮箱。',[
      button('创建管理员',() => this.createAdmin(),'primary'),button('邮箱邀请管理员',() => this.inviteByEmail(true)),button('管理邀请',() => this.invitations(true))]),view];
  }
  async devices() {
    const devices = (await this.api('GET',this.isRoot ? `/api/admin/devices?offset=${this.offset}` : '/api/me/devices')).items;
    const view = table(this.isRoot ? '设备列表' : '我的设备',['设备','账号 / 指纹','邮箱验证','状态','操作'],devices.map(d => [textPair(d.name,d.os),
      textPair(d.login,short(d.thumbprint)),pill(d.email_verified ? 'approved' : 'pending_email'),pill(d.status),
      d.id !== this.device && d.status !== 'revoked' ? button('撤销设备',async () => {
        if (!confirm(`撤销 ${d.name}？该设备现有会话与项目授权将失效。`)) return;
        await this.api('POST',`/api/${this.isRoot ? 'admin' : 'me'}/devices/${d.id}/revoke`,{}); toast('设备已撤销'); await this.renderView();
      },'danger small') : d.id === this.device ? '当前设备' : '—']));
    if (this.isRoot && (devices.length === 100 || this.offset)) view.append(this.pagination(devices.length));
    return [this.heading(this.isRoot ? '设备管理' : '我的设备','新设备需要独立审批；绑定邮箱的账号还需完成邮箱验证码验证。',
      [button('绑定 / 更换我的邮箱',() => this.changeEmail())]),view];
  }
  async approvals() {
    const [result, users, devices] = await Promise.all([
      this.allPages('/api/admin/approvals'), this.allPages('/api/admin/users'), this.allPages('/api/admin/devices')
    ]);
    const items = result;
    const repositoryNames = new Map();
    await Promise.all([...new Set(items.filter(a => a.kind !== 'device').map(a => a.payload.project_id))].map(async projectId => {
      const repositories = await this.api('GET',`/api/projects/${projectId}/repositories`);
      for (const repo of repositories.items) repositoryNames.set(repo.id,repo.name);
    }));
    const resourceNames = {'code:read':'读取代码','code:write':'开发代码','documents:read':'读取文档'};
    const details = a => {
      const user = users.find(u => u.id === a.subject_id), requester = users.find(u => u.id === a.requester_id);
      const device = devices.find(d => d.id === a.payload.device_id), project = this.projects.find(p => p.id === a.payload.project_id);
      const row = (label, value) => h('div',{},h('span',{},label),h('strong',{class:'mono'},value));
      return h('div',{class:'detail-list'},row('申请账号',user?.login || short(a.subject_id)),
        row('提交人',requester?.login || short(a.requester_id)),row('设备',device ? `${device.name} · ${device.os}` : short(a.payload.device_id)),
        device && row('设备指纹',device.thumbprint),a.kind !== 'device' && [row('项目',project?.name || short(a.payload.project_id)),
          row('授权能力',(a.payload.capabilities || []).map(c => caps[c] || c).join('、')),
          row('仓库范围',(a.payload.repository_permissions || []).map(permission =>
            `${repositoryNames.get(permission.repository_id) || permission.repository_id}：${permission.capabilities.map(cap => resourceNames[cap] || cap).join('、')}`)
            .join('；') || '无代码或仓库资料授权'),
          row('授权期限',`${a.payload.duration_days} 天`)]);
    };
    return [this.heading('授权审批','核对申请的设备、项目与能力，按当前版本批准或拒绝。',[button('刷新申请',() => this.renderView())]),
      ...items.map(a => h('article',{class:'approval'},h('div',{class:'approval-head'},h('div',{},h('h2',{},a.kind === 'device' ? '新设备登录申请' : '项目授权申请'),
        h('span',{class:'hint'},`申请 ${short(a.id)} · 到期 ${date(a.expires_at)}`)),pill(a.status)),
        details(a),h('div',{class:'actions'},button('批准申请',async () => {
          if (!confirm('确认批准当前显示的申请范围？')) return;
          await this.api('POST',`/api/admin/approvals/${a.id}/decision`,{approve:true,expected_version:a.version,expected_hash:a.payload_hash});
          toast('申请已批准'); await this.renderView();
        },'primary'),button('拒绝申请',async () => {
          await this.api('POST',`/api/admin/approvals/${a.id}/decision`,{approve:false,expected_version:a.version,expected_hash:a.payload_hash});
          toast('申请已拒绝'); await this.renderView();
        },'danger')))),!items.length && h('section',{class:'card empty'},'当前没有待审批申请。')];
  }
  async settings() {
    const data = await this.api('GET','/api/admin/settings');
    const form = h('form',{},this.configFields(data));
    form.append(h('div',{class:'actions'},submit(form,'保存设置',async values => {
      await this.api('PUT','/api/admin/settings',this.configBody(values,data.version)); toast('系统设置已加密保存'); await this.renderView();
    }),button('发送测试邮件',async () => {
      const result = await this.api('POST','/api/admin/settings/test-email',{}); toast(`测试邮件已发送至 ${result.recipient}`);
    })));
    return [this.heading('系统设置','维护服务地址和发信服务。仅超级管理员可以查看或修改。',
      [button('更换超管邮箱',() => this.changeEmail())]),
      h('section',{class:'card'},h('h2',{},'发信与服务地址'),form),h('div',{class:'help-block'},
        h('strong',{},'服务地址状态：'),data.public_url || '尚未填写',h('br'),'当前接入地址：',data.active_origin,
        h('br'),'服务地址保存后，需在部署端完成 DNS 与 HTTPS 入口启用。发信设置保存后立即用于验证码邮件。')];
  }
  async audit() {
    if (!this.isRoot && !this.project) return [this.heading('操作记录','查看账号管理和项目授权的操作记录。'),h('p',{class:'help-block'},'请先选择有审计权限的项目。')];
    const items = (await this.api('GET',`/api/audit${this.isRoot ? '' : `?project_id=${this.project.id}`}`)).items;
    return [this.heading('操作记录','显示最近 100 条记录，不包含密码、验证码或原始凭证。'),
      table('近期操作',['时间','动作','对象','说明'],items.map(e => [date(e.created_at),e.action,h('span',{class:'mono'},short(e.object_id)),JSON.stringify(e.details)]))];
  }
  createProject() {
    dialog('创建项目','只有超级管理员可以创建项目。项目标识用于稳定引用。',h('form',{},h('div',{class:'fields single'},
      field('name','项目名称',{required:true,maxlength:200}),field('slug','项目标识',{required:true,pattern:'[a-z][a-z0-9\\-]{1,79}',placeholder:'例如 agent-platform'}))),
      '创建项目',async data => { await this.api('POST','/api/admin/projects',data); this.projects = (await this.api('GET','/api/projects')).items; this.shell(); });
  }
  createAdmin() {
    dialog('创建管理员','新账号必须先验证邮箱和设备，再分配项目与授权。',h('form',{},h('div',{class:'fields single'},
      field('login','登录名',{...loginOptions}),field('email','邮箱',{type:'email',required:true}),
      field('password','初始密码',{type:'password',required:true,minlength:12,maxlength:256,autocomplete:'new-password'}))),
      '创建管理员',data => this.api('POST','/api/admin/users/admins',data));
  }
  inviteMember() {
    dialog('添加普通成员','新建普通成员账号并加入当前项目；设备和项目授权仍需超级管理员审批。',h('form',{},h('div',{class:'fields single'},
      field('login','登录名',{...loginOptions}),field('email','邮箱（可选）',{type:'email'}),
      field('password','初始密码',{type:'password',required:true,minlength:12,maxlength:256,autocomplete:'new-password'}))),
      '添加成员',data => this.api('POST',`/api/projects/${this.project.id}/member-invitations`,{new_user:{...data,email:data.email || null}}));
  }
  async assignAdmin() {
    const users = (await this.allPages('/api/admin/users')).filter(u => u.admin_eligible && u.email_verified && u.status === 'active' && u.platform_role !== 'superadmin');
    if (!users.length) throw new Error('请先创建管理员，并让其完成邮箱与设备验证。');
    dialog('分配项目管理员','分配管理员角色后，仍需为其设备签发项目授权。',h('form',{},field('user_id','管理员账号',{items:users.map(u => [u.id,`${u.login} · ${u.email}`]),required:true})),
      '分配管理员',data => this.api('PUT',`/api/admin/projects/${this.project.id}/administrators`,data));
  }
  async requestGrant() {
    const members = (await this.allPages(`/api/projects/${this.project.id}/members`)).filter(u => u.membership_status === 'active' && u.status === 'active');
    const memberIds = new Set(members.map(u => u.id));
    const devices = (await this.allPages(`/api/projects/${this.project.id}/member-devices`)).filter(d => d.status === 'approved' && memberIds.has(d.user_id));
    if (!devices.length) throw new Error('请先让成员完成设备验证和审批。');
    const form = h('form',{},h('div',{class:'fields single'},
      field('device_id','成员设备',{items:devices.map(d => [d.id,`${d.login} · ${d.name}`]),required:true}),
      field('duration_days','授权天数',{type:'number',min:1,max:90,value:30,required:true})));
    const repositories = (await this.api('GET',`/api/projects/${this.project.id}/repositories`)).items.filter(repo => repo.status === 'active');
    const checks = h('div',{class:'capabilities'});
    function updateCapabilities() {
      const device = devices.find(d => d.id === form.elements.device_id.value);
      const member = members.find(m => m.id === device.user_id);
      checks.replaceChildren(...Object.entries(caps).filter(([cap]) => member.membership_role === 'administrator' || !['tasks:publish','members:manage','grants:request','audit:read','documents:manage','repositories:manage'].includes(cap))
        .map(([cap,label]) => h('label',{},h('input',{type:'checkbox',name:'capability',value:cap,checked:cap === 'project:read'}),label)));
    }
    form.append(h('h3',{},'授权能力'),checks); updateCapabilities(); form.elements.device_id.addEventListener('change',updateCapabilities);
    const repositoryChecks = h('div',{},...repositories.map(repo => h('section',{},h('h4',{},repo.name),
      h('div',{class:'capabilities'},Object.entries({'code:read':'读取代码','code:write':'开发代码','documents:read':'读取文档'}).map(([cap,label]) =>
        h('label',{},h('input',{type:'checkbox','data-repository':repo.id,value:cap}),label))))));
    if (repositories.length) form.append(h('h3',{},'各仓库权限'),h('p',{class:'hint'},'未勾选的仓库不会开放。开发代码需同时勾选读取代码；读取同事日志还需相应代码读取权限。'),repositoryChecks);
    dialog(this.isRoot ? '签发项目授权' : '申请项目授权',this.isRoot ? '授权将绑定选定人员、设备和当前项目。' : '申请提交后需由超级管理员批准；当前不会获得额外权限。',form,
      this.isRoot ? '批准并签发' : '提交申请',data => {
        const device = devices.find(d => d.id === data.device_id);
        return this.api('POST',this.isRoot ? '/api/admin/grants' : '/api/grant-requests',{
          project_id:this.project.id,user_id:device.user_id,device_id:device.id,duration_days:Number(data.duration_days),
          capabilities:[...checks.querySelectorAll('input:checked')].map(c => c.value),
          repository_permissions:repositories.map(repo => ({repository_id:repo.id,
            capabilities:[...repositoryChecks.querySelectorAll('input:checked')].filter(c => c.dataset.repository === repo.id).map(c => c.value)}))
            .filter(permission => permission.capabilities.length)});
      });
  }
  changeEmail() {
    let challenge;
    const form = h('form',{},h('div',{class:'fields single'},field('email','新邮箱',{type:'email',required:true}),
      field('password','当前账号密码',{type:'password',required:true,autocomplete:'current-password'})));
    const verify = h('form',{hidden:true},field('code','新邮箱验证码',{required:true,pattern:'[0-9]{8}',inputmode:'numeric',autocomplete:'one-time-code'}));
    form.append(h('div',{class:'actions'},submit(form,'发送新邮箱验证码',async data => {
      challenge = await this.api('POST','/api/me/email-challenges',data); form.elements.password.value = '';
      verify.hidden = false; toast('验证码已发送至新邮箱。');
    })));
    verify.append(h('div',{class:'actions end'},button('取消',() => modal.close()),submit(verify,'验证并更换邮箱',async data => {
      this.client.setSession(await this.api('POST','/api/me/email-verifications',{challenge_id:challenge.challenge_id,code:data.code}));
      modal.close(); await this.enter(); toast('邮箱已更新，当前设备已验证。');
    })));
    modal.querySelector('#modal-body').replaceChildren(h('h2',{},'绑定或更换邮箱'),h('p',{},
      '验证新邮箱后，其他设备需要重新验证邮箱，已有项目授权需要超级管理员重新签发。当前设备会获得新的登录会话。'),form,verify);
    modal.showModal();
  }
}

const app = new Console();
await app.start();
