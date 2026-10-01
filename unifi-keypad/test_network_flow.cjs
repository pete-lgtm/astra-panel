const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const nodes={},requests=[];
const make=()=>({value:'',dataset:{},children:[],classList:{toggle(){}},querySelectorAll(){return []},setAttribute(k,v){this[k]=v},append(...items){this.children.push(...items)},focus(){},
  set textContent(value){this.valueText=value;this.children=[]},get textContent(){return (this.valueText||'')+this.children.map(child=>child.textContent).join(' ')}});
const get=id=>nodes[id]??=make(),ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(){}},Date,Math,
  performance:{now:()=>1000},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},clearTimeout(){}});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx);
let response={state:'ready',site:{id:'home',name:'Home'},sites:[{id:'home',name:'Home'},{id:'office',name:'Office'}],observedAt:Date.now(),devices:[
  {id:'plug',name:'Desk <img onerror=alert(1)>',kind:'power',model:'UP1',state:'online',outlets:[{index:0,name:'Monitor',on:false,watts:0},{index:1,name:'Unknown outlet',on:null,watts:null}]},
  {id:'switch',name:'Office switch',kind:'switch',model:'USW',state:'online',ports:[{index:1,name:'Office AP',linkUp:true,speedMbps:1000,poe:true,poeEnabled:true,poePowered:true,watts:4.2}]}]};
ctx.fakeCall=async(path,data)=>{requests.push({path,data});if(path==='/hub/network')return response;if(path==='/lock')return {};throw new Error(path)};
run('call=fakeCall');
(async()=>{
  await run('loadNetworkHub()');assert.equal(requests.length,0,'No discovery while locked');
  run("unlockToken='grant';show('systems');selectSystemsTab('network')");await run('loadNetworkHub()');
  assert.equal(get('hub-network-site-label').hidden,false);
  assert.match(get('hub-power-list').textContent,/Monitor.*Off · 0.0 W/);
  assert.match(get('hub-power-list').textContent,/Unknown outlet.*State unknown/);
  assert.match(get('hub-power-list').textContent,/<img onerror=alert\(1\)>/,'Names are text, not HTML');
  assert.doesNotMatch(get('hub-power-list').textContent,/Office switch/);
  assert.match(get('hub-network-list').textContent,/Office switch.*1 ports · 1 PoE.*Office AP.*PoE supplying power · 4.2 W/);
  assert.doesNotMatch(get('hub-network-list').textContent,/Monitor/);
  response={state:'denied',message:'Check Network permissions.',sites:[],devices:[]};await run('loadNetworkHub()');
  assert.equal(get('hub-network-list').textContent,'','A failed refresh removes stale equipment');
  assert.equal(get('hub-network-status').textContent,'Check Network permissions.');
  response={state:'ready',site:{id:'home',name:'Home'},sites:[{id:'home',name:'Home'}],observedAt:Date.now(),devices:[]};await run('loadNetworkHub()');
  assert.match(get('hub-power-list').textContent,/No smart outlets found/);
  assert.equal(get('hub-network-site-label').hidden,true);
  let resolve;response=new Promise(r=>resolve=r);
  const pending=run('loadNetworkHub()');
  assert.equal(get('hub-power-refresh').disabled,true);
  run('lock()');resolve({state:'ready',site:{name:'Private home'},sites:[],devices:[]});await pending;
  assert.equal(get('hub-network-list').textContent,'');assert.equal(get('hub-network-status').textContent,'');
  assert.equal(run('networkInventory'),null,'No late reply can expose inventory after locking');
  console.log('Network/Power: PIN gate, site selection, safe labels, outlet/PoE separation, errors, empty lists and lock races passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
