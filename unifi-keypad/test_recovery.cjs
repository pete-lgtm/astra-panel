const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('deploy/kiosk/recovery.js','utf8');
function harness(location={protocol:'http:',pathname:'/'}){
  const elements={},events={},timers=[];let now=0,healthy=false,instance='first',calls=0,reloads=0;
  const node=id=>elements[id]??={hidden:id==='keypad'||id==='notice'||id==='boot-continue',listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},contentWindow:{}};
  Object.defineProperty(node('keypad'),'src',{set(value){this.url=value;reloads++;}});
  const context=vm.createContext({document:{body:{dataset:{}},getElementById:node},location,
    window:{astraAurora:{start(){}},addEventListener(type,fn){events[type]=fn;}},
    performance:{now:()=>now},AbortController,setTimeout(fn,ms){timers.push({fn,ms});},clearTimeout(){},setInterval(){},
    fetch:async()=>{calls++;if(!healthy)throw Error('offline');return{ok:true,json:async()=>({service:'loxicom-keypad',processHealthy:true,instance})};}
  });
  vm.runInContext(source,context);
  return {context,elements,events,timers,setTime:value=>now=value,setHealthy:value=>healthy=value,setInstance:value=>instance=value,
    counts:()=>({calls,reloads}),poll:()=>vm.runInContext('poll()',context),
    loaded:()=>elements.keypad.listeners.load(),click:()=>elements['boot-continue'].listeners.click(),
    message:extra=>events.message({origin:'http://127.0.0.1:8765',source:elements.keypad.contentWindow,data:{type:'astra-startup',ready:true,urgent:true},...extra})};
}
async function flush(){await Promise.resolve();await Promise.resolve();await Promise.resolve();}
(async()=>{
  const h=harness();await flush();
  assert.equal(h.elements.keypad.hidden,true);
  h.setHealthy(true);h.setTime(2000);await h.poll();h.loaded();
  assert.equal(h.elements.keypad.hidden,true,'Healthy controls preload without ending the first 15-second intro');
  assert.equal(h.elements['boot-continue'].hidden,false,'The user can reach controls sooner');
  h.setTime(14999);await h.poll();assert.equal(h.elements.keypad.hidden,true);
  h.setTime(15000);h.timers.find(t=>t.ms===15000).fn();
  assert.equal(h.elements.keypad.hidden,false);assert.equal(h.elements.recovery.hidden,true);
  const reloads=h.counts().reloads;await h.poll();assert.equal(h.counts().reloads,reloads,'Health polling preserves a PIN entry');
  h.setInstance('second');await h.poll();assert.equal(h.elements.keypad.hidden,true);h.loaded();
  assert.equal(h.elements.keypad.hidden,false,'A service restart does not repeat the decorative delay');
  assert.equal(h.counts().reloads,reloads+1,'A new backend obtains a fresh session');
  h.setHealthy(false);await h.poll();assert.equal(h.elements.keypad.hidden,true);assert.equal(h.elements.notice.hidden,false);
  assert.equal(vm.runInContext('document.body.dataset.phase',h.context),'reconnecting');
  h.setHealthy(true);await h.poll();h.loaded();assert.equal(h.elements.keypad.hidden,false);
  assert.equal(h.counts().reloads,reloads+2,'Reconnection always returns to fresh locked controls');

  const early=harness();await flush();early.setHealthy(true);await early.poll();early.loaded();early.click();
  assert.equal(early.elements.keypad.hidden,false,'Open panel skips the intro');
  const urgent=harness();await flush();urgent.setHealthy(true);await urgent.poll();
  urgent.message({origin:'https://untrusted.example'});assert.equal(urgent.elements.keypad.hidden,true);
  urgent.message({source:{}});assert.equal(urgent.elements.keypad.hidden,true);
  urgent.message();assert.equal(urgent.elements.keypad.hidden,false,'An active alarm bypasses the 15-second animation');
  const offline=harness();await flush();offline.setTime(20000);await offline.poll();
  assert.equal(offline.elements.notice.hidden,false);assert.equal(offline.elements['boot-continue'].hidden,true);
  assert.equal(offline.elements.keypad.hidden,true,'Elapsed intro time never invents a healthy backend');
  const unloaded=harness();await flush();unloaded.setHealthy(true);unloaded.setTime(20000);await unloaded.poll();
  assert.equal(unloaded.elements.keypad.hidden,true,'A healthy process does not imply that its frame has loaded');
  for(const location of [{protocol:'file:',pathname:'/index.html'},{protocol:'http:',pathname:'/preview'}]){
    const preview=harness(location);assert.equal(preview.elements['preview-label'].hidden,false);
    assert.equal(preview.counts().calls,0);assert.equal(preview.timers.length,0);
  }
  console.log('Astra startup: 15-second intro, early access, active-alarm priority, outage recovery, trusted messages, session reload and isolated previews passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
