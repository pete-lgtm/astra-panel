const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},timers=new Map(),requests=[];
function node(){let content='';return {dataset:{},children:[],hidden:false,scrollTop:0,open:false,setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]},append(...n){this.children.push(...n)},focus(){},showModal(){this.open=true},close(){this.open=false},get textContent(){return content},set textContent(v){content=v;this.children=[]}};}
const el=id=>nodes[id]??=node();
const zones=[
 {id:'a',groupId:'a',name:'Family Room',playback:'playing'},
 {id:'b',groupId:'a',name:'Kitchen',playback:'playing'},
 {id:'c',groupId:'c',name:'Guest Bathroom',playback:'stopped'},
 {id:'d',groupId:'d',name:'Bedroom',playback:'playing'},
 {id:'e',groupId:'d',name:'Study',playback:'playing'},
 {id:'f',groupId:'f',name:'Other household',groupScope:'elsewhere',playback:'stopped'}
];
let revision=1,hold=false,resolveGroup,fail=false;
function snapshot(){return {state:'ready',zones:zones.map(z=>({...z,groupScope:z.groupScope||'home',groupRevision:String(revision),groupName:zones.find(o=>o.id===z.groupId).name,
 groupMembers:zones.filter(o=>o.groupId===z.groupId).map(o=>({id:o.id,name:o.name})),groupRooms:zones.filter(o=>o.groupId===z.groupId).map(o=>o.name)}))};}
const ctx=vm.createContext({Map,Set,document:{hidden:false,addEventListener(){}},el,activeScreen:'audio',unlockToken:'grant',activity(){ctx.interactions++},interactions:0,
 text(id,s){el(id).textContent=s},hubNode(tag,cls,text=''){const n=node();n.className=cls;n.textContent=text;return n},
 setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
 async call(path,body){requests.push({path,body});if(path==='/hub/audio')return snapshot();
  assert.equal(path,'/hub/audio/group');if(hold)await new Promise(resolve=>resolveGroup=resolve);if(fail)throw Error('Some links may have changed. Refresh.');
  for(const z of zones){if(body.roomIds.includes(z.id))z.groupId=body.groupId;else if(z.groupId===body.groupId)z.groupId=z.id;}
  revision++;return {accepted:true,confirmed:true};
 }});
const run=s=>vm.runInContext(s,ctx),flush=()=>new Promise(resolve=>setImmediate(resolve));
const writes=()=>requests.filter(r=>r.path==='/hub/audio/group');
const option=name=>el('audio-group-options').children.find(row=>row.children[0]['aria-label']===name)?.children[0];
const toggle=(name,checked)=>{const input=option(name);assert.ok(input,name);input.checked=checked;input.onchange();};
run(fs.readFileSync('audio.js','utf8'));
(async()=>{
 await flush();assert.equal(el('audio-link-rooms').disabled,false);
 assert.equal(el('audio-rooms').children[0].children[1].children[2].textContent,'Linked: Kitchen');
 assert.match(el('audio-rooms').children[1]['aria-label'],/linked with Family Room/);
 el('audio-link-rooms').onclick();assert.equal(el('audio-group-dialog').open,true);
 assert.equal(option('Family Room · Audio source').disabled,true);assert.equal(option('Kitchen').checked,true);
 assert.equal(option('Other household'),undefined,'Only rooms from the same household can be chosen');
 assert.equal(option('Bedroom + Study').checked,false,'Existing groups are one explicit choice');
 assert.equal(el('audio-group-save').disabled,true,'No changes means no command');
 toggle('Guest Bathroom',true);toggle('Bedroom + Study',true);toggle('Kitchen',false);
 assert.equal(writes().length,0,'Selection stays local until Save links');assert.equal(el('audio-group-count').textContent,'4 rooms selected');
 hold=true;el('audio-group-save').onclick();el('audio-group-save').onclick();
 assert.equal(writes().length,1);assert.deepEqual(JSON.parse(JSON.stringify(writes()[0].body)),{unlock:'grant',roomId:'a',groupId:'a',revision:'1',roomIds:['a','c','d','e']});
 assert.equal(el('audio-group-close').disabled,true);assert.equal(option('Kitchen').disabled,true);
 resolveGroup();hold=false;await flush();assert.equal(el('audio-group-dialog').open,false);
 assert.equal(el('audio-group-detail').textContent,'Linked with Guest Bathroom · Bedroom · Study');
 assert.equal(el('audio-rooms').children[1].children[1].children[2].hidden,true,'Unlinked room no longer has a linked badge');
 el('audio-link-rooms').onclick();toggle('Kitchen',true);const original=option('Kitchen');revision++;await run('loadAudio()');
 assert.equal(option('Kitchen'),original,'Background polls preserve focused choices');assert.equal(el('audio-group-save').disabled,true);
 assert.match(el('audio-group-message').textContent,/changed/);el('audio-group-close').onclick();
 el('audio-link-rooms').onclick();toggle('Kitchen',true);fail=true;el('audio-group-save').onclick();await flush();
 const count=writes().length;assert.equal(el('audio-group-dialog').open,false);assert.match(el('audio-notice').textContent,/Some links/);
 for(const timer of [...timers.keys()])timer();await flush();assert.equal(writes().length,count,'No automatic retry after an uncertain result');
 fail=false;el('audio-link-rooms').onclick();toggle('Kitchen',true);hold=true;el('audio-group-save').onclick();
 ctx.unlockToken='';ctx.activeScreen='sleep';run('clearAudioHub()');assert.equal(el('audio-group-dialog').open,false);
 const before=requests.length;resolveGroup();hold=false;await flush();assert.equal(requests.length,before);assert.equal(timers.size,0);
 ctx.unlockToken='next';ctx.activeScreen='audio';await run('loadAudio()');ctx.liveObservation={};ctx.alarmPresentation=()=>({mode:'entry'});
 el('audio-link-rooms').onclick();assert.equal(el('audio-group-dialog').open,false,'A grouping modal cannot cover an active alarm countdown');
 console.log('Sonos grouping: source selection, linked badges, whole-group choices, stale reviews, one write, failures and lock races passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
