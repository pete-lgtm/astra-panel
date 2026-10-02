const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function harness(provider){
  const nodes=new Map(),timers=new Map(),requests=[],controls=[],frames=[];
  let now=10000,serial=0,staleReads=false,failReads=false;
  function node(){let content='';return {dataset:{},children:[],hidden:false,disabled:false,value:'',scrollTop:0,
    setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]},
    append(...items){for(const n of items){n.parent=this;this.children.push(n)}},
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this)},
    get textContent(){return content},set textContent(v){content=v;this.children=[]},
    focus(){},showModal(){this.open=true},close(){this.open=false},querySelectorAll(){return []}};}
  const el=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)};
  const zone={id:'a',groupId:'a',name:'Living room',playback:'playing',title:'Music',source:'AirPlay',volume:40,muted:false,transportActions:['play','pause','stop'],groupRooms:['Living room']};
  const lamp={id:'a',name:'Sofa lamp',on:true,brightness:40,reachable:true,dimmable:true};
  const spare={id:'b',name:'Reading lamp',on:true,brightness:65,reachable:true,dimmable:true};
  const snapshot=()=>provider==='audio'?{state:'ready',zones:[{...zone,volume:staleReads?40:zone.volume}]}:
    {state:'ready',rooms:[],scenes:[],lights:[{...lamp,brightness:staleReads?40:lamp.brightness}, {...spare}]};
  const ctx=vm.createContext({Map,Set,Number,Date:class extends Date{static now(){return now}},
    document:{hidden:false,addEventListener(){}},el,activeScreen:provider,unlockToken:'grant',activity(){},
    text(id,s){el(id).textContent=s},hubNode(tag,cls,s=''){const n=node();n.className=cls;n.textContent=s;return n},
    setTimeout(fn,ms){const id=++serial;timers.set(id,{fn,at:now+ms});return id},clearTimeout(id){timers.delete(id)},
    async call(path,body){
      requests.push({path,body});
      if(path.endsWith('/control'))return new Promise((resolve,reject)=>controls.push({body,finish(error){
        if(error){reject(Error(error));return;}
        if(provider==='audio')zone.volume=body.volume;
        else {const light=body.id==='a'?lamp:spare;light.brightness=body.value;light.on=body.value>0;}
        resolve({accepted:true});
      }}));
      assert.equal(path,'/hub/'+provider);
      if(failReads)throw Error('Device offline');return snapshot();
    }
  });
  const run=s=>vm.runInContext(s,ctx);
  run(fs.readFileSync('level-control.js','utf8'));
  run(fs.readFileSync(provider+'.js','utf8'));
  const slider=()=>provider==='audio'?el('audio-volume-slider'):run("lightingCards.get('a')?.slider");
  const output=()=>provider==='audio'?el('audio-volume-value'):run("lightingCards.get('a').value");
  function frame(){if(slider())frames.push({value:slider().value,disabled:slider().disabled,label:output().textContent});}
  const render=provider==='audio'?'renderAudioControls':'renderLighting';
  ctx.record=frame;
  run(`{const original=${render};${render}=()=>{original();record();};}`);
  return {ctx,run,el,controls,frames,timers,requests,zone,lamp,spare,slider,output,
    read:()=>run(provider==='audio'?'loadAudio()':'loadLighting()'),
    stale(value){staleReads=value},failRead(value){failReads=value},
    async advance(ms){now+=ms;for(const [id,t] of [...timers])if(t.at<=now){timers.delete(id);t.fn();}await flush()},
    drag(value,release=false){const s=slider();s.onpointerdown();s.value=String(value);s.oninput();if(release)s.onchange()},
    lock(){ctx.unlockToken='';ctx.activeScreen='sleep';run(provider==='audio'?'clearAudioHub()':'clearLightingHub()')},
    unlock(){ctx.unlockToken='new-grant';ctx.activeScreen=provider;run(provider==='audio'?'syncAudioScreen()':'syncLightingScreen()')},
    notice:()=>el(provider==='audio'?'audio-notice':'lighting-notice').textContent
  };
}
(async()=>{
  for(const provider of ['audio','lighting']){
    const h=harness(provider);await flush();
    assert.equal(h.slider().value,'40');
    h.drag(70);assert.equal(h.output().textContent,'70%');
    await h.advance(149);assert.equal(h.controls.length,0,'Brief movements are combined');
    await h.advance(1);assert.equal(h.controls.length,1,'Device starts changing before release');
    h.drag(20);h.drag(80);h.drag(100,true);
    assert.equal(h.slider().value,'100');assert.equal(h.slider().disabled,false,'Slider stays touchable during writes');
    assert.equal(h.controls.length,1,'Only one request can be in flight');
    await h.advance(500);assert.equal(h.controls.length,1);
    h.frames.length=0;h.controls[0].finish();await flush();
    await h.advance(150);assert.equal(h.controls.length,2);
    const sent=h.controls.map(x=>provider==='audio'?x.body.volume:x.body.value);
    assert.deepEqual(sent,[70,100],'The next write uses the newest position, without intermediate backlog');
    h.stale(true);h.controls[1].finish();await flush();
    assert.equal(h.slider().value,'100','An old device read cannot undo the requested level');
    assert.match(h.slider()['aria-valuetext'],/updating/);
    assert.ok(h.frames.every(f=>f.value==='100'&&f.label==='100%'&&!f.disabled),'Every intermediate render is stable');
    if(provider==='lighting')assert.equal(h.run("lightingCards.get('b').slider.value"),'65','Other lights never blank');
    h.stale(false);await h.advance(1000);
    assert.equal(h.slider().value,'100');assert.doesNotMatch(h.slider()['aria-valuetext'],/updating/);
    h.zone.volume=33;h.lamp.brightness=33;await h.read();assert.equal(h.slider().value,'33','External changes appear after confirmation');
    h.slider().onpointerdown();h.slider().onpointerup();
    h.zone.volume=34;h.lamp.brightness=34;await h.read();
    assert.equal(h.slider().value,'34','Touching without moving cannot leave the slider frozen');
    h.zone.volume=33;h.lamp.brightness=33;await h.read();

    // Keyboard ranges emit input/change without a pointerdown.
    h.slider().value='64';h.slider().oninput();h.slider().onchange();
    h.drag(67,true);assert.equal(h.slider().value,'67');
    const failedCount=h.controls.length;
    h.controls.at(-1).finish('Could not set level');await flush();
    assert.equal(h.slider().value,'33');assert.match(h.notice(),/Could not set level/);
    await h.advance(10000);assert.equal(h.controls.length,failedCount,'Uncertain writes and queued movements are never retried');

    // A non-confirming device eventually wins; the UI never claims indefinite success.
    h.drag(88,true);h.stale(true);h.controls.at(-1).finish();await flush();
    assert.equal(h.slider().value,'88');await h.advance(6001);
    assert.equal(h.slider().value,'40');assert.match(h.notice(),/not confirmed/);
    h.stale(false);await h.read();

    h.drag(0,true);assert.equal(h.slider().value,'0','Zero remains a real requested value');
    h.controls.at(-1).finish();await flush();assert.equal(h.slider().value,'0');
    h.drag(51,true);h.failRead(true);h.controls.at(-1).finish('Result unknown');await flush();
    assert.equal(h.slider().disabled,true,'A failed verification read disables unknown/offline controls');
    h.failRead(false);await h.read();

    h.drag(76);const beforeLock=h.controls.length;h.lock();await h.advance(1000);
    assert.equal(h.controls.length,beforeLock,'Lock cancels unsent changes');assert.equal(h.timers.size,0);
    h.unlock();await flush();h.drag(30,true);h.drag(90,true);const inFlight=h.controls.length;
    h.lock();h.controls.at(-1).finish();await flush();await h.advance(1000);
    assert.equal(h.controls.length,inFlight,'Lock cancels trailing writes behind a slow command');assert.equal(h.timers.size,0);
    assert.equal(h.requests.filter(r=>r.path.endsWith('/control')).at(-1).body.unlock,'new-grant');
    // Reopening waits for fresh state; a prior view's failed reply is ignored.
    h.unlock();await flush();h.drag(20,true);const old=h.controls.at(-1);
    h.lock();h.unlock();await flush();const beforeReturn=h.controls.length;
    assert.equal(h.controls.length,beforeReturn);old.finish('Old session ended');await flush();
    h.drag(60,true);
    assert.equal(h.controls.length,beforeReturn+1);assert.equal(h.slider().value,'60');
    h.controls.at(-1).finish();await flush();assert.equal(h.slider().value,'60');
    if(provider==='lighting'){
      h.drag(45,true);
      const second=h.run("lightingCards.get('b').slider");second.value='15';second.oninput();second.onchange();
      assert.equal(second.value,'15');h.controls.at(-1).finish();await flush();await h.advance(150);
      assert.equal(h.controls.at(-1).body.id,'b','Each queued light keeps its own identity');
      h.controls.at(-1).finish();await flush();assert.equal(second.value,'15');assert.equal(h.slider().value,'45');
    }
  }
  console.log('Both sliders: immediate drag feedback, serialized/coalesced writes, no reset/dim frames, delayed confirmation, failures, external changes, zero and PIN races passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
