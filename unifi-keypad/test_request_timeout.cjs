const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const elements={},timeouts=new Set();
const element=()=>({dataset:{},classList:{toggle(){}},querySelectorAll(){return []},append(){},setAttribute(){},value:'',focus(){}});
let abortRequests=true;
const context=vm.createContext({
  document:{body:element(),getElementById:id=>elements[id]??=element(),addEventListener(){},createElement:element},
  performance:{now:()=>1000},Date,Math,AbortController,setInterval(){},
  setTimeout(fn){timeouts.add(fn);return fn;},clearTimeout(fn){timeouts.delete(fn);},
  fetch:(path,options)=>{
    if(path==='/session')return new Promise(()=>{});
    if(path==='/settings/status')return Promise.resolve({ok:true,json:async()=>({pinSet:true,settingsUnlocked:false,expiresIn:0})});
    if(!abortRequests)return Promise.resolve({ok:true,json:async()=>({fresh:false,state:'unknown',message:'Sign in to restore status.'})});
    return new Promise((_,reject)=>options.signal.addEventListener('abort',()=>{const error=new Error('Timeout');error.name='AbortError';reject(error);}));
  }
});
vm.runInContext(fs.readFileSync('app.js','utf8'),context);
(async()=>{
  vm.runInContext("token='test-only';render({connected:true,pinSet:true,webhookReady:true,armAwayConfigured:true,armStayConfigured:true});unlockToken='test';show('home');",context);
  const attempt=vm.runInContext("sendAlarmCommand('disarm')",context);
  for(const timeout of [...timeouts])timeout();
  await attempt;
  assert.equal(vm.runInContext('busy',context),false);
  assert.equal(vm.runInContext('unlockToken',context),'');
  assert.equal(vm.runInContext('activeScreen',context),'pin-screen');
  assert.match(elements.message.textContent,/result is unknown/);
  assert.equal(elements.wake.disabled,false,'A stalled request must release controls');
  abortRequests=false;
  await vm.runInContext('observeEntry()',context);
  assert.equal(elements['connection-alert'].hidden,false);
  await elements.reconnect.onclick();
  assert.equal(vm.runInContext('activeScreen',context),'pin-screen');
  assert.equal(elements['pin-title'].textContent,'Unlock settings');
  abortRequests=true;
  for(const path of ['/hub/audio','/hub/audio/control']){
    const request=vm.runInContext(`call('${path}',{unlock:'test'})`,context);
    for(const timeout of [...timeouts])timeout();
    await assert.rejects(request,path.endsWith('/control')?/Sonos result is unknown/:/Sonos did not respond/);
  }
  console.log('Request timeout releases busy state, preserves uncertainty, and permits reconnection.');
})().catch(error=>{console.error(error);process.exitCode=1;});
