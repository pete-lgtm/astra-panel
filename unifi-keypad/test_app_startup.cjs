// Use only IDs present in the shipped HTML. Permissive DOM mocks used by the
// isolated flow tests cannot catch a removed control crashing application boot.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('index.html','utf8'),nodes=new Map();
const make=()=>({value:'',textContent:'',hidden:false,dataset:{},children:[],style:{setProperty(){}},
  classList:{toggle(){}},querySelectorAll(){return []},setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]},
  append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},
  addEventListener(){},focus(){},showModal(){this.open=true},close(){this.open=false}});
for(const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)){
  assert.ok(!nodes.has(match[1]),'Duplicate HTML ID: '+match[1]);
  nodes.set(match[1],Object.assign(make(),{hidden:/\shidden(?:\s|>)/.test(match[0])}));
}
const body=make(),ctx=vm.createContext({document:{body,getElementById:id=>nodes.get(id)||null,
  createElement:make,addEventListener(){},querySelectorAll(){return []}},Date,Math,performance:{now:()=>1000},
  fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},clearTimeout(){}});
const run=source=>vm.runInContext(source,ctx);
for(const file of ['app.js','management.js','level-control.js','audio.js','lighting.js'])vm.runInContext(fs.readFileSync(file,'utf8'),ctx,{filename:file});
assert.equal(body.dataset.screen,'sleep','Startup must reach the fixed lock-screen layout');
assert.ok(nodes.get('clock').textContent,'The clock must start without waiting for a connection');
assert.ok(nodes.get('date').textContent);
assert.equal(nodes.get('number-pad').children.length,12);
assert.equal(nodes.get('hub-navigation').hidden,true,'Private navigation stays locked');
assert.equal(nodes.get('systems-panel-power').hidden,false);
assert.equal(nodes.get('systems-panel-network').hidden,true,'Only the selected Systems panel starts visible');
assert.equal(nodes.get('systems-panel-wifi').hidden,true);
run("show('systems')");
assert.equal(body.dataset.screen,'sleep','Systems still requires a PIN');
run("unlockToken='fixture';show('systems');networkInventory={state:'ready',site:{id:'test',name:'Test'},sites:[],devices:[],wifiPasswordsAvailable:true,speedTestAvailable:true};renderNetworkHub();");
for(const tab of ['network','wifi','power']){
  nodes.get('systems-tab-'+tab).onclick();
  for(const panel of ['power','network','wifi'])assert.equal(nodes.get('systems-panel-'+panel).hidden,panel!==tab);
}
assert.equal(nodes.get('hub-wifi-passwords').hidden,false);
assert.equal(nodes.get('hub-speedtest').hidden,false);
nodes.get('systems-tab-wifi').onclick();
nodes.get('hub-wifi-passwords').onclick();
assert.equal(nodes.get('wifi-dialog').open,true);
nodes.get('wifi-close').onclick();
assert.equal(nodes.get('wifi-dialog').open,false);
console.log('Shipped HTML boots the clock, PIN pad, protected hub, Systems tabs and Wi-Fi controls.');
