const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {MessageChannel}=require('node:worker_threads');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../desktop/plugin.js'),'utf8');
const code=source.replace(/^import .*;$/gm,'').replace('export default','const plugin =');
const context=vm.createContext({TextEncoder,crypto:require('node:crypto').webcrypto,setTimeout,clearTimeout,MessageChannel});
vm.runInContext(code+'\nglobalThis.boundary={createBridge,project};',context);
const {createBridge,project}=context.boundary;
const revision='a'.repeat(64);
function fixture(overrides={}) {
 let current='A1',puts=0,consents=0;
 const snapshot={api_version:1,scope_id:'scope-A',generation:1,revision,data:{mode:'managed',lifecycle:'ready',management_ready:true,strategy:'round-robin',request_retry:3,request_log:false,revision}};
 const channel=new MessageChannel();const messages=[];channel.port2.on('message',m=>messages.push(m));
 const bridge=createBridge({port:channel.port1,nonce:'nonce',generation:'session-A1',snapshot,selection:()=>current,captured:'A1',confirm:async()=>{consents++;return true;},rest:async(p,o)=>{if(o?.method==='PUT')puts++;return {...snapshot,data:p==='/metrics'?{available:false,total_success:0,secret:'CANARY'}:snapshot.data};},...overrides});
 const send=(op,args={},id=String(messages.length+1))=>channel.port2.postMessage({v:2,id,generation:'session-A1',op,args});
 channel.port2.postMessage({v:2,type:'HELLO',nonce:'nonce',generation:'session-A1'});
 channel.port2.postMessage({v:2,type:'ACK',nonce:'nonce',generation:'session-A1'});
 return {bridge,messages,send,raw:m=>channel.port2.postMessage(m),change:()=>{current='A3';},counts:()=>({puts,consents}),close:()=>{bridge.close();channel.port2.close();}};
}
for(const op of ['providers.read','auth.read','models.read','quota.read','logs.read','plugins.read']) {
 test(op+' projects canary responses through real MessageChannel',async()=>{
  const f=fixture({rest:async()=>({api_version:1,scope_id:'scope-A',generation:1,revision,
    data:{available:true,entries:1,secret:'CANARY',items:[{provider:'CANARY',status:'CANARY',name:'CANARY',disabled:false}]}})});
  try {await pause();f.send(op);await pause();assert.equal(f.messages[1].ok,true);assert.ok(!JSON.stringify(f.messages).includes('CANARY'));}
  finally {f.close();}
 });
 test(op+' rejects arbitrary parameters',async()=>{
  let calls=0;const f=fixture({rest:async()=>{calls++;throw Error('CANARY');}});
  try {await pause();f.send(op,{url:'CANARY'});await pause();assert.equal(calls,0);assert.equal(f.messages[1].ok,false);}
  finally {f.close();}
 });
}
for(const op of ['providers.apply','auth.upload','auth.delete','auth.disable','oauth.start','oauth.refresh','oauth.callback','models.apply','quota.refresh','logs.clear']) {
 test(op+' disabled before REST or native consent',async()=>{
  let calls=0,consents=0;const f=fixture({rest:async()=>{calls++;throw Error('CANARY');},confirm:async()=>{consents++;return true;}});
  try{await pause();f.send(op);await pause();assert.equal(f.messages[1].ok,false);assert.equal(calls,0);assert.equal(consents,0);}finally{f.close();}
 });
}
test('finite model and numeric projections reject secret-like suffixes and booleans',()=>{
 const d=project('models.read',{available:true,items:[{model:'gpt-5.5-CANARY',context_length:true,max_completion_tokens:Infinity}]});
 assert.equal(d.items[0].model,'withheld');assert.equal(d.items[0].context_length,null);assert.equal(d.items[0].max_completion_tokens,null);
});
const pause=()=>new Promise(r=>setTimeout(r,30));
test('usage mapping and redaction preserve unavailable versus zero',async()=>{const f=fixture();try{await pause();f.send('usage.read');await pause();assert.equal(f.messages[1].data.available,false);assert.equal(f.messages[1].data.total_success,null);assert.ok(!JSON.stringify(f.messages).includes('CANARY'));assert.equal(project('usage.read',{available:true,total_success:0,total_failed:0}).total_success,0);}finally{f.close();}});
test('typed apply requires consent and revision; no replay',async()=>{const f=fixture();try{await pause();f.send('config.apply',{expected_revision:revision,changes:{request_retry:4}},'1');await pause();assert.equal(f.counts().puts,1);f.send('config.apply',{expected_revision:revision,changes:{request_retry:4}},'1');await pause();assert.equal(f.counts().puts,1);}finally{f.close();}});
test('invalid fields and bool retry never prompt',async()=>{const f=fixture();try{await pause();f.send('config.apply',{expected_revision:revision,changes:{request_retry:true}});await pause();assert.equal(f.counts().consents,0);assert.equal(f.counts().puts,0);}finally{f.close();}});
test('scope ABA during consent revokes mutation',async()=>{let resolve;const f=fixture({confirm:()=>new Promise(r=>{resolve=r;})});try{await pause();f.send('config.apply',{expected_revision:revision,changes:{request_retry:4}});await pause();f.change();resolve(true);await pause();assert.equal(f.counts().puts,0);}finally{f.close();}});
test('declined confirmation never writes',async()=>{const f=fixture({confirm:async()=>false});try{await pause();f.send('config.apply',{expected_revision:revision,changes:{request_log:true}});await pause();assert.equal(f.counts().puts,0);}finally{f.close();}});
test('unknown operation fails closed',async()=>{const f=fixture();try{await pause();f.send('fetch',{url:'https://example.invalid'});await pause();assert.equal(f.messages[1].ok,false);assert.equal(f.counts().puts,0);}finally{f.close();}});
for(const attack of [
 {v:1,id:'1',generation:'session-A1',op:'status.read',args:{}},
 {v:2,id:'1',generation:'wrong',op:'status.read',args:{}},
 {v:2,id:'1',generation:'session-A1',op:'status.read',args:{x:'界'.repeat(400000)}},
])test('bad version/generation/UTF8 byte budget revokes channel',async()=>{let closed=false;const f=fixture({onClose:()=>{closed=true;}});try{await pause();f.raw(attack);await pause();assert.equal(closed,true);}finally{f.close();}});
test('33rd concurrent read closes channel',async()=>{let closed=false;const f=fixture({rest:()=>new Promise(()=>{}),onClose:()=>{closed=true;}});try{await pause();for(let i=1;i<=33;i++)f.send('status.read',{},String(i));await pause();assert.equal(closed,true);}finally{f.close();}});
test('wrong HELLO nonce cannot establish a port',async()=>{let closed=false;const c=new MessageChannel();const b=createBridge({port:c.port1,nonce:'correct',generation:'g',snapshot:{},selection:()=>1,captured:1,onClose:()=>{closed=true;}});try{c.port2.postMessage({v:2,type:'HELLO',nonce:'wrong',generation:'g'});await pause();assert.equal(closed,true);}finally{b.close();c.port2.close();}});
test('previous protocol version fails closed at the handshake instead of running with mismatched phase budgets',async()=>{let closed=false,calls=0;const c=new MessageChannel();const b=createBridge({port:c.port1,nonce:'n',generation:'g',snapshot:{},selection:()=>1,captured:1,onClose:()=>{closed=true;},rest:async()=>{calls++;}});try{c.port2.postMessage({v:1,type:'HELLO',nonce:'n',generation:'g'});await pause();assert.equal(closed,true);assert.equal(calls,0);}finally{b.close();c.port2.close();}});
test('external mode never prompts or writes',async()=>{const f=fixture({rest:async()=>({api_version:1,scope_id:'scope-A',generation:1,revision,data:{mode:'external'}})});try{await pause();f.send('config.apply',{expected_revision:revision,changes:{request_retry:4}});await pause();assert.equal(f.counts().consents,0);assert.equal(f.counts().puts,0);}finally{f.close();}});
test('owner generation replacement closes channel',async()=>{let closed=false;const f=fixture({onClose:()=>{closed=true;},rest:async()=>({api_version:1,scope_id:'scope-A',generation:2,revision,data:{}})});try{await pause();f.send('status.read');await pause();assert.equal(closed,true);}finally{f.close();}});
test('hung REST times out and suppresses late response',async()=>{let closed=false,resolve;const f=fixture({onClose:()=>{closed=true;},rest:()=>new Promise(r=>{resolve=r;})});try{await pause();f.send('status.read');await new Promise(r=>setTimeout(r,10100));assert.equal(closed,true);resolve({});await pause();assert.equal(f.messages.length,1);}finally{f.close();}});
