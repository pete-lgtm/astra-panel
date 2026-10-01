let pendingLogin = null, loginTimer, loginGeneration = 0;
let pinSubmitTimer;
let pinKeyPress = null;
let token, unlockToken = '', busy = false, status = null, idleTimer, expiryTimer, activeScreen = 'sleep', exitDeadline = 0;
let autoLockSeconds = 60, activityRevision = 0;
let bypassChoosing = false, bypassChoosingPending = false;
let liveObservation = null, verifiedExit = false, entryDeadline = 0, alarmTone = 'blue', disarmOnUnlock = false;
let siteDoors = null;
let armReview = null, armReviewGeneration = 0, armReviewMode = 'arm';
let settingsOnUnlock=false, settingsOpen=false, settingsIdleTimer, settingsExpiryTimer, pinConfigured=null, settingsGeneration=0;
let settingsTarget='overview';
let localConnection = null, apiConnectionMissing = false;
const hubScreens = ['home','cameras','systems','audio'];
const hubLabels = {home:'Alarm',cameras:'Cameras',systems:'Systems',audio:'Audio'};
let hubCameras = [], swipeStart = null;
let lockSwipe = null;
let networkInventory = null, networkLoading = false, networkGeneration = 0, selectedNetworkSite = null;
let equipmentAction = null, equipmentGeneration = 0;
let equipmentKeyboardSymbols = false, equipmentKeyboardShift = true, equipmentKeyPress = null, equipmentKeyClickUntil = 0;
let equipmentNotice = null, equipmentNoticeTimer;
let wifiView = null, wifiGeneration = 0, wifiHideTimer;
let speedTestView = null, speedTestTimer;
const el = id => document.getElementById(id);
const iconFrame = paths => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const lockIcons = {
  open: iconFrame('<rect x="5" y="10" width="14" height="11" rx="2.5"/><path d="M8 10V6a4 4 0 0 1 7.5-2M12 14v3"/>'),
  closed: iconFrame('<rect x="5" y="10" width="14" height="11" rx="2.5"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>'),
  unknown: iconFrame('<circle cx="12" cy="12" r="9"/><path d="M9.5 8.5a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4M12 16h.01"/>')
};
function text(id, value) { const n = el(id); if (n && n.textContent !== value) n.textContent = value; }
function lockIcon(id, kind) {
  if (el(id).dataset.icon !== kind) { el(id).innerHTML = lockIcons[kind]; el(id).dataset.icon = kind; }
}
async function call(path, body = {}) {
  const requestStarted = performance.now();
  const abort = new AbortController();
  const timeout = setTimeout(()=>abort.abort(), path==='/local/login' || path.startsWith('/hub/network/speedtest/') ? 45000 : 30000);
  try {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Keypad-Token':token},body:JSON.stringify(body),signal:abort.signal});
    const data = await response.json();
    if (data.settingsLocked) {
      lock();settingsOnUnlock=true;show('pin-screen');activity();
    }
    if (!response.ok) throw new Error(data.error || 'Request failed.');
    if (typeof data.exitSeconds === 'number') {
      // Account conservatively for transport time once; never restart from cached seconds.
      data.exitDeadline = data.exitSeconds > 0 ? requestStarted + data.exitSeconds * 1000 : 0;
    }
    return data;
  } catch(error) {
    if(error.name==='AbortError'&&path.startsWith('/hub/audio'))throw new Error(['/hub/audio/control','/hub/audio/group'].includes(path)?'The Sonos result is unknown. Refresh before trying again.':'Sonos did not respond. Tap Find rooms to try again.');
    if(error.name==='AbortError') throw new Error(path==='/hub/network/apply' ? 'The result is unknown. Check this equipment in UniFi before trying again.' : path.startsWith('/webhook/') ? 'The request timed out; its result is unknown. Check Protect before trying again.' : 'The connection timed out. Try reconnecting to UniFi.');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
function show(screen) {
  clearTimeout(pinSubmitTimer);
  cancelPinKeyPress();
  resetLockSwipe();
  closeDoorStatus();
  if (hubScreens.includes(screen) && !unlockToken) screen='sleep';
  if(screen!=='systems'){closeWifiPasswords();closeSpeedTest();}
  activeScreen = screen;
  for (const id of ['sleep','pin-screen',...hubScreens,'setup-screen','setup-success']) el(id).hidden = id !== screen;
  renderPresentation();
  if(typeof syncAudioScreen==='function')syncAudioScreen();
}
function availableHubPages() { return ['home',...(hubCameras.length ? ['cameras'] : []),'systems','audio']; }
function renderHubNavigation() {
  const pages=availableHubPages(), index=pages.indexOf(activeScreen), unlocked=!!unlockToken && index>=0;
  el('hub-navigation').hidden=!unlocked;
  el('nav-cameras').hidden=!hubCameras.length;
  for(const id of hubScreens)el('nav-'+id).setAttribute('aria-current',activeScreen===id?'page':'false');
  el('hub-previous').disabled=index<=0;
  el('hub-next').disabled=index<0 || index===pages.length-1;
  text('hub-page-hint',index<pages.length-1 && index>=0 ? 'Swipe left for '+hubLabels[pages[index+1]] : 'Swipe right to go back');
}
function moveHub(direction) {
  if(!unlockToken || !hubScreens.includes(activeScreen))return;
  const pages=availableHubPages(), next=pages[pages.indexOf(activeScreen)+direction];
  if(next){show(next);activity();}
}
function clearHub() {
  if(typeof clearAudioHub==='function')clearAudioHub();
  closeEquipmentAction();
  clearTimeout(equipmentNoticeTimer);equipmentNotice=null;
  closeWifiPasswords();
  closeSpeedTest();
  hubCameras=[];swipeStart=null;el('hub-camera-list').textContent='';
  networkGeneration++;networkInventory=null;networkLoading=false;selectedNetworkSite=null;
  for(const page of ['power','network']){
    el('hub-'+page+'-list').textContent='';el('hub-'+page+'-site').textContent='';
    text('hub-'+page+'-status','');
  }
}
async function loadHub() {
  const grant=unlockToken;
  loadNetworkHub();
  try {
    const data=await call('/hub/capabilities',{unlock:grant});
    if(!grant || unlockToken!==grant)return;
    hubCameras=Array.isArray(data.cameras)?data.cameras:[];
    el('hub-camera-list').textContent='';
    for(const camera of hubCameras){
      const row=document.createElement('div'),name=document.createElement('strong'),state=document.createElement('span');
      row.className='hub-device';name.textContent=camera.name;state.textContent=({CONNECTED:'Connected',CONNECTING:'Connecting',DISCONNECTED:'Offline'})[camera.state]||'Status unknown';
      row.append(name,state);el('hub-camera-list').append(row);
    }
    if(activeScreen==='cameras' && !hubCameras.length)show('home');
    renderHubNavigation();
  } catch(error) { /* Optional discovery never interrupts the alarm controls. */ }
}
const networkStateLabels={online:'Online',offline:'Offline',pending:'Pending adoption',firmwareMismatch:'Firmware mismatch',
  updating:'Updating',provisioning:'Provisioning',unreachable:'Unreachable',adopting:'Adopting',removing:'Removing',
  connectionError:'Connection error',adoptionFailed:'Adoption failed',isolated:'Isolated',unknown:'Status unknown'};
const networkKindLabels={accessPoint:'Access point',switch:'Switch',gateway:'Gateway',power:'Smart power',bridge:'Bridge',console:'Console',other:'Device'};
function hubNode(tag,className,value) {
  const node=document.createElement(tag);node.className=className;
  if(value!==undefined)node.textContent=value;
  return node;
}
function equipmentHeading(device) {
  const heading=hubNode('div','equipment-heading'),identity=hubNode('div','equipment-identity');
  identity.append(hubNode('strong','',device.name),hubNode('span','equipment-meta',
    [networkKindLabels[device.kind]||'Device',device.model,device.ip].filter(Boolean).join(' · ')));
  heading.append(identity,hubNode('span','equipment-state '+(device.state==='online'?'is-online':''),networkStateLabels[device.state]||'Status unknown'));
  return heading;
}
const equipmentLabels={rename_device:'Rename device',rename_port:'Rename port',rename_outlet:'Rename outlet',restart_device:'Reboot device',cycle_poe:'Cycle PoE',outlet_on:'Turn on',outlet_off:'Turn off'};
function equipmentControls(device, target=device, kind='device') {
  const controls=hubNode('div','equipment-controls');
  if(!networkInventory?.actionsAvailable)return controls;
  const add=(action,label)=>{const button=hubNode('button','equipment-button',label);button.type='button';
    button.setAttribute('aria-label',label+' · '+target.name);
    button.onclick=()=>openEquipmentAction(device,target,action);controls.append(button);};
  if(target.controls?.rename)add('rename_'+kind,'Rename');
  if(kind==='device' && target.controls?.restart)add('restart_device','Reboot');
  if(kind==='port' && target.controls?.cycle)add('cycle_poe','Cycle PoE');
  if(kind==='outlet' && target.controls?.toggle && typeof target.on==='boolean')add(target.on?'outlet_off':'outlet_on',target.on?'Turn off':'Turn on');
  return controls;
}
function powerDeviceCard(device) {
  const card=hubNode('article','power-device'),header=hubNode('div','power-device-heading');
  header.append(equipmentHeading(device),equipmentControls(device));card.append(header);
  const outlets=hubNode('div','power-outlets');
  const commonNote=device.outlets[0]?.controlNote;
  const sharedNote=commonNote && device.outlets.every(outlet=>outlet.controlNote===commonNote) ? commonNote : '';
  for(const outlet of device.outlets){
    const row=hubNode('article','equipment-card power-outlet'),heading=hubNode('div','equipment-heading');
    const identity=hubNode('div','equipment-identity');
    identity.append(hubNode('strong','',outlet.name));
    if(outlet.name!=='Outlet '+outlet.index)identity.append(hubNode('span','equipment-meta','Outlet '+outlet.index));
    const state=outlet.on===true?'On':outlet.on===false?'Off':'State unknown';
    heading.append(identity,hubNode('span','equipment-state '+(outlet.on===true?'is-online':''),
      state+(typeof outlet.watts==='number'?' · '+outlet.watts.toFixed(1)+' W':'')));
    row.setAttribute('aria-label',device.name+' · '+outlet.name);
    row.append(heading,equipmentControls(device,outlet,'outlet'));
    if(networkInventory.actionsAvailable && outlet.controlNote && !sharedNote)row.append(hubNode('p','equipment-control-note',outlet.controlNote));
    outlets.append(row);
  }
  card.append(outlets);
  if(networkInventory.actionsAvailable && sharedNote)card.append(hubNode('p','equipment-control-note',sharedNote));
  return card;
}
function cancelEquipmentKeyPress() {
  const press=equipmentKeyPress;equipmentKeyPress=null;
  if(press){equipmentKeyClickUntil=performance.now()+750;
    if(press.button.hasPointerCapture?.(press.id))press.button.releasePointerCapture(press.id);}
}
function equipmentNameChanged() {
  const action=equipmentAction;
  if(!action || action.sending || action.committed || !action.request.action.startsWith('rename_'))return;
  action.planId=null;el('equipment-dialog').dataset.reviewed='false';
  el('equipment-submit').disabled=!el('equipment-name').value.trim();
  text('equipment-submit','Review name');text('equipment-effect','Review the new name before applying it.');text('equipment-result','');
  text('equipment-name-count',el('equipment-name').value.length+' / 64');
}
function revealEquipmentNameCaret(input) {
  // Programmatic edits do not scroll a text input like native keystrokes do.
  // Keep the insertion point visible without moving the name field or keyboard.
  if(typeof getComputedStyle!=='function')return;
  const style=getComputedStyle(input),context=document.createElement('canvas').getContext('2d');
  if(!context)return;
  context.font=style.font;
  const caret=context.measureText(input.value.slice(0,input.selectionEnd)).width;
  const width=input.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
  if(caret<input.scrollLeft+8)input.scrollLeft=Math.max(0,caret-8);
  else if(caret>input.scrollLeft+width-8)input.scrollLeft=caret-width+8;
}
function enterEquipmentNameKey(key) {
  const action=equipmentAction,input=el('equipment-name');
  if(!action || action.sending || action.committed || unlockToken!==action.grant || input.disabled || !action.request.action.startsWith('rename_'))return;
  activity();
  if(key==='symbols'){equipmentKeyboardSymbols=!equipmentKeyboardSymbols;renderEquipmentKeyboard();return;}
  if(key==='shift'){equipmentKeyboardShift=!equipmentKeyboardShift;renderEquipmentKeyboard();return;}
  const value=input.value;
  let start=input.selectionStart??value.length,end=input.selectionEnd??start;
  const previous=()=>Math.max(0,start-(Array.from(value.slice(0,start)).pop()?.length||0));
  const next=()=>Math.min(value.length,end+(Array.from(value.slice(end))[0]?.length||0));
  if(key==='left'||key==='right'){
    const caret=key==='left'?(start===end?previous():start):(start===end?next():end);
    input.focus({preventScroll:true});input.setSelectionRange(caret,caret);revealEquipmentNameCaret(input);return;
  }
  let insert=key==='space'?' ':key;
  if(key==='clear'){start=0;end=value.length;insert='';}
  if(key==='backspace'){if(start===end)start=previous();insert='';}
  if(!equipmentKeyboardSymbols && /^[a-z]$/.test(insert))insert=equipmentKeyboardShift?insert.toUpperCase():insert;
  if(value.length-(end-start)+insert.length>64)return;
  input.value=value.slice(0,start)+insert+value.slice(end);
  input.focus({preventScroll:true});input.setSelectionRange(start+insert.length,start+insert.length);
  revealEquipmentNameCaret(input);
  equipmentNameChanged();
  if(!equipmentKeyboardSymbols && /^[a-z]$/i.test(insert) && equipmentKeyboardShift){equipmentKeyboardShift=false;renderEquipmentKeyboard();}
}
function renderEquipmentKeyboard() {
  const keyboard=el('equipment-keyboard');keyboard.textContent='';
  if(keyboard.hidden)return;
  const rows=equipmentKeyboardSymbols?
    ['1234567890'.split(''),['-','/',':','(',')','&','@','#','_'],['.',',','?','!',"'",'"','+','=','backspace']]:
    ['qwertyuiop'.split(''),'asdfghjkl'.split(''),['shift',...'zxcvbnm','backspace']];
  rows.push(['symbols','clear','space','left','right']);
  const labels={shift:'⇧',backspace:'⌫',symbols:equipmentKeyboardSymbols?'ABC':'123',clear:'Clear',space:'Space',left:'←',right:'→'};
  const names={shift:'Shift',backspace:'Backspace',symbols:equipmentKeyboardSymbols?'Letters':'Numbers and symbols',clear:'Clear name',space:'Space',left:'Move cursor left',right:'Move cursor right'};
  for(const keys of rows){
    const row=hubNode('div','text-keyboard-row');
    for(const key of keys){
      const label=labels[key]||(!equipmentKeyboardSymbols && equipmentKeyboardShift?key.toUpperCase():key);
      const button=hubNode('button','text-key',label);button.type='button';button.dataset.key=key;
      button.setAttribute('aria-label',names[key]||label);
      if(key==='shift')button.setAttribute('aria-pressed',String(equipmentKeyboardShift));
      button.disabled=!!equipmentAction?.sending || !!equipmentAction?.committed;
      button.onpointerdown=event=>{
        if(button.disabled || equipmentKeyPress || event.isPrimary===false || event.button!==0)return;
        event.preventDefault();equipmentKeyPress={id:event.pointerId,button};button.setPointerCapture?.(event.pointerId);
      };
      button.onpointerup=event=>{
        if(equipmentKeyPress?.id!==event.pointerId || equipmentKeyPress.button!==button)return;
        const bounds=button.getBoundingClientRect();
        const inside=event.clientX>=bounds.left && event.clientX<=bounds.right && event.clientY>=bounds.top && event.clientY<=bounds.bottom;
        cancelEquipmentKeyPress();event.preventDefault();if(inside)enterEquipmentNameKey(key);
      };
      const cancel=event=>{if(equipmentKeyPress?.id===event.pointerId && equipmentKeyPress.button===button)cancelEquipmentKeyPress();};
      button.onpointercancel=cancel;button.onlostpointercapture=cancel;
      button.onclick=(event={detail:0})=>{
        if((event.detail!==0 || event.pointerType) && (performance.now()<equipmentKeyClickUntil || equipmentKeyPress?.button===button))return;
        enterEquipmentNameKey(key);
      };
      row.append(button);
    }
    keyboard.append(row);
  }
}
function disableEquipmentKeyboard(disabled) {
  for(const button of el('equipment-keyboard').querySelectorAll('button'))button.disabled=disabled;
}
function closeEquipmentAction() {
  cancelEquipmentKeyPress();el('equipment-keyboard').hidden=true;el('equipment-keyboard').textContent='';
  equipmentGeneration++;equipmentAction=null;
  const dialog=el('equipment-dialog');if(dialog.open)dialog.close();
  dialog.dataset.rename='false';dialog.dataset.reviewed='false';
  el('equipment-name').value='';text('equipment-name-count','');
  for(const id of ['equipment-title','equipment-target','equipment-effect','equipment-result'])text(id,'');
}
function openEquipmentAction(device,target,action) {
  if(!unlockToken || networkLoading || !networkInventory?.actionsAvailable)return;
  closeEquipmentAction();closeSpeedTest();activity();
  const rename=action.startsWith('rename_');
  el('equipment-dialog').dataset.rename=String(rename);
  equipmentKeyboardSymbols=false;equipmentKeyboardShift=true;el('equipment-keyboard').hidden=!rename;
  equipmentAction={request:{action,site:networkInventory.site.id,deviceId:device.id,...(target===device?{}:{index:target.index})},grant:unlockToken,page:el('systems').dataset.subtab==='power'?'power':'network',target:target.name};
  text('equipment-title',rename?equipmentLabels[action]:'Are you sure?');
  text('equipment-target',target===device?device.name:device.name+' · '+target.name);
  text('equipment-effect',rename?'Review the change before applying it.':'Checking device…');
  el('equipment-name-label').hidden=!rename;
  el('equipment-name').disabled=!rename;el('equipment-name').required=rename;el('equipment-name').value=rename?target.name:'';
  el('equipment-submit').hidden=false;el('equipment-submit').disabled=!rename;text('equipment-submit',rename?'Review name':equipmentLabels[action]);
  text('equipment-name-count',rename?target.name.length+' / 64':'');renderEquipmentKeyboard();
  text('equipment-cancel','Cancel');el('equipment-dialog').showModal();
  if(rename){el('equipment-name').focus();el('equipment-name').setSelectionRange(0,target.name.length);}
  else{el('equipment-cancel').focus();return reviewEquipmentAction();}
}
el('equipment-cancel').onclick=()=>{closeEquipmentAction();activity();};
el('equipment-dialog').oncancel=()=>closeEquipmentAction();
el('equipment-name').oninput=equipmentNameChanged;
async function reviewEquipmentAction(){
  const action=equipmentAction,generation=equipmentGeneration;
  if(!action || action.sending || unlockToken!==action.grant)return;
  if(action.request.action.startsWith('rename_') && !el('equipment-name').value.trim()){equipmentNameChanged();return;}
  const current=()=>generation===equipmentGeneration && unlockToken===action.grant && equipmentAction===action;
  action.sending=true;el('equipment-submit').disabled=true;el('equipment-name').disabled=true;disableEquipmentKeyboard(true);text('equipment-result','');activity();
  try{
    if(action.request.action.startsWith('rename_'))action.request.name=el('equipment-name').value;
    const plan=await call('/hub/network/review',{unlock:action.grant,request:action.request});
    if(!current())return;
    action.planId=plan.planId;action.target=plan.target;text('equipment-target',plan.target);text('equipment-effect',plan.effect);
    el('equipment-dialog').dataset.reviewed='true';
    text('equipment-submit',action.request.action.startsWith('rename_')?'Save name':equipmentLabels[action.request.action]);
  }catch(error){
    if(!current())return;
    action.planId=null;text('equipment-result',error.message);text('equipment-submit','Review again');
  }finally{
    if(current()){action.sending=false;el('equipment-submit').disabled=false;
      el('equipment-name').disabled=!action.request.action.startsWith('rename_');disableEquipmentKeyboard(false);}
  }
}
function equipmentFeedback(action,result){
  const label=action.request.action==='restart_device'?'Reboot requested.':
    action.request.action==='cycle_poe'?'Power cycle requested.':result.message;
  const notice={page:action.page,grant:action.grant,message:action.target+' · '+label};
  clearTimeout(equipmentNoticeTimer);equipmentNotice=notice;
  text('hub-'+action.page+'-status',notice.message);
  equipmentNoticeTimer=setTimeout(()=>{
    if(equipmentNotice!==notice)return;
    equipmentNotice=null;
    if(unlockToken===notice.grant)renderNetworkHub({keepRows:true});
  },8000);
}
el('equipment-form').onsubmit=async event=>{
  event.preventDefault();const action=equipmentAction,generation=equipmentGeneration;
  if(!action || action.sending || action.committed || unlockToken!==action.grant)return;
  if(!action.planId)return reviewEquipmentAction();
  const current=()=>generation===equipmentGeneration && unlockToken===action.grant && equipmentAction===action;
  action.sending=true;action.committed=true;
  el('equipment-submit').disabled=true;el('equipment-name').disabled=true;disableEquipmentKeyboard(true);text('equipment-result','');activity();
  const planId=action.planId;action.planId=null;
  try{
    const result=await call('/hub/network/apply',{unlock:action.grant,planId,confirm:true});
    if(!current())return;
    if(['submitted','verified'].includes(result.status)){
      closeEquipmentAction();activity();equipmentFeedback(action,result);
      loadNetworkHub(selectedNetworkSite,{preserveView:true});
    }else{
      text('equipment-result',result.message);el('equipment-submit').hidden=true;text('equipment-cancel','Close');
    }
  }catch(error){
    if(!current())return;
    text('equipment-result',error.message);el('equipment-submit').hidden=true;text('equipment-cancel','Close');
  }finally{
    // A consumed or uncertain write can never be resubmitted from this dialog.
    if(current())action.sending=false;
  }
};
function renderNetworkHub({keepRows=false}={}) {
  const data=networkInventory,ready=data?.state==='ready';
  el('hub-wifi-passwords').hidden=!data?.wifiPasswordsAvailable;
  el('hub-wifi-passwords').disabled=!ready || networkLoading || !unlockToken;
  el('hub-speedtest').hidden=!data?.speedTestAvailable;
  el('hub-speedtest').disabled=!ready || networkLoading || !unlockToken;
  for(const page of ['power','network']){
    const list=el('hub-'+page+'-list'),selector=el('hub-'+page+'-site');
    if(!keepRows)list.textContent='';selector.textContent='';
    el('hub-'+page+'-refresh').disabled=networkLoading;
    text('hub-'+page+'-refresh',networkLoading?'Refreshing…':'Refresh');
    el('hub-'+page+'-site-label').hidden=!data?.sites?.length || data.sites.length<2;
    selector.disabled=networkLoading;
    if(data?.sites?.length>1){
      const placeholder=hubNode('option','','Choose a site');placeholder.value='';placeholder.disabled=true;
      placeholder.selected=!data.site;selector.append(placeholder);
      for(const site of data.sites){const option=hubNode('option','',site.name);option.value=site.id;option.selected=site.id===data.site?.id;selector.append(option);}
    }
    text('hub-'+page+'-status',equipmentNotice?.page===page && equipmentNotice.grant===unlockToken?equipmentNotice.message:networkLoading?'Finding your devices…':ready?
      data.site.name+' · Updated '+new Date(data.observedAt).toLocaleTimeString([],{hour:'numeric',minute:'2-digit',second:'2-digit'}):
      data?.message||'Unlock to discover your devices.');
    text('hub-'+page+'-hint',data?.actionsAvailable ? (page==='power'?'Individual relay controls appear where supported.':'Restart and power controls require the alarm to be disarmed.') : 'Equipment controls will be available after the next service update.');
    if(keepRows || !ready || networkLoading)continue;
    const devices=(data.devices||[]).filter(device=>page==='power'?device.kind==='power'||device.outlets?.length:device.kind!=='power');
    if(!devices.length){
      const empty=hubNode('div','hub-empty');
      empty.append(hubNode('strong','',page==='power'?'No smart outlets found':'No network equipment found'),
        hubNode('p','',page==='power'?'Adopt smart plugs or outlets in UniFi Network, then tap Refresh. Switch PoE ports are on the Network page.':'No equipment was returned for this site. Check your devices in UniFi Network.'));
      list.append(empty);continue;
    }
    for(const device of devices){
      if(page==='power' && device.outlets?.length){list.append(powerDeviceCard(device));continue;}
      const card=hubNode('article','equipment-card');card.append(equipmentHeading(device),equipmentControls(device));
      if(page==='power'){
        card.append(hubNode('p','equipment-meta','This device is not reporting its individual outlets.'));
      }else if(device.ports?.length){
        const details=hubNode('details','equipment-ports'),poeCount=device.ports.filter(port=>port.poe).length;
        details.dataset.deviceId=device.id;
        details.setAttribute('data-no-swipe','');
        details.append(hubNode('summary','',device.ports.length+' ports'+(poeCount?' · '+poeCount+' PoE':'')+' — view status'));
        for(const port of device.ports){
          const row=hubNode('div','equipment-row'),identity=hubNode('div','equipment-identity');
          const link=port.linkUp===true?'Connected':port.linkUp===false?'No link':'Link unknown';
          identity.append(hubNode('strong','',port.name),hubNode('span','equipment-meta','Port '+port.index+' · '+link+
            (port.linkUp===true && typeof port.speedMbps==='number'?' · '+port.speedMbps+' Mbps':'')));
          let power='';
          if(port.poe){power=port.poePowered===true?'PoE supplying power':port.poeEnabled===false?'PoE off':port.poeEnabled===true?'PoE enabled':'PoE unknown';
            if(typeof port.watts==='number')power+=' · '+port.watts.toFixed(1)+' W';}
          row.append(identity,hubNode('span','outlet-reading',power),equipmentControls(device,port,'port'));details.append(row);
        }
        card.append(details);
      }
      list.append(card);
    }
  }
}
async function loadNetworkHub(site=selectedNetworkSite,{preserveView=false}={}) {
  const grant=unlockToken;if(!grant)return;
  closeWifiPasswords();
  closeSpeedTest();
  const view=preserveView?['power','network'].map(page=>{
    const list=el('hub-'+page+'-list');
    return {page,top:list.scrollTop,expanded:new Set(Array.from(list.querySelectorAll('.equipment-ports[open]')).map(node=>node.dataset.deviceId))};
  }):[];
  if(site!==selectedNetworkSite){clearTimeout(equipmentNoticeTimer);equipmentNotice=null;}
  const generation=++networkGeneration;selectedNetworkSite=site;networkLoading=true;renderNetworkHub({keepRows:preserveView});
  try{
    const data=await call('/hub/network',{unlock:grant,...(site?{site}:{})});
    if(unlockToken!==grant || networkGeneration!==generation)return;
    networkInventory=data;selectedNetworkSite=data.site?.id||null;
  }catch(error){
    if(unlockToken!==grant || networkGeneration!==generation)return;
    networkInventory={state:'unavailable',message:error.message,sites:[],devices:[]};
  }
  if(unlockToken!==grant || networkGeneration!==generation)return;
  networkLoading=false;renderNetworkHub();
  for(const saved of view){
    const list=el('hub-'+saved.page+'-list');
    for(const details of list.querySelectorAll('.equipment-ports'))details.open=saved.expanded.has(details.dataset.deviceId);
    list.scrollTop=saved.top;
  }
}
for(const page of ['power','network']){
  el('hub-'+page+'-refresh').onclick=()=>{activity();loadNetworkHub();};
  el('hub-'+page+'-site').onchange=event=>{activity();loadNetworkHub(event.target.value);};
}
const systemsTabs={power:el('systems-tab-power'),network:el('systems-tab-network'),wifi:el('systems-tab-wifi')};
function selectSystemsTab(page){
  if(!systemsTabs.power||!systemsTabs.network)return;
  for(const p of ['power','network','wifi']){
    const active=p===page;
    systemsTabs[p].classList.toggle('is-active',active);
    systemsTabs[p].setAttribute('aria-selected',active?'true':'false');
  }
  el('systems').dataset.subtab=page;
  updateSystemsPanels(page);
  activity();
}
systemsTabs.power.onclick=()=>selectSystemsTab('power');
systemsTabs.network.onclick=()=>selectSystemsTab('network');
systemsTabs.wifi.onclick=()=>selectSystemsTab('wifi');
function updateSystemsPanels(page){
  for(const p of ['power','network','wifi']){
    const panel=document.getElementById('systems-panel-'+p);
    if(panel)panel.hidden=(p!==page);
  }
}
function closeWifiPasswords() {
  wifiGeneration++;wifiView=null;clearTimeout(wifiHideTimer);
  const dialog=el('wifi-dialog');if(dialog.open)dialog.close();
  el('wifi-pin').value='';el('wifi-list').textContent='';text('wifi-result','');text('wifi-site','');
  el('wifi-pin-form').hidden=false;el('wifi-networks').hidden=true;el('wifi-submit').disabled=false;
  text('wifi-submit','Show Wi-Fi passwords');
}
function openWifiPasswords() {
  if(!unlockToken || activeScreen!=='systems' || networkLoading || networkInventory?.state!=='ready' || !networkInventory.wifiPasswordsAvailable)return;
  closeWifiPasswords();closeSpeedTest();closeEquipmentAction();activity();
  wifiView={generation:wifiGeneration,grant:unlockToken,site:networkInventory.site.id,busy:false};
  text('wifi-site',networkInventory.site.name);
  el('wifi-dialog').showModal();el('wifi-pin').focus();
}
function wifiViewCurrent(view) {
  return wifiView===view && wifiGeneration===view.generation && unlockToken===view.grant &&
    activeScreen==='systems' && networkInventory?.site?.id===view.site;
}
el('hub-wifi-passwords').onclick=openWifiPasswords;
el('wifi-close').onclick=()=>{closeWifiPasswords();activity();};
el('wifi-dialog').oncancel=()=>closeWifiPasswords();
function closeSpeedTest() {
  clearTimeout(speedTestTimer);speedTestView=null;
  const dialog=el('speedtest-dialog');if(dialog.open)dialog.close();
  el('speedtest-results').hidden=true;
  for(const id of ['gateway','phase','message','result-label','download','upload','latency'])text('speedtest-'+id,'');
  el('speedtest-start').disabled=true;text('speedtest-start','Start test');
}
function speedTestCurrent(view) {
  return speedTestView===view && unlockToken===view.grant && activeScreen==='systems' && networkInventory?.site?.id===view.site;
}
async function openSpeedTest() {
  if(!unlockToken || activeScreen!=='systems' || networkLoading || networkInventory?.state!=='ready' || !networkInventory.speedTestAvailable)return;
  closeWifiPasswords();closeEquipmentAction();closeSpeedTest();activity();
  const view={grant:unlockToken,site:networkInventory.site.id,busy:false,retryRead:false,until:performance.now()+120000};
  speedTestView=view;el('speedtest-dialog').showModal();
  await updateSpeedTest(view);
}
async function updateSpeedTest(view,start=false) {
  if(!speedTestCurrent(view) || view.busy)return;
  clearTimeout(speedTestTimer);view.busy=true;el('speedtest-start').disabled=true;
  text('speedtest-phase',start?'Starting test…':'Checking gateway…');
  try {
    const data=await call('/hub/network/speedtest/'+(start?'start':'status'),{unlock:view.grant,site:view.site});
    if(!speedTestCurrent(view))return;
    view.retryRead=false;
    if(start)view.until=performance.now()+120000;
    text('speedtest-gateway',data.gateway?.name||'UniFi gateway');
    text('speedtest-phase',data.phase);text('speedtest-message',data.message);
    const result=data.result;
    el('speedtest-results').hidden=!result;
    if(result){
      text('speedtest-result-label',(data.state==='completed'?'Tested ':'Last result · ')+new Date(result.testedAt).toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}));
      for(const [name,key] of [['download','downloadMbps'],['upload','uploadMbps'],['latency','latencyMs']]){
        const value=result[key];text('speedtest-'+name,typeof value==='number' && Number.isFinite(value)?value.toLocaleString([],{maximumFractionDigits:1}):'—');
      }
    }
    text('speedtest-start',data.state==='running'?'Testing…':result?'Run again':'Start test');
    el('speedtest-start').disabled=!data.canStart;
    if(data.state==='running'){
      if(performance.now()<view.until)speedTestTimer=setTimeout(()=>updateSpeedTest(view),5000);
      else {view.retryRead=true;text('speedtest-phase','Still waiting for UniFi');text('speedtest-start','Check result');el('speedtest-start').disabled=false;}
    }
  }catch(error){
    if(!speedTestCurrent(view))return;
    // A failed start response is ambiguous. The next tap only reads status.
    view.retryRead=true;text('speedtest-phase',start?'Start not confirmed':'Test unavailable');
    text('speedtest-message',start?'Check the result before starting another test. No second test has been sent.':error.message);
    text('speedtest-start','Check result');el('speedtest-start').disabled=false;
  }finally{if(speedTestCurrent(view))view.busy=false;}
}
el('hub-speedtest').onclick=openSpeedTest;
el('speedtest-start').onclick=()=>{activity();const view=speedTestView;if(view && !el('speedtest-start').disabled)return updateSpeedTest(view,!view.retryRead);};
el('speedtest-close').onclick=()=>{closeSpeedTest();activity();};
el('speedtest-dialog').oncancel=closeSpeedTest;
el('wifi-number-pad').onclick=event=>{
  const button=event.target.closest('button[data-key]');
  if(!button || !wifiView || wifiView.busy)return;
  const key=button.dataset.key,input=el('wifi-pin');
  input.value=key==='clear'?'':key==='back'?input.value.slice(0,-1):(input.value+key).slice(0,8);
};
el('wifi-pin').oninput=()=>{el('wifi-pin').value=el('wifi-pin').value.replace(/\D/g,'').slice(0,8);};
el('wifi-pin-form').onsubmit=async event=>{
  event.preventDefault();
  const view=wifiView;if(!view || view.busy || !wifiViewCurrent(view))return;
  const pin=el('wifi-pin').value;el('wifi-pin').value='';
  if(!/^\d{4,8}$/.test(pin)){text('wifi-result','Enter your 4–8 digit administrator PIN.');return;}
  view.busy=true;el('wifi-submit').disabled=true;text('wifi-submit','Checking…');text('wifi-result','');activity();
  try{
    const data=await call('/hub/wifi/reveal',{unlock:view.grant,site:view.site,pin});
    if(!wifiViewCurrent(view))return;
    el('wifi-pin').value='';el('wifi-pin-form').hidden=true;el('wifi-networks').hidden=false;
    el('wifi-list').textContent='';
    for(const network of data.networks||[]){
      const row=hubNode('article','wifi-network');
      row.append(hubNode('h3','',network.name));
      const details=[network.enabled===false?'Disabled':network.enabled===true?'Enabled':'Status unknown',...(network.hidden?['Hidden network']:[])];
      row.append(hubNode('p','wifi-network-state',details.join(' · ')));
      if(network.passwordState==='available' && typeof network.password==='string'){
        row.append(hubNode('span','wifi-password-label','Password'),hubNode('p','wifi-password',network.password));
      }else row.append(hubNode('p','wifi-unavailable',network.message||'No shared password returned by UniFi.'));
      el('wifi-list').append(row);
    }
    if(!data.networks?.length)el('wifi-list').append(hubNode('p','wifi-unavailable','No saved Wi-Fi networks were found for this site.'));
    text('wifi-result','');
    // The deadline is absolute. Interaction cannot leave credentials visible indefinitely.
    wifiHideTimer=setTimeout(()=>{if(wifiViewCurrent(view)){closeWifiPasswords();message('Wi-Fi passwords hidden. Enter your PIN again to view them.');}},30000);
    el('wifi-close').focus();
  }catch(error){
    if(wifiViewCurrent(view)){text('wifi-result',error.message);el('wifi-pin').focus();}
  }finally{
    if(wifiViewCurrent(view)){view.busy=false;el('wifi-submit').disabled=false;text('wifi-submit','Show Wi-Fi passwords');}
  }
};
for(const id of hubScreens)el('nav-'+id).onclick=()=>{if(unlockToken && availableHubPages().includes(id)){show(id);activity();}};
el('hub-previous').onclick=()=>moveHub(-1);
el('hub-next').onclick=()=>moveHub(1);
function resetLockSwipe() {
  const pointer=lockSwipe?.id;
  lockSwipe=null;
  const handle=el('swipe-unlock');
  if(pointer!==undefined && handle.hasPointerCapture?.(pointer))handle.releasePointerCapture(pointer);
  el('sleep').dataset.swiping='false';
  el('sleep').style?.setProperty('--swipe-lift','0px');
}
// Only a deliberate upward drag from the bottom opens PIN entry. It never
// grants access or sends an alarm command. Keyboard/assistive activation remains.
const swipeUnlock=el('swipe-unlock');
swipeUnlock.onpointerdown=event=>{
  resetLockSwipe();
  if(busy || activeScreen!=='sleep' || swipeUnlock.hidden || event.isPrimary===false || event.button!==0)return;
  lockSwipe={id:event.pointerId,x:event.clientX,y:event.clientY,at:performance.now()};
  swipeUnlock.setPointerCapture?.(event.pointerId);
  el('sleep').dataset.swiping='true';
};
swipeUnlock.onpointermove=event=>{
  if(!lockSwipe || lockSwipe.id!==event.pointerId)return;
  const lift=Math.max(0,Math.min(100,lockSwipe.y-event.clientY));
  el('sleep').style?.setProperty('--swipe-lift',`${lift}px`);
};
swipeUnlock.onpointerup=event=>{
  const start=lockSwipe;
  if(!start || start.id!==event.pointerId)return;
  resetLockSwipe();
  const rise=start.y-event.clientY,side=Math.abs(event.clientX-start.x);
  if(activeScreen==='sleep' && !swipeUnlock.hidden && !busy && performance.now()-start.at<=1800 && rise>=60 && rise>side*1.4){
    event.preventDefault();openPin();
  }
};
swipeUnlock.onpointercancel=resetLockSwipe;
swipeUnlock.onlostpointercapture=resetLockSwipe;
swipeUnlock.onkeydown=event=>{
  if(['Enter',' '].includes(event.key) && activeScreen==='sleep' && !swipeUnlock.hidden){event.preventDefault();openPin();}
};
swipeUnlock.onclick=event=>{
  if(event.detail===0 && activeScreen==='sleep' && !swipeUnlock.hidden)openPin();
};
document.addEventListener('pointerdown',event=>{
  swipeStart=null;
  if(!unlockToken || !hubScreens.includes(activeScreen) || event.isPrimary===false || event.button!==0)return;
  if(!event.target.closest('.hub-screen') || event.target.closest('button,a,input,select,textarea,video,[data-no-swipe]'))return;
  swipeStart={id:event.pointerId,x:event.clientX,y:event.clientY,at:performance.now(),screen:activeScreen};
});
document.addEventListener('pointerup',event=>{
  const start=swipeStart;swipeStart=null;
  if(!start || start.id!==event.pointerId || start.screen!==activeScreen)return;
  const dx=event.clientX-start.x,dy=event.clientY-start.y;
  if(performance.now()-start.at<=900 && Math.abs(dx)>=60 && Math.abs(dx)>Math.abs(dy)*1.4){event.preventDefault();moveHub(dx<0?1:-1);}
});
document.addEventListener('pointercancel',()=>{swipeStart=null;});
document.addEventListener('keydown',event=>{
  if(event.target.closest?.('#hub-navigation') && ['ArrowLeft','ArrowRight'].includes(event.key)){
    event.preventDefault();moveHub(event.key==='ArrowLeft'?-1:1);
  }
});
function message(text) { el('message').textContent = text; }
function lock() {
  closeArmReview();
  clearLogin();
  closeSignInSuccess();
  clearHub();
  const closeSettings=settingsOpen;
  settingsGeneration++;
  settingsOpen=false;settingsOnUnlock=false;
  if(typeof clearSettingsManagement==='function')clearSettingsManagement();
  clearTimeout(settingsIdleTimer);clearTimeout(settingsExpiryTimer);
  for(const id of ['key','new-pin','entry-enable-pin'])el(id).value='';
  const previous = unlockToken; unlockToken = ''; activityRevision++; disarmOnUnlock = false; clearTimeout(expiryTimer); clearTimeout(idleTimer);
  el('pin').value = ''; show('sleep');
  if (previous) call('/lock', {unlock:previous}).catch(()=>{});
  if(closeSettings)return call('/settings/lock').catch(()=>{});
}
function setAutoLock(seconds) {
  autoLockSeconds = [60,120,180].includes(seconds) ? seconds : 60;
  text('hub-idle',`Locks after ${autoLockSeconds/60} minute${autoLockSeconds===60?'':'s'} idle`);
}
function grantExpiry(grant, seconds, started=performance.now()) {
  clearTimeout(expiryTimer);
  expiryTimer=setTimeout(()=>{if(unlockToken===grant)lock();},Math.max(0,started+seconds*1000-performance.now()));
}
async function renewHubActivity() {
  const grant=unlockToken, revision=++activityRevision, started=performance.now();
  if(!grant)return;
  try {
    const data=await call('/hub/activity',{unlock:grant});
    if(grant!==unlockToken || revision!==activityRevision)return;
    setAutoLock(data.autoLockSeconds);
    grantExpiry(grant,data.expiresIn,started);
  } catch(error) {
    // A failed renewal cannot revive a revoked/expired grant. Other requests,
    // including background Network polling, never extend access.
    if(grant===unlockToken && revision===activityRevision){lock();message('Enter your PIN to unlock again.');}
  }
}
function activity(interaction=false) {
  clearTimeout(idleTimer);
  if(settingsOpen){clearTimeout(settingsIdleTimer);settingsIdleTimer=setTimeout(()=>{lock();message('Settings locked after inactivity.');},autoLockSeconds*1000);}
  if (activeScreen !== 'sleep' && activeScreen !== 'setup-screen' && activeScreen !== 'setup-success') idleTimer = setTimeout(lock,autoLockSeconds*1000);
  if(interaction && unlockToken)renewHubActivity();
}
for (const event of ['pointerdown','keydown']) document.addEventListener(event,()=>activity(true));
document.addEventListener('visibilitychange',()=>{if(document.hidden) lock();});
globalThis.addEventListener?.('pagehide',()=>lock());
function render(data) {
  status = data;
  if(typeof data.pinSet==='boolean')pinConfigured=data.pinSet;
  if (!liveObservation && typeof data.exitDeadline === 'number') exitDeadline = data.exitDeadline;
  el('state').textContent = data.canControl ? data.state : 'Status unverified';
  el('pin-setup').hidden = !!data.pinSet || pinConfigured === true;
  el('disarm').disabled = busy || !data.webhookReady || !unlockToken;
  el('arm-stay').disabled = el('arm-away').disabled = busy || !data.webhookReady || !unlockToken;
  el('arm-stay').hidden = !data.armStayConfigured;
  el('arm-away').hidden = !data.armAwayConfigured;
  if (liveObservation) renderObservation(liveObservation);
  else renderPresentation();
}
async function refresh() {
  try {
    let data;
    try { data=await call('/status'); }
    catch(error) {
      if(error.message!=='Reload the local app to continue.')throw error;
      // A service update rotates the page token, not the saved API key.
      const response=await fetch('/session');
      if(!response.ok)throw error;
      const session=await response.json();
      await lock();
      token=session.token;pinConfigured=session.pinSet;localConnection=null;
      data=await call('/status');
    }
    apiConnectionMissing=false;
    render(data);
    if(data.connected && /API connection|Connect to your console first|Reload the local app/.test(el('message').textContent||''))message('');
  } catch(error) {
    apiConnectionMissing=error.message==='Connect to your console first.';
    render({connected:false});
    message(localConnection===true && error.message==='Connect to your console first.' ? 'Your UniFi account is signed in. Reconnect the API connection in Settings.' : error.message);
  }
}
function openPin(disarm = false) {
  if (busy) return;
  message('');
  if(!(pinConfigured || status?.pinSet)) return openSettings();
  else {disarmOnUnlock=disarm;show('pin-screen'); el('pin').focus({preventScroll:true});activity();}
}
function requestDisarm() {
  if (busy) return;
  if (confirmedDisarmed()) {alreadyDisarmed();return;}
  if (unlockToken) return sendAlarmCommand('disarm');
  openPin(true);
}
function enterSettings(access) {
  settingsOnUnlock=false;settingsOpen=true;show('setup-screen');
  clearTimeout(settingsExpiryTimer);
  if(access.pinSet)settingsExpiryTimer=setTimeout(()=>{lock();message('Settings access expired. Enter your PIN again.');},access.expiresIn*1000);
  activity();message('');
  if(typeof openSettingsSection==='function')openSettingsSection(settingsTarget);
}
async function openSettings(target='overview') {
  if(busy)return;
  await lock();
  settingsTarget=typeof target==='string'?target:'overview';
  const attempt=settingsGeneration;
  try{
    const access=await call('/settings/status');pinConfigured=access.pinSet;
    if(attempt!==settingsGeneration)return;
    if(access.settingsUnlocked)enterSettings(access);
    else {settingsOnUnlock=true;show('pin-screen');el('pin').focus();activity();message('');}
  }catch(error){message(error.message);}
}
const reconnect=()=>openSettings('connection');
el('reconnect').onclick=reconnect;
el('wake').onclick=()=>{
  const mode=alarmPresentation(liveObservation || {}).mode;
  if (['exit','entry','breach'].includes(mode)) return requestDisarm();
  openPin();
};
el('strip-cancel').onclick=requestDisarm;
el('back').onclick = el('lock').onclick = lock;
el('settings').onclick=openSettings;
el('close-setup').onclick=lock;
el('setup-continue').onclick=()=>{message('');lock();};
function cancelPinKeyPress() {
  const press=pinKeyPress;pinKeyPress=null;
  press?.finished();
  if(press?.button.hasPointerCapture?.(press.id))press.button.releasePointerCapture(press.id);
}
function enterPinKey(value) {
  if(busy || activeScreen!=='pin-screen' || el('pin').disabled)return;
  const input=el('pin');
  if(value==='Clear')input.value='';
  else if(value==='⌫')input.value=input.value.slice(0,-1);
  else if(input.value.length<8)input.value+=value;
  queuePinSubmit();
}
for (const value of ['1','2','3','4','5','6','7','8','9','Clear','0','⌫']) {
  const button=document.createElement('button');button.type='button';button.textContent=value;
  let pointerClickUntil=0;
  button.setAttribute('aria-label',value==='⌫'?'Delete last digit':value);
  button.onpointerdown=event=>{
    if(busy || activeScreen!=='pin-screen' || pinKeyPress || event.isPrimary===false || event.button!==0)return;
    // Keep focus in the PIN field. Touch entry does not depend on Chromium's
    // subsequent compatibility click, which can be lost after the swipe.
    event.preventDefault();
    pointerClickUntil=0;
    pinKeyPress={id:event.pointerId,button,finished:()=>{pointerClickUntil=performance.now()+1000;}};
    button.setPointerCapture?.(event.pointerId);
  };
  button.onpointerup=event=>{
    if(!pinKeyPress || pinKeyPress.id!==event.pointerId || pinKeyPress.button!==button)return;
    const bounds=button.getBoundingClientRect();
    const inside=event.clientX>=bounds.left && event.clientX<=bounds.right && event.clientY>=bounds.top && event.clientY<=bounds.bottom;
    cancelPinKeyPress();event.preventDefault();
    if(inside)enterPinKey(value);
  };
  const cancel=event=>{if(pinKeyPress?.id===event.pointerId && pinKeyPress.button===button)cancelPinKeyPress();};
  button.onpointercancel=cancel;button.onlostpointercapture=cancel;
  // Keep click-only browsers/accessibility tools working as well. Suppress a
  // pointer's follow-up click, but never swallow keyboard activation.
  button.onclick=(event={detail:0})=>{
    if((event.detail!==0 || event.pointerType) && (performance.now()<pointerClickUntil || pinKeyPress?.button===button))return;
    enterPinKey(value);
  };
  el('number-pad').append(button);
}
function queuePinSubmit(){
  clearTimeout(pinSubmitTimer);
  if(busy||activeScreen!=='pin-screen')return;
  const value=el('pin').value;
  if(!/^[0-9]{4,8}$/.test(value))return;
  // A pause marks the end of a variable-length PIN. Never send a request for
  // every digit or relax the server's attempt limit for automatic entry.
  pinSubmitTimer=setTimeout(()=>{
    if(!busy&&activeScreen==='pin-screen'&&el('pin').value===value)submitPin();
  },value.length===8?0:1000);
}
el('pin').oninput=queuePinSubmit;
el('unlock-form').onsubmit=event=>{event.preventDefault();return submitPin();};
async function submitPin(){
  clearTimeout(pinSubmitTimer);
  if(busy||activeScreen!=='pin-screen')return;
  const pin=el('pin').value;
  if(!/^[0-9]{4,8}$/.test(pin)){message('Enter your 4–8 digit PIN.');return;}
  busy=true;el('pin').disabled=true;el('unlock-button').disabled=true;
  for(const button of el('number-pad').querySelectorAll('button'))button.disabled=true;
  message('Checking PIN…');
  const shouldDisarm=disarmOnUnlock;
  const forSettings=settingsOnUnlock;
  const settingsAttempt=settingsGeneration;
  el('pin').value='';
  try {
    if(forSettings){
      const access=await call('/settings/unlock',{pin});
      if(settingsAttempt===settingsGeneration)enterSettings(access);
      else await call('/settings/lock');
    }
    else {
      const data=await call('/unlock',{pin});
      if(settingsAttempt!==settingsGeneration){await call('/lock',{unlock:data.unlock});return;}
      unlockToken=data.unlock;
      setAutoLock(data.autoLockSeconds);
      grantExpiry(unlockToken,data.expiresIn);
      if (shouldDisarm) await sendAlarmCommand('disarm');
      else {show('home');message('');activity();loadHub();}
    }
  }
  catch(error){message(error.message);}
  finally{
    busy=false;el('pin').disabled=false;el('unlock-button').disabled=false;
    for(const button of el('number-pad').querySelectorAll('button'))button.disabled=false;
    render(status||{});
    if(activeScreen==='pin-screen')el('pin').focus();
  }
}
async function sendAlarmCommand(action, extra = {}) {
  if(action==='disarm' && confirmedDisarmed()) {alreadyDisarmed();return;}
  const armAction=action==='arm'||action==='arm-stay'||action==='arm-away';
  const armNoun=action==='arm-stay'?'Arm Stay':action==='arm-away'?'Arm Away':'Arm';
  busy=true;render(status||{});message(action==='disarm'?'Sending Disarm request…':'Sending '+armNoun+' request…');
  try {
    const data=await call('/webhook/'+action,{...extra,confirm:true,unlock:unlockToken});
    if(armAction)exitDeadline=data.exitDeadline||0;
    render(data);lock();
    message(action==='disarm'?'Disarm requested. Waiting for Protect to confirm…':armNoun+' request accepted. Verify the state in Protect.');
    await observeEntry();
  } catch(error) {
    if(action==='disarm') {
      // The server consumes an unlock token for every disarm attempt. A retry
      // needs the PIN again; never cancel the displayed timer on a failed send.
      const previous=unlockToken;unlockToken='';clearTimeout(expiryTimer);
      if(previous)call('/lock',{unlock:previous}).catch(()=>{});
      disarmOnUnlock=true;show('pin-screen');el('pin').focus();activity();
    } else lock();
    message(error.message);
  } finally {busy=false;render(status||{});}
}
function closeArmReview() {
  armReviewGeneration++;armReview=null;
  if(el('arm-review-dialog').open)el('arm-review-dialog').close();
  el('mute-bypass').checked=false;
}
async function beginArmReview(mode='arm') {
  if(!liveObservation?.arming)return sendAlarmCommand(mode);
  const revision=++armReviewGeneration, grant=unlockToken;
  busy=true;render(status||{});message('Checking door coverage…');
  try {
    const plan=await call('/arm/review',{unlock:grant,mode});
    if(revision!==armReviewGeneration || grant!==unlockToken || !unlockToken)return;
    if(!plan.needsConfirmation){await sendAlarmCommand(mode,{planId:plan.planId});return;}
    armReview=plan;armReviewMode=mode;
    text('arm-review-doors',plan.bypassed.map(s=>s.name+' — '+s.reason).join('\n'));
    text('arm-review-active','Will protect: '+plan.active.map(s=>s.name).join(' · '));
    el('mute-bypass').checked=false;el('confirm-bypass').disabled=false;
    el('arm-review-dialog').showModal();activity();message('');
  } catch(error){message(error.message);}
  finally {busy=false;render(status||{});}
}
el('cancel-bypass').onclick=closeArmReview;
el('arm-review-dialog').oncancel=closeArmReview;
el('confirm-bypass').onclick=async()=>{
  if(!armReview || !unlockToken || busy)return;
  const plan=armReview, mute=el('mute-bypass').checked;
  closeArmReview();
  await sendAlarmCommand(armReviewMode,{planId:plan.planId,confirmBypass:true,muteBypass:mute});
};
el('arm-stay').onclick=()=>{
  if(busy||!unlockToken)return;
  return beginArmReview('arm-stay');
};
el('arm-away').onclick=()=>{
  if(busy||!unlockToken)return;
  return beginArmReview('arm-away');
};
el('disarm').onclick=requestDisarm;
el('setup').onsubmit=async event=>{
  event.preventDefault(); const key=el('key').value;el('key').value='';
  try{render(await call('/connect',{host:el('host').value,key}));message('Console connected.');}catch(error){message(error.message);}
};
el('pin-setup').onsubmit=async event=>{
  event.preventDefault();const pin=el('new-pin').value;el('new-pin').value='';
  try{await call('/pin/setup',{pin});pinConfigured=true;await refresh();lock();message('PIN saved. Swipe up to unlock.');}catch(error){message(error.message);}
};
function clock(){const now=new Date();el('clock').textContent=now.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});el('date').textContent=now.toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});}
clock();setInterval(clock,1000);
fetch('/session').then(r=>r.json()).then(async data=>{
  if(el('installer-link')) el('installer-link').hidden=!data.installerAvailable;
  token=data.token;
  setAutoLock(data.autoLockSeconds);
  pinConfigured=data.pinSet;
  if(data.site) {
    siteDoors=data.site.doors;
    text('site-name',data.site.name);
    text('configured-doors',siteDoors.length ? 'Configured doors: '+siteDoors.join(', ')+'. Verify each sensor before enabling the delay.' : 'Installer setup required: configure this site’s doors and alarm rules.');
  }
  await refresh();
}).catch(()=>message('Local server unavailable.'));
setInterval(()=>{if(!busy&&token)refresh();},10000);

function exitDisplay() {
  const remaining = Math.max(0, Math.ceil((exitDeadline - performance.now())/1000));
  const output = el('exit-countdown');
  output.hidden = !exitDeadline;
  output.textContent = remaining ? `Exit countdown · ${remaining}s ${verifiedExit ? '(Protect timing)' : '(estimated)'}` : 'Exit time elapsed · Verify armed state in Protect';
  renderPresentation();
}
setInterval(exitDisplay, 250);

function setLocalConnection(connected) {
  localConnection=connected;
  el('local-login').hidden=connected;
  el('local-connected-note').hidden=!connected;
  renderConnection();
}
function closeSignInSuccess() {
  if(el('signin-success').open)el('signin-success').close();
}
function showSignInSuccess() {
  const needsApi=!status?.connected;
  text('signin-success-detail','Your UniFi account is connected.');
  text('signin-success-next',needsApi ? 'Reconnect the API connection to restore alarm controls. You do not need to sign in to your account again.' : 'You can continue to your keypad.');
  el('signin-finish-connection').hidden=!needsApi;
  el('signin-success').showModal();
  el('signin-continue').focus();
}
el('signin-continue').onclick=()=>{closeSignInSuccess();message('');lock();};
el('signin-finish-connection').onclick=()=>{
  closeSignInSuccess();
  el('advanced-api').open=true;
  el('advanced-api').scrollIntoView?.({block:'center'});
  el('key').focus();activity();
};
async function localResult(data, {announce=false,current=()=>true}={}) {
  if(!current())return;
  el('verification-field').hidden = !data.mfaRequired;
  el('verification-code').required = !!data.mfaRequired;
  el('local-submit').textContent = data.mfaRequired ? 'Verify and sign in' : 'Sign in locally';
  el('local-result').textContent = (data.message || '') + (data.profiles ? '\n' + data.profiles.map(p => `${p.title || 'Unnamed profile'}: ${p.state || 'Unknown'}`).join(' · ') : '');
  if(typeof data.localConnected==='boolean')setLocalConnection(data.localConnected);
  if (data.localConnected && Array.isArray(data.profiles)) {
    await refresh();
    if(!current())return;
    message('');renderConnection();
    if(announce)showSignInSuccess();
  } else if(data.localConnected===false) {
    closeSignInSuccess();
  }
}
function clearLogin() {
  pendingLogin = null; clearTimeout(loginTimer); loginGeneration++;
  el('local-password').value = ''; el('verification-code').value = '';
  el('local-password').required = true; el('local-password-field').hidden = false;
  el('verification-field').hidden = true; el('verification-code').required = false;
  el('local-host').disabled = false; el('local-user').disabled = false;
  el('cancel-verification').hidden = true;
  el('local-submit').textContent = 'Sign in locally';
}
el('cancel-verification').onclick=()=>{clearLogin();el('local-result').textContent='Sign-in cancelled.';};
el('local-login').onsubmit = async event => {
  event.preventDefault(); el('local-submit').disabled=true;
  const attempt = ++loginGeneration;
  const settingsAttempt=settingsGeneration;
  let credentials = pendingLogin || {host:el('local-host').value,username:el('local-user').value,password:el('local-password').value,expires:performance.now()+180000};
  const verificationCode=el('verification-code').value;
  el('local-password').value='';el('verification-code').value='';
  el('local-result').textContent='Signing in to your console…';
  try {
    if (performance.now() >= credentials.expires) throw new Error('Verification expired. Please sign in again.');
    const data = await call('/local/login',{host:credentials.host,username:credentials.username,password:credentials.password,verificationCode});
    if (attempt !== loginGeneration) return;
    if (data.mfaRequired) {
      pendingLogin = credentials;
      clearTimeout(loginTimer);
      loginTimer=setTimeout(()=>{clearLogin();el('local-result').textContent='Verification expired. Please sign in again.';},Math.max(0,credentials.expires-performance.now()));
      el('local-password').required=false;el('local-password-field').hidden=true;
      el('local-host').disabled=true;el('local-user').disabled=true;
      el('cancel-verification').hidden=false;
      data.message='Enter the six-digit authenticator code for this UniFi account. This is not your keypad PIN.';
    } else { clearLogin(); }
    const resultGeneration=loginGeneration;
    await localResult(data,{announce:!data.mfaRequired,current:()=>settingsAttempt===settingsGeneration && resultGeneration===loginGeneration});
    if(data.mfaRequired) el('verification-code').focus();
  } catch(error) {
    if(attempt===loginGeneration){clearLogin();el('local-result').textContent=error.message;message(error.message);}
  } finally {credentials=null;el('local-submit').disabled=false;}
};
for(const [id,path] of [['local-refresh','/local/status'],['local-logout','/local/logout']]) el(id).onclick=async()=>{
  clearLogin();
  const generation=loginGeneration,settingsAttempt=settingsGeneration;
  const current=()=>generation===loginGeneration && settingsAttempt===settingsGeneration;
  try{await localResult(await call(path),{announce:path==='/local/status',current});}catch(error){if(!current())return;setLocalConnection(false);el('verification-field').hidden=true;el('verification-code').required=false;el('local-submit').textContent='Sign in locally';el('local-result').textContent=error.message;message(error.message);}
};

let observing = false;
function observationFresh(data) {
  return !!data.fresh && (typeof data.receivedAt !== 'number' || performance.now()-data.receivedAt < 8000);
}
function confirmedDisarmed() {
  return liveObservation && observationFresh(liveObservation) && liveObservation.state==='disarmed' && exitDeadline<=performance.now() && !entryDeadline;
}
function alreadyDisarmed() {
  disarmOnUnlock=false;
  if(unlockToken && activeScreen==='pin-screen')show('home');
  message('The system is already disarmed.');
  renderPresentation();
}
function renderConnection() {
  const api=!!status?.connected, fresh=observationFresh(liveObservation||{});
  const lost=!!liveObservation && !fresh;
  const apiMissing=liveObservation?.apiConfigured===false || apiConnectionMissing;
  el('connection-dot').classList.toggle('online',api && fresh);
  const signedIn=liveObservation?.localConnected===false ? false : localConnection===true || fresh;
  text('connection',signedIn ? (api ? (fresh ? 'UniFi connected · '+(status?.pinSet?'PIN ready':'Set a PIN below') : 'UniFi signed in · API connected · Alarm status unverified') : apiMissing ? 'UniFi signed in · API connection needed' : 'UniFi signed in · Checking API connection') : (api ? 'API connected · UniFi sign-in needed' : apiMissing ? 'UniFi connection needed' : 'Checking UniFi connection'));
  text('connection-summary',signedIn ? (api ? 'UniFi connected' : apiMissing ? 'Signed in · API connection needed' : 'Checking API connection') : 'UniFi needs reconnecting');
  el('connection-alert').hidden=!lost || activeScreen==='setup-screen' || activeScreen==='setup-success';
  text('connection-alert-title','Alarm status unavailable');
  const signInNeeded=liveObservation?.localConnected===false || /sign in|sign-in|session/i.test(liveObservation?.message||'');
  text('connection-alert-detail',signedIn && !api && apiMissing ? 'Your UniFi account is signed in. Reconnect the API connection in Settings to restore alarm controls.' : api && signInNeeded ? 'Your saved API key is working. Sign in to UniFi to restore alarm status. You can still try Disarm with your PIN.' : 'Arming is paused until the connection returns. Check Protect for the current alarm state.');
  text('reconnect',signedIn && !api && apiMissing ? 'Finish connection' : 'Reconnect UniFi');
}
function armingBlockReason(data) {
  if (!observationFresh(data)) {
    if(data.localConnected===false)return 'Sign in to UniFi in Settings to restore alarm status.';
    if(data.apiConfigured===false || apiConnectionMissing)return 'Connect the API in Settings to restore alarm status.';
    return 'Waiting for a verified alarm state from Protect.';
  }
  if(data.state!=='disarmed')return '';
  if(data.arming){
    if(!data.arming.canReview)return data.arming.bypassed.some(door=>door.code==='unchecked') ? 'Open and close each alarm door once to finish its sensor check.' : 'No alarm doors are ready. Check Door status.';
    if(data.arming.bypassed.length)return 'Arm the available doors and review any bypasses.';
    return '';
  }
  const names=siteDoors || Object.keys(data.doors||{});
  const unchecked=names.filter(name=>!(data.verifiedDoors||[]).includes(name));
  if(!data.liveDelayEnabled && unchecked.length)return 'Finish sensor checks in Settings: '+unchecked.join(' · ');
  const unavailable=names.filter(name=>!data.doors?.[name]?.connected || data.doors[name].position==='unknown');
  if(unavailable.length)return 'Check sensor connection: '+unavailable.join(' · ');
  const open=names.filter(name=>data.doors?.[name]?.position==='open');
  if(open.length)return 'Close before arming: '+open.join(' · ');
  if(!data.liveDelayEnabled)return data.recoveryMessage || 'Enable protection in Settings to arm.';
  return '';
}
function canArm(data) {
  if(exitDeadline>performance.now())return false;
  if(data.arming)return observationFresh(data) && data.state==='disarmed' && data.arming.canReview;
  const doors = Object.values(data.doors || {});
  const names=Object.keys(data.doors || {});
  const coverage=siteDoors ? siteDoors.length>0 && names.length===siteDoors.length && siteDoors.every(name=>names.includes(name)) : doors.length===3;
  return observationFresh(data) && data.state === 'disarmed' && data.liveDelayEnabled && coverage && doors.every(door=>door.connected && door.position==='closed');
}
function countdownTime(seconds) { return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`; }
function alarmPresentation(data) {
  const fresh = observationFresh(data), state = fresh ? data.state : 'unknown';
  const entry = entryDeadline ? Math.max(0,Math.ceil((entryDeadline-performance.now())/1000)) : null;
  const exit = exitDeadline ? Math.max(0,Math.ceil((exitDeadline-performance.now())/1000)) : null;
  // A native breach can arrive before our notification delay finishes. Keep the
  // entry countdown visible; a normal exit countdown must never become a breach.
  if (state !== 'disarmed' && state !== 'arming') {
    if (entry !== null && entry > 0) return {mode:'entry',remaining:entry,state,fresh};
    if (entry === 0 || state === 'breached' || ['sending','accepted','uncertain'].includes(data.notification)) return {mode:'breach',state,fresh};
  }
  if (state === 'arming' || (['disarmed','unknown'].includes(state) && exit > 0)) return {mode:'exit',remaining:exit,state,fresh};
  return {mode:state,state,fresh};
}
function renderPresentation() {
  const data = liveObservation || {}, view = alarmPresentation(data);
  if (view.fresh && view.state === 'disarmed') alarmTone = 'blue';
  else if (['armed','breached'].includes(view.state) || ['entry','breach'].includes(view.mode)) alarmTone = 'red';
  document.body.dataset.tone = alarmTone;
  document.body.dataset.alarmMode = view.mode;
  document.body.dataset.screen = activeScreen;
  el('arm-stay').disabled = el('arm-away').disabled = busy || !status?.webhookReady || !unlockToken || !canArm(data);
  el('disarm').disabled = busy || !status?.webhookReady || !unlockToken || confirmedDisarmed();
  el('wake').disabled = el('strip-cancel').disabled = busy;
  const counting = ['exit','entry'].includes(view.mode), urgent = counting || view.mode === 'breach';
  const armed = ['armed','breached'].includes(view.state) || ['entry','breach'].includes(view.mode);
  const labels = {disarmed:'Disarmed',armed:'Armed',exit:'Arming',entry:'Entry delay',breach:'THERE HAS BEEN A BREACH',unknown:'Status unverified'};
  text('state', labels[view.mode] || 'Status unverified');
  text('sleep-state', labels[view.mode] || 'Status unverified');
  el('standby-display').hidden = urgent;
  el('alarm-display').hidden = !urgent;
  el('countdown-face').hidden = !counting;
  el('breach-display').hidden = view.mode !== 'breach';
  text('alarm-phase',view.mode === 'exit' ? 'TIME TO LEAVE' : view.mode === 'entry' ? 'TIME TO DISARM' : 'SECURITY ALERT');
  if (counting) {
    text('countdown-value',view.remaining === null ? '—:—' : countdownTime(view.remaining));
    el('countdown-face').setAttribute('aria-label',`${view.mode === 'exit' ? 'Exit' : 'Entry'} delay: ${view.remaining === null ? 'waiting for confirmation' : view.remaining+' seconds remaining'}`);
    el('timer-progress').setAttribute('stroke-dashoffset',String(60-Math.min(60,view.remaining ?? 60)));
  }
  let detail = view.mode === 'exit' ? (view.remaining === 0 ? 'Confirming the system is armed…' : verifiedExit ? 'Leave and close the door before time runs out.' : 'Confirming the exit delay with Protect…') : view.mode === 'entry' ? 'Tap Disarm and enter your PIN before time runs out.' : 'Tap Disarm and enter your PIN to stop the alarm.';
  if (!view.fresh && urgent) detail = 'Connection unverified. Check Protect; you can still try Disarm.';
  text('alarm-detail',detail);
  const disarmLabel=view.mode === 'exit' ? 'Cancel arming' : 'Disarm';
  el('wake').hidden=!urgent;
  swipeUnlock.hidden=urgent;
  swipeUnlock.setAttribute('aria-disabled',String(busy));
  if(urgent && lockSwipe)resetLockSwipe();
  text('wake-label',disarmLabel);
  text('disarm-label',confirmedDisarmed() ? 'Disarmed' : disarmLabel);text('strip-cancel',disarmLabel);
  const icon = armed ? 'closed' : view.state === 'disarmed' || view.mode === 'exit' ? 'open' : 'unknown';
  for (const id of ['home-lock','standby-lock']) lockIcon(id,icon);
  for (const id of ['arm-stay-lock','arm-away-lock']) lockIcon(id,'closed');
  lockIcon('disarm-lock','open');
  // An armed system always offers Disarm, even with every door closed and no
  // entry countdown. Keep the closed lock in the status, not a disabled button.
  el('arm-stay').hidden = armed || urgent || !status?.armStayConfigured;
  el('arm-away').hidden = armed || urgent || !status?.armAwayConfigured;
  text('pin-title',settingsOnUnlock ? 'Unlock settings' : disarmOnUnlock ? (view.mode === 'exit' ? 'Cancel arming' : 'Disarm system') : 'Unlock keypad');
  text('pin-help',settingsOnUnlock ? 'Enter your administrator PIN. Settings open automatically.' : disarmOnUnlock ? (counting ? 'Enter your PIN to stop the alarm and countdown.' : 'Enter your PIN to disarm the system.') : 'Enter your PIN. The keypad unlocks automatically.');
  text('unlock-button',settingsOnUnlock ? 'Open settings' : disarmOnUnlock ? disarmLabel : 'Unlock');
  el('alert-strip').hidden = !urgent || activeScreen === 'sleep';
  el('strip-cancel').hidden = activeScreen === 'pin-screen';
  text('strip-label',view.mode === 'entry' ? 'Disarm before time runs out' : view.mode === 'exit' ? 'Exit countdown' : 'Alarm active · Disarm');
  text('strip-time',counting && view.remaining !== null ? countdownTime(view.remaining) : '');
  el('entry-observation').hidden = activeScreen !== 'setup-screen' && view.fresh && data.notification !== 'uncertain';
  renderConnection();
  renderHubNavigation();
}
function acceptObservation(data) {
  const previousState = liveObservation?.state;
  if(typeof data.localConnected==='boolean' && localConnection!==data.localConnected)setLocalConnection(data.localConnected);
  liveObservation = {...data,receivedAt:performance.now()};
  if (data.fresh && ['disarmed','arming'].includes(data.state)) entryDeadline = 0;
  else if (typeof data.remaining === 'number') entryDeadline = performance.now()+Math.max(0,data.remaining)*1000;
  if (data.fresh) {
    if (data.state === 'arming' && typeof data.exitRemaining === 'number') {
      exitDeadline = performance.now()+data.exitRemaining*1000; verifiedExit = true;
    } else if (data.state==='disarmed' && data.pendingArmSeconds>0) {
      exitDeadline=performance.now()+data.pendingArmSeconds*1000;verifiedExit=false;
    } else if (['armed','breached','disarmed'].includes(data.state)) {
      exitDeadline = 0; verifiedExit = false;
    }
    if (previousState !== data.state && /request accepted|Sending (Arm|Disarm|command)/.test(el('message').textContent)) message('');
    if(data.state==='disarmed' && el('message').textContent==='Disarm requested. Waiting for Protect to confirm…') message('');
  } else {
    verifiedExit = false;
    if(data.pendingArmSeconds>0)exitDeadline=performance.now()+data.pendingArmSeconds*1000;
  }
  renderObservation(liveObservation);
  if(typeof window!=='undefined' && typeof window.parent?.postMessage==='function' && window.parent!==window){
    window.parent.postMessage({type:'astra-startup',ready:true,
      urgent:['arming','armed','breached'].includes(data.state)||typeof data.remaining==='number'},'http://127.0.0.1:8764');
  }
}
function renderObservation(data) {
  const needsSetup=observationFresh(data) && data.state==='disarmed' && !(data.arming?.canReview || data.liveDelayEnabled);
  el('finish-protection').hidden=!needsSetup;
  el('state-note').textContent=data.sirenMessage || armingBlockReason(data) || (data.liveDelayEnabled ? 'Entry delay: '+(data.activeDoors || Object.keys(data.doors || {})).join(' · ') : 'Alarm status verified by Protect');
  const active=observationFresh(data) && ['arming','armed','breached'].includes(data.state);
  const frozen=data.activeDoors!=null && data.state!=='disarmed';
  const bypasses=frozen ? (data.bypassedDoors||[]) : (data.arming?.bypassed||[]);
  const permanent=(data.statusSensors||[]).filter(s=>s.mode==='bypass');
  const unavailable=active ? (data.activeDoors || []).filter(name=>!data.doors?.[name]?.connected || data.doors[name].position==='unknown') : [];
  const unmuted=bypasses.filter(s=>!s.mutedUntil);
  el('bypass-summary').hidden=!unmuted.length && !permanent.length && !unavailable.length;
  const pendingChecks=!frozen && !active ? unmuted.filter(s=>s.code==='unchecked') : [];
  const otherBypasses=unmuted.filter(s=>!pendingChecks.includes(s));
  const count=(n,singular,plural)=>n+' '+(n===1?singular:plural);
  text('bypass-text',[
    pendingChecks.length ? count(pendingChecks.length,'sensor check needed','sensor checks needed') : '',
    otherBypasses.length ? count(otherBypasses.length,active||frozen?'door bypassed':'door needs review',active||frozen?'doors bypassed':'doors need review') : '',
    permanent.length ? count(permanent.length,'sensor bypassed in Settings','sensors bypassed in Settings') : '',
    unavailable.length ? count(unavailable.length,'sensor unavailable while armed','sensors unavailable while armed') : ''
  ].filter(Boolean).join(' · '));
  text('bypass-detail',[
    pendingChecks.length ? 'Open and close these doors once while disarmed: '+pendingChecks.map(s=>s.name).join(' · ')+'.' : '',
    otherBypasses.length ? (active?'Bypassed this arm cycle: ':frozen?'Bypasses from the last arm request: ':'Bypass review needed: ')+otherBypasses.map(s=>s.name+' ('+s.reason+')').join(' · ') : '',
    permanent.length ? 'Bypassed in Settings: '+permanent.map(s=>s.name).join(' · ') : '',
    unavailable.length ? 'Sensor unavailable while armed: '+unavailable.join(' · ') : ''
  ].filter(Boolean).join('\n'));
  const dismissable=observationFresh(data) && data.state==='disarmed' && !active && !frozen && otherBypasses.length>0;
  if(bypassChoosingPending && unlockToken && dismissable){bypassChoosing=true;bypassChoosingPending=false;}
  el('bypass-dismiss').hidden=!dismissable || bypassChoosing;
  el('bypass-choices').hidden=!dismissable || !bypassChoosing;
  const rows=Object.entries(data.doors||{}).map(([name,door])=>name+' — '+(door.connected?door.position:'unreachable')+' · '+(pendingChecks.some(s=>s.name===name)?'check needed':bypasses.some(s=>s.name===name)?'bypass':'alarm'));
  rows.push(...(data.statusSensors||[]).map(s=>s.name+' — '+(s.connected?s.position:'unreachable')+' · '+(s.mode==='bypass'?'bypassed':'status only')));
  text('home-sensor-status',rows.join('\n'));
  if(armReview && (!observationFresh(data) || data.state!=='disarmed'))closeArmReview();
  if(equipmentAction && ['arming','armed','breached'].includes(data.state))closeEquipmentAction();
  if(wifiView && (data.state==='breached' || typeof data.remaining==='number'))closeWifiPasswords();
  if(speedTestView && (data.state==='breached' || typeof data.remaining==='number'))closeSpeedTest();
  if(['exit','entry','breach'].includes(alarmPresentation(data).mode))closeDoorStatus();
  if(['exit','entry','breach'].includes(alarmPresentation(data).mode)&&typeof closeAudioGrouping==='function')closeAudioGrouping();
  el('door-coverage').textContent=Object.entries(data.doors || {}).map(([name,door])=>name+': '+(door.connected ? door.position : 'offline')).join(' · ');
  el('arm-stay').disabled=el('arm-away').disabled=busy || !status?.webhookReady || !unlockToken || !canArm(data);
  renderPresentation();
}
el('finish-protection').onclick=()=>openSettings('sensors');
function closeDoorStatus(){
  const dialog=el('door-status-dialog');if(dialog.open)dialog.close();
}
function openDoorStatus(){
  if(!unlockToken){openPin();return;}
  activity();el('door-status-dialog').showModal();el('close-door-status').focus();
}
el('door-status-button').onclick=el('bypass-details').onclick=openDoorStatus;
el('close-door-status').onclick=()=>{closeDoorStatus();activity();};
let bypassMuteSending = false;
el('bypass-dismiss').onclick=()=>{
  if(busy||bypassMuteSending)return;
  if(!unlockToken){bypassChoosingPending=true;openPin();message('Enter your PIN to dismiss the reminder.');return;}
  bypassChoosing=true;
  el('bypass-dismiss').hidden=true;
  el('bypass-choices').hidden=false;
};
el('bypass-choices').querySelectorAll('button[data-days]').forEach(btn=>{
  btn.onclick=async()=>{
    if(busy||bypassMuteSending||!unlockToken)return;
    bypassMuteSending=true;
    try{
      const result=await call('/bypass/mute',{unlock:unlockToken,days:parseInt(btn.dataset.days,10)});
      bypassChoosing=false;bypassChoosingPending=false;
      message('Bypass reminder dismissed for '+result.mutedDays+' day'+(result.mutedDays===1?'':'s')+'.');
      refresh();
    }catch(error){message(error.message);}
    finally{bypassMuteSending=false;}
  };
});
async function observeEntry() {
  if (!token || observing) return;
  observing = true;
  try {
    const data = await call('/entry/status');
    const detail=localConnection===true && data.apiConfigured===false && !data.fresh ? 'Reconnect the API connection to restore door events and alarm controls.' : data.message;
    el('entry-observation').textContent = data.fresh && data.state==='disarmed' && data.arming?.canReview && !data.liveDelayEnabled ? 'One-minute exit and entry delays will be checked before arming.' : (data.liveDelayEnabled ? 'ENTRY DELAY · ' : 'ENTRY TEST · ') + (data.remaining !== null ? data.remaining + ' seconds · ' : '') + detail;
    acceptObservation(data);
  } catch(error) { liveObservation={...liveObservation,fresh:false}; verifiedExit=false; renderObservation(liveObservation); el('entry-observation').textContent = 'Entry observation unavailable. Delayed protection cannot be verified; check Protect.'; }
  finally { observing = false; }
}
setInterval(observeEntry,1000);

el('entry-enable-form').onsubmit=async event=>{
  event.preventDefault();
  const pin=el('entry-enable-pin').value;el('entry-enable-pin').value='';
  try {const result=await call('/entry/enable',{pin,immediateRuleAdjusted:el('entry-rule-adjusted').checked});message(result.message);await observeEntry();}
  catch(error){message(error.message);}
};
renderPresentation();

document.addEventListener('phone-api-connected',()=>{refresh();message('Console connected. API key saved on this device.');});

/* Ambient lock-screen light. It cannot capture a touch, unlock the panel, or
   change alarm state. Hidden/PIN/countdown screens do not spend time drawing. */
(()=>{
  const canvas=el('lock-aurora');
  if(typeof canvas?.getContext!=='function')return;
  const context=canvas.getContext('2d');if(!context)return;
  const screen=el('sleep'),display=el('standby-display'),time=el('clock'),status=el('standby-status');
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let frame=0,last=0,width=0,height=0,cx=0,cy=0,radius=0,scale=1;
  let touched=-100,touchAngle=0,started=performance.now(),previousTone='';
  const visible=()=>document.body.dataset.screen==='sleep'&&!screen.hidden&&!display.hidden&&!document.hidden&&width>0&&height>0;
  function draw(seconds){
    if(reduced.matches)seconds=0;
    const armed=document.body.dataset.tone==='red';
    const hue=armed?352+Math.sin(seconds*.12)*9:(220+seconds*9)%360;
    const pulse=reduced.matches?0:Math.max(0,1-(seconds-touched)/2);
    const r=radius*(1+.035*Math.sin(seconds*.8)+pulse*.035);
    context.setTransform(scale,0,0,scale,0,0);context.clearRect(0,0,width,height);
    const glow=context.createRadialGradient(cx,cy,r*.28,cx,cy,r*1.3);
    glow.addColorStop(0,`hsla(${hue},95%,65%,0)`);
    glow.addColorStop(.55,`hsla(${hue},95%,65%,.035)`);
    glow.addColorStop(.74,`hsla(${hue+30},95%,68%,.13)`);
    glow.addColorStop(1,`hsla(${hue+50},95%,68%,0)`);
    context.fillStyle=glow;context.fillRect(0,0,width,height);
    context.globalCompositeOperation='lighter';
    const gradient=context.createLinearGradient(cx-r,cy-r,cx+r,cy+r);
    for(let n=0;n<=6;n++)gradient.addColorStop(n/6,`hsla(${hue+n*(armed?3:30)},92%,${67+n%2*9}%,.8)`);
    for(let layer=0;layer<6;layer++){
      context.beginPath();
      for(let n=0;n<=128;n++){
        const angle=n/128*Math.PI*2;
        const flow=Math.sin(angle*3+seconds*.38+layer*.7)*.027+Math.sin(angle*5-seconds*.23+layer)*.012;
        const ripple=pulse*.024*Math.cos(angle-touchAngle);
        const rr=r*(1+(layer-2.5)*.019+flow+ripple);
        const x=cx+Math.cos(angle)*rr,y=cy+Math.sin(angle)*rr;
        if(n===0)context.moveTo(x,y);else context.lineTo(x,y);
      }
      context.closePath();context.strokeStyle=gradient;
      context.lineWidth=layer===2?2:layer===4?1.15:.65;
      context.globalAlpha=layer===2?.62:layer===4?.43:.22;
      context.shadowColor=`hsl(${hue+25},95%,72%)`;context.shadowBlur=layer===2?10:0;context.stroke();
    }
    context.shadowBlur=0;
    for(let n=0;n<3;n++){
      const angle=seconds*(n%2?-.075:.065)+n*2.3;
      context.beginPath();context.arc(cx,cy,r*(1.095+n*.024),angle,angle+.34+n*.12);
      context.strokeStyle=`hsl(${hue+n*(armed?5:45)},95%,80%)`;context.lineWidth=.85;
      context.globalAlpha=.22+.08*Math.sin(seconds*.4+n)**2;context.stroke();
    }
    context.globalAlpha=1;context.globalCompositeOperation='source-over';
  }
  function stop(){if(frame)cancelAnimationFrame(frame);frame=0;canvas.dataset.motion='paused';}
  function tick(now){
    frame=0;if(!visible())return stop();
    // Slow movement needs at most 24 drawings/second, at a bounded resolution.
    if(now-last>=1000/24){draw((now-started)/1000);last=now;}
    if(!reduced.matches)frame=requestAnimationFrame(tick);
  }
  function sync(){
    // The canvas spans the lock screen, so hide it explicitly for urgent states.
    canvas.hidden=!visible();
    if(!visible())return stop();
    const tone=document.body.dataset.tone;
    if(reduced.matches){stop();canvas.dataset.motion='still';if(tone!==previousTone)draw(0);}
    else if(!frame){canvas.dataset.motion='running';frame=requestAnimationFrame(tick);}
    previousTone=tone;
  }
  function resize(){
    const box=screen.getBoundingClientRect(),clockBox=time.getBoundingClientRect(),statusBox=status.getBoundingClientRect();
    width=box.width;height=box.height;scale=Math.min(devicePixelRatio||1,1.25);
    canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
    const clockCenter=clockBox.top+clockBox.height/2,statusCenter=statusBox.top+statusBox.height/2;
    // Lower the light toward the status badge without moving text or touch targets.
    cx=width/2;cy=clockCenter*.4+statusCenter*.6-box.top;
    // Breathing + touch can expand by 7%; the glow reaches 1.3× that radius.
    // Reserve the entire 1.4× envelope plus padding on all four edges.
    radius=Math.max(0,Math.min(width*.34,(Math.min(cx,width-cx,cy,height-cy)-8)/1.4,230));
    if(visible())draw((performance.now()-started)/1000);
    sync();
  }
  const observer=new MutationObserver(sync);
  observer.observe(document.body,{attributes:true,attributeFilter:['data-screen','data-tone']});
  observer.observe(display,{attributes:true,attributeFilter:['hidden']});
  if(typeof ResizeObserver==='function'){
    const sizes=new ResizeObserver(resize);sizes.observe(screen);sizes.observe(display);
  }
  addEventListener('resize',resize);document.addEventListener('visibilitychange',sync);
  reduced.addEventListener('change',()=>{previousTone='';stop();resize();});
  screen.addEventListener('pointerdown',event=>{
    if(!visible()||reduced.matches||event.isPrimary===false||event.button!==0)return;
    const box=canvas.getBoundingClientRect();
    touchAngle=Math.atan2(event.clientY-box.top-cy,event.clientX-box.left-cx);
    touched=(performance.now()-started)/1000;
  },{passive:true});
  canvas.addEventListener('contextlost',stop);
  canvas.addEventListener('contextrestored',resize);
  resize();
})();
