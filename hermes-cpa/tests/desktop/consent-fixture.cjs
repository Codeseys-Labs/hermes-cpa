// Runs the real host bridge (desktop/plugin.js) and the real guest transport
// (upstream-console/src/hermesTransport.ts, transpiled) in isolated VM documents joined by a
// real MessageChannel. Only REST responses and the native dialog are generated; this is not a
// browser or native-dialog claim.
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {MessageChannel}=require('node:worker_threads');
const {performance}=require('node:perf_hooks');
const root=path.resolve(__dirname,'../..');
const consoleRoot=path.resolve(root,'../upstream-console');
const ts=require(require.resolve('typescript',{paths:[consoleRoot]}));
const revision='a'.repeat(64);
const generation='fixture-generation-long-enough';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
// Objects built inside the host VM carry that realm's prototypes; compare them as plain data.
const plain=value=>JSON.parse(JSON.stringify(value));
async function until(check){for(let n=0;n<800;n++){if(check())return;await pause(5);}throw Error('Fixture never reached expected boundary');}
// options.scale shrinks every host and guest budget after the channel is ready, so budget expiry
// is exercised in milliseconds; real-time behavior has its own unscaled test.
async function fixture(options={}) {
 const started=performance.now(),events=[],channels=new MessageChannel();
 let selection='A1',dialog=false,resolveConsent,puts=0,closed=false,ready=false;
 const accepted=[],mutations=[],timers=new Set(),scale=options.scale||1;
 const set=(fn,ms)=>{const id=setTimeout(()=>{timers.delete(id);fn();},ms*(ready?scale:1));timers.add(id);return id;};
 const clear=id=>{clearTimeout(id);timers.delete(id);};
 const event=(type,value={})=>events.push({ms:Math.round(performance.now()-started),type,...value});
 const config={strategy:'round-robin',request_retry:3,request_log:false,...options.config};
 const snapshot={api_version:1,scope_id:'scope-A',generation:1,revision,data:{mode:'managed',lifecycle:'ready',management_ready:true,...config}};
 const host=vm.createContext({TextEncoder,setTimeout:set,clearTimeout:clear,MessageChannel});
 const hostSource=fs.readFileSync(path.join(root,'desktop/plugin.js'),'utf8');
 vm.runInContext(hostSource.replace(/^import .*;$/gm,'').replace('export default','const plugin =')+'\nglobalThis.createBridge=createBridge;',host);
 const bridge=host.createBridge({port:channels.port1,nonce:'fixture-nonce-long-enough',generation,snapshot,selection:()=>selection,captured:'A1',
  onReady:()=>{ready=true;event('host-ready');},
  onAccepted:v=>{accepted.push(v);event('accepted');},
  onUncertain:()=>event('uncertain'),
  onClose:()=>{closed=true;dialog=false;resolveConsent?.(false);event('host-closed');},
  confirm:diff=>{dialog=true;event('consent-open',{fields:diff.map(d=>d.key)});return new Promise(r=>{resolveConsent=r;});},
  rest:async(p,o)=>{
   event('rest',{path:p,method:o?.method||'GET'});
   if(o?.method==='PUT'){
    puts++;mutations.push(o.body);
    if(options.putHangs)return new Promise(()=>{});
    if(options.putError)throw options.putError;
    return {...snapshot,data:{operation_id:'fixture-operation'}};
   }
   return options.read?options.read(p,snapshot):snapshot;
  }});
 const listeners={},parent={},window={addEventListener:(name,fn)=>{listeners[name]=fn;}};
 const guest=vm.createContext({window,parent,TextEncoder,setTimeout:set,clearTimeout:clear,exports:{}});
 const guestSource=fs.readFileSync(path.join(consoleRoot,'src/hermesTransport.ts'),'utf8');
 vm.runInContext(ts.transpileModule(guestSource,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,guest);
 channels.port2.on('close',()=>event('guest-port-closed'));
 channels.port2.on('message',m=>event('host-message',{message:m}));
 listeners.message({source:parent,ports:[channels.port2],data:{type:'cpa-bridge-init',v:2,nonce:'fixture-nonce-long-enough',generation}});
 await until(()=>ready);
 return {
  events,mutations,until,pause,
  request:(op='config.apply',args={expected_revision:revision,changes:{request_retry:4}})=>guest.exports.request(op,args).then(
   data=>{event('guest-resolved');return {ok:true,data};},
   error=>{event('guest-rejected',{error:error.message});return {ok:false,error:error.message,kind:error.kind};}),
  state:()=>({dialog,closed,puts,accepted:accepted.length}),
  confirm:()=>{event('confirm-attempt',{dialog});dialog=false;resolveConsent?.(true);},
  decline:()=>{dialog=false;resolveConsent?.(false);},
  change:()=>{selection='A3';},
  close:()=>{listeners.pagehide?.();bridge.close();channels.port2.close();for(const t of timers)clearTimeout(t);},
  hostRaw:m=>channels.port1.postMessage(m),
  raw:m=>channels.port2.postMessage(m),
 };
}
module.exports={fixture,revision,generation,pause,until,plain};
