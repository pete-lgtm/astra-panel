// The real setup script with synthetic requests/DOM. No household credentials.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture({saved=false,browser=true,computer=true}={}){
  const elements={},listeners={},timers=new Map(),requests=[];
  function make(tag='div'){
    return {tag,value:'',textContent:'',hidden:false,children:[],dataset:{},classList:{toggle(){},add(){},remove(){}},
      append(...children){this.children.push(...children)},replaceChildren(){this.children=[]},setAttribute(){},removeAttribute(){},focus(){},reportValidity(){return !!this.value},
      querySelector(){return make()},querySelectorAll(selector){
        const all=[];function walk(node){for(const child of node.children||[]){all.push(child);walk(child)}}walk(this);
        return selector.includes('input:checked')?all.filter(x=>x.tag==='input'&&x.checked&&!x.disabled):selector==='[data-digit]'?[]:all;
      }};
  }
  const get=id=>elements[id]??=make();
  let pinSet=saved,settingsUnlocked=!saved,api=saved,signedIn=false,expire=false,statusFails=false,applyError=null;
  const plan={name:'Test home',doors:['Front'],profile:'Home alarm',recipients:['Test owner'],actions:['Save test rules'],message:'Review setup',planId:'test-plan'};
  const context=()=>({pinSet,profiles:[],sensors:[{id:'front',name:'Front',available:true,state:'CONNECTED',isOpened:false}],users:[{id:'owner',name:'Test owner'}],choices:{name:'Test home',sensorIds:['front'],profileId:'',profileName:'Home alarm',receivers:['owner']}});
  const sandbox=vm.createContext({Event,AbortSignal,document:{hidden:false,getElementById:get,createElement:make,querySelector:make,
      addEventListener(name,fn){listeners[name]=fn},dispatchEvent(){}},
    window:{location:{href:''},scrollTo(){},addEventListener(name,fn){listeners[name]=fn}},performance:{now:()=>0},
    setTimeout(fn,ms){timers.set(fn,ms);return fn},clearTimeout(fn){timers.delete(fn)},
    fetch:async(path,options={})=>{
      const body=JSON.parse(options.body||'{}');requests.push({path,body});let result;
      if(path==='/session')result={token:'synthetic',browserSetup:browser,browserExpiresIn:1800,apiConfigured:api,setupRequired:!saved,computerSetupAvailable:computer,consoleHost:'192.168.0.1',site:{name:'Test home',doors:[]}};
      else if(expire)result={pairingRequired:true,error:'Session expired.'};
      else if(path==='/settings/status')result={pinSet,settingsUnlocked,expiresIn:1800};
      else if(path==='/settings/unlock'){settingsUnlocked=body.pin==='927461';result=settingsUnlocked?{pinSet,settingsUnlocked,expiresIn:1800}:{error:'Incorrect PIN.'};}
      else if(path==='/settings/lock'){settingsUnlocked=false;result={};}
      else if(path==='/connect'){api=true;result={connected:true};}
      else if(path==='/status')result=statusFails?{error:'Console unreachable'}:{connected:true};
      else if(path==='/installer/context')result=signedIn?context():{error:'Sign in to UniFi.'};
      else if(path==='/installer/login'){if(body.verificationCode==='123456'){signedIn=true;result=context()}else result={mfaRequired:true};}
      else if(path==='/installer/pin/status')result={pinSet};
      else if(path==='/installer/pin/create'){pinSet=true;settingsUnlocked=true;result={pinSet,settingsUnlocked,expiresIn:1800};}
      else if(path==='/installer/review')result=plan;
      else if(path==='/installer/apply'){result=applyError||{...plan,saved:true};applyError=null;}
      else if(path==='/entry/status')result={fresh:true,state:'disarmed',liveDelayEnabled:false,doors:{Front:{connected:true,position:'closed'}},verifiedDoors:[]};
      else if(path==='/browser/end')result={state:'complete'};
      else throw Error('Unexpected test endpoint '+path);
      return {ok:!result.error,json:async()=>result};
    }});
  vm.runInContext(fs.readFileSync('setup_lab.js','utf8'),sandbox);
  return {get,listeners,timers,requests,sandbox,expire(){expire=true},failStatus(){statusFails=true},failApply(value){applyError=value},run:code=>vm.runInContext(code,sandbox)};
}
const event={preventDefault(){}};
(async()=>{
  const panel=fixture({browser:false,computer:false});await flush();
  assert.equal(panel.get('setup-start').hidden,false,'A fresh panel offers both setup methods first');
  assert.equal(panel.get('api-page').hidden,true,'Do not ask for a key before choosing the setup method');
  assert.equal(panel.get('keypad-link').hidden,true,'There is no configured keypad to return to yet');
  assert.equal(panel.get('computer-setup').hidden,false,'Show the network address even when secure access needs configuring');
  await panel.get('setup-on-panel').onclick();
  assert.equal(panel.get('api-page').hidden,false);
  assert.equal(panel.get('computer-setup').hidden,true);
  await panel.get('setup-options').onclick();
  assert.equal(panel.get('setup-start').hidden,false);
  assert.equal(panel.get('api-page').hidden,true);
  const f=fixture();await flush();
  assert.equal(f.get('api-page').hidden,false);
  assert.equal(f.get('computer-setup').hidden,true);
  assert.equal(f.requests.some(x=>x.path==='/installer/context'),false,'API connection comes before discovery');
  f.get('api-key').value='test-only-key';await f.get('api-form').onsubmit(event);
  assert.equal(f.get('api-key').value,'');assert.equal(f.get('welcome').hidden,false);
  assert.equal(f.requests.find(x=>x.path==='/connect').body.key,'test-only-key');
  f.get('username').value='test';f.get('password').value='test-only-password';await f.get('login').onsubmit(event);
  assert.equal(f.get('password').value,'');assert.equal(f.get('code-field').hidden,false);
  f.get('code').value='123456';await f.get('login').onsubmit(event);
  assert.equal(f.get('code').value,'');assert.equal(f.get('doors-page').hidden,false);
  f.get('next-doors').onclick();await f.get('next-alerts').onclick();
  f.get('setup-pin').value='927461';f.get('confirm-pin').value='927461';await f.get('pin-form').onsubmit(event);
  assert.equal(f.get('setup-pin').value,'');assert.equal(f.get('review-page').hidden,false);
  // Switching to UniFi in another tab must not revoke the paired desktop setup.
  f.run('document.hidden=true');f.listeners.visibilitychange();
  assert.equal(f.run('settingsOpen'),true);
  assert.equal(f.requests.some(x=>x.path==='/settings/lock'),false);
  f.failApply({error:'Settings changed. Review again.',reviewRequired:true});
  await f.get('save').onclick();await flush();
  assert.equal(f.get('pin-page').hidden,false,'A rejected review must leave a usable route to review again');
  assert.equal(f.get('existing-pin').hidden,false,'The saved PIN is retained');
  assert.equal(f.run('plan'),null,'Discard the stale plan');
  await f.get('pin-form').onsubmit(event);
  assert.equal(f.get('review-page').hidden,false);
  assert.equal(f.get('back-pin').disabled,false);
  f.failApply({error:'A write could not be verified.'});
  await f.get('save').onclick();
  assert.equal(f.get('review-page').hidden,false,'An uncertain write stays in the resume workflow');
  assert.equal(f.get('back-pin').disabled,true);
  assert.equal(f.get('save').textContent,'Finish setup');
  await f.get('save').onclick();await flush();
  assert.equal(f.get('complete').hidden,false);
  assert.equal(f.get('setup-enable').disabled,true,'Unchecked door cannot be claimed as verified');
  assert.match(f.get('setup-protection-message').textContent,/pending/);
  assert.equal(f.get('continue').textContent,'Finish setup →');
  await f.get('continue').onclick(event);
  assert.equal(f.get('browser-ended').hidden,false);
  assert.equal(f.get('settings-gate').hidden,true);
  assert.equal(f.requests.at(-1).path,'/browser/end');
  assert.equal(f.run('window.location.href'),'','Computer never redirects to its own localhost');

  const g=fixture({saved:true});await flush();
  assert.equal(g.get('settings-gate').hidden,false);
  assert.equal(g.requests.some(x=>x.path==='/status'),false,'Paired browser still needs administrator PIN');
  g.get('settings-pin').value='927461';await g.get('settings-unlock-form').onsubmit(event);
  assert.equal(g.get('welcome').hidden,false);
  assert.equal(g.requests.some(x=>x.path==='/connect'),false,'Saved API key must be reused');
  g.get('password').value='test-only-password';g.get('api-key').value='test-only-key';
  for(const [fn,ms] of [...g.timers])if(ms===600000)fn();await flush();
  assert.equal(g.get('settings-gate').hidden,false);
  assert.equal(g.get('password').value,'');assert.equal(g.get('api-key').value,'');
  g.expire();g.get('settings-pin').value='927461';await g.get('settings-unlock-form').onsubmit(event);
  assert.equal(g.get('browser-ended').hidden,false);
  assert.equal(g.get('settings-gate').hidden,true);

  const h=fixture({saved:true});await flush();h.failStatus();
  h.get('settings-pin').value='927461';await h.get('settings-unlock-form').onsubmit(event);
  assert.equal(h.get('api-page').hidden,false);
  assert.equal(h.get('api-retry').hidden,false);
  assert.match(h.get('message').textContent,/try the saved connection again/);
  console.log('Computer setup: API copy/paste, MFA, PIN, sensor review/save, tab switching, finish, expiry, and saved-key retry passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
