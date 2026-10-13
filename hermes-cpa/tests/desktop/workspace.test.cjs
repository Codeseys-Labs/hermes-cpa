const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../desktop/plugin.js'),'utf8');
const code=source.replace(/^import .*;$/gm,'').replace('export default','const plugin =');
const pause=ms=>new Promise(r=>setTimeout(r,ms));

// A host stand-in with the real workspace contract: one tab per id, re-opening an id refreshes
// that tab in place and re-fronts it, the returned closer (or the tab's own close) tears down only
// that tab and fires its onClose. Chat and another plugin's tab are always present.
function world({workspaces=true,failOpen=false,hash='#/session/1'}={}) {
  const tabs=new Map([['chat',{title:'Chat'}],['other-plugin',{title:'Other plugin'}]]);
  const closers=new Map(),log=[];
  const win={location:{hash:'#/cpa'},history:{back(){log.push('back');win.location.hash=hash;}}};
  const host={navigate:to=>{log.push('navigate '+to);},state:{}};
  if(workspaces)host.openWorkspace=(id,options)=>{
    if(failOpen)throw new Error('workspace unavailable');
    log.push((tabs.has(id)?'refront ':'open ')+id);
    tabs.set(id,options);
    const close=()=>{tabs.delete(id);closers.delete(id);log.push('close '+id);options.onClose?.();};
    closers.set(id,close);
    return close;
  };
  const context=vm.createContext({TextEncoder,setTimeout,clearTimeout,host,window:win,
    ROUTES_AREA:'routes',SIDEBAR_NAV_AREA:'sidebar-nav',PALETTE_AREA:'palette',SandboxedFrame:'SandboxedFrame',ConfirmDialog:'ConfirmDialog',
    useEffect(){},useState:value=>[value,()=>{}],useRef:value=>({current:value}),
    jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})});
  vm.runInContext(code,context);
  const registered=[],disposers=[];
  vm.runInContext('plugin',context).register({registerMany:items=>{registered.push(...items);return()=>{};},onDispose:fn=>{disposers.push(fn);},rest:async()=>{throw Error('REST is not part of docking');}});
  const area=name=>registered.find(item=>item.area===name);
  return {tabs,log,win,
    openFromPalette:()=>area('palette').data.run(),
    route:()=>area('routes').render().props,
    enterRoute:options=>vm.runInContext('enterRoute',context)({win,delay:5,...options}),
    userClosesTab:id=>closers.get(id)?.(),
    disposePlugin:()=>disposers.forEach(fn=>fn()),
  };
}
const others=w=>[...w.tabs.keys()].filter(id=>id!=='hermes-cpa');

test('palette opens the console docked beside the chat and leaves chat and other tabs alone',()=>{
 const w=world();w.openFromPalette();
 assert.deepEqual(others(w),['chat','other-plugin']);
 const tab=w.tabs.get('hermes-cpa');
 assert.deepEqual({...tab.dock},{pane:'workspace',pos:'right'});
 assert.equal(typeof tab.render,'function');
 assert.deepEqual(w.log,['open hermes-cpa'],'no navigation may replace the chat');
});

test('opening again from any entry point re-fronts the one workspace instead of stacking another',()=>{
 const w=world();
 w.openFromPalette();w.openFromPalette();
 const cleanup=w.enterRoute({dock:w.route().dock,onUnavailable:()=>assert.fail('docking is available')});cleanup();
 assert.deepEqual([...w.tabs.keys()].filter(id=>id==='hermes-cpa'),['hermes-cpa']);
 assert.deepEqual(others(w),['chat','other-plugin']);
 assert.deepEqual(w.log.filter(entry=>entry!=='back'),['open hermes-cpa','refront hermes-cpa','refront hermes-cpa']);
});

test('the sidebar route opens the tab then returns the main pane to where the user was',async()=>{
 const w=world();
 const cleanup=w.enterRoute({dock:w.route().dock,onUnavailable:()=>assert.fail('docking is available')});
 await pause(30);cleanup();
 assert.deepEqual(w.log,['open hermes-cpa','back'],'router stepped back; no forced navigation');
 assert.ok(w.tabs.has('hermes-cpa'));assert.deepEqual(others(w),['chat','other-plugin']);
});

test('a direct load of the route has no entry to pop, so it falls back to the chat',async()=>{
 const w=world({hash:'#/cpa'});
 const cleanup=w.enterRoute({dock:w.route().dock,onUnavailable:()=>assert.fail('docking is available')});
 await pause(30);cleanup();
 assert.deepEqual(w.log,['open hermes-cpa','back','navigate /']);
});

test('leaving the route before the fallback fires cancels it',async()=>{
 const w=world({hash:'#/cpa'});
 const cleanup=w.enterRoute({dock:w.route().dock,onUnavailable:()=>assert.fail('docking is available'),delay:20});
 cleanup();await pause(50);
 assert.ok(!w.log.includes('navigate /'));
});

test('an older host without workspaces renders the console on the route instead',()=>{
 const w=world({workspaces:false});
 assert.equal(w.route().dock.supported(),false);
 w.openFromPalette();
 assert.deepEqual(w.log,['navigate /cpa']);
 let unavailable=0;
 w.enterRoute({dock:w.route().dock,onUnavailable:()=>{unavailable++;}})();
 assert.equal(unavailable,1);assert.ok(!w.log.includes('back'),'a route that stays on screen must not be popped');
});

test('a workspace that cannot open falls back to the route page',()=>{
 const w=world({failOpen:true});
 w.openFromPalette();
 assert.deepEqual(w.log,['navigate /cpa']);
 let unavailable=0;
 w.enterRoute({dock:w.route().dock,onUnavailable:()=>{unavailable++;}})();
 assert.equal(unavailable,1);assert.ok(!w.log.includes('back'));
 assert.deepEqual(others(w),['chat','other-plugin']);
});

test('disposing the plugin closes only its own workspace',()=>{
 const w=world();w.openFromPalette();
 w.disposePlugin();
 assert.ok(!w.tabs.has('hermes-cpa'));assert.deepEqual(others(w),['chat','other-plugin']);
 assert.deepEqual(w.log,['open hermes-cpa','close hermes-cpa']);
 w.disposePlugin();
 assert.deepEqual(w.log,['open hermes-cpa','close hermes-cpa'],'a second dispose closes nothing');
});

test('disposing a plugin that never opened its workspace closes nothing',()=>{
 const w=world();w.disposePlugin();
 assert.deepEqual(w.log,[]);assert.deepEqual([...w.tabs.keys()],['chat','other-plugin']);
});

test('a tab the user closed stays closed on dispose, and the next open creates a fresh one',()=>{
 const w=world();w.openFromPalette();
 w.userClosesTab('hermes-cpa');
 w.disposePlugin();
 assert.deepEqual(w.log,['open hermes-cpa','close hermes-cpa'],'dispose must not close again');
 w.openFromPalette();
 assert.deepEqual(w.log,['open hermes-cpa','close hermes-cpa','open hermes-cpa']);
 assert.deepEqual(others(w),['chat','other-plugin']);
});
