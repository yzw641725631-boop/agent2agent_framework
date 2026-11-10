'use strict';

const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');
const path = require('node:path');
const { Readable, Transform } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { extractWindowsZip } = require('./extract-win.cjs');
const { compareVersions, releaseName, verifiedRelease, verifyFreshness, MetadataError,
  readJson, writeJson, verifyTree, releaseFs } =
  require('./update-integrity.cjs');

const fs = releaseFs.promises;
const releaseId = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const checkInterval = 15000;
const retryInterval = 30000;

async function realDirectory(directory) {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  if (!(await fs.lstat(directory)).isDirectory()) throw new Error('Invalid update directory');
  return fs.realpath(directory);
}

async function removeStaging(directory, root) {
  const actual = await fs.realpath(directory);
  if (path.dirname(actual) !== root || !path.basename(actual).startsWith('download-')) {
    throw new Error('Refusing to remove an unexpected directory');
  }
  await fs.rm(actual, { recursive: true, force: true, maxRetries: 3 });
}

async function readEnvelope(response) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.byteLength;
    if (bytes > 65536) throw new Error('Release response is too large');
    chunks.push(Buffer.from(chunk));
  }
  return JSON.parse(Buffer.concat(chunks));
}

class Updater extends EventEmitter {
  constructor({ directory, version, channel, restart, transport = fetch, extract = extractWindowsZip }) {
    super();
    Object.assign(this, { directory, version, channel, restart, transport, extract });
    compareVersions(version, version);
    this.lifetime = new AbortController();
    this.lastCheck = 0;
    this.retryAt = 0;
    this.state = { status: 'checking', installed_version: version, latest_version: null,
      can_continue: false, code: 'UPDATE_CHECK_PENDING', message: '正在检查客户端版本。' };
  }

  async open() {
    const data = await realDirectory(this.directory);
    this.root = await realDirectory(path.join(data, 'desktop-updates'));
    this.versions = await realDirectory(path.join(this.root, 'versions'));
    if (path.dirname(this.root) !== data || path.dirname(this.versions) !== this.root) {
      throw new Error('Update directory changed');
    }
    this.known = await this.readRecord('known.json');
    this.activationFailure = (await readJson(this.file('failed.json')).catch(() => null))?.version;
  }

  file(name) { return path.join(this.root, name); }

  async readRecord(name) {
    try {
      const record = await readJson(this.file(name),65536);
      return { ...record, release: verifiedRelease(record.envelope, this.channel.publicKey) };
    } catch { return null; }
  }

  async executable(record) {
    if (!releaseId.test(record.id || '')) throw new Error('Invalid activation ID');
    const slot = path.join(this.versions, `${releaseName(record.release.version)}-${record.id}`);
    if (await fs.realpath(slot) !== slot) throw new Error('Release directory changed');
    return verifyTree(slot, record.release);
  }

  async boot(executable, activationId) {
    if (activationId) {
      const pending = await this.readRecord('pending.json');
      if (pending?.id !== activationId || pending.release.version !== this.version ||
          await this.executable(pending) !== await fs.realpath(executable)) throw new Error('Invalid pending release');
      return { pending };
    }
    const active = await this.readRecord('active.json');
    if (active && compareVersions(active.release.version, this.version) > 0) {
      const target = await this.executable(active).catch(() => null);
      if (target) return { target };
    }
    return {};
  }

  async activate(record) {
    await writeJson(this.file('active.json'), { id: record.id, envelope: record.envelope });
    await fs.unlink(this.file('pending.json')).catch(() => {});
    await fs.unlink(this.file('failed.json')).catch(() => {});
    this.activationFailure = null;
  }

  set(status, code, message, extra = {}) {
    this.state = { status, code, message, installed_version: this.version,
      latest_version: this.known?.release.version || null, can_continue: status === 'current', ...extra };
    this.emit('change', this.state);
    return this.state;
  }

  check({ fresh = false, retry = false } = {}) {
    if (this.lifetime.signal.aborted || this.installing) return Promise.resolve(this.state);
    if (this.checking) return this.checking;
    if (this.state.can_continue && this.known) {
      try { verifyFreshness(this.known.release, null, { checkedAt: this.known.checked_at || 0 }); }
      catch (error) {
        this.set('unavailable', error.code, '发布清单已失效或设备时间异常，请重新核验版本。');
      }
    }
    const interval = fresh ? 2000 : checkInterval;
    if (!retry && Date.now() >= this.lastCheck && Date.now() - this.lastCheck < interval) {
      return Promise.resolve(this.state);
    }
    this.checking = this.checkFeed(retry).finally(() => {
      this.lastCheck = Date.now();
      this.checking = null;
    });
    return this.checking;
  }

  async request(name, timeout) {
    const response = await this.transport(new URL(name, this.channel.url), {
      redirect: 'error', cache: 'no-store', signal: AbortSignal.any([this.lifetime.signal, AbortSignal.timeout(timeout)]),
    });
    if (!response.ok || !response.body) {
      await response.body?.cancel();
      throw new Error('Release endpoint is unavailable');
    }
    return response;
  }

  async checkFeed(retry) {
    let checked = false;
    let failure;
    try {
      const envelope = await readEnvelope(await this.request('stable.json', 7000));
      const release = verifiedRelease(envelope, this.channel.publicKey);
      verifyFreshness(release, this.known?.release, { checkedAt: this.known?.checked_at || 0 });
      const order = this.known ? compareVersions(release.version, this.known.release.version) : 1;
      if (order < 0 || compareVersions(release.version, this.version) < 0) {
        throw new MetadataError('UPDATE_METADATA_ROLLBACK');
      }
      if (order === 0 && (release.sha256 !== this.known.release.sha256 ||
          release.files_sha256 !== this.known.release.files_sha256 || release.bytes !== this.known.release.bytes)) {
        throw new Error('Release version was reused');
      }
      const checked_at = Math.max(Date.now(), this.known?.checked_at || 0);
      await writeJson(this.file('known.json'), { envelope, checked_at });
      this.known = { envelope, release, checked_at };
      checked = true;
    } catch (error) {
      if (this.lifetime.signal.aborted) return this.state;
      failure = error;
    }
    // A cached artifact cannot authorize a new download or project call while the
    // current feed is stale/unverifiable. Already installed releases can still boot.
    if (!checked) {
      const code = failure instanceof MetadataError ? failure.code : 'UPDATE_CHECK_UNAVAILABLE';
      const messages = {
        UPDATE_METADATA_EXPIRED: '发布清单已到期。请联系管理员续签清单后重新检查。',
        UPDATE_CLOCK_INVALID: '系统时间异常。请校准设备时间后重新检查版本。',
        UPDATE_METADATA_ROLLBACK: '发布清单版本回退，项目操作已暂停。请联系管理员。',
        UPDATE_METADATA_REUSED: '发布清单编号被重复使用，项目操作已暂停。请联系管理员。',
        UPDATE_METADATA_INVALID: '发布清单缺少有效的新鲜度信息。请联系管理员更新清单。',
        UPDATE_TRUST_ROLLBACK: '更新签名密钥版本回退，已暂停项目操作，请联系管理员。',
        UPDATE_TRUST_EXPIRED: '更新签名密钥授权已到期或时间异常，请联系管理员续签。',
      };
      return this.set('unavailable', code,
        messages[code] || '无法核验发布清单。请恢复连接后重新检查版本。');
    }
    const newest = this.known;
    if (!newest || compareVersions(newest.release.version, this.version) <= 0) {
      return this.set('current', 'CLIENT_CURRENT', '客户端已是可用版本。');
    }
    if (newest.release.version === this.activationFailure && !retry) {
      return this.set('failed', 'CLIENT_UPDATE_FAILED', '新版启动未完成，已恢复原版本。请在桌面端重试更新。');
    }
    if (!retry && Date.now() < this.retryAt) return this.state;
    this.set('downloading', 'CLIENT_UPDATE_REQUIRED', '发现新版，正在下载。更新完成后再执行项目操作。', { progress: 0 });
    this.installing = this.install(newest).catch(() => {
      if (this.lifetime.signal.aborted) return;
      this.retryAt = Date.now() + retryInterval;
      this.set('failed', 'CLIENT_UPDATE_FAILED', '更新未完成。请检查网络和磁盘空间，然后重试。');
    }).finally(() => { this.installing = null; });
    return this.state;
  }

  async install({ envelope, release }) {
    const signal = this.lifetime.signal;
    const staging = await fs.mkdtemp(path.join(this.root, 'download-'));
    try {
      const name = releaseName(release.version);
      const archive = path.join(staging, `${name}.zip`);
      const response = await this.request(`${name}.zip`, 600000);
      const hash = crypto.createHash('sha256');
      let size = 0, reported = 0;
      const verifier = new Transform({ transform: (chunk, _encoding, done) => {
        size += chunk.length;
        if (size > release.bytes) return done(new Error('Archive exceeds signed size'));
        hash.update(chunk);
        const progress = Math.floor(100 * size / release.bytes);
        if (progress >= reported + 5) {
          reported = progress;
          this.set('downloading', 'CLIENT_UPDATE_REQUIRED', '正在下载已发布的更新。', { progress });
        }
        done(null, chunk);
      } });
      await pipeline(Readable.fromWeb(response.body), verifier, releaseFs.createWriteStream(archive, { flags: 'wx' }), { signal });
      if (size !== release.bytes || hash.digest('hex') !== release.sha256) throw new Error('Invalid archive digest');
      this.set('staging', 'CLIENT_UPDATE_REQUIRED', '正在校验并准备新版。');
      const expanded = path.join(staging, 'expanded');
      await fs.mkdir(expanded);
      await this.extract(archive, expanded, name, signal);
      await verifyTree(expanded, release);
      signal.throwIfAborted();
      const id = crypto.randomUUID();
      const slot = path.join(this.versions, `${name}-${id}`);
      if (await fs.realpath(expanded) !== expanded || path.dirname(slot) !== await fs.realpath(this.versions)) {
        throw new Error('Release staging directory changed');
      }
      await fs.rename(expanded, slot);
      await writeJson(this.file('pending.json'), { id, envelope });
      this.set('restarting', 'CLIENT_UPDATE_REQUIRED', '新版已校验，正在切换版本。');
      await this.restart({ id, version: release.version, executable: path.join(slot, 'Agent Hub.exe') });
    } finally {
      await removeStaging(staging, this.root).catch(() => {});
    }
  }

  async close() {
    this.lifetime.abort();
    await Promise.allSettled([this.checking, this.installing]);
  }
}

module.exports = { Updater };
