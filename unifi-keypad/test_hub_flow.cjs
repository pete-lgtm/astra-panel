const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const nodes={},listeners={},timers=new Map(),requests=[];
const make=()=>({value:'',dataset:{},children:[],classList:{toggle(){}},querySelectorAll(){return []},setAttribute(k,v){this[k]=v},append(...items){this.children.push(...items)},focus(){}});
const get=id=>nodes[id]??=make();
let now=1000,cameras=[],holdDiscovery,holdUnlock;
const ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(type,fn){(listeners[type]??=[]).push(fn)}},Date,Math,performance:{now:()=>now},
  fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)}});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),flush=()=>new Promise(resolve=>setImmediate(resolve));
ctx.fakeCall=async(path,data={})=>{
  requests.push({path,data});
  if(path==='/unlock'){if(holdUnlock)await holdUnlock;return {unlock:'session-'+requests.length,expiresIn:60}}
  if(path==='/hub/capabilities'){if(holdDiscovery)await holdDiscovery;return {cameras,camerasDiscovered:true}}
  if(path==='/lock')return {};
  throw new Error('Unexpected request '+path);
};
run("call=fakeCall;render({connected:true,pinSet:true,webhookReady:true,armAwayConfigured:true,armStayConfigured:true});");
const unlock=async()=>{get('swipe-unlock').onkeydown({key:'Enter',preventDefault(){}});get('pin').value='753159';await get('unlock-form').onsubmit({preventDefault(){}});await flush()};
const target={closest(selector){return selector==='.hub-screen'?{}:null}};
function swipe(dx,dy=0,swipeTarget=target){
  const base={isPrimary:true,button:0,pointerId:1,target:swipeTarget,clientX:200,clientY:200,preventDefault(){}};
  for(const fn of listeners.pointerdown||[])fn(base);
  for(const fn of listeners.pointerup||[])fn({...base,clientX:200+dx,clientY:200+dy});
}
(async()=>{
  assert.equal(get('hub-navigation').hidden,true);
  get('nav-systems').onclick();swipe(-100);run("show('systems')");
  assert.equal(run('activeScreen'),'sleep','Locked gestures and links cannot open a hub page');
  assert.equal(requests.length,0,'No camera discovery before a PIN');
  await unlock();
  assert.equal(run('activeScreen'),'home');
  assert.equal(get('hub-navigation').hidden,false);
  assert.equal(get('nav-cameras').hidden,true);
  assert.equal(requests.filter(r=>r.path.startsWith('/webhook/')).length,0,'Unlock never arms or disarms');
  swipe(-100);assert.equal(run('activeScreen'),'systems','No-camera sites skip Cameras');
  swipe(-100);assert.equal(run('activeScreen'),'audio','Audio follows Systems');
  swipe(-100);assert.equal(run('activeScreen'),'lighting','Lighting follows Audio');
  swipe(-100);assert.equal(run('activeScreen'),'lighting','No wraparound');
  swipe(100);assert.equal(run('activeScreen'),'audio');
  swipe(100);assert.equal(run('activeScreen'),'systems');
  for(const tab of ['network','wifi','power']){
    get('systems-tab-'+tab).onclick();
    assert.equal(get('systems').dataset.subtab,tab);
    for(const panel of ['power','network','wifi'])assert.equal(get('systems-panel-'+panel).hidden,panel!==tab);
  }
  swipe(100);assert.equal(run('activeScreen'),'home');
  swipe(-100);assert.equal(run('activeScreen'),'systems');
  swipe(-10,120);assert.equal(run('activeScreen'),'systems','Vertical scrolling must not change pages');
  swipe(-100,0,{closest(selector){return selector==='.hub-screen'?{}:{}}});
  assert.equal(run('activeScreen'),'systems','Swiping an interactive control must not navigate');
  get('nav-home').onclick();assert.equal(run('activeScreen'),'home');
  run('lock()');cameras=[{id:'front',name:'Front camera',state:'CONNECTED'}];await unlock();
  assert.equal(get('nav-cameras').hidden,false);
  swipe(-100);assert.equal(run('activeScreen'),'cameras');
  assert.equal(get('nav-cameras')['aria-current'],'page');
  assert.equal(get('hub-camera-list').children.at(-1).children[0].textContent,'Front camera');
  run("acceptObservation({fresh:true,state:'breached',remaining:60,notification:'not_sent'});");
  assert.equal(get('alert-strip').hidden,false,'Countdown remains visible over Cameras');
  assert.equal(get('strip-cancel').hidden,false,'Disarm remains reachable over Cameras');
  get('nav-systems').onclick();assert.equal(get('alert-strip').hidden,false);
  for(const [fn,ms]of [...timers])if(ms===60000)fn();
  assert.equal(run('activeScreen'),'sleep');
  assert.equal(get('hub-navigation').hidden,true);
  assert.equal(get('hub-camera-list').textContent,'','Lock clears camera inventory');
  run("acceptObservation({fresh:true,state:'disarmed',remaining:null,notification:'not_sent'});");
  let resolveDiscovery;holdDiscovery=new Promise(resolve=>resolveDiscovery=resolve);
  await unlock();run('lock()');resolveDiscovery();await flush();holdDiscovery=null;
  assert.equal(get('hub-navigation').hidden,true);
  assert.equal(get('nav-cameras').hidden,true,'Late inventory must not reopen a locked screen');
  let resolveUnlock;holdUnlock=new Promise(resolve=>resolveUnlock=resolve);
  get('swipe-unlock').onkeydown({key:'Enter',preventDefault(){}});get('pin').value='753159';
  const pending=get('unlock-form').onsubmit({preventDefault(){}});run('lock()');resolveUnlock();await pending;
  assert.equal(run('activeScreen'),'sleep','A cancelled PIN attempt must stay locked');
  assert.equal(run('unlockToken'),'');
  assert.ok(requests.some(r=>r.path==='/lock'));
  console.log('PIN-first hub, conditional Cameras, swipe/tap navigation, countdown access, and lock-race checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
