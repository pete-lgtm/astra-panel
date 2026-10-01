// Exercise the standalone installer gate without a real console or saved PIN.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const elements={},timers=new Map(),requests=[];
const make=()=>({value:'',children:[],dataset:{},classList:{toggle(){}},append(...children){this.children.push(...children)},replaceChildren(){this.children=[]},setAttribute(){},removeAttribute(){},focus(){},querySelector(){return make()},querySelectorAll(){return this.children}});
const get=id=>elements[id]??=make();
let correct=false;
const state=vm.createContext({document:{getElementById:get,createElement:make,querySelector:make,addEventListener(){}},window:{scrollTo(){},addEventListener(){}},performance:{now:()=>0},AbortSignal,
  setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
  fetch:async(path,options={})=>{
    requests.push(path);
    const result=path==='/session'?{token:'test-csrf'}:path==='/settings/status'?{pinSet:true,settingsUnlocked:false}:
      path==='/settings/unlock'?(correct?{pinSet:true,settingsUnlocked:true,expiresIn:300}:{error:'Incorrect PIN.'}):
      path==='/installer/context'?{error:'Sign in to UniFi.'}:{settingsUnlocked:false};
    return {ok:!result.error,json:async()=>result};
  }});
vm.runInContext(fs.readFileSync('setup_lab.js','utf8'),state);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();
  assert.equal(get('settings-gate').hidden,false);
  assert.equal(get('welcome').hidden,true);
  assert.equal(get('wizard').hidden,true);
  assert.equal(requests.includes('/installer/context'),false,'Discovery must wait for a settings PIN');
  get('settings-pin').value='0000';await get('settings-unlock-form').onsubmit({preventDefault(){}});
  assert.equal(get('settings-pin').value,'');assert.equal(get('welcome').hidden,true);
  assert.equal(requests.includes('/installer/context'),false);
  correct=true;get('settings-pin').value='752946';await get('settings-unlock-form').onsubmit({preventDefault(){}});
  assert.equal(get('settings-gate').hidden,true);
  assert.equal(get('welcome').hidden,false,'UniFi login appears only after the keypad PIN');
  assert.equal(requests.includes('/installer/context'),true);
  get('password').value='test-password';get('setup-pin').value='752946';
  for(const [fn,ms]of [...timers])if(ms===120000)fn();await flush();
  assert.equal(get('settings-gate').hidden,false);
  assert.equal(get('welcome').hidden,true);
  assert.equal(get('password').value,'');assert.equal(get('setup-pin').value,'');
  assert.equal(requests.includes('/settings/lock'),true);
  console.log('Installer settings stay hidden until PIN verification and clear/relock on idle.');
})().catch(error=>{console.error(error);process.exitCode=1});
