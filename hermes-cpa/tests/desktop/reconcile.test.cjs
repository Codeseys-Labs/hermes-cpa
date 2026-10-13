const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../desktop/plugin.js'),'utf8');
const context=vm.createContext({TextEncoder,setTimeout,clearTimeout});
vm.runInContext(source.replace(/^import .*;$/gm,'').replace('export default','const plugin =')+'\nglobalThis.reconcile=reconcileOperation;',context);
const reconcile=context.reconcile;
const id='accepted-operation';
const envelope=state=>({api_version:1,scope_id:'A',data:{operation_id:id,state}});
test('pending is not success; bounded reads reconcile without mutation retries',async()=>{
 const seen=[],states=['pending','running','succeeded'];let reads=0;
 await reconcile({id,scope:'A',live:()=>true,rest:async(p,o)=>{assert.equal(p,'/operations/'+id);assert.equal(o.method,undefined);reads++;return envelope(states.shift());},publish:s=>seen.push(s),wait:async()=>{}});
 assert.equal(reads,3);assert.deepEqual(seen,['pending','running','succeeded']);
});
test('switch during in-flight read suppresses old result and further requests',async()=>{
 let live=true;const seen=[];let reads=0;
 await reconcile({id,scope:'A',live:()=>live,rest:async()=>{reads++;live=false;return envelope('succeeded');},publish:s=>seen.push(s),wait:async()=>{}});
 assert.equal(reads,1);assert.deepEqual(seen,[]);
});
test('wrong scope/id and unknown states fail closed',async()=>{
 for(const response of [{...envelope('succeeded'),scope_id:'B'}, {...envelope('succeeded'),data:{operation_id:'other',state:'succeeded'}},envelope('invented')]) {
  const seen=[];await reconcile({id,scope:'A',live:()=>true,rest:async()=>response,publish:s=>seen.push(s),wait:async()=>{}});
  assert.deepEqual(seen,['unavailable']);
 }
});
test('finite reconciliation pauses and never resubmits accepted mutation',async()=>{
 let reads=0;const seen=[];
 await reconcile({id,scope:'A',live:()=>true,rest:async()=>{reads++;return envelope('pending');},publish:s=>seen.push(s),wait:async()=>{}});
 assert.equal(reads,20);assert.equal(seen.at(-1),'unavailable');
});
test('errors are sanitized and disposal prevents any initial request',async()=>{
 const seen=[];await reconcile({id,scope:'A',live:()=>true,rest:async()=>{throw Error('CANARY');},publish:s=>seen.push(s),wait:async()=>{}});
 assert.deepEqual(seen,['unavailable']);
 await reconcile({id,scope:'A',live:()=>false,rest:async()=>{throw Error('must not call');},publish:()=>assert.fail(),wait:async()=>{}});
});
