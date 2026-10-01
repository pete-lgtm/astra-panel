const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('app.js','utf8');
function harness(mode='disarmed'){
 const elements={},requests=[],timers=new Map();let now=0,next=0,reject=false,hold;
 const node=()=>({value:'',hidden:false,children:[],dataset:{},classList:{toggle(){}},querySelectorAll(){return this.children;},append(...items){this.children.push(...items);},setAttribute(){},focus(){},showModal(){this.open=true;},close(){this.open=false;}});
 const get=id=>elements[id]??=node();
 const context=vm.createContext({document:{body:node(),getElementById:get,createElement:node,addEventListener(){}},Math,Date,performance:{now:()=>now},setInterval(){},setTimeout(fn,delay){const id=++next;timers.set(id,{fn,at:now+delay});return id;},clearTimeout(id){timers.delete(id);},fetch:()=>new Promise(()=>{})});
 vm.runInContext(source,context);
 context.observation={state:mode==='entry'?'breached':'disarmed',fresh:true,remaining:mode==='entry'?45:null,notification:'not_sent',liveDelayEnabled:true,doors:{Front:{connected:true,position:'closed'}},verifiedDoors:['Front']};
 context.ready={connected:true,pinSet:true,webhookReady:true,armStayConfigured:true,armAwayConfigured:true};
 context.fakeCall=async(path,data={})=>{
  requests.push({path,data});
  if(path==='/unlock'||path==='/settings/unlock'){
   if(hold)await hold;
   if(reject)throw new Error('Incorrect PIN.');
   return path==='/unlock'?{unlock:'mock-grant',expiresIn:60}:{settingsUnlocked:true,pinSet:true,expiresIn:300};
  }
  if(path==='/settings/status')return {pinSet:true,settingsUnlocked:false};
  if(path==='/webhook/disarm')return context.ready;
  if(path==='/entry/status')return context.observation;
  if(path==='/hub/capabilities')return {cameras:[]};
  return {};
 };
 const run=s=>vm.runInContext(s,context);
 run('call=fakeCall;render(ready);acceptObservation(observation);');
 if(mode==='entry')get('wake').onclick();
 else get('swipe-unlock').onkeydown({key:'Enter',preventDefault(){}});
 const flush=()=>new Promise(resolve=>setImmediate(resolve));
 async function advance(ms){now+=ms;for(const [id,timer]of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}await flush();}
 const type=value=>{get('pin').value=value;get('pin').oninput();};
 const tap=value=>get('number-pad').children.find(b=>b.textContent===value).onclick();
 return {get,run,requests,type,tap,advance,flush,reject:v=>reject=v,hold:v=>hold=v};
}
(async()=>{
 const four=harness();for(const digit of '2468')four.tap(digit);
 await four.advance(999);assert.equal(four.requests.length,0,'Do not verify digits while the user is still entering a PIN');
 await four.advance(1);assert.equal(four.run('activeScreen'),'home');
 assert.equal(four.requests.filter(r=>r.path==='/unlock').length,1);
 assert.equal(four.requests.filter(r=>r.path.startsWith('/webhook/')).length,0,'Ordinary unlock must not disarm');
 for(const pin of ['24680','246802','2468024','24680246']){
  const h=harness();h.type(pin.slice(0,4));await h.advance(900);h.type(pin);await h.advance(pin.length===8?0:1000);
  assert.equal(h.run('activeScreen'),'home');assert.equal(h.requests.filter(r=>r.path==='/unlock').length,1);
  assert.equal(h.requests.find(r=>r.path==='/unlock').data.pin,pin,'Send the complete variable-length PIN once');
 }
 const edits=harness();edits.type('2468');await edits.advance(500);edits.tap('⌫');await edits.advance(1000);assert.equal(edits.requests.length,0);
 edits.tap('8');edits.tap('Clear');await edits.advance(1000);assert.equal(edits.requests.length,0);
 edits.type('123x');await edits.advance(1000);assert.equal(edits.requests.length,0,'Invalid input is never sent');
 const wrong=harness();wrong.reject(true);wrong.type('1234');await wrong.advance(1000);assert.equal(wrong.run('activeScreen'),'pin-screen');assert.equal(wrong.get('pin').value,'');assert.equal(wrong.get('message').textContent,'Incorrect PIN.');
 await wrong.advance(1000);assert.equal(wrong.requests.length,1,'No automatic retry can consume more PIN attempts');
 wrong.reject(false);wrong.type('2468');await wrong.advance(1000);assert.equal(wrong.run('activeScreen'),'home');
 const cancel=harness();cancel.type('2468');cancel.get('back').onclick();await cancel.advance(1000);assert.equal(cancel.requests.length,0);assert.equal(cancel.run('activeScreen'),'sleep');
 const pending=harness();let resolve;pending.hold(new Promise(r=>resolve=r));pending.type('2468');await pending.advance(1000);
 assert.equal(pending.get('pin').disabled,true);assert.ok(pending.get('number-pad').children.every(b=>b.disabled));pending.tap('5');assert.equal(pending.get('pin').value,'');
 await pending.get('unlock-form').onsubmit({preventDefault(){}});assert.equal(pending.requests.length,1,'Pending verification cannot submit twice');
 pending.get('back').onclick();resolve();await pending.flush();assert.equal(pending.run('activeScreen'),'sleep');assert.equal(pending.run('unlockToken'),'');assert.ok(pending.requests.some(r=>r.path==='/lock'),'Cancelled requests revoke a late grant');
 const settings=harness();settings.get('back').onclick();await settings.get('settings').onclick();settings.type('2468');await settings.advance(1000);assert.equal(settings.run('activeScreen'),'setup-screen');assert.equal(settings.requests.filter(r=>r.path==='/settings/unlock').length,1);
 const alarm=harness('entry');alarm.type('2468');await alarm.advance(1000);assert.equal(alarm.requests.filter(r=>r.path==='/webhook/disarm').length,1);assert.equal(alarm.requests.find(r=>r.path==='/webhook/disarm').data.unlock,'mock-grant');
 console.log('Automatic PIN: 4–8 digits, touch and keyboard, edits, errors, single attempts, cancellation, settings and alarm-disarm flows passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
