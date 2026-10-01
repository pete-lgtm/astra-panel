const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const nodes={},requests=[],timers=new Map(),listeners={};let nextTimer=0;
const make=()=>({value:'',dataset:{},children:[],classList:{toggle(){}},querySelectorAll(){return []},setAttribute(k,v){this[k]=v},append(...items){this.children.push(...items)},focus(){},
  showModal(){this.open=true},close(){this.open=false},
  set textContent(value){this.valueText=value;this.children=[]},get textContent(){return (this.valueText||'')+this.children.map(child=>child.textContent).join(' ')}});
const get=id=>nodes[id]??=make(),ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(name,fn){listeners[name]=fn}},Date,Math,
  performance:{now:()=>1000},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,ms});return id},clearTimeout(id){timers.delete(id)}});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),event={preventDefault(){}};
const inventory={state:'ready',wifiPasswordsAvailable:true,site:{id:'default',name:'Home'},sites:[],devices:[]};
const networks=[{name:'<Home & family>',password:' synthetic password ',passwordState:'available',enabled:true},
  {name:'Guest',passwordState:'open',message:'No Wi-Fi password required.',enabled:true},
  {name:'Office',passwordState:'enterprise',message:'Individual sign-in',enabled:false}];
let answer={networks,hideAfterSeconds:30};
ctx.inventory=inventory;
ctx.fakeCall=async(path,data)=>{requests.push({path,data});if(path==='/hub/wifi/reveal')return answer;if(path==='/hub/network')return inventory;if(path==='/lock')return {};throw new Error(path)};
run('call=fakeCall');
const open=()=>get('hub-wifi-passwords').onclick();
const start=()=>run("unlockToken='grant';networkInventory=inventory;networkLoading=false;show('systems');selectSystemsTab('wifi');renderNetworkHub()");
const submit=()=>{get('wifi-pin').value='739126';return get('wifi-pin-form').onsubmit(event)};
(async()=>{
  open();assert.equal(get('wifi-dialog').open,undefined,'Locked panel cannot open passwords');
  start();assert.equal(get('hub-wifi-passwords').hidden,false);
  open();assert.equal(get('wifi-dialog').open,true);assert.equal(requests.length,0,'Opening requests no credentials');
  assert.equal(get('wifi-list').textContent,'');assert.equal(get('wifi-pin-form').hidden,false);
  get('wifi-number-pad').onclick({target:{closest:()=>({dataset:{key:'7'}})}});
  assert.equal(get('wifi-pin').value,'7');
  get('wifi-number-pad').onclick({target:{closest:()=>({dataset:{key:'back'}})}});
  assert.equal(get('wifi-pin').value,'');
  await submit();assert.equal(get('wifi-pin').value,'');
  assert.equal(requests[0].path,'/hub/wifi/reveal');assert.equal(requests[0].data.pin,'739126');
  assert.equal(get('wifi-pin-form').hidden,true);assert.equal(get('wifi-networks').hidden,false);
  assert.match(get('wifi-list').textContent,/<Home & family>/);assert.match(get('wifi-list').textContent,/ synthetic password /);
  assert.match(get('wifi-list').textContent,/No Wi-Fi password/);assert.match(get('wifi-list').textContent,/Disabled/);
  assert.doesNotMatch(run('JSON.stringify(networkInventory)'),/synthetic password/,'Ordinary inventory never caches credentials');
  const timer=run('wifiHideTimer');assert.equal(timers.get(timer).ms,30000);
  run('activity()');assert.equal(run('wifiHideTimer'),timer,'Interaction cannot extend reveal');
  timers.get(timer).fn();assert.equal(get('wifi-dialog').open,false);assert.equal(get('wifi-list').textContent,'');
  open();assert.equal(get('wifi-pin-form').hidden,false);assert.equal(get('wifi-list').textContent,'');
  get('wifi-close').onclick();assert.equal(get('wifi-pin').value,'');
  let resolve;answer=new Promise(r=>resolve=r);open();const pending=submit();await submit();
  assert.equal(requests.filter(r=>r.path==='/hub/wifi/reveal').length,2,'Double tap sends one fresh PIN request');
  run('lock()');resolve({networks});await pending;
  assert.equal(get('wifi-list').textContent,'');assert.equal(get('wifi-dialog').open,false,'Late result cannot reopen after lock');
  start();open();answer={networks};await submit();run("show('home')");assert.equal(get('wifi-list').textContent,'');
  start();open();await submit();ctx.document.hidden=true;listeners.visibilitychange();assert.equal(get('wifi-list').textContent,'');
  start();open();answer=new Promise(r=>resolve=r);const changing=submit();await run("loadNetworkHub('other')");resolve({networks});await changing;
  assert.equal(get('wifi-list').textContent,'','Changing Network site clears and invalidates pending secrets');
  start();open();answer=Promise.reject(new Error('Incorrect PIN.'));await submit();
  assert.match(get('wifi-result').textContent,/Incorrect PIN/);assert.equal(get('wifi-pin').value,'');assert.equal(get('wifi-pin-form').hidden,false);
  get('wifi-dialog').oncancel();assert.equal(get('wifi-result').textContent,'');
  start();open();answer={networks};await submit();run("renderObservation({state:'breached',fresh:true,doors:{},remaining:null})");
  assert.equal(get('wifi-dialog').open,false,'Alarm attention takes priority over passwords');
  start();run('networkInventory={state:"ready",site:{id:"default"}};renderNetworkHub()');assert.equal(get('hub-wifi-passwords').hidden,true,'Older backend exposes no unsupported route');
  console.log('Wi-Fi UI: fresh PIN, exact credential text, no inventory caching, absolute hide, lock/navigation/site/alarm races and retry passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
