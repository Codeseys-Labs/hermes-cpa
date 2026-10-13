const {test}=require('node:test'),assert=require('node:assert/strict');
const {fixture,pause,until,revision,generation,plain}=require('./consent-fixture.cjs');
// Scaled budgets: host and guest phase budgets shrink 100x once the channel is ready.
const SCALE=0.01;

for(const mode of ['decline','selection','revision','generation','scope','external','unauthorized'])test(mode+' after consent causes zero PUTs',async()=>{
 let altered=false;
 const f=await fixture({read:(p,s)=>{
  if(!altered)return s;
  if(mode==='unauthorized')throw Error('generated-denial-canary');
  return {...s,revision:mode==='revision'?'b'.repeat(64):s.revision,generation:mode==='generation'?2:s.generation,scope_id:mode==='scope'?'scope-B':s.scope_id,data:{...s.data,mode:mode==='external'?'external':'managed'}};
 }});
 try {const request=f.request();await until(()=>f.state().dialog);altered=true;
  if(mode==='decline')f.decline();else {if(mode==='selection')f.change();f.confirm();}
  await pause(30);assert.equal(f.state().puts,0);
  assert.ok(!JSON.stringify(f.events).includes('generated-denial-canary'));
  f.close();await request;
 }finally{f.close();}
});

test('a stalled preview expires without a dialog or a write',async()=>{
 const f=await fixture({scale:SCALE,read:(p,s)=>p==='/status'?new Promise(()=>{}):s});
 try {
  const result=await f.request();
  assert.equal(result.ok,false);assert.equal(result.kind,'unavailable');
  assert.match(result.error,/Preview unavailable; no admission requested/);
  assert.equal(f.state().puts,0);assert.ok(!f.events.some(e=>e.type==='consent-open'));
 } finally {f.close();}
});

test('consent expiry closes the dialog, and a late Confirm cannot submit',async()=>{
 const f=await fixture({scale:SCALE});
 try {
  const request=f.request();await until(()=>f.state().dialog);
  await until(()=>f.state().closed);
  assert.equal(f.state().dialog,false);f.confirm();await pause(30);
  const result=await request;
  assert.equal(f.state().puts,0);assert.equal(result.ok,false);assert.equal(result.kind,'consent-expired');
 } finally {f.close();}
});

for(const [name,error] of [
 ['HTTP conflict carrying a reconcile code',Object.assign(Error('HTTP 409 generated-canary'),{status:409,code:'RECONCILE_REQUIRED'})],
 ['lost admission response',Error('lost admission response generated-canary')],
])test('uncertain admission ('+name+') warns and is never retried, automatically or by a second apply',async()=>{
 const f=await fixture({putError:error});
 try {
  const request=f.request();await until(()=>f.state().dialog);f.confirm();const result=await request;
  assert.equal(result.ok,false);assert.equal(result.kind,'outcome-unknown');
  assert.match(result.error,/Outcome unknown; reconcile without resubmitting/);
  assert.equal(f.state().puts,1);assert.equal(f.state().accepted,0);assert.ok(f.events.some(e=>e.type==='uncertain'));
  const again=await f.request('config.apply',{expected_revision:revision,changes:{request_retry:5}});
  assert.equal(again.ok,false);assert.equal(again.kind,'outcome-unknown');
  assert.equal(f.state().puts,1);assert.equal(f.events.filter(e=>e.type==='consent-open').length,1,'no second consent');
  // Reconciliation reads stay available on the same channel.
  assert.equal((await f.request('config.read',{})).ok,true);
  assert.ok(!JSON.stringify(f.events).includes('generated-canary'));
 } finally {f.close();}
});

test('a submitted write whose answer never arrives is unknown and never resubmitted',async()=>{
 const f=await fixture({putHangs:true,scale:SCALE});
 try {
  const request=f.request();await until(()=>f.state().dialog);f.confirm();const result=await request;
  assert.equal(result.ok,false);assert.equal(result.kind,'outcome-unknown');
  assert.equal(f.state().puts,1);assert.ok(f.events.some(e=>e.type==='uncertain'));
  assert.equal(f.state().closed,true);
 } finally {f.close();}
});

test('second concurrent apply closes consent and neither pending Confirm can write',async()=>{
 const f=await fixture();
 try {
  const first=f.request();await until(()=>f.state().dialog);
  const second=f.request();await until(()=>f.state().closed);
  f.confirm();await pause(20);assert.equal(f.state().puts,0);
  f.close();await Promise.all([first,second]);
 } finally {f.close();}
});

for(const phase of ['consent','preview','invented'])test('duplicate/backward/unknown '+phase+' host phase cannot renew guest lifetime',async()=>{
 const f=await fixture();
 try {
  const request=f.request();await until(()=>f.events.some(e=>e.message?.phase==='consent'));
  f.hostRaw({v:2,type:'PHASE',id:'1',generation,phase});
  const result=await request;
  assert.equal(result.ok,false);assert.equal(f.state().puts,0);
 } finally {f.close();}
});

test('guest forged phase cannot extend privileged consent lifetime',async()=>{
 const f=await fixture();
 try {
  const request=f.request();await until(()=>f.state().dialog);
  f.raw({v:2,type:'PHASE',id:'2',generation,phase:'consent',timeoutMs:99999999});
  await until(()=>f.state().closed);f.confirm();assert.equal(f.state().puts,0);
  f.close();await request;
 } finally {f.close();}
});

test('an apply that changes nothing is neither reviewed, written nor restarted',async()=>{
 const f=await fixture();
 try {
  const result=await f.request('config.apply',{expected_revision:revision,changes:{strategy:'round-robin',request_retry:3,request_log:false}});
  assert.equal(result.ok,true);assert.equal(result.data.accepted,false);assert.equal(result.data.unchanged,true);
  assert.equal(f.state().puts,0);assert.equal(f.state().accepted,0);assert.equal(f.state().closed,false);
  assert.ok(!f.events.some(e=>e.type==='consent-open'),'a no-op must never prompt');
 } finally {f.close();}
});

test('a mixed apply reviews and writes only the fields that differ',async()=>{
 const f=await fixture();
 try {
  const request=f.request('config.apply',{expected_revision:revision,changes:{strategy:'round-robin',request_retry:5,request_log:false}});
  await until(()=>f.state().dialog);
  assert.deepEqual(plain(f.events.find(e=>e.type==='consent-open').fields),['request_retry']);
  f.confirm();assert.equal((await request).ok,true);
  assert.deepEqual(plain(f.mutations[0].changes),{request_retry:5});
 } finally {f.close();}
});
