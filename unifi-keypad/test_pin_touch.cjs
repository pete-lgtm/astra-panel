const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},requests=[],timers=new Map();let now=0,nextTimer=0;
const element=()=>({value:'',hidden:false,disabled:false,dataset:{},children:[],captures:new Set(),style:{setProperty(){}},classList:{toggle(){}},
 append(...items){this.children.push(...items)},querySelectorAll(){return this.children},setAttribute(){},focus(){},close(){this.open=false},
 getBoundingClientRect(){return {left:100,right:200,top:300,bottom:400}},
 setPointerCapture(id){this.captures.add(id)},hasPointerCapture(id){return this.captures.has(id)},releasePointerCapture(id){this.captures.delete(id)}
});
const get=id=>nodes[id]??=element();
const ctx=vm.createContext({document:{body:element(),getElementById:get,createElement:element,addEventListener(){}},Math,Date,performance:{now:()=>now},setInterval(){},setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,at:now+ms});return id},clearTimeout(id){timers.delete(id)},fetch:()=>new Promise(()=>{})});
const run=source=>vm.runInContext(source,ctx);
run(fs.readFileSync('app.js','utf8'));
ctx.mockCall=async(path,body)=>{requests.push({path,body});if(path==='/unlock')return {unlock:'test',expiresIn:60,autoLockSeconds:60};if(path==='/hub/capabilities')return {cameras:[]};return {}};
run("call=mockCall;render({connected:true,pinSet:true,webhookReady:true});acceptObservation({fresh:true,state:'disarmed',remaining:null,notification:'not_sent'});");
const swipe=get('swipe-unlock'),key=value=>get('number-pad').children.find(button=>button.textContent===value);
const event=(extra={})=>({pointerId:1,pointerType:'touch',isPrimary:true,button:0,clientX:150,clientY:350,preventDefault(){this.prevented=true},...extra});
function open(){swipe.onpointerdown(event({clientY:650}));now+=300;swipe.onpointerup(event({clientY:520}));assert.equal(run('activeScreen'),'pin-screen');assert.equal(swipe.captures.size,0,'The swipe must release capture before PIN entry');}
function tap(value,id=2,compatibility=true){const button=key(value),down=event({pointerId:id});button.onpointerdown(down);assert.equal(down.prevented,true,'A number tap keeps focus on the PIN field');button.onpointerup(event({pointerId:id}));if(compatibility)button.onclick({detail:1,pointerType:'touch'});}
function clear(){tap('Clear');assert.equal(get('pin').value,'')}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function advance(ms){now+=ms;for(const [id,timer]of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}await flush()}
(async()=>{
 open();key('0').onclick({detail:1});
 assert.equal(get('pin').value,'0','Click-only accessibility activation must also enter the first zero');
 run('lock()');
 open();tap('0',2,false);
 assert.equal(get('pin').value,'0','The first touch works even when Chromium emits no compatibility click');
 key('0').onclick({detail:1,pointerType:'touch'});key('0').onclick({detail:0,pointerType:'touch'});
 assert.equal(get('pin').value,'0','A later click from the same touch must not repeat the digit');
 tap('0',3);assert.equal(get('pin').value,'00','Separate rapid zero taps each count once');
 tap('⌫');assert.equal(get('pin').value,'0');clear();
 key('1').onpointerdown(event());key('1').onpointerup(event({clientX:240}));key('1').onclick({detail:1,pointerType:'touch'});
 assert.equal(get('pin').value,'','Sliding off a key cancels the tap');
 for(const cancel of ['onpointercancel','onlostpointercapture']){
  key('2').onpointerdown(event());key('2')[cancel](event());key('2').onpointerup(event());key('2').onclick({detail:1,pointerType:'touch'});
  assert.equal(get('pin').value,'','Interrupted touches do not enter digits');
 }
 key('3').onpointerdown(event({isPrimary:false}));key('3').onpointerup(event({isPrimary:false}));
 key('3').onpointerdown(event({button:2}));key('3').onpointerup(event({button:2}));
 assert.equal(get('pin').value,'','Secondary touches and right clicks cannot enter digits');
 key('4').onpointerdown(event());run('lock()');assert.equal(key('4').captures.size,0);key('4').onpointerup(event());
 open();assert.equal(get('pin').value,'','Leaving PIN entry cancels an in-flight press');
 key('5').onclick({detail:0});assert.equal(get('pin').value,'5','Keyboard/assistive activation still works');clear();
 key('6').onpointerdown(event());run('busy=true');key('6').onpointerup(event());assert.equal(get('pin').value,'');run('busy=false');
 for(const digit of '0123')tap(digit);
 assert.equal(get('pin').value,'0123','Leading zero is preserved');
 assert.equal(requests.filter(r=>r.path==='/unlock').length,0,'A tap cannot authenticate before PIN entry settles');
 await advance(1000);
 assert.equal(requests.filter(r=>r.path==='/unlock').length,1);
 assert.equal(requests.find(r=>r.path==='/unlock').body.pin,'0123');
 assert.equal(run('activeScreen'),'home');
 assert.equal(requests.filter(r=>r.path.startsWith('/webhook/')).length,0,'Ordinary PIN unlock never changes alarm state');
 console.log('PIN touch: first zero after swipe, missing/duplicate clicks, rapid taps, drag/cancel, multi-touch, lock/busy races, keyboard, and single auto-submit passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
