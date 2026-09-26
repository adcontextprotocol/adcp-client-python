'use strict';
const fs=require('fs'), path=require('path'), Module=require('node:module');
const base=path.resolve(process.argv[2]);
const req=Module.createRequire(path.join(base,'noop.js'));
const S=req('@adcp/sdk/signing');
const sample=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const keys=JSON.parse(fs.readFileSync(path.join(process.argv[4],'keys.json'),'utf8')).keys
  .map(k=>Object.fromEntries(Object.entries(k).filter(([n])=>!n.startsWith('_'))));
const byKid=Object.fromEntries(keys.map(k=>[k.kid,k]));
(async()=>{
  const opts={
    jwks:{ async resolve(kid){ return byKid[kid] ?? null; } },
    replayStore:{ async has(){return false;}, async isCapHit(){return false;}, async insert(){return 'ok';} },
    revocationStore:{ async isRevoked(){return false;} },
    now: ()=>1776520800,
  };
  try{
    const r=await S.verifyWebhookSignature(sample.request, opts);
    console.log(JSON.stringify({ts_accepts_python_signed_webhook:true, result:r}));
  }catch(e){
    console.log(JSON.stringify({ts_accepts_python_signed_webhook:false, code:e?.code, message:(e?.message||'').slice(0,140)}));
  }
})();
