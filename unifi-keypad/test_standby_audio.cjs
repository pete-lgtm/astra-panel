const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},timers=new Map(),requests=[];
const el=id=>nodes[id]??={hidden:false,disabled:false,dataset:{},textContent:'',setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]}};
let holdRead=false,resolveRead,failWrite=false,failRead=false;
const zone={id:'room-a',groupId:'room-a',name:'Family Room',source:'Music',title:'A song',artist:'An artist',playback:'playing',volume:35,transportActions:['play','pause','next']};
const context=vm.createContext({el,Map,Date,Math,document:{hidden:false,body:{dataset:{alarmMode:'disarmed'}},addEventListener(){}},
  activeScreen:'sleep',token:'local-session',pinConfigured:true,unlockToken:'',
  text(id,value){el(id).textContent=value},musicText(id,value){el(id).textContent=value},audioCoverKey(){return ''},
  setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
  async call(path,body){
    requests.push({path,body});
    if(path==='/standby/audio'){
      if(failRead)throw Error('Offline');
      const value={zone:structuredClone(zone),mediaToken:'music-only'};
      if(holdRead)await new Promise(resolve=>resolveRead=resolve);
      return value;
    }
    assert.equal(path,'/standby/audio/control');
    assert.equal(body.mediaToken,'music-only');assert.equal(body.unlock,undefined);
    if(failWrite)throw Error('Unknown result');
    if(body.action==='volume')zone.volume=body.volume;
    if(body.action==='pause')zone.playback='paused';
    if(body.action==='play')zone.playback='playing';
    if(body.action==='next')zone.title='Next song';
    return {accepted:true};
  }
});
const run=code=>vm.runInContext(code,context),flush=()=>new Promise(resolve=>setImmediate(resolve));
const writes=()=>requests.filter(r=>r.path==='/standby/audio/control');
run(fs.readFileSync('level-control.js','utf8'));run(fs.readFileSync('standby-audio.js','utf8'));
(async()=>{
  await flush();assert.equal(el('standby-music').hidden,false);assert.equal(el('standby-track-title').textContent,'A song');
  assert.equal(context.unlockToken,'','Music discovery never grants panel access');
  el('standby-play').onclick();await flush();
  assert.equal(zone.playback,'paused');assert.equal(el('standby-music').hidden,false);assert.equal(el('standby-play')['aria-label'],'Play Family Room');
  el('standby-play').onclick();await flush();assert.equal(zone.playback,'playing');
  el('standby-next').onclick();await flush();assert.equal(el('standby-track-title').textContent,'Next song');
  el('standby-volume-up').onclick();el('standby-volume-up').onclick();
  assert.equal(el('standby-volume').textContent,'39%','Volume responds immediately and coalesces rapid taps');
  run('standbyLevels.flush()');await flush();assert.equal(zone.volume,39);
  assert.equal(writes().filter(r=>r.body.action==='volume').length,1);
  zone.transportActions=['play','pause'];await run('loadStandbyAudio()');assert.equal(el('standby-next').hidden,true);
  const count=writes().length;el('standby-next').onclick();await flush();assert.equal(writes().length,count);
  failWrite=true;el('standby-play').onclick();await flush();assert.equal(el('standby-music-notice').textContent,'Unknown result');
  assert.equal(writes().length,count+1);await run('loadStandbyAudio()');assert.equal(writes().length,count+1,'Polling does not replay an uncertain write');failWrite=false;
  holdRead=true;const pending=run('loadStandbyAudio()');
  context.activeScreen='pin-screen';run('syncStandbyAudio()');resolveRead();holdRead=false;await pending;
  assert.equal(el('standby-music').hidden,true);assert.equal(run('standbyData'),null);assert.equal(timers.size,0,'PIN entry stops music work');
  context.activeScreen='sleep';run('syncStandbyAudio()');await flush();assert.equal(el('standby-music').hidden,false);
  context.document.body.dataset.alarmMode='entry';run('syncStandbyAudio()');assert.equal(el('standby-music').hidden,true);assert.equal(el('sleep').dataset.music,'false');
  assert.equal(el('standby-play').disabled,true);assert.equal(timers.size,0,'Alarm countdown has priority over music');
  context.document.body.dataset.alarmMode='disarmed';run('syncStandbyAudio()');await flush();
  failRead=true;await run('loadStandbyAudio()');assert.equal(el('standby-music').hidden,true,'Offline reads do not leave stale live controls');
  assert.equal(context.unlockToken,'');assert(requests.every(r=>r.path.startsWith('/standby/audio')));
  console.log('Lock-screen music: immediate controls, pause/resume, capabilities, PIN isolation, late results and alarm priority passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
