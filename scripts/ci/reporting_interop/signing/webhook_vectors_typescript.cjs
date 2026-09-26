'use strict';
// RLX-XL-002 TS side: protocol rc.4 webhook-signing vectors through the installed public verifier.
const fs=require('fs'), path=require('path'), Module=require('node:module');
const base=path.resolve(process.argv[2]);
const req=Module.createRequire(path.join(base,'noop.js'));
const VEC=path.resolve(process.argv[3]);
const S=req('@adcp/sdk/signing');
const keys=JSON.parse(fs.readFileSync(path.join(VEC,'keys.json'),'utf8')).keys
  .map(k=>Object.fromEntries(Object.entries(k).filter(([n])=>!n.startsWith('_'))));
const BY_KID=Object.fromEntries(keys.map(k=>[k.kid,k]));

function makeReplay(entries, capFilledFor){
  const seen=new Set();
  for(const e of entries||[]) seen.add(e.keyid+'|*|'+e.nonce);
  return {
    async has(keyid,scope,nonce){ return seen.has(keyid+'|*|'+nonce); },
    async isCapHit(keyid){ return capFilledFor===keyid; },
    async insert(keyid,scope,nonce){
      if(capFilledFor===keyid) return 'rate_abuse';
      const k=keyid+'|*|'+nonce;
      if(seen.has(k)) return 'replayed';
      seen.add(k); return 'ok';
    },
  };
}

async function run(file){
  const v=JSON.parse(fs.readFileSync(file,'utf8'));
  const r=v.request, state=v.test_harness_state||{};
  const table={...BY_KID, ...(v.jwks_override||{})};
  const revoked=new Set(state.revoked_kids||[]);
  const opts={
    jwks: { async resolve(kid){ return table[kid] ?? null; } },
    replayStore: makeReplay(state.replay_cache_entries, state.per_keyid_cap_filled_for),
    revocationStore: { async isRevoked(kid){ return revoked.has(kid); } },
    ...(v.reference_now!==undefined ? { now: ()=>v.reference_now } : {}),
  };
  let got;
  try{
    await S.verifyWebhookSignature({method:r.method,url:r.url,headers:r.headers,body:r.body??''},opts);
    got={success:true};
  }catch(e){
    got={success:false, error_code: e?.code ?? e?.reason ?? (e?.message||'').slice(0,60)};
  }
  const exp=v.expected_outcome;
  const match = got.success===exp.success && (exp.success || got.error_code===exp.error_code);
  return {vector: path.basename(path.dirname(file))+'/'+path.basename(file),
          requires_contract: v.requires_contract ?? null, expected: exp, got, match};
}

(async()=>{
  const files=[]
    .concat(fs.readdirSync(path.join(VEC,'positive')).sort().map(f=>path.join(VEC,'positive',f)))
    .concat(fs.readdirSync(path.join(VEC,'negative')).sort().map(f=>path.join(VEC,'negative',f)))
    .filter(f=>f.endsWith('.json'));
  const rows=[]; for(const f of files) rows.push(await run(f));
  process.stdout.write(JSON.stringify(rows,null,1)+'\n');
})().catch(e=>{process.stderr.write('RUNNER: '+e.stack+'\n');process.exitCode=2;});
