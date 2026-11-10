'use strict';

const crypto=require('node:crypto');
const invalid=()=>Object.assign(new Error('UPDATE_TRUST_INVALID'),{code:'UPDATE_TRUST_INVALID'});
function keyId(key) {
  const value=key instanceof crypto.KeyObject && key.type==='public'?key:crypto.createPublicKey(key);
  if(value.asymmetricKeyType!=='ed25519')throw invalid();
  return crypto.createHash('sha256').update(value.export({type:'spki',format:'der'})).digest('hex');
}
function initial(publicKey) {return {schema:1,type:'agent-hub-update-root',version:1,
  threshold:1,keys:{[keyId(publicKey)]:publicKey}};}
function threshold(payload,signatures,root) {
  if(!Array.isArray(signatures) || signatures.length>8)throw invalid();
  const accepted=new Set();
  for(const item of signatures) {
    if(!item || typeof item.key_id!=='string' || typeof item.signature!=='string' || item.signature.length>100)throw invalid();
    if(accepted.has(item.key_id))continue;
    const key=root.keys[item.key_id];
    if(key) {
      const bytes=Buffer.from(item.signature,'base64url');
      if(bytes.length===64 && crypto.verify(null,payload,key,bytes))accepted.add(item.key_id);
    }
  }
  if(accepted.size<root.threshold)throw invalid();
}
function validateRoot(root) {
  if(!root || root.schema!==1 || root.type!=='agent-hub-update-root' ||
      !Number.isSafeInteger(root.version) || root.version<2 || !root.keys || Array.isArray(root.keys) ||
      Object.keys(root.keys).length<1 || Object.keys(root.keys).length>4 ||
      !Number.isSafeInteger(root.threshold) || root.threshold<1 || root.threshold>Object.keys(root.keys).length)throw invalid();
  for(const [id,key] of Object.entries(root.keys))if(typeof key!=='string' || key.length>256 || keyId(key)!==id)throw invalid();
  const issued=Date.parse(root.issued_at),expires=Date.parse(root.expires_at);
  if(!Number.isFinite(issued) || !Number.isFinite(expires) || issued>=expires ||
    expires-issued>366*86400000 || new Date(issued).toISOString()!==root.issued_at ||
    new Date(expires).toISOString()!==root.expires_at)throw invalid();
}
function resolve(chain,publicKey) {
  let root=initial(publicKey);
  if(chain===undefined)return root;
  if(!Array.isArray(chain) || chain.length>16 || JSON.stringify(chain).length>60000)throw invalid();
  for(const link of chain) {
    if(typeof link?.payload!=='string' || link.payload.length>4096)throw invalid();
    const payload=Buffer.from(link.payload,'base64url'),next=JSON.parse(payload.toString('utf8'));
    validateRoot(next);
    if(next.version!==root.version+1)throw invalid();
    // Both the currently trusted threshold and the replacement threshold must sign.
    // Expiry is checked for the latest root at online use, not historical recovery.
    threshold(payload,link.signatures,root);threshold(payload,link.signatures,next);root=next;
  }
  return root;
}
function verifyEnvelope(envelope,publicKey,payload) {
  const root=resolve(envelope.root_chain,publicKey);
  if(root.version===1 && !envelope.signatures) {
    const signature=Buffer.from(envelope.signature || '','base64url');
    if(signature.length!==64 || !crypto.verify(null,payload,publicKey,signature))throw invalid();
  } else threshold(payload,envelope.signatures,root);
  return root;
}
function sign(payload,privateKey) {
  const publicKey=crypto.createPublicKey(privateKey).export({type:'spki',format:'pem'});
  return {key_id:keyId(publicKey),signature:crypto.sign(null,payload,privateKey).toString('base64url')};
}
module.exports={initial,keyId,resolve,verifyEnvelope,threshold,validateRoot,sign};
