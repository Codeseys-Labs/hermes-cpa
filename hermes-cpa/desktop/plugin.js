// Trusted host boundary. Only SDK REST can authenticate; guest never receives keys.
import { host, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, SandboxedFrame, ConfirmDialog } from '@hermes/plugin-sdk';
import { useEffect, useState, useRef, useMemo } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';

const LIMIT = 1048576;
const paths = { 'status.read': '/status', 'usage.read': '/metrics', 'config.read': '/config',
  'providers.read': '/features/providers', 'auth.read': '/features/auth',
  'models.read': '/features/models', 'quota.read': '/features/quota',
  'logs.read': '/features/logs', 'plugins.read': '/features/plugins' };
const record = x => x !== null && typeof x === 'object' && !Array.isArray(x);
const bytes = x => new TextEncoder().encode(JSON.stringify(x)).length;
const revisionOK = x => typeof x === 'string' && /^[a-f0-9]{64}$/.test(x);
function changesOK(x) {
  return record(x) && Object.keys(x).length > 0 && Object.entries(x).every(([k,v]) =>
    k === 'strategy' ? ['round-robin','fill-first'].includes(v) :
    k === 'request_retry' ? Number.isInteger(v) && v >= 0 && v <= 10 :
    k === 'request_log' && typeof v === 'boolean');
}
const USAGE_PROVIDERS = ['gemini','gemini-interactions','vertex','codex','claude','xai','meta','other'];
function projectUsage(d) {
  const invalid = () => { throw new Error('Unavailable'); };
  const count = v => Number.isSafeInteger(v) && v >= 0;
  const pair = row => record(row) && count(row.success) && count(row.failed) && count(row.success + row.failed);
  if (typeof d.available !== 'boolean' || d.scope !== 'api_key_credentials' ||
      !Array.isArray(d.providers) || d.providers.length > USAGE_PROVIDERS.length ||
      !record(d.window) || typeof d.window.available !== 'boolean' ||
      d.window.bucket_seconds !== 600 || !Array.isArray(d.window.buckets)) invalid();
  if (!d.available) {
    if (d.credential_entries !== null || d.total_success !== null || d.total_failed !== null ||
        d.providers.length !== 0 || d.window.available || d.window.buckets.length !== 0) invalid();
    return {available:false,scope:'api_key_credentials',credential_entries:null,
      total_success:null,total_failed:null,providers:[],
      window:{available:false,bucket_seconds:600,buckets:[]}};
  }
  if (!count(d.credential_entries) || d.credential_entries > 1000 ||
      !pair({success:d.total_success,failed:d.total_failed})) invalid();
  const windowAvailable = d.window.available;
  const bucketCount = windowAvailable ? 20 : 0;
  if (d.window.buckets.length !== bucketCount || (windowAvailable && d.credential_entries === 0)) invalid();
  const seen = new Set(), providers = [];
  let entries = 0, success = 0, failed = 0;
  for (const row of d.providers) {
    if (!record(row)) invalid();
    const provider = USAGE_PROVIDERS.find(value => value === row.provider);
    if (!provider || seen.has(provider) || !count(row.credential_entries) ||
        row.credential_entries < 1 || row.credential_entries > 1000 || !pair(row) ||
        !Array.isArray(row.buckets) || row.buckets.length !== bucketCount) invalid();
    seen.add(provider);
    entries += row.credential_entries; success += row.success; failed += row.failed;
    if (entries > 1000 || !pair({success,failed})) invalid();
    const buckets = [];
    for (const bucket of row.buckets) {
      if (!pair(bucket)) invalid();
      buckets.push({success:bucket.success,failed:bucket.failed});
    }
    providers.push({provider,credential_entries:row.credential_entries,success:row.success,failed:row.failed,buckets});
  }
  if (entries !== d.credential_entries || success !== d.total_success || failed !== d.total_failed) invalid();
  const buckets = [];
  let windowSuccess = 0, windowFailed = 0;
  for (let index = 0; index < bucketCount; index++) {
    const bucket = d.window.buckets[index];
    if (!pair(bucket)) invalid();
    let success = 0, failed = 0;
    for (const provider of providers) {
      success += provider.buckets[index].success; failed += provider.buckets[index].failed;
      if (!pair({success,failed})) invalid();
    }
    if (success !== bucket.success || failed !== bucket.failed) invalid();
    windowSuccess += success; windowFailed += failed;
    if (!pair({success:windowSuccess,failed:windowFailed})) invalid();
    buckets.push({success,failed});
  }
  providers.sort((a,b) => USAGE_PROVIDERS.indexOf(a.provider) - USAGE_PROVIDERS.indexOf(b.provider));
  return {available:true,scope:'api_key_credentials',credential_entries:entries,
    total_success:success,total_failed:failed,providers,
    window:{available:windowAvailable,bucket_seconds:600,buckets}};
}
// Explicit allowlists, including values: arbitrary backend strings are never rendered.
function project(op, d) {
  if (!record(d)) throw new Error('Unavailable');
  if (['providers.read','auth.read','quota.read','models.read','logs.read','plugins.read'].includes(op)) {
    if(['auth.read','quota.read','plugins.read'].includes(op))return {available:false,reason:'PLUGIN_CALLBACK_UNSAFE'};
    if(d.available!==true)return {available:false};
    const integer=(v,min=0,max=10000000)=>Number.isInteger(v)&&v>=min&&v<=max?v:null;
    const models=['gpt-5.5','gpt-6-astra','gpt-6-sol','gpt-6.1-sol','gpt-6-luna','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','codex-auto-review'];
    if(!Array.isArray(d.items) || d.items.length>1000)throw new Error('Unavailable');
    const items=d.items.map((row,index)=>{
      if(!record(row))throw new Error('Unavailable');
      if(op==='logs.read')return {ordinal:index+1,severity:['trace','debug','info','warning','error','fatal','panic'].includes(row.severity)?row.severity:'unknown'};
      if(op==='models.read')return {ordinal:index+1,model:models.includes(row.model)?row.model:'withheld',context_length:integer(row.context_length),max_completion_tokens:integer(row.max_completion_tokens)};
      return {ordinal:index+1,provider:['codex','claude','gemini','interactions','vertex','xai','meta','openai-compatibility'].includes(row.provider)?row.provider:'other',priority:integer(row.priority,-10000000),credentials:integer(row.credentials,0,1000),disabled_credentials:integer(row.disabled_credentials,0,1000)};
    });
    return {available:true,items,withheld:integer(d.withheld,0,1000)};
  }
  if (op === 'config.read') {
    const fields = {strategy:d.strategy,request_retry:d.request_retry,request_log:d.request_log};
    if (!changesOK(fields) || !revisionOK(d.revision)) throw new Error('Unavailable');
    return {...fields,revision:d.revision,restart_required:true};
  }
  if (op === 'status.read') return {
    mode:['managed','external'].includes(d.mode) ? d.mode : 'unavailable',
    lifecycle:['unconfigured','stopped','starting','ready','degraded','stopping','failed','external_unreachable','external_ready','ownership_lost'].includes(d.lifecycle) ? d.lifecycle : 'unavailable',
    management_ready:d.management_ready === true,
    inference_configured:typeof d.inference_configured === 'boolean' ? d.inference_configured : null,
  };
  if (op === 'usage.read') return projectUsage(d);
  return {accepted:true}; // Pending is NOT persisted/completed.
}
async function reconcileOperation({id,scope,live,rest,publish,wait=ms=>new Promise(resolve=>setTimeout(resolve,ms))}) {
  // Read-only reconciliation of the immutable accepted ID. Never retry a mutation.
  for(let attempt=0;attempt<20 && live();attempt++) {
    try {
      const result=await rest('/operations/'+encodeURIComponent(id),{timeoutMs:9000});
      if(!live())return;
      const state=result?.data?.state;
      if(result?.api_version!==1 || result.scope_id!==scope || result.data.operation_id!==id || !['pending','running','succeeded','failed'].includes(state))throw new Error('Unavailable');
      publish(state);
      if(state==='succeeded' || state==='failed')return;
    } catch {if(live())publish('unavailable');return;}
    if(attempt<19)await wait(3000);
  }
  if(live())publish('unavailable');
}
// Phase budgets are fixed by the trusted host; no guest message can renew them. Only human review
// may outlast an ordinary request, and only while the native consent dialog is open. The guest
// (hermesTransport.ts) mirrors these budgets plus one second, so the host always closes first.
const READ_MS=10000, PREVIEW_MS=20000, CONSENT_MS=120000, COMMIT_MS=30000;
function createBridge({port,nonce,generation,snapshot,selection,captured,confirm,rest,onAccepted=()=>{},onUncertain=()=>{},onReady=()=>{},onClose=()=>{}}) {
  let acknowledged=false;
  let closed=false,ready=false,last=0,inflight=0,mutation=false,uncertain=false,tokens=64,refill=Date.now();
  const timers=new Set();
  const close=()=>{if(closed)return;closed=true;port.close();for(const t of timers)clearTimeout(t);timers.clear();onClose();};
  const live=()=>!closed && selection()===captured;
  const check=r=>{if(!live() || r?.api_version!==1 || r.scope_id!==snapshot.scope_id || r.generation!==snapshot.generation || !revisionOK(r.revision)){close();throw new Error('Unavailable');}return r;};
  const markUncertain=()=>{if(uncertain)return;uncertain=true;onUncertain();};
  const boot=setTimeout(close,10000);timers.add(boot);
  port.onmessage=async({data:m})=>{
    if(!live()){close();return;}
    try {
      if(bytes(m)>LIMIT || !record(m)){close();return;}
      if(!ready){
        if(m.v!==2 || m.type!=='HELLO' || m.nonce!==nonce || m.generation!==generation){close();return;}
        ready=true;
        port.postMessage({v:2,type:'READY',nonce,generation});return;
      }
      if(!acknowledged){
        if(m.v!==2 || m.type!=='ACK' || m.nonce!==nonce || m.generation!==generation){close();return;}
        acknowledged=true;clearTimeout(boot);timers.delete(boot);onReady();return;
      }
      tokens=Math.min(64,tokens+(Date.now()-refill)*0.032);refill=Date.now();
      // Requests never carry a type; PHASE is a host-to-guest message a guest cannot forge.
      if(--tokens<0 || m.v!==2 || m.type!==undefined || m.generation!==generation || typeof m.id!=='string' || !/^[1-9][0-9]{0,9}$/.test(m.id) || Number(m.id)<=last){close();return;}
      last=Number(m.id);
      if(inflight>=32 || (m.op==='config.apply' && mutation)){close();return;}
      let timer,submitted=false;
      const arm=ms=>{clearTimeout(timer);timers.delete(timer);timer=setTimeout(()=>{if(submitted)markUncertain();close();},ms);timers.add(timer);};
      const phase=name=>port.postMessage({v:2,type:'PHASE',id:m.id,generation,phase:name});
      arm(m.op==='config.apply'?PREVIEW_MS:READ_MS);inflight++;
      try {
        let data;
        if(Object.hasOwn(paths,m.op)) {
          if(!record(m.args) || Object.keys(m.args).length)throw new Error('Invalid');
          const r=check(await rest(paths[m.op],{timeoutMs:9000}));
          data=project(m.op,m.op==='config.read'?{...r.data,revision:r.revision}:r.data);
        } else if(m.op==='config.apply') {
          // After a submission with an unknown result, this channel never submits again.
          if(uncertain){port.postMessage({v:2,id:m.id,generation,ok:false,error:{code:'RECONCILE_REQUIRED'}});return;}
          mutation=true;
          if(!record(m.args) || Object.keys(m.args).some(k=>!['changes','expected_revision'].includes(k)) || !changesOK(m.args.changes) || !revisionOK(m.args.expected_revision))throw new Error('Invalid');
          const current=check(await rest('/config',{timeoutMs:9000}));
          const status=project('status.read',check(await rest('/status',{timeoutMs:9000})).data);
          if(status.mode!=='managed' || current.revision!==m.args.expected_revision)throw new Error('Invalid');
          const before=project('config.read',{...current.data,revision:current.revision});
          // Only fields that differ are reviewed and written; an apply that changes nothing is
          // never prompted, submitted or restarted.
          const changes=Object.fromEntries(Object.entries(m.args.changes).filter(([key,value])=>before[key]!==value));
          if(Object.keys(changes).length===0)data={accepted:false,unchanged:true};
          else {
            const diff=Object.entries(changes).map(([key,value])=>({key,before:before[key],after:value}));
            if(!live())throw new Error('Unavailable');
            arm(CONSENT_MS);phase('consent');
            if(!await confirm(diff) || !live())throw new Error('Declined');
            arm(COMMIT_MS);phase('commit');
            // Revalidate scope, revision and ownership after the human decision, before any write.
            const latest=check(await rest('/config',{timeoutMs:9000}));
            const latestStatus=project('status.read',check(await rest('/status',{timeoutMs:9000})).data);
            if(latest.revision!==current.revision || latestStatus.mode!=='managed' || !live())throw new Error('Stale');
            submitted=true;
            const result=await rest('/config',{method:'PUT',timeoutMs:9000,body:{expected_revision:current.revision,expected_generation:snapshot.generation,changes,restart_consent:true}});
            if(!live() || result?.api_version!==1 || result.scope_id!==snapshot.scope_id)throw new Error('Unavailable');
            const id=result?.data?.operation_id;
            if(typeof id!=='string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(id))throw new Error('Unavailable');
            onAccepted({id,scope:snapshot.scope_id});
            data={accepted:true};
          }
        } else throw new Error('Invalid');
        if(live())port.postMessage({v:2,id:m.id,generation,ok:true,data});
      } catch {
        // Once the write was dispatched, any failure leaves its outcome unknown.
        if(submitted)markUncertain();
        if(live())port.postMessage({v:2,id:m.id,generation,ok:false,error:{code:uncertain && m.op==='config.apply'?'RECONCILE_REQUIRED':'UNAVAILABLE'}});
      } finally {clearTimeout(timer);timers.delete(timer);inflight--;if(m.op==='config.apply')mutation=false;}
    } catch {close();}
  };
  port.onmessageerror=close;port.start();return {close};
}

const ID='hermes-cpa', ROUTE='/cpa', WORKSPACE_DOCK={pane:'workspace',pos:'right'};
// One docked CPAMC tab per plugin instance, beside the chat. Re-opening re-fronts the host's
// existing tab for the same id (the host refreshes render in place), so palette, sidebar and route
// all funnel here without stacking duplicates, replacing the chat, or touching other tabs. Only
// this plugin's own tab is ever closed.
function createDock({host,id,title,render}) {
  let close=null;
  return {
    supported:()=>typeof host.openWorkspace==='function',
    open(){
      if(typeof host.openWorkspace!=='function')return false;
      try{
        const closer=host.openWorkspace(id,{title,dock:WORKSPACE_DOCK,minWidth:'30rem',render,onClose:()=>{close=null;}});
        close=typeof closer==='function'?closer:null;
      }catch{return false;}
      return true;
    },
    dispose(){const closer=close;close=null;closer?.();},
  };
}
// Sidebar rows can only navigate. The route therefore opens the tab and steps the router back to
// where the user was, so the main pane never settles on it. A direct load has no entry to pop.
function enterRoute({dock,onUnavailable,win=window,navigate=path=>host.navigate(path),delay=80}) {
  if(!dock.open()){onUnavailable();return()=>{};}
  win.history.back();
  const timer=setTimeout(()=>{if(win.location.hash.slice(1).startsWith(ROUTE))navigate('/');},delay);
  return()=>clearTimeout(timer);
}
// Older hosts without workspaces render the console in place on the route, as before.
function RoutePage({ctx,dock}) {
  const [docked,setDocked]=useState(dock.supported());
  useEffect(()=>docked?enterRoute({dock,onUnavailable:()=>setDocked(false)}):undefined,[docked]);
  if(!docked)return jsx(CpaConsole,{ctx});
  return jsx('div',{role:'status',style:{flex:1,display:'flex',alignItems:'center',justifyContent:'center'},children:'Opening CPA Console beside the chat…'});
}

function CpaConsole({ctx}) {
  const rest=(path,opts={})=>ctx.rest(path,{...opts,selectionBound:true});
  const [asset,setAsset]=useState(null),[frameState,setFrameState]=useState('loading'),[message,setMessage]=useState(''),[consent,setConsent]=useState(null);
  const frameSrc=useMemo(()=>asset===null?undefined:'data:text/html;charset=utf-8;base64,'+btoa(unescape(encodeURIComponent(asset))),[asset]);
  const frameEpoch=useRef(0);
  const [operation,setOperation]=useState(null),[retry,setRetry]=useState(0),[reconciling,setReconciling]=useState(false),[unknown,setUnknown]=useState(false),[session,setSession]=useState(0);
  const operationEpoch=useRef(0);
  const bridge=useRef(null),frame=useRef(null),epoch=useRef(0),pending=useRef(null);
  useEffect(()=>{
    if(!operation)return;
    let active=true;
    const captured=operationEpoch.current;
    setReconciling(true);
    void reconcileOperation({...operation,rest,live:()=>active && operationEpoch.current===captured,publish:state=>setMessage(state==='unavailable'?'Operation result unavailable. Reconcile without resubmitting.':`CPA operation ${state}.`)}).finally(()=>{if(active)setReconciling(false);});
    return()=>{active=false;};
  },[operation,retry]);
  const selection=()=>`${epoch.current}:${host.state.connectionId.get()}:${host.state.profile.get()}`;
  const revoke=()=>{bridge.current?.close();bridge.current=null;pending.current?.(false);pending.current=null;setConsent(null);};
  // A docked tab outlives profile and connection changes and is re-fronted rather than remounted,
  // so a revoked or closed session is replaced only by an explicit Reconnect (a new session).
  const reconnect=()=>{setAsset(null);setFrameState('loading');setUnknown(false);setSession(value=>value+1);};
  useEffect(()=>{
    let active=true;
    const captured=selection();
    const invalidate=()=>{frameEpoch.current++;setFrameState('revoked');operationEpoch.current++;setOperation(null);epoch.current++;revoke();setAsset(null);};
    let previous=`${host.state.connectionId.get()}:${host.state.profile.get()}`;
    const changed=()=>{const next=`${host.state.connectionId.get()}:${host.state.profile.get()}`;if(next!==previous){previous=next;invalidate();}};
    const disposers=[host.state.connectionId.subscribe(changed),host.state.profile.subscribe(changed)];
    (async()=>{try{
      const r=await rest('/console/asset',{timeoutMs:10000});
      const html=r?.data?.html,manifest=r?.data?.manifest;
      if(!active || selection()!==captured)return;
      if(r.api_version!==1 || typeof html!=='string' || bytes(html)>8*1024*1024 || !revisionOK(manifest?.sha256))throw new Error();
      const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(html))),x=>x.toString(16).padStart(2,'0')).join('');
      if(digest!==manifest.sha256)throw new Error();
      if(active && selection()===captured)setAsset(html);
    }catch{if(active && selection()===captured)setFrameState('error');}})();
    return()=>{active=false;frameEpoch.current++;epoch.current++;revoke();disposers.forEach(d=>d());};
  },[ctx,session]);
  const load=async()=>{
    const attempt=++frameEpoch.current;
    revoke();setFrameState('connecting');const captured=selection(),target=frame.current?.contentWindow;
    const current=()=>frameEpoch.current===attempt && selection()===captured;
    try {
      const snapshot=await rest('/status',{timeoutMs:9000});
      if(!current())return;
      if(!target || snapshot?.api_version!==1 || !revisionOK(snapshot.revision))throw new Error();
      const channel=new MessageChannel(),nonce=crypto.randomUUID(),generation=crypto.randomUUID();
      bridge.current=createBridge({port:channel.port1,nonce,generation,snapshot,selection,captured,rest,onReady:()=>{if(current())setFrameState('ready');},onUncertain:()=>{if(current())setUnknown(true);},onAccepted:value=>{operationEpoch.current++;setOperation(value);setMessage('CPA operation accepted; awaiting completion.');},confirm:diff=>new Promise(resolve=>{pending.current=resolve;setConsent(diff);}),onClose:()=>{if(!current())return;pending.current?.(false);pending.current=null;setConsent(null);setFrameState('error');}});
      target.postMessage({type:'cpa-bridge-init',v:2,nonce,generation},'*',[channel.port2]);
    }catch{if(current())setFrameState('error');}
  };
  const frameMessage={loading:'Loading verified console…',connecting:'Connecting console…',ready:'Console ready.',error:'Console unavailable or channel closed. Reconnect to start a new session.',revoked:'Connection changed. Reconnect to open CPA Console for the current profile.'}[frameState];
  // The frame fills whatever container the host gives it: the docked tab, or the route page.
  return jsxs('section',{style:{display:'flex',flexDirection:'column',width:'100%',height:'100%',minHeight:'24rem'},children:[
    jsxs('div',{style:{flex:'none',display:'flex',flexWrap:'wrap',alignItems:'center',gap:12,padding:'6px 12px'},children:[
      jsx('p',{role:'status','data-frame-state':frameState,children:frameMessage}),
      unknown && jsx('p',{role:'alert',children:'Configuration outcome unknown. Do not resubmit; reconcile host status before another apply.'}),
      operation && jsx('p',{role:'status',children:message}),
      operation && jsx('button',{disabled:reconciling,onClick:()=>setRetry(value=>value+1),children:'Reconcile accepted operation'}),
      (frameState==='error' || frameState==='revoked') && jsx('button',{type:'button',onClick:reconnect,children:'Reconnect'}),
    ]}),
    jsx('div',{style:{flex:'1 1 0',minHeight:0,position:'relative'},children:asset && jsx(SandboxedFrame,{ref:frame,title:'CPA Management Console',src:frameSrc,sandbox:'allow-scripts',onLoad:load,style:{position:'absolute',inset:0,width:'100%',height:'100%',border:0}})}),
    jsx(ConfirmDialog,{open:!!consent,title:'Apply CPA configuration and restart?',description:'Only these typed fields will change. A restart interrupts active requests. Acceptance is not completion; refresh status to reconcile.',confirmLabel:'Apply and restart',onClose:()=>{pending.current?.(false);pending.current=null;setConsent(null);},onConfirm:()=>{pending.current?.(true);pending.current=null;setConsent(null);},children:jsx('pre',{children:consent?.map(d=>`${d.key}: ${JSON.stringify(d.before)} → ${JSON.stringify(d.after)}`).join('\n')})})
  ]});
}
export default {id:ID,name:'CLI Proxy API Console',register(ctx){
  const dock=createDock({host,id:ID,title:'CPA Console',render:()=>jsx(CpaConsole,{ctx})});
  ctx.onDispose(dock.dispose);
  ctx.registerMany([
    {id:'nav',area:SIDEBAR_NAV_AREA,order:65,data:{codicon:'server-process',label:'CPA Console',path:ROUTE}},
    {id:'route',area:ROUTES_AREA,data:{path:ROUTE},render:()=>jsx(RoutePage,{ctx,dock})},
    {id:'open',area:PALETTE_AREA,data:{id:`${ID}.open`,label:'Open CPA Console',keywords:['cpa','cliproxyapi','proxy','console','management','models','providers'],run:()=>{if(!dock.open())host.navigate(ROUTE);}}},
  ]);
}};