const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},timers=new Map(),requests=[];
function node(){let content='';return {dataset:{},children:[],hidden:false,scrollTop:0,setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]},append(...n){this.children.push(...n)},get textContent(){return content},set textContent(v){content=v;this.children=[]}};}
const el=id=>nodes[id]??=node();
let resolveRead,fail=false,hold=false;
const cover='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=',artPending=new Map();
let holdArt=false,badArt=false;
const zones=[{id:'a',name:'Family Room',playback:'playing',title:'TV audio',source:'TV',groupRooms:['Family Room','Kitchen']},{id:'b',name:'Kitchen',playback:'paused',title:'<img onerror=bad>',artist:'Artist',groupRooms:['Kitchen']}];
const ctx=vm.createContext({Map,document:{hidden:false,addEventListener(){}},el,
  activeScreen:'home',unlockToken:'grant',activity(){ctx.interactions++},interactions:0,
  text(id,s){el(id).textContent=s},hubNode(tag,cls,text=''){const n=node();n.className=cls;n.textContent=text;return n},
  setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
  async call(path,body){requests.push({path,body});if(path==='/hub/audio/artwork'){if(holdArt)await new Promise(resolve=>artPending.set(body.artworkId,resolve));return {artworkId:body.artworkId,image:badArt?'data:image/svg+xml;base64,AAAA':cover};}if(hold)await new Promise(resolve=>resolveRead=resolve);if(fail)throw Error('offline');return {state:'ready',zones:zones.map(z=>({...z}))}}
});
const run=s=>vm.runInContext(s,ctx),flush=()=>new Promise(resolve=>setImmediate(resolve));
run(fs.readFileSync('audio.js','utf8'));
(async()=>{
  assert.equal(requests.length,0,'No discovery outside Audio');
  ctx.activeScreen='audio';run('syncAudioScreen()');await flush();
  assert.equal(requests[0].path,'/hub/audio');assert.equal(el('audio-track-title').textContent,'TV audio');
  const rows=el('audio-rooms').children;rows[1].onclick();
  assert.equal(el('audio-track-title').textContent,'<img onerror=bad>','Metadata stays literal text');
  assert.equal(el('audio-rooms').children[1],rows[1],'Selection and refresh keep touch targets stable');
  assert.equal(ctx.interactions,1);
  await run('loadAudio()');assert.equal(ctx.interactions,1,'Polling never extends the idle grant');
  assert.equal(el('audio-track-title').textContent,'<img onerror=bad>','Selection preserved across polls');
  fail=true;await run('loadAudio()');assert.equal(el('audio-now-state').textContent,'Unavailable');
  assert.equal(el('audio-track-title').textContent,'Room unavailable','Stale song not presented as live');
  fail=false;hold=true;const pending=run('loadAudio()');
  ctx.unlockToken='';ctx.activeScreen='sleep';run('clearAudioHub()');resolveRead();await pending;
  assert.equal(el('audio-layout').hidden,true);assert.equal(el('audio-rooms').children.length,0);
  assert.equal(timers.size,0,'Late response cannot restart polling while locked');
  ctx.unlockToken='next';ctx.activeScreen='audio';hold=false;run('syncAudioScreen()');await flush();
  ctx.activeScreen='systems';run('syncAudioScreen()');assert.equal(timers.size,0,'Leaving Audio stops polling');
  ctx.activeScreen='audio';zones[0].artworkId='a'.repeat(64);zones[1].artworkId=zones[0].artworkId;
  zones[0].source='Music';zones[0].title='A song';run('syncAudioScreen()');await flush();
  assert.equal(requests.filter(r=>r.path==='/hub/audio/artwork').length,1,'Grouped rooms share one cover request');
  assert.equal(el('audio-cover').src,cover);el('audio-cover').onload();assert.equal(el('audio-cover').hidden,false);
  const artInteractions=ctx.interactions;await run('loadAudio()');await flush();
  assert.equal(requests.filter(r=>r.path==='/hub/audio/artwork').length,1,'Polling reuses the cover');
  assert.equal(ctx.interactions,artInteractions,'Artwork does not renew the idle grant');
  zones[0].artworkId='b'.repeat(64);zones[1].artworkId=null;holdArt=true;await run('loadAudio()');await flush();
  assert.equal(el('audio-cover').src,undefined,'Previous song cover disappears before next cover loads');
  zones[0].artworkId='c'.repeat(64);await run('loadAudio()');await flush();
  artPending.get('b'.repeat(64))();await flush();assert.equal(el('audio-cover').src,undefined,'Late old cover cannot replace the new song');
  el('audio-rooms').children[0].onclick();artPending.get('c'.repeat(64))();await flush();
  assert.equal(el('audio-cover').src,cover);el('audio-cover').onerror();assert.equal(el('audio-cover').hidden,true,'Broken image restores fallback');
  holdArt=false;badArt=true;zones[0].artworkId='d'.repeat(64);await run('loadAudio()');await flush();
  assert.equal(el('audio-cover').src,undefined,'Unsupported images rejected');assert.equal(el('audio-track-title').textContent,'A song');
  holdArt=true;badArt=false;zones[0].artworkId='e'.repeat(64);await run('loadAudio()');await flush();
  ctx.unlockToken='';ctx.activeScreen='sleep';run('clearAudioHub()');artPending.get('e'.repeat(64))();await flush();
  assert.equal(el('audio-cover').src,undefined,'Lock clears artwork and rejects late artwork responses');assert.equal(el('audio-rooms').children.length,0);
  assert.ok(requests.every(r=>['/hub/audio','/hub/audio/artwork'].includes(r.path)),'Audio only sends read requests');
  console.log('Audio navigation, live labels, stable touch targets, privacy/lock races and idle polling passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
