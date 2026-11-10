'use strict';
const crypto=require('node:crypto'),fs=require('node:fs/promises'),path=require('node:path');
const trust=require('../core/update-trust.cjs'),{publicKey}=require('../core/update-channel.cjs');
async function main(args) {
  const options={};
  for(let i=0;i<args.length;i+=2) {
    if(!['--chain','--current-keys','--next-keys','--output','--days','--threshold'].includes(args[i]) || !args[i+1])throw new Error('Invalid root rotation options');
    options[args[i].slice(2)]=args[i+1];
  }
  if(!options.output || !path.isAbsolute(options.output))throw new Error('Use an absolute new output file');
  const chain=options.chain?JSON.parse(await fs.readFile(options.chain,'utf8')):[];
  const previous=trust.resolve(chain,publicKey);
  async function keys(value) {
    if(!value)throw new Error('Pass protected key paths via --current-keys and --next-keys');
    const files=value.split(',');
    if(files.length>4 || files.some(file=>!path.isAbsolute(file)))throw new Error('Invalid key paths');
    return Promise.all(files.map(async file=>crypto.createPrivateKey(await fs.readFile(file))));
  }
  const current=await keys(options['current-keys']),next=await keys(options['next-keys']);
  const days=Number(options.days || 180),threshold=Number(options.threshold || 1);
  if(!Number.isInteger(days) || days<1 || days>365)throw new Error('Invalid lifetime');
  const issued=new Date(),root={schema:1,type:'agent-hub-update-root',version:previous.version+1,threshold,
    issued_at:issued.toISOString(),expires_at:new Date(issued.getTime()+days*86400000).toISOString(),
    keys:Object.fromEntries(next.map(key=>{
      const pem=crypto.createPublicKey(key).export({type:'spki',format:'pem'});return [trust.keyId(pem),pem];
    }))};
  trust.validateRoot(root);
  const payload=Buffer.from(JSON.stringify(root)),signatures=[...current,...next].map(key=>trust.sign(payload,key));
  const result=[...chain,{payload:payload.toString('base64url'),signatures}];
  trust.resolve(result,publicKey);
  await fs.writeFile(options.output,JSON.stringify(result,null,2)+'\n',{flag:'wx',mode:0o600});
  process.stdout.write(JSON.stringify({root_version:root.version,threshold,expires_at:root.expires_at,
    production_feed_changed:false,old_clients_require_bridge_release:true})+'\n');
}
main(process.argv.slice(2)).catch(error=>{process.stderr.write((error.code || error.message)+'\n');process.exitCode=1;});
