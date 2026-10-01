const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(available){
  const nodes={},events={},requests=[],timers=new Map();
  const get=name=>nodes[name]??={textContent:'',hidden:false,disabled:false,children:[],replaceChildren(...v){this.children=v},removeAttribute(name){delete this[name]},setAttribute(){}};
  const box={hidden:false,querySelector:get};
  const sandbox={AbortSignal,Event,
    document:{getElementById:()=>box,addEventListener:(n,f)=>events[n]=f,dispatchEvent(){}},
    window:{addEventListener(){}},setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
    fetch:async(path)=>{requests.push(path);return{ok:true,json:async()=>path==='/session'?{token:'test-csrf',computerSetupAvailable:available,networkSetup:{ipAddress:'192.168.0.21',url:'http://192.168.0.21:8080'}}:path.endsWith('/start')?{url:'http://192.168.0.21:8080',code:'1234 5678',qr:'<svg></svg>'}:{state:'closed'}}}};
  vm.runInNewContext(fs.readFileSync('browser_pair_ui.js','utf8'),sandbox);
  return{get,requests,events,timers};
}
(async()=>{
  const f=fixture(true);await flush();
  assert.equal(f.get('[data-web-ip]').textContent,'192.168.0.21');
  assert.equal(f.get('[data-web-address]').textContent,'http://192.168.0.21:8080');
  assert.equal(f.get('[data-web-unavailable]').hidden,true);
  assert.equal(f.requests.includes('/browser-setup/start'),false,'Showing an IP must not automatically authorize remote access');
  await f.get('[data-web-start]').onclick();
  assert.equal(f.get('[data-web-code]').textContent,'1234 5678');
  assert.equal(f.get('[data-web-url]').textContent,'http://192.168.0.21:8080');
  assert.equal(f.get('[data-web-details]').hidden,false);
  await f.get('[data-web-cancel]').onclick();
  assert.equal(f.get('[data-web-code]').textContent,'');
  assert.equal(f.get('[data-web-cancel]').hidden,true);
  const unavailable=fixture(false);await flush();
  assert.equal(unavailable.get('[data-web-start]').disabled,true);
  assert.equal(unavailable.get('[data-web-unavailable]').hidden,false);
  assert.equal(unavailable.get('[data-web-ip]').textContent,'192.168.0.21');
  await unavailable.get('[data-web-start]').onclick();
  assert.equal(unavailable.requests.includes('/browser-setup/start'),false);
  console.log('Onboarding network address, explicit pairing, code cleanup, and unavailable service checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
