// Installer PIN flow with an isolated DOM and fake requests. No live PIN is used.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const elements={},listeners={};
const element=()=>({value:'',children:[],dataset:{},classList:{toggle(){}},
  append(...nodes){this.children.push(...nodes)},replaceChildren(){this.children=[]},
  setAttribute(){},removeAttribute(){},reportValidity(){return !!this.value},
  focus(){this.onfocus?.()},querySelector(){return element()},
  querySelectorAll(){return this.children}});
const get=id=>elements[id]??=element();
get('pin-touchpad').children=Array.from({length:10},(_,digit)=>Object.assign(element(),{dataset:{digit:String(digit)}}));
const context=vm.createContext({document:{getElementById:get,createElement:element,querySelector:element,addEventListener(){}},
  window:{scrollTo(){},addEventListener(name,handler){listeners[name]=handler}},
  performance:{now:()=>0},setTimeout(){},clearTimeout(){},fetch:()=>new Promise(()=>{})});
vm.runInContext(fs.readFileSync('setup_lab.js','utf8'),context);
const execute=code=>vm.runInContext(code,context);
const submit=()=>get('pin-form').onsubmit({preventDefault(){}});
function enter(pin,confirmation=pin){get('setup-pin').value=pin;get('confirm-pin').value=confirmation;}
function cleared(){assert.equal(get('setup-pin').value,'');assert.equal(get('confirm-pin').value,'');}
execute(`let sent=[];const fakePlan={name:'Test Home',doors:['Test Door'],profile:'Test',recipients:['Installer'],actions:[],message:'Ready'};
context={profiles:[{id:'test',title:'Test'}]};
call=async(path,body)=>{sent.push({path,body});return path==='pin/create'?{pinSet:true}:fakePlan};`);
get('home-name').value='Test Home';get('profile').value='test';
get('sensors').children=[{value:'door'}];get('recipients').children=[{value:'installer'}];
(async()=>{
  execute('renderPin({pinSet:false});showStep(3)');
  for(const [pin,confirmation] of [['12','12'],['1234','5678'],['123456789','123456789'],['12ab','12ab']]){
    enter(pin,confirmation);await submit();cleared();
    assert.equal(execute('sent.length'),0);
    assert.equal(get('review-page').hidden,true);
  }
  // Touch entry respects the length bound and switches between masked fields.
  for(let i=0;i<10;i++)get('pin-touchpad').children[4].onclick();
  assert.equal(get('setup-pin').value.length,8);
  get('pin-next-field').onclick();get('pin-touchpad').children[5].onclick();
  assert.equal(get('confirm-pin').value,'5');get('pin-delete').onclick();
  assert.equal(get('confirm-pin').value,'');
  enter('593274');await submit();cleared();
  assert.equal(execute('sent.map(x=>x.path).join(",")'),'pin/create,review');
  assert.equal(get('review-page').hidden,false);
  assert.equal(get('setup-pin').disabled,true);
  assert.equal(execute('JSON.stringify(sent[1].body).includes("593274")'),false);
  // Saved installations continue without sending another credential request.
  execute('sent=[];renderPin({pinSet:true});showStep(3)');
  await submit();
  assert.equal(execute('sent.map(x=>x.path).join(",")'),'review');
  assert.equal(get('existing-pin').hidden,false);
  assert.equal(get('setup-pin').required,false);
  // A rejected save cannot advance to review or leave a plaintext PIN behind.
  execute('sent=[];renderPin({pinSet:false});showStep(3);call=async()=>{throw new Error("Test rejection")}');
  enter('593274');await submit();cleared();
  assert.equal(get('review-page').hidden,true);
  assert.match(get('message').textContent,/Test rejection/);
  enter('593274');get('back-alerts').onclick();cleared();
  enter('593274');listeners.pagehide();cleared();
  console.log('Installer PIN validation, touch entry, saved-PIN reuse, and credential cleanup checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
