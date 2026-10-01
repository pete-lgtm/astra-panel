const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function harness(seconds=60){
 const nodes={},requests=[],timers=new Map(),listeners={};let now=0,next=0,holdRenewal;
 const element=()=>({value:'',children:[],hidden:false,dataset:{},classList:{toggle(){}},append(...items){this.children.push(...items)},replaceChildren(...items){this.children=[...items]},querySelectorAll(){return this.children},setAttribute(){},focus(){},scrollIntoView(){},close(){this.open=false},showModal(){this.open=true}});
 const get=id=>nodes[id]??=element();
 const context=vm.createContext({document:{body:element(),getElementById:get,createElement:element,querySelectorAll:()=>[],addEventListener(name,fn){(listeners[name]??=[]).push(fn)}},Math,Date,Set,performance:{now:()=>now},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(fn,delay){const id=++next;timers.set(id,{fn,at:now+delay});return id},clearTimeout(id){timers.delete(id)}});
 const run=code=>vm.runInContext(code,context);
 run(fs.readFileSync('app.js','utf8'));run(fs.readFileSync('management.js','utf8'));
 context.mockCall=async(path,body={})=>{
  requests.push(path);
  if(path==='/unlock')return {unlock:'fixture-grant',expiresIn:seconds,autoLockSeconds:seconds};
  if(path==='/hub/activity'){if(holdRenewal)await holdRenewal;return {expiresIn:seconds,autoLockSeconds:seconds};}
  if(path==='/hub/capabilities')return {cameras:[]};
  if(path==='/settings/summary')return {user:{id:'owner',name:'Owner',role:'admin'},users:1,doors:['Front'],managed:true,apiConnected:true,localConnected:true,autoLockSeconds:seconds};
  if(path==='/settings/panel/save'){seconds=body.autoLockSeconds;return {autoLockSeconds:seconds};}
  if(path==='/lock'||path==='/settings/lock')return {};
  throw Error('Unexpected fixture request: '+path);
 };
 run("call=mockCall;render({webhookReady:true,pinSet:true,armStayConfigured:true,armAwayConfigured:true});show('pin-screen');");
 const flush=()=>new Promise(resolve=>setImmediate(resolve));
 async function advance(ms){now+=ms;for(const [id,timer]of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}await flush()}
 async function unlock(){get('pin').value='2468';await run('submitPin()');await flush()}
 return {get,run,requests,advance,unlock,flush,hold(value){holdRenewal=value},touch(){listeners.pointerdown.at(-1)();return flush()}};
}
(async()=>{
 for(const seconds of [60,120,180]){
  const h=harness(seconds);await h.unlock();
  assert.equal(h.run('activeScreen'),'home');
  await h.advance(seconds*1000-1000);assert.equal(h.run('activeScreen'),'home');
  await h.advance(1000);assert.equal(h.run('activeScreen'),'sleep');
  assert.equal(h.run('unlockToken'),'');
  assert.ok(h.requests.includes('/lock'));
 }
 const touch=harness();await touch.unlock();await touch.advance(50000);await touch.touch();
 assert.equal(touch.requests.filter(p=>p==='/hub/activity').length,1);
 await touch.advance(20000);assert.equal(touch.run('activeScreen'),'home','Touch must renew the server grant as well as the UI');
 touch.run('render(status);exitDisplay();');
 await touch.advance(39999);assert.equal(touch.run('activeScreen'),'home');
 await touch.advance(1);assert.equal(touch.run('activeScreen'),'sleep','Background redraws must not keep it unlocked');
 const late=harness();await late.unlock();let resolve;
 late.hold(new Promise(r=>resolve=r));await late.touch();late.run('lock()');resolve();await late.flush();
 assert.equal(late.run('unlockToken'),'');assert.equal(late.run('activeScreen'),'sleep','A late activity response must not revive access');
 const settings=harness();settings.run("settingsOpen=true;show('setup-screen')");
 await settings.run("openSettingsSection('panel')");assert.equal(settings.get('auto-lock').value,'60');
 settings.get('auto-lock').value='180';await settings.get('panel-settings-form').onsubmit({preventDefault(){}});
 assert.equal(settings.run('autoLockSeconds'),180);assert.match(settings.get('settings-message').textContent,/3 minutes/);
 await settings.advance(60000);assert.equal(settings.run('settingsOpen'),true);
 await settings.advance(120000);assert.equal(settings.run('settingsOpen'),false);
 console.log('Panel timeout: 1/2/3 minutes, interaction renewal, idle expiry, background redraws, settings save, and late-response revocation passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
