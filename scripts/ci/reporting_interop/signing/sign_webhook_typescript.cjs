'use strict';
const fs=require('fs'), path=require('path'), Module=require('node:module');
const req=Module.createRequire(path.join(path.resolve(process.argv[2]),'noop.js'));
const S=req('@adcp/sdk/signing');
const V=process.argv[3];
const k=JSON.parse(fs.readFileSync(path.join(V,'keys.json'),'utf8')).keys
  .find(x=>x.kid==='test-ed25519-webhook-2026');
const body='{"idempotency_key":"whk_RLX0000000000000000000002","task_id":"t1","operation_id":"op1","status":"completed"}';
const url='https://buyer.example.com/adcp/webhook/create_media_buy/agent_123/op_abc';
const privateKey={kty:k.kty,crv:k.crv,x:k.x,d:k._private_d_for_test_only,alg:k.alg,kid:k.kid,
                  use:'sig',key_ops:['sign'],adcp_use:k.adcp_use};
const out=S.signWebhook(
  {method:'POST',url,headers:{'Content-Type':'application/json'},body},
  {keyid:k.kid, alg:'ed25519', privateKey},
  {nonce:'RLXnonceTS0000000000', now:()=>1776520800, windowSeconds:300});
const hdrs=out.headers||out;
console.log(JSON.stringify({emitted:hdrs, request:{method:'POST',url,
  headers:{'Content-Type':'application/json',...hdrs}, body}},null,1));
