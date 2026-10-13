const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {MessageChannel}=require('node:worker_threads');
const source=fs.readFileSync(path.join(__dirname,'../../desktop/plugin.js'),'utf8');
const timers=[];
const context=vm.createContext({TextEncoder,clearTimeout,setTimeout:(fn)=>{timers.push(fn);return timers.length;}});
vm.runInContext(source.replace(/^import .*;$/gm,'').replace('export default','const plugin =')+'\nglobalThis.createBridge=createBridge;',context);
const pause=()=>new Promise(r=>setTimeout(r,20));
function fixture(){
 const c=new MessageChannel();let current='A',ready=0,closed=0,calls=0;
 const b=context.createBridge({port:c.port1,nonce:'n',generation:'g',snapshot:{},selection:()=>current,captured:'A',onReady:()=>ready++,onClose:()=>closed++,rest:async()=>{calls++;}});
 return {send:m=>c.port2.postMessage(m),hello:()=>c.port2.postMessage({v:2,type:'HELLO',nonce:'n',generation:'g'}),ack:()=>c.port2.postMessage({v:2,type:'ACK',nonce:'n',generation:'g'}),counts:()=>({ready,closed,calls}),change:()=>{current='B';},close:()=>{b.close();c.port2.close();}};
}
test('HELLO does not mean rendered guest received READY; validated ACK marks ready once',async()=>{
 const f=fixture();try{f.hello();await pause();assert.equal(f.counts().ready,0);f.ack();await pause();assert.equal(f.counts().ready,1);f.ack();await pause();assert.equal(f.counts().ready,1);}finally{f.close();}
});
test('stale ACK after selection replacement cannot clear revoked state',async()=>{
 const f=fixture();try{f.hello();await pause();f.change();f.ack();await pause();assert.deepEqual(f.counts(),{ready:0,closed:1,calls:0});}finally{f.close();}
});
test('missing ACK times out and late ACK cannot clear error',async()=>{
 const f=fixture();try{f.hello();await pause();timers.at(-1)();f.ack();await pause();assert.deepEqual(f.counts(),{ready:0,closed:1,calls:0});}finally{f.close();}
});
test('wrong nonce ACK and pre-ACK operations never reach REST',async()=>{
 for(const message of [{v:2,type:'ACK',nonce:'other',generation:'g'},{v:2,id:'1',generation:'g',op:'status.read',args:{}}]){
  const f=fixture();try{f.hello();await pause();f.send(message);await pause();assert.deepEqual(f.counts(),{ready:0,closed:1,calls:0});}finally{f.close();}
 }
});
