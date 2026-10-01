const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const nodes={},requests=[],timers=new Map(),listeners={};let nextTimer=0;
const make=()=>({value:'',dataset:{},children:[],classList:{toggle(){}},querySelectorAll(){return []},setAttribute(k,v){this[k]=v},append(...items){this.children.push(...items)},focus(){},
  showModal(){this.open=true},close(){this.open=false},
  set textContent(value){this.valueText=value;this.children=[]},get textContent(){return (this.valueText||'')+this.children.map(child=>child.textContent).join(' ')}});
const get=id=>nodes[id]??=make(),ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(name,fn){listeners[name]=fn}},Date,Math,
  performance:{now:()=>1000},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,ms});return id},clearTimeout(id){timers.delete(id)}});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),event={preventDefault(){}};

const inventory={state:'ready',speedTestAvailable:true,wifiPasswordsAvailable:true,site:{id:'default'},sites:[],devices:[]};
ctx.inventory=inventory;
let reply={state:'idle',phase:'Ready to test',message:'Default internet connection',gateway:{name:'Home gateway'},canStart:true,result:null};
ctx.fakeCall=async(path,data)=>{requests.push({path,data});if(path.includes('/speedtest/'))return reply;if(path==='/hub/network')return inventory;if(path==='/lock')return {};throw new Error(path)};
run('call=fakeCall');
const ready=()=>run("unlockToken='grant';networkInventory=inventory;networkLoading=false;show('systems');selectSystemsTab('wifi');renderNetworkHub()");
const open=()=>get('hub-speedtest').onclick(),start=()=>get('speedtest-start').onclick();
(async()=>{
 await open();assert.equal(requests.length,0,'Locked view cannot request tests');
 ready();await open();assert.equal(requests.at(-1).path,'/hub/network/speedtest/status');
 assert.equal(get('speedtest-dialog').open,true);assert.equal(get('speedtest-start').disabled,false);
 assert.equal(get('speedtest-results').hidden,true);
 let resolve;reply=new Promise(r=>resolve=r);const pending=start();await start();
 assert.equal(requests.filter(r=>r.path.endsWith('/start')).length,1,'Double tap starts once');
 resolve({state:'running',phase:'Downloading…',message:'Testing',gateway:{name:'Home gateway'},canStart:false,result:{downloadMbps:90,uploadMbps:10,latencyMs:20,testedAt:1700000000000}});await pending;
 assert.match(get('speedtest-result-label').textContent,/Last result/,'Old numbers remain explicitly labeled');
 assert.equal(get('speedtest-start').disabled,true);
 const poll=run('speedTestTimer');assert.equal(timers.get(poll).ms,5000);
 reply={state:'completed',phase:'Test complete',message:'Measured at gateway',canStart:true,result:{downloadMbps:912.4,uploadMbps:41.8,latencyMs:12.7,testedAt:1790000060000}};
 await timers.get(poll).fn();assert.equal(requests.at(-1).path,'/hub/network/speedtest/status');
 assert.equal(get('speedtest-download').textContent,'912.4');assert.match(get('speedtest-result-label').textContent,/^Tested/);
 assert.equal(get('speedtest-start').textContent,'Run again');
 reply=Promise.reject(new Error('timeout'));await start();assert.equal(get('speedtest-start').textContent,'Check result');
 reply={state:'running',phase:'Testing',canStart:false};await start();assert.equal(requests.at(-1).path,'/hub/network/speedtest/status','Unknown start only checks result, never retries start');
 reply=new Promise(r=>resolve=r);const late=run('updateSpeedTest(speedTestView)');run('lock()');resolve({state:'completed',canStart:true,result:{downloadMbps:999,testedAt:1790000060000}});await late;
 assert.equal(get('speedtest-dialog').open,false);assert.equal(get('speedtest-results').hidden,true);assert.equal(get('speedtest-download').textContent,'');
 ready();reply={state:'idle',phase:'Ready',canStart:true};await open();run("show('home')");assert.equal(get('speedtest-dialog').open,false);
 ready();await open();await run("loadNetworkHub('other')");assert.equal(get('speedtest-dialog').open,false);
 ready();await open();run("renderObservation({state:'breached',fresh:true,doors:{},remaining:null})");assert.equal(get('speedtest-dialog').open,false);
 ready();await open();ctx.document.hidden=true;listeners.visibilitychange();assert.equal(get('speedtest-dialog').open,false);
 ready();run('networkInventory={state:"ready",site:{id:"default"}};renderNetworkHub()');assert.equal(get('hub-speedtest').hidden,true);
 console.log('Gateway speed test UI: read-only opening, one start, polling, distinct old/new results, uncertain response, late/lock/site/alarm cleanup passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
