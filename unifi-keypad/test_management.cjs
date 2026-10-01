const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},requests=[],timers=[];
function element(){return {value:'',hidden:false,children:[],dataset:{},classList:{toggle(){}},append(...items){this.children.push(...items)},replaceChildren(...items){this.children=[...items]},setAttribute(){},focus(){},scrollIntoView(){}};}
const el=id=>nodes[id]??=element();
const owner={id:'owner',name:'Owner',role:'admin',enabled:true,version:1};
let users=[owner],nextResult,resolveHeld;
const sensors={connected:true,selectedIds:['old'],sensors:[{id:'old',name:'Front',state:'MISSING',isOpened:null,available:false},{id:'new',name:'Front replacement',state:'CONNECTED',isOpened:false,available:true},{id:'bad',name:'Faulty',state:'CONNECTED',isOpened:null,available:false}],message:'Choose doors',observation:{fresh:true,state:'disarmed',liveDelayEnabled:false,doors:{Front:{connected:true,position:'closed'}},verifiedDoors:[]}};
const context=vm.createContext({document:{getElementById:el,createElement:element,querySelectorAll:()=>[]},performance:{now:()=>1},setInterval:fn=>timers.push(fn),Date,Set,Math,
  settingsOpen:true,settingsGeneration:1,liveObservation:null,
  text:(id,value)=>{el(id).textContent=value},el,
  message(value){el('message').textContent=value},
  lock(){context.settingsOpen=false;context.settingsGeneration++;vm.runInContext('clearSettingsManagement()',context)},
  acceptObservation(value){context.liveObservation=value},
  async call(path,body={}){
    requests.push({path,body:JSON.parse(JSON.stringify(body))});
    if(nextResult){const fn=nextResult;nextResult=null;return fn();}
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
  await run('openSettingsSection()');
  assert.equal(el('entry-setup').hidden,true,'Legacy test controls stay out of managed settings');
  assert.equal(el('advanced-api').open,false);assert.equal(el('reconnect-details').open,false);
  await el('manage-users').onclick();el('add-user').onclick();
  assert.equal(el('user-editor').hidden,false);assert.equal(el('user-pin').required,true);
  el('user-name').value='Pat';el('user-role').value='user';el('user-pin').value='472951';el('user-pin-confirm').value='472951';
  await el('user-editor').onsubmit({preventDefault(){}});
  assert.equal(el('user-pin').value,'');assert.equal(el('user-pin-confirm').value,'');
  assert.match(combined(el('user-list')),/Pat/);assert.equal(el('user-editor').hidden,true);
  run('editUser(settingsUser)');assert.equal(el('user-pin').required,false);assert.equal(el('user-pin').value,'');
  el('user-name').value='Owner renamed';el('user-role').value='admin';
  await el('user-editor').onsubmit({preventDefault(){}});
  assert.equal(context.settingsOpen,false,'Editing the current user requires a fresh PIN');
  assert.equal(el('user-name').value,'');assert.deepEqual(el('user-list').children,[]);
  context.settingsOpen=true;
  await run("openSettingsSection('activity')");
  assert.match(combined(el('activity-list')),/Awaiting confirmation/);
  assert.match(combined(el('activity-list')),/<script>fake<\/script>/,'User content is literal text');
  assert.equal(el('activity-list').innerHTML,undefined);
  await run("openSettingsSection('sensors')");
  assert.equal(el('sensor-list').children[2].children[1].children[0].children[0].disabled,true,'Unknown sensor is unavailable');
  const replace=el('sensor-list').children[0].children[1].children[1];replace.value='new';replace.onchange();
  assert.equal(el('review-sensors').disabled,false);
  assert.equal(requests.some(r=>r.path==='/settings/sensors/apply'),false,'Replacement is only local until reviewed and saved');
  await el('review-sensors').onclick();assert.match(combined(el('sensor-review-detail')),/Remove: Front/);
  await el('save-sensors').onclick();
  assert.equal(requests.filter(r=>r.path==='/settings/sensors/apply').length,1);
  assert.equal(el('enable-sensors').disabled,true,'Replacement needs its own physical test');
  context.liveObservation={fresh:true,state:'disarmed',liveDelayEnabled:false,doors:{'Front replacement':{connected:true,position:'closed'}},verifiedDoors:['Front replacement']};
  timers[0]();assert.equal(el('enable-sensors').disabled,false);
  context.liveObservation.state='armed';timers[0]();assert.equal(el('enable-sensors').disabled,true);
  nextResult=()=>new Promise(resolve=>resolveHeld=resolve);
  const late=run("openSettingsSection('users')");context.lock();resolveHeld({users:[owner]});await late;
  assert.deepEqual(el('user-list').children,[],'Late responses cannot restore settings after lock');
  console.log('Management: user edits, PIN clearing, self-change lock, activity status, sensor review/replacement and stale-response checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
