// Hue is the first lighting provider. Lutron is the only planned addition.
let lightingData=null,lightingRoom='',lightingGeneration=0,lightingLoading=false,lightingBusy=false,lightingTimer;
let lightingSetupGeneration=0,lightingPairId='',lightingSetupBusy=false,lightingDisconnectArmed=false;
let lightingDrag='',lightingMessage='';
const lightingCards=new Map(),lightingRoomButtons=new Map(),lightingSceneButtons=new Map();
function lightingActive(){return !!unlockToken&&activeScreen==='lighting'&&!document.hidden;}
function lightingElement(tag,cls,value=''){return hubNode(tag,cls,value);}
function lightingButton(name,fn){const n=lightingElement('button','',name);n.type='button';n.onclick=fn;return n;}
function lightingCanManage(){return lightingData?.canManage!==false;}
function closeLightingSetup(){
  lightingSetupGeneration++;lightingPairId='';lightingSetupBusy=false;lightingDisconnectArmed=false;
  if(el('lighting-setup').open)el('lighting-setup').close();
  el('lighting-bridges').textContent='';el('lighting-address').value='';
  el('lighting-ip-pad').hidden=true;
  el('lighting-bridges').hidden=false;el('lighting-setup-help').hidden=false;
}
function clearLightingHub(){
  clearTimeout(lightingTimer);lightingGeneration++;lightingData=null;lightingRoom='';lightingLoading=false;lightingBusy=false;lightingDrag='';lightingMessage='';
  lightingCards.clear();lightingRoomButtons.clear();lightingSceneButtons.clear();closeLightingSetup();
  for(const id of ['lighting-rooms','lighting-lights','lighting-scenes'])el(id).textContent='';
  el('lighting-layout').hidden=true;el('lighting-empty').hidden=false;el('lighting-notice').hidden=true;
}
function syncLightingScreen(){
  clearTimeout(lightingTimer);
  if(!lightingActive()){lightingGeneration++;lightingLoading=false;lightingBusy=false;lightingDrag='';closeLightingSetup();return;}
  if(!lightingLoading)loadLighting();
}
function renderLighting(){
  const ready=lightingData?.state==='ready',lights=lightingData?.lights||[],rooms=lightingData?.rooms||[];
  text('lighting-summary',ready?`${lights.length} lights · ${lights.filter(l=>l.on&&l.reachable).length} on`:'Philips Hue');
  text('lighting-connect',ready?'Hue setup':'Connect Hue');
  el('lighting-connect').disabled=!lightingCanManage()||lightingBusy;
  el('lighting-empty-connect').disabled=!lightingCanManage();
  el('lighting-refresh').disabled=lightingLoading||lightingBusy;
  el('lighting-empty').hidden=ready&&!!lights.length;el('lighting-layout').hidden=!ready||!lights.length;
  text('lighting-empty-title',ready?'No Hue lights yet.':'Your home, in the right light.');
  text('lighting-empty-detail',ready?'Add your lights in the Hue app, then tap Refresh.':lightingMessage||'Connect a Philips Hue Bridge to bring your rooms, lights and scenes here.');
  text('lighting-notice',lightingMessage);el('lighting-notice').hidden=!lightingMessage;
  if(!ready)return;
  if(lightingRoom&&!rooms.some(r=>r.id===lightingRoom))lightingRoom='';
  const choices=[{id:'',name:'All lights',lights:lights.map(l=>l.id)},...rooms];
  const roomIds=new Set(choices.map(r=>r.id));
  for(const [id,button] of lightingRoomButtons)if(!roomIds.has(id)){button.remove();lightingRoomButtons.delete(id);}
  for(const room of choices){
    let button=lightingRoomButtons.get(room.id);
    if(!button){button=lightingButton('',()=>{if(lightingBusy)return;activity();lightingRoom=room.id;lightingDrag='';renderLighting();});button.className='lighting-room';lightingRoomButtons.set(room.id,button);el('lighting-rooms').append(button);}
    button.textContent=room.name;button.setAttribute('aria-pressed',String(room.id===lightingRoom));button.disabled=lightingBusy;
  }
  const selected=rooms.find(r=>r.id===lightingRoom),visible=lights.filter(l=>!selected||selected.lights.includes(l.id));
  const usable=!!unlockToken&&!lightingBusy;
  // Whole-home switching is intentionally absent; room actions target one Hue group.
  el('lighting-room-on').disabled=el('lighting-room-off').disabled=!usable||!selected?.groupId;
  const visibleIds=new Set(visible.map(l=>l.id));
  for(const [id,card] of lightingCards)if(!visibleIds.has(id)){card.node.remove();lightingCards.delete(id);}
  for(const light of visible){
    let card=lightingCards.get(light.id);
    if(!card){
      const node=lightingElement('article','lighting-card'),head=lightingElement('div','lighting-card-head'),copy=lightingElement('div','lighting-card-copy');
      const name=lightingElement('strong',''),state=lightingElement('small',''),toggle=lightingButton('',()=>{
        const current=lightingData?.lights?.find(l=>l.id===light.id);
        if(current&&!toggle.disabled)controlLighting({id:light.id,kind:'light',action:'on',value:!current.on});
      });
      toggle.className='lighting-toggle';copy.append(name,state);head.append(copy,toggle);
      const level=lightingElement('div','lighting-level'),slider=lightingElement('input',''),value=lightingElement('output','');
      slider.type='range';slider.min='0';slider.max='100';slider.step='1';
      slider.onpointerdown=()=>{if(!slider.disabled)lightingDrag=light.id;};
      slider.oninput=()=>{if(slider.disabled)return;lightingDrag=light.id;value.textContent=slider.value+'%';activity();};
      slider.onchange=()=>{const v=Number(slider.value);lightingDrag='';if(!slider.disabled)controlLighting({id:light.id,kind:'light',action:'brightness',value:v});};
      slider.onpointercancel=slider.onblur=()=>{lightingDrag='';renderLighting();};
      level.append(slider,value);node.append(head,level);card={node,name,state,toggle,slider,value,level};lightingCards.set(light.id,card);el('lighting-lights').append(node);
    }
    card.name.textContent=light.name;card.name.title=light.name;
    card.state.textContent=!light.reachable?'Unreachable':light.on===null?'Status unknown':light.on?'On':'Off';
    card.node.dataset.on=String(light.on===true&&light.reachable);
    card.toggle.textContent=light.on?'On':'Off';card.toggle.setAttribute('aria-pressed',String(light.on===true));
    card.toggle.setAttribute('aria-label',(light.on?'Turn off ':'Turn on ')+light.name);
    card.toggle.disabled=!usable||!light.reachable||light.on===null;
    card.level.hidden=!light.dimmable;card.slider.disabled=!usable||!light.reachable||light.brightness===null;
    card.slider.setAttribute('aria-label',light.name+' brightness');
    if(lightingDrag!==light.id){card.slider.value=String(light.on?light.brightness??0:0);card.value.textContent=light.brightness===null?'—':(light.on?light.brightness:0)+'%';}
  }
  const scenes=(lightingData.scenes||[]).filter(s=>!lightingRoom||s.roomId===lightingRoom),sceneIds=new Set(scenes.map(s=>s.id));
  for(const [id,button] of lightingSceneButtons)if(!sceneIds.has(id)){button.remove();lightingSceneButtons.delete(id);}
  el('lighting-scenes').hidden=!scenes.length;
  for(const scene of scenes){
    let button=lightingSceneButtons.get(scene.id);
    if(!button){button=lightingButton(scene.name,()=>{if(!button.disabled)controlLighting({id:scene.id,kind:'scene',action:'scene'});});button.className='lighting-scene';lightingSceneButtons.set(scene.id,button);el('lighting-scenes').append(button);}
    const roomName=rooms.find(r=>r.id===scene.roomId)?.name;
    button.textContent=scene.name+(!lightingRoom&&roomName?' · '+roomName:'');button.setAttribute('aria-label','Activate '+scene.name+' scene'+(roomName?' in '+roomName:''));button.disabled=!usable;
  }
}
function lightingUnavailable(){
  if(lightingData?.lights)lightingData={...lightingData,lights:lightingData.lights.map(l=>({...l,reachable:false,on:null,brightness:null})),rooms:lightingData.rooms.map(r=>({...r,groupId:null})),scenes:[]};
}
async function loadLighting(){
  if(!lightingActive()||lightingLoading||lightingBusy)return;
  clearTimeout(lightingTimer);lightingLoading=true;const generation=++lightingGeneration,grant=unlockToken;
  try{
    const data=await call('/hub/lighting',{unlock:grant});
    if(generation!==lightingGeneration||grant!==unlockToken||!lightingActive())return;
    lightingData=data;lightingMessage='';
  }catch(error){
    if(generation!==lightingGeneration||grant!==unlockToken||!lightingActive())return;
    lightingMessage=error.message;
    lightingUnavailable();
  }
  lightingLoading=false;renderLighting();lightingTimer=setTimeout(loadLighting,8000);
}
async function controlLighting(command){
  if(!lightingActive()||lightingBusy)return;
  // A background refresh must not swallow a first tap. Its late response is
  // invalidated; the server reads fresh capabilities before this one command.
  activity();clearTimeout(lightingTimer);lightingLoading=false;lightingBusy=true;lightingMessage='';renderLighting();
  const generation=++lightingGeneration,grant=unlockToken;
  try{await call('/hub/lighting/control',{unlock:grant,...command});}
  catch(error){if(generation===lightingGeneration&&grant===unlockToken&&lightingActive())lightingMessage=error.message;}
  if(generation!==lightingGeneration||grant!==unlockToken||!lightingActive())return;
  lightingBusy=false;lightingUnavailable();renderLighting();
  // Always verify current state; never retry a write, even after a lost reply.
  await loadLighting();
}
function lightingSetupState(){
  const pairing=!!lightingPairId;
  el('lighting-setup-body').hidden=pairing;el('lighting-pair').hidden=!pairing;
  el('lighting-pair').disabled=lightingSetupBusy;el('lighting-rescan').disabled=lightingSetupBusy;
  text('lighting-rescan',pairing?'Choose another bridge':'Find bridges');
  el('lighting-disconnect').hidden=!lightingData?.bridge||pairing;
  el('lighting-disconnect').disabled=lightingSetupBusy;
  text('lighting-disconnect',lightingDisconnectArmed?'Confirm disconnect':'Disconnect');
  for(const n of el('lighting-setup-body').querySelectorAll('input,button'))n.disabled=lightingSetupBusy;
}
async function lightingSetupCall(action,body={}){
  if(lightingSetupBusy||!lightingActive())return;
  lightingSetupBusy=true;lightingSetupState();activity();
  const generation=lightingSetupGeneration,grant=unlockToken;
  const current=()=>generation===lightingSetupGeneration&&grant===unlockToken&&lightingActive()&&el('lighting-setup').open;
  el('lighting-setup-message').hidden=true;
  try{
    const result=await call('/hub/lighting/'+action,{unlock:grant,...body});
    if(!current())return;
    if(action==='discover'){
      el('lighting-ip-pad').hidden=true;
      el('lighting-bridges').hidden=false;el('lighting-setup-help').hidden=false;
      el('lighting-bridges').textContent='';
      for(const bridge of result.bridges||[]){const b=lightingButton('',()=>lightingSetupCall('prepare',{host:bridge.host}));b.className='lighting-bridge';b.append(lightingElement('strong','',bridge.name),lightingElement('span','',bridge.host));el('lighting-bridges').append(b);}
      text('lighting-setup-help',result.bridges?.length?'Choose your Hue Bridge.':'No Hue Bridge found. Check it is on the same network, or enter its IP address.');
    }else if(action==='prepare'){
      el('lighting-setup-help').hidden=false;
      lightingPairId=result.pairId;text('lighting-setup-help',`Press the button on ${result.name}, then tap Connect. (${result.host})`);
    }else if(action==='pair'||action==='disconnect'){
      lightingGeneration++;lightingLoading=false;lightingData=result;lightingMessage='';closeLightingSetup();renderLighting();loadLighting();return;
    }
  }catch(error){if(current()){text('lighting-setup-message',error.message);el('lighting-setup-message').hidden=false;}}
  if(current()){lightingSetupBusy=false;lightingSetupState();}
}
function openLightingSetup(){
  if(!lightingActive()||!lightingCanManage())return;
  activity();closeLightingSetup();el('lighting-setup').showModal();text('lighting-setup-title','Connect Philips Hue');
  text('lighting-setup-help','Looking for Hue Bridges on your local network…');lightingSetupState();lightingSetupCall('discover');
}
el('lighting-connect').onclick=el('lighting-empty-connect').onclick=openLightingSetup;
el('lighting-refresh').onclick=()=>{activity();loadLighting();};
el('lighting-room-on').onclick=()=>{const r=lightingData?.rooms?.find(r=>r.id===lightingRoom);if(r?.groupId&&!el('lighting-room-on').disabled)controlLighting({id:r.groupId,kind:'grouped_light',action:'on',value:true});};
el('lighting-room-off').onclick=()=>{const r=lightingData?.rooms?.find(r=>r.id===lightingRoom);if(r?.groupId&&!el('lighting-room-off').disabled)controlLighting({id:r.groupId,kind:'grouped_light',action:'on',value:false});};
el('lighting-setup-close').onclick=()=>{activity();closeLightingSetup();};
el('lighting-setup').oncancel=closeLightingSetup;
el('lighting-address-form').onsubmit=e=>{e.preventDefault();lightingSetupCall('prepare',{host:el('lighting-address').value.trim()});};
el('lighting-address').onfocus=()=>{el('lighting-ip-pad').hidden=false;el('lighting-bridges').hidden=true;el('lighting-setup-help').hidden=true;};
for(const key of ['1','2','3','4','5','⌫','6','7','8','9','0','.']){
  const button=lightingButton(key,()=>{
    if(lightingSetupBusy)return;
    activity();const input=el('lighting-address'),start=input.selectionStart??input.value.length,end=input.selectionEnd??start;
    const from=key==='⌫'&&start===end?Math.max(0,start-1):start,insert=key==='⌫'?'':key;
    const value=input.value.slice(0,from)+insert+input.value.slice(end);
    if(value.length<=15){input.value=value;input.focus();input.setSelectionRange(from+insert.length,from+insert.length);}
  });
  button.onpointerdown=e=>e.preventDefault();button.setAttribute('aria-label',key==='⌫'?'Delete digit':key==='.'?'Dot':key);
  el('lighting-ip-pad').append(button);
}
el('lighting-pair').onclick=()=>lightingSetupCall('pair',{pairId:lightingPairId});
el('lighting-rescan').onclick=()=>{lightingPairId='';lightingSetupCall('discover');};
el('lighting-disconnect').onclick=()=>{activity();if(lightingDisconnectArmed)lightingSetupCall('disconnect',{confirm:true});else{lightingDisconnectArmed=true;lightingSetupState();}};
document.addEventListener('visibilitychange',syncLightingScreen);
syncLightingScreen();
