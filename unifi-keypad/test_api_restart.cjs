const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={};const make=()=>({textContent:'',hidden:false,dataset:{},classList:{toggle(){}},querySelectorAll(){return []},setAttribute(){},append(){},focus(){}});
const ctx=vm.createContext({document:{body:make(),getElementById:id=>nodes[id]??=make(),createElement:make,addEventListener(){}},Date,Math,performance:{now:()=>1},fetch:()=>new Promise(()=>{}),setTimeout(){},clearTimeout(){},setInterval(){}});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
(async()=>{
 await vm.runInContext(`(async()=>{
 let attempts=0; let locked=false;
 call=async()=>{if(++attempts===1)throw Error('Reload the local app to continue.');return {connected:true};};
 fetch=async()=>({ok:true,json:async()=>({token:'replacement',pinSet:true})});
 lock=async()=>{locked=true;};render=()=>{};
 message('Your UniFi account is signed in. Reconnect the API connection in Settings.');
 await refresh();
 if(attempts!==2 || !locked || token!=='replacement' || localConnection!==null)throw Error('Restart recovery failed');
 if(el('message').textContent!=='')throw Error('Stale API warning retained');
 call=async()=>{throw Error('Network unavailable');};await refresh();
 if(apiConnectionMissing)throw Error('Network outage mislabeled as missing key');
 call=async()=>{throw Error('Connect to your console first.');};await refresh();
 if(!apiConnectionMissing)throw Error('Missing key not detected');
 })()`,ctx);
 vm.runInContext(`
 status={connected:true,webhookReady:true,armAwayConfigured:true,armStayConfigured:true};localConnection=true;apiConnectionMissing=false;
 liveObservation={fresh:false,localConnected:false,apiConfigured:true,message:'Sign in to the local console first.'};
 renderConnection();
 `,ctx);
 assert.match(nodes['connection-alert-detail'].textContent,/saved API key is working/);
 assert.doesNotMatch(nodes['connection-alert-detail'].textContent,/Reconnect the API/);
 const reason=vm.runInContext(`armingBlockReason({fresh:true,receivedAt:performance.now(),state:'disarmed',liveDelayEnabled:false,verifiedDoors:['Front Door','Back Door'],doors:{'Front Door':{connected:true,position:'closed'},'Back Door':{connected:true,position:'closed'},'Garage Door':{connected:true,position:'closed'}}})`,ctx);
 assert.match(reason,/Garage Door/);
 console.log('API restart recovery, login-state warnings, and exact sensor blocker tests passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
