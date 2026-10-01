const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},requests=[];
function make(tag='div'){
  return {tagName:tag,value:'',disabled:false,hidden:false,dataset:{},children:[],selectionStart:null,selectionEnd:null,
    classList:{toggle(){}},setAttribute(k,v){this[k]=v},append(...items){this.children.push(...items)},
    querySelectorAll(selector){const found=[];const visit=node=>{for(const child of node.children||[]){if(selector==='button'&&child.tagName==='button')found.push(child);visit(child)}};visit(this);return found},
    focus(){},setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end},showModal(){this.open=true},close(){this.open=false},
    getBoundingClientRect(){return {left:0,top:0,right:60,bottom:44}},
    set textContent(value){this.valueText=value;this.children=[]},get textContent(){return (this.valueText||'')+this.children.map(n=>n.textContent).join(' ')}};
}
const get=id=>nodes[id]??=make();let now=1000;
const ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(){}},Date,Math,
  performance:{now:()=>now},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},clearTimeout(){}});
const run=s=>vm.runInContext(s,ctx);
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
ctx.device={id:'fixture-ap',name:'Office AP',kind:'accessPoint',controls:{rename:true,restart:true}};
let review={planId:'review',target:'Office AP',effect:'Rename the device.'};
ctx.fakeCall=async(path,data)=>{requests.push({path,data});if(path==='/hub/network/review')return review;if(path==='/lock')return {};throw new Error('Unexpected write: '+path)};
run('call=fakeCall');
const start=()=>run("unlockToken='grant';networkLoading=false;networkInventory={actionsAvailable:true,site:{id:'default'}};show('systems');selectSystemsTab('network');openEquipmentAction(device,device,'rename_device')");
const key=k=>get('equipment-keyboard').querySelectorAll('button').find(b=>b.dataset.key===k);
const tap=k=>key(k).onclick({detail:0});
const input=get('equipment-name'),event={preventDefault(){}};
(async()=>{
  start();assert.equal(input.value,'Office AP');assert.equal(input.selectionStart,0);assert.equal(input.selectionEnd,9);
  assert.equal(get('equipment-keyboard').hidden,false);
  const first=key('g'),pointer={pointerId:1,isPrimary:true,button:0,clientX:20,clientY:20,preventDefault(){}};
  first.onpointerdown(pointer);assert.equal(input.value,'Office AP','A press waits for release inside its key');
  first.onpointerup(pointer);first.onclick({detail:1,pointerType:'touch'});
  assert.equal(input.value,'G','First tap replaces the selection exactly once');
  tap('u');tap('e');tap('s');tap('t');tap('space');tap('shift');tap('a');tap('shift');tap('p');
  assert.equal(input.value,'Guest AP');assert.equal(get('equipment-name-count').textContent,'8 / 64');
  tap('symbols');tap('-');tap('2');assert.equal(input.value,'Guest AP-2');
  tap('left');tap('backspace');assert.equal(input.value,'Guest AP2','Backspace edits at the cursor, not only at the end');
  tap('-');tap('right');assert.equal(input.value,'Guest AP-2');assert.equal(input.selectionStart,10);
  input.setSelectionRange(0,5);tap('clear');assert.equal(input.value,'','Clear removes the whole name');
  assert.equal(get('equipment-submit').disabled,true,'An empty name cannot be reviewed');
  await get('equipment-form').onsubmit(event);assert.equal(requests.length,0);
  tap('symbols');tap('shift');tap('n');assert.equal(input.value,'N');
  input.value='AP 👋';input.setSelectionRange(5,5);tap('backspace');assert.equal(input.value,'AP ','Backspace removes a complete Unicode character');
  input.value='A'.repeat(64);input.setSelectionRange(64,64);tap('a');assert.equal(input.value.length,64);
  input.setSelectionRange(0,64);tap('a');assert.equal(input.value,'a','Replacing selected text still works at the length limit');
  tap('p');await get('equipment-form').onsubmit(event);
  assert.equal(requests[0].path,'/hub/network/review');assert.equal(requests[0].data.request.name,'ap');
  assert.equal(get('equipment-submit').textContent,'Save name');
  tap('space');assert.equal(run('equipmentAction.planId'),null,'Touch edits invalidate the reviewed name');
  assert.equal(get('equipment-submit').textContent,'Review name');
  let resolve;review=new Promise(r=>resolve=r);const pending=get('equipment-form').onsubmit(event);
  assert.equal(key('a').disabled,true);const value=input.value;tap('a');assert.equal(input.value,value,'No edit while review is in flight');
  resolve({planId:'new',target:'Office AP',effect:'Rename reviewed'});await pending;
  assert.equal(key('a').disabled,false);
  const before=input.value,cancelled=key('x');now+=1000;cancelled.onpointerdown(pointer);
  cancelled.onpointerup({...pointer,clientX:100});cancelled.onclick({detail:1,pointerType:'touch'});
  assert.equal(input.value,before,'Sliding off a key does not type');
  cancelled.onpointerdown(pointer);cancelled.onpointercancel(pointer);cancelled.onclick({detail:1,pointerType:'touch'});
  assert.equal(input.value,before,'Cancelled touches do not type');
  run('lock()');assert.equal(get('equipment-dialog').open,false);assert.equal(input.value,'');
  assert.equal(get('equipment-keyboard').hidden,true);assert.equal(get('equipment-keyboard').children.length,0);
  assert.equal(get('equipment-name-count').textContent,'');
  start();await run("openEquipmentAction(device,device,'restart_device')");
  assert.equal(get('equipment-keyboard').hidden,true,'Reboot keeps its normal confirmation dialog');
  assert.ok(requests.every(r=>r.path!=='/hub/network/apply'),'Typing and reviewing never rename a real device');
  console.log('Rename keyboard: first touch, replacement/selection, case/symbols, cursor edits, length limit, reviewed-name invalidation, busy/cancel/lock and reboot isolation passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
