'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const trust=require('../core/update-trust.cjs'),{verifiedRelease,verifyFreshness}=require('../core/update-integrity.cjs');
const {options,sign}=require('../scripts/windows-signing.cjs');
function key(){const {publicKey,privateKey}=crypto.generateKeyPairSync('ed25519');return {
  privateKey,publicKey:publicKey.export({type:'spki',format:'pem'})};}
function rotate(previous,next,version=2) {
  const issued=new Date(Date.now()-60000).toISOString(),expires=new Date(Date.now()+86400000).toISOString();
  const root={schema:1,type:'agent-hub-update-root',version,issued_at:issued,expires_at:expires,
    keys:Object.fromEntries(next.map(k=>[trust.keyId(k.publicKey),k.publicKey])),threshold:next.length};
  const payload=Buffer.from(JSON.stringify(root));
  return {payload:payload.toString('base64url'),signatures:[...previous,...next].map(k=>trust.sign(payload,k.privateKey))};
}
function release(signers,chain) {
  const body={schema:1,channel:'stable',platform:'win-x64',version:'0.21.0',metadata_version:22,
    issued_at:new Date(Date.now()-1000).toISOString(),expires_at:new Date(Date.now()+3600000).toISOString(),
    bytes:1,sha256:'a'.repeat(64),files_sha256:'b'.repeat(64)};
  const payload=Buffer.from(JSON.stringify(body));
  return {payload:payload.toString('base64url'),root_chain:chain,
    signatures:signers.map(k=>trust.sign(payload,k.privateKey))};
}
test('rotation requires old and new thresholds, verifies new releases and blocks trust rollback',()=>{
  const old=key(),next=key(),chain=[rotate([old],[next])];
  const current=verifiedRelease(release([next],chain),old.publicKey);
  assert.equal(current.root_version,2);verifyFreshness(current,null);
  const legacy=release([old],undefined),payload=Buffer.from(legacy.payload,'base64url');
  delete legacy.signatures;legacy.signature=crypto.sign(null,payload,old.privateKey).toString('base64url');
  const previous=verifiedRelease(legacy,old.publicKey);
  assert.throws(()=>verifyFreshness(previous,current),{code:'UPDATE_TRUST_ROLLBACK'});
  const changed=structuredClone(chain);changed[0].signatures=changed[0].signatures.filter(s=>s.key_id===trust.keyId(next.publicKey));
  assert.throws(()=>verifiedRelease(release([next],changed),old.publicKey));
  assert.throws(()=>verifiedRelease(release([old],chain),old.publicKey));
});
test('thresholds count distinct keys and root versions cannot skip',()=>{
  const a=key(),b=key(),c=key(),chain=[rotate([a],[b,c])];
  assert.equal(verifiedRelease(release([b,c],chain),a.publicKey).root_version,2);
  assert.throws(()=>verifiedRelease(release([b,b],chain),a.publicKey));
  assert.throws(()=>trust.resolve([rotate([a],[b],3)],a.publicKey));
});
test('expired root can verify recovery artifacts but cannot authorize online work',()=>{
  const a=key(),b=key(),root=rotate([a],[b]);
  const value=JSON.parse(Buffer.from(root.payload,'base64url'));
  value.issued_at=new Date(Date.now()-3*86400000).toISOString();value.expires_at=new Date(Date.now()-86400000).toISOString();
  const payload=Buffer.from(JSON.stringify(value));root.payload=payload.toString('base64url');
  root.signatures=[a,b].map(k=>trust.sign(payload,k.privateKey));
  const artifact=verifiedRelease(release([b],[root]),a.publicKey);
  assert.equal(artifact.root_version,2);
  assert.throws(()=>verifyFreshness(artifact,null),{code:'UPDATE_TRUST_EXPIRED'});
});
test('Windows signing is explicit, required builds fail closed, and verification failure blocks packaging',()=>{
  assert.equal(options({}).enabled,false);
  assert.throws(()=>options({HUB_REQUIRE_AUTHENTICODE:'1'}));
  assert.throws(()=>options({HUB_WINDOWS_SIGNTOOL:'relative',HUB_WINDOWS_CERT_THUMBPRINT:'a'.repeat(40)}));
  if(process.platform!=='win32')return;
  const env={HUB_WINDOWS_SIGNTOOL:'C:\\tools\\signtool.exe',HUB_WINDOWS_CERT_THUMBPRINT:'a'.repeat(40)};
  let calls=[];
  assert.equal(sign('C:\\build\\app.exe',{env,run:(tool,args)=>{calls.push(args);return {status:0};}}).authenticode_signed,true);
  assert.equal(calls.length,2);assert.ok(calls[0].includes('/tr'));
  assert.throws(()=>sign('C:\\build\\app.exe',{env,run:()=>({status:1})}));
});
