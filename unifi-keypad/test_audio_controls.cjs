const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},timers=new Map(),requests=[];
function node(){let content='';return {dataset:{},children:[],hidden:false,scrollTop:0,setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]},append(...n){this.children.push(...n)},get textContent(){return content},set textContent(v){content=v;this.children=[]}};}
const el=id=>nodes[id]??=node();
const zones=[
  {id:'a',groupId:'a',name:'Family Room',playback:'playing',source:'AirPlay',title:'A song',transportActions:['play','pause','stop'],volume:57,muted:false,groupRooms:['Family Room','Kitchen']},
  {id:'b',groupId:'a',name:'Kitchen',playback:'playing',source:'AirPlay',title:'A song',transportActions:['play','pause','stop'],volume:20,muted:false,groupRooms:['Family Room','Kitchen']},
  {id:'c',groupId:'c',name:'TV Room',playback:'playing',source:'TV',title:'TV audio',transportActions:[],volume:35,muted:false,fixedVolume:true,groupRooms:['TV Room']}
];
let holdControl=false,resolveControl,failControl=false,holdRead=false,resolveRead;
const ctx=vm.createContext({Map,document:{hidden:false,addEventListener(){}},el,
  activeScreen:'audio',unlockToken:'grant',activity(){ctx.interactions++},interactions:0,
  text(id,s){el(id).textContent=s},hubNode(tag,cls,text=''){const n=node();n.className=cls;n.textContent=text;return n},
  setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
  async call(path,body){
    requests.push({path,body});
    if(path==='/hub/audio/control'){
      if(holdControl)await new Promise(resolve=>resolveControl=resolve);
      if(failControl)throw Error('Sonos result unknown');
      const zone=zones.find(z=>z.id===body.roomId);
      if(body.action==='volume')zone.volume=body.volume;
      if(body.action==='mute')zone.muted=body.muted;
      if(body.action==='volume_up')zone.volume+=2;
      if(body.action==='volume_down')zone.volume-=2;
      if(['play','pause','stop'].includes(body.action))for(const z of zones.filter(z=>z.groupId===body.groupId))z.playback={play:'playing',pause:'paused',stop:'stopped'}[body.action];
      return {accepted:true};
    }
    assert.equal(path,'/hub/audio');
    const snapshot={state:'ready',zones:zones.map(z=>({...z}))};
    if(holdRead)await new Promise(resolve=>resolveRead=resolve);
    return snapshot;
  }
});
const run=s=>vm.runInContext(s,ctx),flush=()=>new Promise(resolve=>setImmediate(resolve));
const writes=()=>requests.filter(r=>r.path==='/hub/audio/control');
run(fs.readFileSync('audio.js','utf8'));
(async()=>{
  await flush();
  assert.equal(el('audio-source').textContent,'AirPlay');
  assert.equal(el('audio-play-label').textContent,'Pause');
  assert.equal(el('audio-volume-value').textContent,'57%');
  assert.equal(el('audio-play-pause')['aria-label'],'Pause Family Room group');
  el('audio-rooms').children[1].onclick();holdControl=true;
  el('audio-play-pause').onclick();el('audio-play-pause').onclick();el('audio-stop').onclick();
  assert.equal(writes().length,1,'Double taps and other buttons cannot duplicate an in-flight command');
  assert.deepEqual(JSON.parse(JSON.stringify(writes()[0].body)),{unlock:'grant',roomId:'b',groupId:'a',action:'pause'});
  assert.equal(el('audio-volume-slider').disabled,true);
  assert.equal(timers.size,0,'Polls stop during commands');
  resolveControl();holdControl=false;await flush();
  assert.equal(el('audio-play-label').textContent,'Play');
  assert.equal(el('audio-now-state').textContent,'Paused');
  el('audio-play-pause').onclick();await flush();assert.equal(writes().at(-1).body.action,'play');
  el('audio-stop').onclick();await flush();assert.equal(writes().at(-1).body.action,'stop');
  el('audio-mute').onclick();await flush();assert.equal(writes().at(-1).body.muted,true);
  assert.equal(el('audio-mute').textContent,'Unmute');assert.equal(el('audio-mute')['aria-pressed'],'true');
  el('audio-volume-up').onclick();await flush();assert.equal(zones[1].volume,22);
  el('audio-volume-down').onclick();await flush();assert.equal(zones[1].volume,20);
  const count=writes().length,slider=el('audio-volume-slider');
  slider.onpointerdown();slider.value='28';slider.oninput();slider.value='31';slider.oninput();
  await run('loadAudio()');assert.equal(slider.value,'31','Polling does not move the slider under a finger');
  assert.equal(writes().length,count,'Dragging does not flood the network with writes');
  slider.onchange();await flush();assert.equal(writes().length,count+1);assert.equal(writes().at(-1).body.volume,31);
  assert.equal(zones[0].volume,57,'Grouped playback never makes room volume a group volume command');
  slider.onpointerdown();slider.value='45';slider.oninput();el('audio-rooms').children[0].onclick();slider.onchange();await flush();
  assert.equal(writes().length,count+1,'A stale slider gesture cannot change a newly selected room');
  el('audio-rooms').children[2].onclick();
  assert.equal(el('audio-play-pause').disabled,true);assert.equal(el('audio-stop').disabled,true);
  assert.equal(slider.disabled,true,'Fixed outputs cannot be changed');
  assert.equal(el('audio-mute').disabled,false,'Mute capability remains independent');
  zones[2].playback='offline';await run('loadAudio()');assert.equal(el('audio-mute').disabled,true);
  el('audio-rooms').children[0].onclick();failControl=true;
  const failedBefore=writes().length;el('audio-play-pause').onclick();await flush();
  assert.equal(writes().length,failedBefore+1);assert.equal(el('audio-notice').textContent,'Sonos result unknown');
  for(const fn of [...timers.keys()])fn();await flush();assert.equal(writes().length,failedBefore+1,'Polling never retries a failed command');
  failControl=false;
  holdRead=true;const oldPoll=run('loadAudio()');holdControl=true;el('audio-play-pause').onclick();
  resolveRead();holdRead=false;await oldPoll;assert.equal(el('audio-play-pause').disabled,true,'A late poll cannot enable controls during a write');
  const requestCount=requests.length;ctx.unlockToken='';ctx.activeScreen='sleep';run('clearAudioHub()');resolveControl();holdControl=false;await flush();
  assert.equal(requests.length,requestCount,'A late command result cannot read private room state after locking');
  assert.equal(timers.size,0);assert.equal(el('audio-rooms').children.length,0);assert.equal(el('audio-play-pause').disabled,true);
  console.log('Audio controls: correct rooms/groups, single writes, volume gestures, capabilities, failed writes and lock races passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
