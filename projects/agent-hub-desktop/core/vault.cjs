'use strict';
const fs=require('node:fs/promises');
const path=require('node:path');
const {HubError}=require('./remote.cjs');

class Vault {
  constructor(directory,protector) {
    this.directory=directory; this.protector=protector; this.pending=Promise.resolve();
    this.data={version:1,profiles:{},current:null};
  }
  async load() {
    if (!await this.protector.isAsyncEncryptionAvailable()) {
      throw new HubError('SECURE_STORAGE_UNAVAILABLE','系统凭证保护暂时不可用，请解锁系统后重新打开客户端。');
    }
    await fs.mkdir(this.directory,{recursive:true,mode:0o700});
    try {
      const sealed=await fs.readFile(path.join(this.directory,'vault.bin'));
      const decoded=await this.protector.decryptStringAsync(sealed);
      const data=JSON.parse(decoded.result);
      if (data.version!==1 || !data.profiles) throw new Error('Invalid vault');
      this.data=data;
      if (decoded.shouldReEncrypt) await this.save();
    } catch(error) {
      if (error.code!=='ENOENT') throw new HubError('VAULT_UNAVAILABLE','无法解锁本机凭证，请检查系统登录状态；原文件已保留。');
    }
    return this.data;
  }
  save() {
    const snapshot=JSON.stringify(this.data);
    const save=this.pending.catch(()=>{}).then(async()=>{
      const sealed=await this.protector.encryptStringAsync(snapshot);
      const target=path.join(this.directory,'vault.bin');
      const temporary=target+'.next';
      await fs.writeFile(temporary,sealed,{mode:0o600});
      await fs.rename(temporary,target);
    });
    this.pending=save; return save;
  }
}
module.exports={Vault};
