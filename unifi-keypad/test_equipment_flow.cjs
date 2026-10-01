const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const nodes={},requests=[];
const make=()=>({value:'',scrollTop:0,dataset:{},children:[],classList:{toggle(){}},querySelectorAll(selector){const found=[];const visit=node=>{for(const child of node.children||[]){if(child.className==='equipment-ports' && (selector!=='.equipment-ports[open]' || child.open))found.push(child);visit(child)}};visit(this);return found},setAttribute(k,v){this[k]=v},append(...items){this.children.push(...items)},focus(){},
  showModal(){this.open=true},close(){this.open=false},setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end},
  set textContent(value){this.valueText=value;this.children=[];this.scrollTop=0},get textContent(){return (this.valueText||'')+this.children.map(child=>child.textContent).join(' ')}});
const get=id=>nodes[id]??=make(),ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(){}},Date,Math,
  performance:{now:()=>1000},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},clearTimeout(){}});
vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),event={preventDefault(){}};
ctx.device={id:'0123456789abcdef01234567',name:'Office AP',kind:'accessPoint',controls:{rename:true,restart:true}};
let networkResult={state:'ready',site:{id:'default',name:'Home'},sites:[],observedAt:Date.now(),actionsAvailable:true,devices:[]};
let reviewResult={planId:'review-1',target:'Office AP',effect:'Rename Office AP to Upstairs AP.'},applyResult={status:'verified',message:'Change confirmed by UniFi Network.'};
ctx.fakeCall=async(path,data)=>{requests.push({path,data});if(path==='/hub/network/review')return reviewResult;if(path==='/hub/network/apply')return applyResult;if(path==='/hub/network')return networkResult;if(path==='/lock')return {};throw new Error(path)};
run('call=fakeCall');
const open=()=>run("openEquipmentAction(device,device,'rename_device')");
const start=()=>run("unlockToken='grant';networkGeneration++;networkLoading=false;selectedNetworkSite='default';networkInventory={actionsAvailable:true,site:{id:'default'}};show('systems');selectSystemsTab('network')");
(async()=>{
  open();assert.equal(get('equipment-dialog').open,undefined,'Locked hub cannot open controls');
  start();run('networkInventory.actionsAvailable=false');
  assert.equal(run('equipmentControls(device).children.length'),0,'Older backend exposes no unsupported controls');
  start();open();assert.equal(get('equipment-dialog').open,true);assert.equal(requests.length,0);
  get('equipment-name').value='Upstairs AP';await get('equipment-form').onsubmit(event);
  assert.equal(requests.length,1);assert.equal(requests[0].path,'/hub/network/review');
  assert.equal(requests[0].data.request.name,'Upstairs AP');
  get('equipment-name').value='Edited after review';get('equipment-name').oninput();
  await get('equipment-form').onsubmit(event);
  assert.equal(requests[1].path,'/hub/network/review','Editing invalidates the previous plan');
  let resolve;applyResult=new Promise(r=>resolve=r);
  const pending=get('equipment-form').onsubmit(event);await get('equipment-form').onsubmit(event);
  assert.equal(requests.filter(r=>r.path==='/hub/network/apply').length,1,'Double tap sends only once');
  assert.equal(requests.at(-1).data.confirm,true);assert.equal(requests.at(-1).data.unlock,'grant');
  resolve({status:'submitted',message:'Accepted; completion is not confirmed.'});await pending;
  assert.equal(get('equipment-dialog').open,false,'Accepted changes return without a Done screen');
  assert.match(get('hub-network-status').textContent,/Accepted; completion is not confirmed/);
  start();open();reviewResult=new Promise(r=>resolve=r);
  const reading=get('equipment-form').onsubmit(event);run('lock()');resolve({planId:'late',target:'Private equipment',effect:'Private data'});await reading;
  assert.equal(get('equipment-dialog').open,false);assert.equal(run('equipmentAction'),null);
  assert.doesNotMatch(get('equipment-target').textContent,/Private/,'Late replies cannot reveal data after lock');

  start();reviewResult={planId:'reboot-review',target:'Office AP',effect:'Connected devices will briefly disconnect.'};
  requests.length=0;
  await run("openEquipmentAction(device,device,'restart_device')");
  assert.deepEqual(requests.map(r=>r.path),['/hub/network/review'],'Reboot prepares automatically with no extra Review tap');
  assert.equal(get('equipment-title').textContent,'Are you sure?');
  assert.equal(get('equipment-submit').textContent,'Reboot device');
  assert.equal(get('equipment-submit').disabled,false);
  assert.equal(get('equipment-dialog').open,true);
  applyResult={status:'submitted',message:'Accepted; completion is not confirmed.'};
  await get('equipment-form').onsubmit(event);
  assert.equal(requests.filter(r=>r.path==='/hub/network/apply').length,1);
  assert.equal(get('equipment-dialog').open,false,'No third screen after confirmation');
  assert.equal(run('activeScreen'),'systems');
  assert.match(get('hub-network-status').textContent,/Office AP.*Reboot requested/);
  assert.doesNotMatch(get('hub-network-status').textContent,/complete|rebooted/i,'Acceptance does not claim completion');

  // While refresh is in flight, keep the original list. Restore scroll and open
  // port groups on the fresh list, so a reboot never sends the user to the top.
  start();ctx.switchDevice={id:'switch',name:'Office switch',kind:'switch',controls:{restart:true},state:'online',ports:[{index:1,name:'AP',poe:true,controls:{cycle:true}}]};
  run("networkInventory={actionsAvailable:true,state:'ready',site:{id:'default',name:'Home'},sites:[],devices:[switchDevice]};renderNetworkHub()");
  const list=get('hub-network-list');list.scrollTop=180;list.querySelectorAll('.equipment-ports')[0].open=true;
  const oldCard=list.children[0];
  await run("openEquipmentAction(switchDevice,switchDevice,'restart_device')");
  networkResult=new Promise(r=>resolve=r);
  await get('equipment-form').onsubmit(event);
  assert.equal(list.children[0],oldCard,'Background refresh does not blank the device list');
  assert.equal(list.scrollTop,180);
  resolve({state:'ready',site:{id:'default',name:'Home'},sites:[],devices:[ctx.switchDevice]});
  await new Promise(setImmediate);
  assert.equal(list.scrollTop,180,'Return to the previous scroll position');
  assert.equal(list.querySelectorAll('.equipment-ports')[0].open,true,'Keep port groups expanded');

  // Auto-review is only a read. Cancel/lock/late responses cannot apply anything.
  start();requests.length=0;reviewResult=new Promise(r=>resolve=r);
  const autoReview=run("openEquipmentAction(device,device,'restart_device')");
  assert.equal(get('equipment-submit').disabled,true);
  await get('equipment-form').onsubmit(event);
  get('equipment-cancel').onclick();resolve({planId:'cancelled',target:'Late private name',effect:'Private effect'});await autoReview;
  assert.deepEqual(requests.map(r=>r.path),['/hub/network/review']);
  assert.equal(get('equipment-dialog').open,false);
  assert.doesNotMatch(get('equipment-target').textContent,/private/);

  for(const result of [{status:'unverified',message:'Result unknown. Check UniFi.'},{status:'rejected',message:'UniFi rejected this change.'}]){
    start();requests.length=0;reviewResult={planId:'failure',target:'Office AP',effect:'Brief interruption'};applyResult=result;
    await run("openEquipmentAction(device,device,'restart_device')");
    await get('equipment-form').onsubmit(event);
    assert.equal(get('equipment-dialog').open,true,'Failures remain visible');
    assert.equal(get('equipment-result').textContent,result.message);
    assert.equal(get('equipment-submit').hidden,true);
    await get('equipment-form').onsubmit(event);
    assert.equal(requests.filter(r=>r.path==='/hub/network/apply').length,1,'Never replay a consumed or uncertain action');
    get('equipment-cancel').onclick();
  }
  start();requests.length=0;reviewResult={planId:'power',target:'Outlet 1',effect:'Turn off power'};
  applyResult={status:'verified',message:'Change confirmed.'};networkResult={state:'ready',site:{id:'default',name:'Home'},sites:[],devices:[]};
  run("show('systems');selectSystemsTab('power')");await run("openEquipmentAction(device,{index:1,name:'Outlet 1'},'outlet_off')");
  await get('equipment-form').onsubmit(event);
  assert.equal(run('activeScreen'),'systems');
  assert.equal(get('equipment-dialog').open,false);
  assert.match(get('hub-power-status').textContent,/Outlet 1.*confirmed/);
  run('lock()');assert.equal(run('equipmentNotice'),null);
  assert.equal(get('hub-power-status').textContent,'','Lock removes private action feedback');
  console.log('Equipment UI: two-step reboot, cancel/lock races, single submission, error handling, return page, scroll/expansion and private feedback passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
