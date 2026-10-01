const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},timers=new Map(),requests=[];
function node(){let content='';return {dataset:{},children:[],hidden:false,disabled:false,value:'',setAttribute(k,v){this[k]=v},
  append(...items){for(const n of items){n.parent=this;this.children.push(n)}},
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this)},
  get textContent(){return content},set textContent(v){content=v;this.children=[]},focus(){this.onfocus?.()},
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b},showModal(){this.open=true},close(){this.open=false},
  querySelectorAll(){return this.children.flatMap(n=>[n,...n.querySelectorAll()])}};}
const el=id=>nodes[id]??=node();
const data={state:'ready',canManage:true,bridge:{name:'Test bridge'},rooms:[{id:'r',name:'Living room',groupId:'g',lights:['a','b']}],
  lights:[{id:'a',kind:'light',name:'<Living lamp>',on:true,brightness:65,reachable:true,dimmable:true},
          {id:'b',kind:'light',name:'Reading lamp',on:false,brightness:40,reachable:true,dimmable:true}],
  scenes:[{id:'s',name:'Relax',roomId:'r'}]};
const clone=x=>JSON.parse(JSON.stringify(x));
let holdRead=false,resolveRead,holdControl=false,resolveControl,failRead=false,failControl=false,pairFails=false;
const ctx=vm.createContext({Map,Set,Number,document:{hidden:false,addEventListener(){}},el,
 activeScreen:'home',unlockToken:'grant',interactions:0,activity(){ctx.interactions++},
 text(id,s){el(id).textContent=s},hubNode(tag,cls,text=''){const n=node();n.tagName=tag;n.className=cls;n.textContent=text;return n},
 setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
 async call(path,body){
  requests.push({path,body});
  if(path==='/hub/lighting'){
    const result=clone(data);if(holdRead)await new Promise(resolve=>resolveRead=resolve);
    if(failRead)throw Error('Bridge unavailable');return result;
  }
  if(path.endsWith('/control')){
    if(holdControl)await new Promise(resolve=>resolveControl=resolve);
    if(failControl)throw Error('Result unknown');
    if(body.action==='on'&&body.kind==='light')data.lights.find(l=>l.id===body.id).on=body.value;
    return {accepted:true};
  }
  if(path.endsWith('/discover'))return {bridges:[{host:'192.168.1.50',name:'Test bridge'}]};
  if(path.endsWith('/prepare'))return {pairId:'plan',name:'Test bridge',host:'192.168.1.50'};
  if(path.endsWith('/pair')){if(pairFails)throw Error('Press the bridge button');return clone(data)}
  if(path.endsWith('/disconnect'))return {state:'not_connected',canManage:true,lights:[],rooms:[],scenes:[]};
  throw Error(path);
 }
});
const run=s=>vm.runInContext(s,ctx),flush=()=>new Promise(resolve=>setImmediate(resolve));
run(fs.readFileSync('lighting.js','utf8'));
const writes=()=>requests.filter(r=>r.path.endsWith('/control'));
(async()=>{
 assert.equal(requests.length,0,'No reads before opening Lighting');
 ctx.activeScreen='lighting';run('syncLightingScreen()');await flush();
 assert.equal(el('lighting-summary').textContent,'2 lights · 1 on');
 assert.equal(el('lighting-layout').hidden,false);
 const card=run("lightingCards.get('a')");
 assert.equal(card.name.textContent,'<Living lamp>','Names render as text');
 const interactions=ctx.interactions;await run('loadLighting()');
 assert.equal(ctx.interactions,interactions,'Polling never renews the PIN grant');
 assert.equal(run("lightingCards.get('a').toggle"),card.toggle,'Touch target survives polls');
 el('lighting-rooms').children[1].onclick();
 assert.equal(el('lighting-room-on').disabled,false);
 el('lighting-room-off').onclick();await flush();
 assert.equal(writes().at(-1).body.id,'g');assert.equal(writes().at(-1).body.value,false);
 card.slider.value='42';card.slider.oninput();card.slider.value='47';card.slider.oninput();
 const count=writes().length;assert.equal(writes().length,count,'Slider does not send on every move');
 card.slider.onchange();await flush();assert.equal(writes().length,count+1);assert.equal(writes().at(-1).body.value,47);
 el('lighting-scenes').children[0].onclick();await flush();assert.equal(writes().at(-1).body.action,'scene');
 // A user tap wins over a background read; a late snapshot cannot revert it.
 holdRead=true;const pending=run('loadLighting()');card.toggle.onclick();holdRead=false;await flush();
 assert.equal(writes().at(-1).body.value,false);resolveRead();await pending;
 assert.equal(card.toggle.textContent,'Off');
 holdControl=true;card.toggle.onclick();card.toggle.onclick();const inFlight=writes().length;
 assert.equal(card.toggle.disabled,true);resolveControl();holdControl=false;await flush();
 assert.equal(writes().length,inFlight,'One touch produces at most one command');
 failControl=true;failRead=true;card.toggle.onclick();await flush();
 assert.equal(card.toggle.disabled,true,'Unknown or offline state is not presented as controllable');
 const failedCount=writes().length;await run('loadLighting()');assert.equal(writes().length,failedCount,'Failures never replay writes');
 failControl=false;failRead=false;await run('loadLighting()');
 // Bridge setup has explicit physical approval and no key in the browser.
 el('lighting-connect').onclick();await flush();assert.equal(el('lighting-setup').open,true);
 el('lighting-address').onfocus();assert.equal(el('lighting-ip-pad').hidden,false);
 el('lighting-ip-pad').children[0].onclick();assert.equal(el('lighting-address').value,'1');
 el('lighting-ip-pad').children[5].onclick();assert.equal(el('lighting-address').value,'');
 el('lighting-bridges').children[0].onclick();await flush();
 assert.equal(el('lighting-pair').hidden,false);assert.match(el('lighting-setup-help').textContent,/Press the button/);
 pairFails=true;el('lighting-pair').onclick();await flush();assert.equal(el('lighting-setup').open,true);
 assert.match(el('lighting-setup-message').textContent,/Press/);
 pairFails=false;el('lighting-pair').onclick();await flush();assert.equal(el('lighting-setup').open,false);
 assert.ok(!JSON.stringify(requests).includes('application-key'));
 // Poll/command replies cannot restore controls or polling after locking.
 holdRead=true;const late=run('loadLighting()');ctx.unlockToken='';ctx.activeScreen='sleep';run('clearLightingHub()');resolveRead();await late;
 assert.equal(el('lighting-layout').hidden,true);assert.equal(el('lighting-lights').children.length,0);assert.equal(timers.size,0);
 ctx.unlockToken='next';ctx.activeScreen='lighting';holdRead=false;run('syncLightingScreen()');await flush();
 holdControl=true;run("lightingCards.get('a').toggle.onclick()");ctx.unlockToken='';ctx.activeScreen='sleep';run('clearLightingHub()');resolveControl();await flush();
 assert.equal(timers.size,0);assert.equal(el('lighting-lights').children.length,0);
 ctx.unlockToken='next';ctx.activeScreen='lighting';data.canManage=false;run('syncLightingScreen()');await flush();
 assert.equal(el('lighting-connect').disabled,true,'Only an administrator connects a bridge');
 ctx.activeScreen='audio';run('syncLightingScreen()');assert.equal(timers.size,0,'Leaving Lighting stops polling');
 console.log('Hue controls, pairing, stable touch targets, stale-state handling, PIN races and polling passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
