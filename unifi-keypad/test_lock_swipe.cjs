const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},requests=[];let now=0;
const node=()=>({value:'',hidden:false,dataset:{},children:[],style:{setProperty(){}},classList:{toggle(){}},querySelectorAll(){return []},append(...items){this.children.push(...items)},setAttribute(){},focus(){},setPointerCapture(){},close(){this.open=false;}});
const get=id=>nodes[id]??=node();
const ctx=vm.createContext({document:{body:node(),getElementById:get,createElement:node,addEventListener(){}},Math,Date,performance:{now:()=>now},setInterval(){},setTimeout(){},clearTimeout(){},fetch:()=>new Promise(()=>{})});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
const run=s=>vm.runInContext(s,ctx);
ctx.fakeCall=async(path)=>{requests.push(path);return {};};
run("call=fakeCall;render({connected:true,pinSet:true,webhookReady:true});acceptObservation({fresh:true,state:'disarmed',remaining:null,notification:'not_sent'});");
const swipe=get('swipe-unlock');
const event=(x=200,y=650,extra={})=>({pointerId:1,button:0,isPrimary:true,clientX:x,clientY:y,preventDefault(){},...extra});
function gesture(dx,dy,extra={},delay=300){swipe.onpointerdown(event(200,650,extra));now+=delay;swipe.onpointermove(event(200+dx,650+dy,extra));swipe.onpointerup(event(200+dx,650+dy,extra));}
function locked(){assert.equal(run('activeScreen'),'sleep');assert.equal(run('unlockToken'),'');}
assert.equal(get('wake').hidden,true);assert.equal(swipe.hidden,false);
for(const [dx,dy]of [[0,0],[0,-25],[90,0],[0,100],[100,-70]]){gesture(dx,dy);locked();}
swipe.onclick({detail:1});locked();
gesture(0,-100,{button:2});locked();gesture(0,-100,{isPrimary:false});locked();
gesture(0,-100,{},2000);locked();
swipe.onpointerdown(event());swipe.onpointercancel();swipe.onpointerup(event(200,530));locked();
swipe.onpointerdown(event());swipe.onlostpointercapture();swipe.onpointerup(event(200,530));locked();
swipe.onpointerdown(event());swipe.onpointerup(event(200,530,{pointerId:2}));locked();swipe.onpointercancel();
gesture(5,-90);assert.equal(run('activeScreen'),'pin-screen');assert.equal(run('unlockToken'),'');
assert.equal(requests.length,0,'A swipe only opens PIN entry; it never authenticates or sends an alarm command');
get('back').onclick();locked();
run('busy=true');gesture(0,-90);locked();run('busy=false');
swipe.onkeydown({key:'Enter',preventDefault(){}});assert.equal(run('activeScreen'),'pin-screen');get('back').onclick();
swipe.onkeydown({key:' ',preventDefault(){}});assert.equal(run('activeScreen'),'pin-screen');get('back').onclick();
swipe.onclick({detail:0});assert.equal(run('activeScreen'),'pin-screen','Assistive activation remains available');get('back').onclick();
run("acceptObservation({fresh:false,state:'unknown',remaining:null,notification:'not_sent'})");
gesture(0,-90);assert.equal(run('activeScreen'),'pin-screen','Connection loss cannot prevent PIN entry');get('back').onclick();
run("acceptObservation({fresh:true,state:'armed',remaining:null,notification:'not_sent'})");
gesture(0,-90);assert.equal(run('activeScreen'),'pin-screen');assert.equal(run('disarmOnUnlock'),false,'An ordinary armed lock-screen swipe does not disarm');get('back').onclick();
swipe.onpointerdown(event());
run("acceptObservation({fresh:true,state:'breached',remaining:40,notification:'not_sent'})");
assert.equal(swipe.hidden,true);assert.equal(get('wake').hidden,false);swipe.onpointerup(event(200,530));locked();
get('wake').onclick();assert.equal(run('activeScreen'),'pin-screen');assert.equal(run('disarmOnUnlock'),true,'Urgent alarms retain explicit PIN-protected disarm');
console.log('Lock swipe: direction, distance, cancellation, busy/urgent races, keyboard access and PIN-only authorization passed.');
