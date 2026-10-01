const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},requests=[],timers=[];
function element(){return {value:'',hidden:false,children:[],dataset:{},classList:{toggle(){}},append(...items){this.children.push(...items)},replaceChildren(...items){this.children=[...items]},setAttribute(){},focus(){},scrollIntoView(){}};}
const el=id=>nodes[id]??=element();
const owner={id:'owner',name:'Owner',role:'admin',enabled:true,version:1};
let users=[owner],nextResult,resolveHeld;
const sensors={connected:true,selectedIds:['old'],sensors:[{id:'old',name:'Front',state:'MISSING',isOpened:null,available:false},{id:'new',name:'Front replacement',state:'CONNECTED',isOpened:false,available:true},{id:'bad',name:'Faulty',state:'CONNECTED',isOpened:null,available:false}],message:'Choose doors',observation:{fresh:true,state:'disarmed',liveDelayEnabled:false,doors:{Front:{connected:true,position:'closed'}},verifiedDoors:[]}};
const sirens={connected:true,sirenIds:[],sirenDuration:30,sirenDurations:[5,10,20,30],sirens:[{id:'s1',name:'Hall siren',available:true,state:'CONNECTED'},{id:'bad',name:'Offline',available:false,state:'DISCONNECTED'}],message:'Choose sirens'};
const context=vm.createContext({document:{getElementById:el,createElement:element,querySelectorAll:()=>[]},performance:{now:()=>1},setInterval:fn=>timers.push(fn),Date,Set,Math,
  settingsOpen:true,settingsGeneration:1,liveObservation:null,
  text:(id,value)=>{el(id).textContent=value},el,
  message(value){el('message').textContent=value},
  lock(){context.settingsOpen=false;context.settingsGeneration++;vm.runInContext('clearSettingsManagement()',context)},
  acceptObservation(value){context.liveObservation=value},
  async call(path,body={}){
    requests.push({path,body:JSON.parse(JSON.stringify(body))});
    if(nextResult){const fn=nextResult;nextResult=null;return fn();}
    if(path==='/settings/sirens')return sirens;
    if(path==='/settings/sirens/review')return {planId:'siren-plan',sirens:['Hall siren'],sirenDuration:30};
    if(path==='/settings/sirens/apply'){sirens.sirenIds=['s1'];return {saved:true};}
    if(path==='/settings/summary')return {user:owner,users:users.length,managed:true,doors:['Front'],apiConnected:true,localConnected:true};
    if(path==='/settings/users')return {users};
    if(path==='/settings/users/save'){const user={...body,id:body.id||'new-person'};delete user.pin;delete user.confirmation;users.push(user);return {user,reauthenticate:body.id==='owner'};}
    if(path==='/settings/activity')return {events:[{id:1,user_name:'<script>fake</script>',action:'disarm',status:'accepted',occurred_at:'2026-09-25T05:00:00Z',detail:'Waiting for Protect'}]};
    if(path==='/settings/sensors')return sensors;
    if(path==='/settings/sensors/review')return {planId:'test-plan',doors:['Front replacement'],added:['Front replacement'],removed:['Front']};
    if(path==='/settings/sensors/apply'){sensors.selectedIds=['new'];return {saved:true};}
    throw Error('Unexpected test request '+path);
  }});
const run=code=>vm.runInContext(code,context);
run(fs.readFileSync('management.js','utf8'));
const combined=node=>[node.textContent||'',...node.children.map(combined)].join(' ');
(async()=>{
  await run("openSettingsSection('sirens')");
  assert.equal(el('siren-list').children[0].children[0].checked,false,'Scanning must not select a new siren');
  assert.equal(el('siren-list').children[1].children[0].disabled,true);
  assert.equal(el('review-sirens').disabled,true);
  const check=el('siren-list').children[0].children[0];check.checked=true;check.onchange();
  assert.equal(el('review-sirens').disabled,false);
  assert.equal(requests.filter(r=>r.path.endsWith('/apply')).length,0);
  await el('refresh-sirens').onclick();
  assert.equal(el('siren-list').children[0].children[0].checked,true,'Rescan keeps unsaved selection');
  await el('review-sirens').onclick();
  assert.equal(el('siren-review').hidden,false);assert.match(combined(el('siren-review-detail')),/30 seconds/);
  assert.equal(requests.filter(r=>r.path.endsWith('/apply')).length,0);
  el('cancel-sirens').onclick();assert.equal(el('siren-review').hidden,true);
  await el('review-sirens').onclick();await el('save-sirens').onclick();
  assert.equal(requests.filter(r=>r.path==='/settings/sirens/apply').length,1);
  assert.equal(el('review-sirens').disabled,true);
  assert.equal(el('siren-review').hidden,true);
  nextResult=()=>new Promise(resolve=>{resolveHeld=resolve});
  const late=el('refresh-sirens').onclick();context.lock();resolveHeld(sirens);await late;
  assert.deepEqual(el('siren-list').children,[],'Late scans cannot repopulate locked settings');
  context.settingsOpen=true;await run("openSettingsSection('sirens')");
  assert.equal(el('refresh-sirens').disabled,false,'Re-entering clears stale busy state');
  console.log('Siren UI scan, explicit selection, review/save, offline device and lock-race checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
