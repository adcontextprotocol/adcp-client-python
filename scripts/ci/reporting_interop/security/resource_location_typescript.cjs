const fs=require('fs'),path=require('path'),Module=require('node:module');
const base=path.resolve(process.argv[2]);
const req=Module.createRequire(path.join(base,'noop.js'));
const led=req('@adcp/sdk/reporting/ledger');
const fn=led.assertCredentialFreeReportingResourceLocationV1;
const corpus=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const out=corpus.map(c=>{
  let r;
  try{ fn(c.v); r='accept'; }
  catch(e){ r='reject'; }
  return {id:c.id, expect:c.expect, ts:r};
});
console.log(JSON.stringify({available:typeof fn, rows:out}));
