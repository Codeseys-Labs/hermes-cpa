// Real packaged Desktop + unchanged SandboxedFrame + actual built console and
// production host component. Generated REST fixture ONLY: not auth/CPA proof.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
// The host build under test: the scoped-patched Desktop (selection-bound REST), never the live install.
const agentRoot=process.env.HERMES_AGENT_ROOT;
if(!agentRoot)throw new Error('HERMES_AGENT_ROOT must name an isolated qualification host');
const {_electron}=require(path.join(agentRoot,'node_modules/playwright'));
(async()=>{
 const root=path.resolve(__dirname,'../..'),output=path.resolve(process.env.CPA_EVIDENCE||path.join(root,'tests/desktop/evidence'));
 fs.mkdirSync(output,{recursive:true});
 const sandbox=fs.mkdtempSync(path.join(output,'isolated-')),home=path.join(sandbox,'home'),plugin=path.join(home,'desktop-plugins','hermes-cpa');
 fs.mkdirSync(plugin,{recursive:true,mode:0o700});
 const html=fs.readFileSync(process.env.CPA_CONSOLE_ASSET||path.join(root,'assets/cpamc.html'),'utf8');
 const source=fs.readFileSync(path.join(root,'desktop/plugin.js'),'utf8').split('export default')[0];
 const digest=crypto.createHash('sha256').update(html).digest('hex');
 fs.writeFileSync(path.join(plugin,'plugin.js'),source+`\nexport default {id:'hermes-cpa',register(ctx){
 window.__fixture={puts:0,requests:[],revision:'a'.repeat(64),config:{strategy:'round-robin',request_retry:3,request_log:false}};
 const fixture={...ctx,rest:async(p,o)=>{const f=window.__fixture;f.requests.push({path:p,method:o?.method||'GET'});
 if(o?.method==='PUT'){f.puts++;f.config={...f.config,...o.body.changes};f.revision='b'.repeat(64);}
 const data=p==='/console/asset'?{html:${JSON.stringify(html)},manifest:{sha256:${JSON.stringify(digest)}}}:p==='/status'?{mode:'managed',lifecycle:'ready',management_ready:true,inference_configured:false}:p==='/metrics'?{available:false,total_success:0,total_failed:0,secret:'GENERATED_CANARY'}:{...f.config,revision:f.revision};
 return {api_version:1,scope_id:'generated-scope',generation:1,revision:f.revision,data};}};
 ctx.onDispose(host.openWorkspace('hermes-cpa-fixture',{title:'CPA fixture',render:()=>jsx(CpaConsole,{ctx:fixture})}));}};`);
 const env={PATH:process.env.PATH,DISPLAY:process.env.DISPLAY,XAUTHORITY:process.env.XAUTHORITY,HOME:sandbox,HERMES_HOME:home,XDG_CONFIG_HOME:sandbox,XDG_CACHE_HOME:sandbox,HERMES_DESKTOP_USER_DATA_DIR:path.join(sandbox,'user-data'),HERMES_DESKTOP_IGNORE_EXISTING:'1',HERMES_DESKTOP_ISOLATED_BACKEND:'1',HERMES_DESKTOP_BOOT_FAKE:'1',HERMES_DESKTOP_BOOT_FAKE_STEP_MS:'50',HERMES_DESKTOP_APP_NAME:'CPA-fixture-'+Date.now(),HERMES_DESKTOP_SKIP_QUIT_CONFIRM:'1',LIBGL_ALWAYS_SOFTWARE:'1'};
 const app=await _electron.launch({executablePath:path.join(agentRoot,'apps/desktop/release/linux-unpacked/Hermes'),args:['--no-sandbox','--disable-gpu'],env,timeout:20000});
 try{
 const page=await app.firstWindow(),errors=[];page.on('pageerror',()=>errors.push('Renderer error (text withheld)'));
 const iframe=page.locator('iframe[title="CPA Management Console"]');await iframe.waitFor({timeout:30000});
 // The frame fills its container (the docked tab), not a fixed viewport fraction.
 const [frameBox,paneBox]=await Promise.all([iframe.boundingBox(),iframe.locator('xpath=..').boundingBox()]);
 assert.ok(Math.abs(frameBox.height-paneBox.height)<=1&&Math.abs(frameBox.width-paneBox.width)<=1,'frame must fill its container');
 const frame=await(await iframe.elementHandle()).contentFrame();
 await frame.getByText('Host-projected status and usage',{exact:true}).waitFor({timeout:15000});
 await page.waitForFunction(()=>window.__fixture.requests.some(r=>r.path==='/metrics'));
 await frame.getByRole('navigation').getByRole('link',{name:'Configuration',exact:true}).evaluate(el=>el.click());
 await frame.getByRole('button',{name:'Request host review'}).waitFor();
 // Nothing changed yet: reviewing is unavailable, so an untouched form can never restart CPA.
 assert.equal(await frame.getByRole('button',{name:'Request host review'}).isDisabled(),true);
 await frame.getByRole('spinbutton',{name:'Request retries'}).fill('4');
 // fake boot setup overlay prevents pointer use. DOM activation is diagnostic,
 // not evidence of unobstructed real integrated onboarding.
 await frame.getByRole('button',{name:'Request host review'}).evaluate(el=>el.click());
 await page.getByRole('button',{name:'Apply and restart',exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>window.__fixture.puts),0);
 // Human review outlasts the ten-second transport budget: still open and unsubmitted at 11 s.
 await page.waitForTimeout(11000);
 assert.equal(await page.getByRole('button',{name:'Apply and restart',exact:true}).isVisible(),true);
 assert.equal(await page.evaluate(()=>window.__fixture.puts),0);
 await page.getByRole('button',{name:'Apply and restart',exact:true}).evaluate(el=>el.click());
 await page.waitForFunction(()=>window.__fixture.puts===1);
 await frame.getByText(/Operation accepted, not yet confirmed persisted/).waitFor();
 const isolation=await frame.evaluate(async()=>{let storageDenied=false,networkDenied=false;try{localStorage.getItem('probe');}catch{storageDenied=true;}try{await fetch('https://hermes.invalid/denied');}catch{networkDenied=true;}return {storageDenied,networkDenied,canaryInDOM:document.documentElement.outerHTML.includes('GENERATED_CANARY'),csp:document.querySelector('meta[http-equiv="Content-Security-Policy"]').content};});
 assert.equal(isolation.storageDenied,true);assert.equal(isolation.networkDenied,true);assert.equal(isolation.canaryInDOM,false);
 const fixture=await page.evaluate(()=>window.__fixture);assert.equal(fixture.config.request_retry,4);
 assert.equal(await iframe.getAttribute('sandbox'),'allow-scripts');
 const result={acceptance:false,reason:'Fake boot + generated REST fixture, no authenticated CPA backend or real restart persistence',assetBytes:Buffer.byteLength(html),jsonBytes:Buffer.byteLength(JSON.stringify({html})),dataURLBytes:Buffer.byteLength('data:text/html;charset=utf-8;base64,'+Buffer.from(html).toString('base64')),sha256:digest,errors,isolation,fixture};
 fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2));await page.screenshot({path:path.join(output,'console.png')});console.log(JSON.stringify(result,null,2));
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
