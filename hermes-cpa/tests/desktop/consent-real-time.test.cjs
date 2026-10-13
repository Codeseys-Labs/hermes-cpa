const {test}=require('node:test'),assert=require('node:assert/strict');
const {fixture,pause,until,plain}=require('./consent-fixture.cjs');
// Unscaled on purpose: a human reading the native review must outlast the ordinary ten-second
// transport budget, then Confirm must submit exactly once. Deliberately slow (~10.5 s).
test('human can read consent for more than ten REAL seconds, then Confirm exactly once',async()=>{
 const f=await fixture();
 try {
  const pending=f.request();await until(()=>f.state().dialog);
  const start=performance.now();await pause(10500);const elapsedMs=performance.now()-start;
  const afterWait=f.state();
  f.confirm();const result=await pending;
  assert.ok(elapsedMs>10000);
  assert.equal(afterWait.puts,0,'nothing may be submitted while the human is still reading');
  assert.equal(afterWait.dialog,true,'consent dialog was closed during human reading');
  assert.equal(afterWait.closed,false,'channel was closed during human reading');
  assert.equal(result.ok,true);assert.equal(f.state().puts,1);assert.equal(f.state().accepted,1);
  assert.deepEqual(plain(f.mutations[0]),{expected_revision:'a'.repeat(64),expected_generation:1,changes:{request_retry:4},restart_consent:true});
 } finally {f.close();}
});
