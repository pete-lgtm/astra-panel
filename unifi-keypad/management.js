// Administrator settings. Names and history from the server are always rendered as text.
let settingsSection = 'overview', managedSetup = false, settingsUser = null;
let managementRevision = 0, editingUser = null, pinField = 'user-pin';
let sirenData=null, sirenSelection=new Set(), sirenPlan=null, sirenBusy=false;
let sensorData = null, sensorSelection = new Set(), sensorModes = {}, sensorPlan = null, activityCursor = null;
const settingsPages = ['overview','users','activity','sensors','sirens','panel','connection'];
function node(tag, content, className) {
  const item = document.createElement(tag);
  if(content !== undefined) item.textContent = content;
  if(className) item.className = className;
  return item;
}
function settingsNotice(value) { text('settings-message', value); }
function clearUserForm() {
  editingUser=null;
  for(const id of ['user-name','user-pin','user-pin-confirm','user-duress-pin','user-duress-pin-confirm']) el(id).value='';
  el('user-expires').value='';el('user-duress-remove').checked=false;el('user-duress-remove-label').hidden=true;
  el('user-editor').hidden=true;el('user-list').hidden=false;
  el('save-user').disabled=false;
  el('user-pin-pad').hidden=true;
}
function clearSettingsManagement() {
  document.dispatchEvent?.(new Event('browser-pair-hide'));
  managementRevision++;clearUserForm();settingsUser=null;sensorData=null;sensorPlan=null;
  sensorSelection=new Set();sensorModes={};activityCursor=null;sirenData=null;sirenSelection=new Set();sirenPlan=null;sirenBusy=false;
  for(const id of ['user-list','activity-list','sensor-list','sensor-review-detail','siren-list','siren-review-detail']) el(id).replaceChildren();
  settingsNotice('');
}
async function managementTask(button, operation) {
  const generation=settingsGeneration, revision=managementRevision;
  const current=()=>settingsOpen && generation===settingsGeneration && revision===managementRevision;
  if(button)button.disabled=true;
  try { await operation(current); }
  catch(error) { if(current())settingsNotice(error.message); }
  finally { if(button && current())button.disabled=false; }
}
async function loadSettingsSummary(current=()=>settingsOpen) {
  const data=await call('/settings/summary');
  if(!current())return;
  settingsUser=data.user;managedSetup=data.managed;
  text('settings-identity',data.user ? data.user.name+' · Administrator' : 'Installer setup');
  text('users-summary',data.users+' '+(data.users===1?'person':'people')+' with keypad access');
  text('sensors-summary',(data.doors||[]).join(' · ') || 'Choose the doors to protect');
  if(typeof setLocalConnection==='function')setLocalConnection(data.localConnected);
  text('connection-summary',data.localConnected ? (data.apiConnected ? 'UniFi connected' : 'Signed in · API connection needed') : 'UniFi needs reconnecting');
  el('entry-setup').hidden=managedSetup;
  el('reconnect-details').open=!data.localConnected;
  el('advanced-api').open=!data.apiConnected;
  for(const id of ['local-host','host'])if(!el(id).value)el(id).value=data.consoleHost||'';
  el('manage-users').disabled=!data.user;el('manage-activity').disabled=!data.user;
  el('manage-panel').disabled=!data.user;
  if(typeof setAutoLock==='function')setAutoLock(data.autoLockSeconds);
  const seconds=[60,120,180].includes(data.autoLockSeconds)?data.autoLockSeconds:60;
  el('auto-lock').value=String(seconds);
  text('panel-summary',`Locks after ${seconds/60} minute${seconds===60?'':'s'} idle`);
  if(!data.user)settingsNotice('Complete alarm setup to create your administrator PIN.');
}
async function openSettingsSection(section='overview') {
  if(!settingsOpen)return;
  if(settingsSection==='connection' && section!=='connection'){
    if(typeof clearLogin==='function')clearLogin();
    el('key').value='';
  }
  managementRevision++;sirenBusy=false;el('refresh-sirens').disabled=false;el('cancel-sirens').disabled=false;clearUserForm();settingsSection=section;settingsNotice('');
  for(const name of settingsPages)el('settings-'+name).hidden=name!==section;
  el('settings-section-back').hidden=section==='overview';
  el('settings-identity').hidden=section!=='overview';
  el('sensor-help').open=false;
  el('setup-screen').scrollIntoView?.({block:'start'});
  return managementTask(null,async current=>{
    if(['overview','connection','panel'].includes(section))await loadSettingsSummary(current);
    if(section==='users')await loadUsers(current);
    if(section==='activity')await loadHistory(current,false);
    if(section==='sensors')await loadSensors(current);
    if(section==='sirens')await loadSirens(current);
  });
}
for(const section of settingsPages.slice(1))el('manage-'+section).onclick=()=>openSettingsSection(section);
for(const button of document.querySelectorAll('.settings-back'))button.onclick=()=>openSettingsSection();

el('panel-settings-form').onsubmit=event=>{
  event.preventDefault();
  return managementTask(el('save-panel-settings'),async current=>{
    const result=await call('/settings/panel/save',{autoLockSeconds:Number(el('auto-lock').value)});
    if(!current())return;
    setAutoLock(result.autoLockSeconds);activity();
    settingsNotice(`Saved. The panel locks after ${result.autoLockSeconds/60} minute${result.autoLockSeconds===60?'':'s'} without a touch.`);
  });
};

function toLocalInput(epochSeconds) {
  const date=new Date(epochSeconds*1000), pad=n=>String(n).padStart(2,'0');
  return date.getFullYear()+'-'+pad(date.getMonth()+1)+'-'+pad(date.getDate())+'T'+pad(date.getHours())+':'+pad(date.getMinutes());
}
async function loadUsers(current) {
  const data=await call('/settings/users');if(!current())return;
  el('user-list').replaceChildren();
  for(const user of data.users) {
    const row=node('div',undefined,'settings-row'), detail=node('div');
    const badges=[];
    if(user.expired)badges.push('Temporary PIN expired');
    else if(user.expires_at)badges.push('Temporary until '+localDate(user.expires_at*1000));
    if(user.has_duress_pin)badges.push('Duress PIN set');
    const sub=(user.role==='admin'?'Administrator':'User')+(user.enabled?'':' · Access disabled')+(badges.length?' · '+badges.join(' · '):'');
    detail.append(node('strong',user.name+(user.id===settingsUser?.id?' · You':'')),
      node('p',sub,'sub small'));
    const edit=node('button','Edit','compact-button');edit.type='button';edit.onclick=()=>editUser(user);
    row.append(detail,edit);el('user-list').append(row);
  }
}
function editUser(user=null) {
  clearUserForm();settingsNotice('');editingUser=user;
  el('user-editor').hidden=false;el('user-list').hidden=true;
  text('user-editor-title',user?'Edit '+user.name:'Add user');
  el('user-name').value=user?.name||'';el('user-role').value=user?.role||'user';
  el('user-enabled').checked=user?.enabled??true;
  for(const id of ['user-pin','user-pin-confirm'])el(id).required=!user;
  text('user-pin-hint',user?'Leave both PIN fields blank to keep the current PIN. Changing this user signs them out.':'Choose a unique PIN of 4–8 digits. PINs are never shown after saving.');
  el('user-expires').value=user?.expires_at?toLocalInput(user.expires_at):'';
  el('user-duress-remove-label').hidden=!user?.has_duress_pin;
  text('user-duress-hint',user?.has_duress_pin?'A duress PIN is already set for this person. Enter a new one to replace it, or tick “Remove duress PIN”. It disarms like the normal PIN but silently alerts you.':'Optional: a duress PIN disarms like the normal PIN but silently alerts you. Useful if someone is forced to disarm under threat.');
  pinField='user-pin';el('user-name').focus();
}
el('add-user').onclick=()=>editUser();el('cancel-user').onclick=()=>{clearUserForm();settingsNotice('');};
for(const id of ['user-pin','user-pin-confirm','user-duress-pin','user-duress-pin-confirm'])el(id).onfocus=()=>{pinField=id;el('user-pin-pad').hidden=false;};
for(const id of ['user-name','user-role'])el(id).onfocus=()=>{el('user-pin-pad').hidden=true;};
for(const digit of ['1','2','3','4','5','6','7','8','9','Clear','0','⌫']) {
  const button=node('button',digit);button.type='button';
  button.setAttribute('aria-label',digit==='⌫'?'Delete last PIN digit':digit);
  button.onclick=()=>{const input=el(pinField);if(digit==='Clear')input.value='';else if(digit==='⌫')input.value=input.value.slice(0,-1);else if(input.value.length<8)input.value+=digit;input.focus();};
  el('user-pin-pad').append(button);
}
el('user-editor').onsubmit=event=>{
  event.preventDefault();
  const expiresValue=el('user-expires').value;
  let expiresAt=null;
  if(expiresValue){
    const parsed=new Date(expiresValue);
    if(Number.isNaN(parsed.getTime())){settingsNotice('Enter a valid date and time for temporary access, or leave it blank.');return;}
    expiresAt=parsed.toISOString();
  }
  const data={name:el('user-name').value,role:el('user-role').value,enabled:el('user-enabled').checked,
    pin:el('user-pin').value,confirmation:el('user-pin-confirm').value,expires_at:expiresAt};
  if(editingUser)data.id=editingUser.id;
  const duressPin=el('user-duress-pin').value, duressConfirm=el('user-duress-pin-confirm').value;
  if(duressPin||duressConfirm){data.duress_pin=duressPin;data.duress_confirmation=duressConfirm;}
  else if(editingUser&&el('user-duress-remove').checked){data.duress_pin='';data.duress_confirmation='';}
  for(const id of ['user-pin','user-pin-confirm','user-duress-pin','user-duress-pin-confirm'])el(id).value='';
  return managementTask(el('save-user'),async current=>{
    settingsNotice('Saving user…');
    let result;
    try { result=await call('/settings/users/save',data); }
    finally { data.pin='';data.confirmation='';data.duress_pin='';data.duress_confirmation=''; }
    if(!current())return;
    if(result.reauthenticate){await lock();message('Your user was updated. Enter your PIN again to open settings.');return;}
    clearUserForm();await loadUsers(current);if(current())settingsNotice(result.user.name+' saved.');
  });
};

const activityLabels={sirens_updated:'Alarm sirens updated',doors_bypassed:'Doors bypassed',bypass_reminders_reset:'Bypass reminders restored',arm:'Arm',arm_stay:'Arm stay',arm_away:'Arm away',disarm:'Disarm',power_outage:'Power outage',power_restored:'Power restored',power_low_battery:'Low battery',controller_shutdown:'Panel shut down',user_created:'User added',user_updated:'User updated',sensors_updated:'Sensors updated',protection_enabled:'Protection enabled',protection_restored:'Protection restored',network_rename_device:'Rename device',network_rename_port:'Rename port',network_rename_outlet:'Rename outlet',network_restart_device:'Restart device',network_cycle_poe:'Cycle PoE',network_outlet_on:'Outlet on',network_outlet_off:'Outlet off',wifi_passwords_viewed:'Wi-Fi passwords viewed'};
const statusLabels={requested:'Requested',accepted:'Awaiting confirmation',confirmed:'Confirmed by Protect',unverified:'Result unverified',saved:'Saved',already_disarmed:'Already disarmed',submitted:'Accepted by Network',verified:'Confirmed by Network',rejected:'Rejected'};
function localDate(value) { const date=new Date(value);return Number.isNaN(date.getTime())?'Time unavailable':date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'medium'}); }
async function loadHistory(current, more) {
  const data=await call('/settings/activity',more && activityCursor ? {before:activityCursor}:{});if(!current())return;
  if(!more)el('activity-list').replaceChildren();
  if(!more && !data.events.length)el('activity-list').append(node('p','No activity recorded yet. New keypad actions will appear here.','sub'));
  for(const event of data.events) {
    const row=node('article',undefined,'settings-row activity-row'), detail=node('div');
    detail.append(node('strong',(activityLabels[event.action]||event.action)+' · '+event.user_name),node('p',localDate(event.occurred_at),'sub small'));
    if(event.detail)detail.append(node('p',event.detail,'sub small'));
    if(event.confirmed_at)detail.append(node('p','Confirmed '+localDate(event.confirmed_at),'sub small'));
    row.append(detail,node('span',statusLabels[event.status]||event.status,'status-badge '+event.status));el('activity-list').append(row);
  }
  activityCursor=data.nextBefore;el('more-activity').hidden=!activityCursor;
}
el('refresh-activity').onclick=()=>managementTask(el('refresh-activity'),current=>loadHistory(current,false));
el('more-activity').onclick=()=>managementTask(el('more-activity'),current=>loadHistory(current,true));

async function loadSensors(current) {
  settingsNotice('Checking adopted sensors…');
  const data=await call('/settings/sensors');if(!current())return;
  sensorData=data;sensorSelection=new Set(data.selectedIds);sensorModes=Object.fromEntries(data.sensors.map(s=>[s.id,s.mode||(sensorSelection.has(s.id)?'alarm':'status')]));sensorPlan=data.pending||null;
  text('sensor-message',data.message);settingsNotice('');renderSensors();
  el('sensor-review').hidden=!sensorPlan;
  if(sensorPlan)renderSensorReview(sensorPlan,true);
  renderSensorTest(data.observation);
}
function renderSensors() {
  el('sensor-list').replaceChildren();
  for(const sensor of sensorData.sensors) {
    const row=node('div',undefined,'settings-row sensor-row'), detail=node('div');
    detail.append(node('strong',sensor.name),node('p',
      sensor.state==='CONNECTED' ? (sensor.isOpened===true?'Open':sensor.isOpened===false?'Closed':'Position unknown') : sensor.state.toLowerCase().replaceAll('_',' '),'sub small'));
    const controls=node('div',undefined,'sensor-controls'), mode=node('select');mode.setAttribute('aria-label',sensor.name+' mode');
    for(const [value,label] of [['alarm','Alarm door'],['status','Status only'],['bypass','Bypassed']]){
      const option=node('option',label);option.value=value;
      option.disabled=value==='alarm' && !sensor.available && !sensorData.selectedIds.includes(sensor.id);
      mode.append(option);
    }
    mode.value=sensorModes[sensor.id];mode.disabled=!sensorData.connected;
    mode.onchange=()=>{sensorModes[sensor.id]=mode.value;if(mode.value==='alarm')sensorSelection.add(sensor.id);else sensorSelection.delete(sensor.id);sensorPlan=null;el('sensor-review').hidden=true;renderSensors();};
    controls.append(mode);row.append(detail,controls);
    if(sensorData.selectedIds.includes(sensor.id) && sensorSelection.has(sensor.id) && sensorData.connected) {
      const choices=sensorData.sensors.filter(s=>s.available && !sensorSelection.has(s.id));
      if(choices.length){
        const replace=node('select');replace.setAttribute('aria-label','Replace '+sensor.name);
        const placeholder=node('option','Replace with…');placeholder.value='';replace.append(placeholder);
        for(const choice of choices){const option=node('option',choice.name);option.value=choice.id;replace.append(option);}
        replace.onchange=()=>{if(!replace.value)return;sensorSelection.delete(sensor.id);sensorSelection.add(replace.value);sensorModes[sensor.id]='bypass';sensorModes[replace.value]='alarm';sensorPlan=null;el('sensor-review').hidden=true;renderSensors();};controls.append(replace);
      }
    }
    el('sensor-list').append(row);
  }
  const changed=sensorData.sensors.some(s=>sensorModes[s.id] !== (s.mode||(sensorData.selectedIds.includes(s.id)?'alarm':'status')));
  el('review-sensors').disabled=!sensorData.connected || !changed || !sensorSelection.size;
}
function renderSensorReview(plan,resume=false) {
  sensorPlan=plan;el('sensor-review').hidden=false;el('sensor-review-detail').replaceChildren();
  if(resume)el('sensor-review-detail').append(node('p','A previous save needs to finish. Review its sensor list before resuming.'));
  if(plan.added?.length)el('sensor-review-detail').append(node('p','Add: '+plan.added.join(' · ')));
  if(plan.removed?.length)el('sensor-review-detail').append(node('p','Remove: '+plan.removed.join(' · ')));
  el('sensor-review-detail').append(node('p','Protected doors: '+plan.doors.join(' · ')));
  if(plan.statusOnly?.length)el('sensor-review-detail').append(node('p','Status only: '+plan.statusOnly.join(' · ')));
  if(plan.bypassed?.length)el('sensor-review-detail').append(node('p','Bypassed until changed in Settings: '+plan.bypassed.join(' · ')));
  text('save-sensors',resume?'Finish saving sensor changes':'Save sensor changes');
}
function renderSensorTest(observation) {
  if(!settingsOpen || settingsSection!=='sensors' || !sensorData)return;
  const data=observation||{}, doors=Object.entries(data.doors||{}),verified=new Set(data.verifiedDoors||[]);
  const fresh=!!data.fresh && (typeof data.receivedAt!=='number' || performance.now()-data.receivedAt<8000);
  const allChecked=doors.length>0 && doors.every(([name,door])=>verified.has(name) && door.connected && door.position==='closed');
  const matchesSaved=doors.length===sensorData.selectedIds.length && sensorData.selectedIds.every(id=>doors.some(([name])=>name===sensorData.sensors.find(s=>s.id===id)?.name));
  const active=data.activeDoors || doors.map(([name])=>name);
  const checked=doors.filter(([name,door])=>verified.has(name) && door.connected && door.position==='closed').length;
  text('sensor-test-summary',!fresh?'Reconnect to verify':data.liveDelayEnabled?`${active.length} ${active.length===1?'door':'doors'} · 1 min delay`:data.state!=='disarmed'?'Disarm to check':!doors.length?'Choose alarm doors':`${checked} of ${doors.length} doors ready`);
  text('sensor-test-state',!fresh?'Reconnect UniFi to verify protection.':data.liveDelayEnabled?'One-minute entry delay is enabled for '+active.join(' · ')+'.':data.state!=='disarmed'?'Disarm the system before testing or enabling sensor changes.':allChecked?'All door checks passed. You can enable protection.':'Open and close each alarm door once. Still to check: '+doors.filter(([name,door])=>!verified.has(name)||!door.connected||door.position!=='closed').map(([name])=>name).join(' · ')+'.');
  el('enable-sensors').hidden=fresh && !!data.liveDelayEnabled;
  el('enable-sensors').disabled=!fresh || data.state!=='disarmed' || data.liveDelayEnabled || !allChecked || !matchesSaved;
}
el('refresh-sensors').onclick=()=>managementTask(el('refresh-sensors'),loadSensors);
el('review-sensors').onclick=()=>managementTask(el('review-sensors'),async current=>{
  settingsNotice('Reviewing sensor changes…');
  const plan=await call('/settings/sensors/review',{sensorIds:[...sensorSelection],modes:sensorModes});
  if(current()){renderSensorReview(plan);settingsNotice('Review the changes, then save.');}
});
el('cancel-sensors').onclick=()=>{sensorPlan=null;el('sensor-review').hidden=true;settingsNotice('');};
el('save-sensors').onclick=()=>managementTask(el('save-sensors'),async current=>{
  if(!sensorPlan)return;
  settingsNotice('Saving and verifying alarm rules…');
  await call('/settings/sensors/apply',{planId:sensorPlan.planId});
  if(!current())return;
  await loadSensors(current);if(current())settingsNotice('Sensor modes saved. New alarm sensors need an open/close check. You can arm checked doors and confirm bypassing any others.');
});
el('enable-sensors').onclick=()=>managementTask(null,async current=>{
  el('enable-sensors').disabled=true;settingsNotice('Checking rules and sensor tests…');
  const result=await call('/settings/sensors/enable');
  if(current()){acceptObservation(result);renderSensorTest(result);settingsNotice('Protection enabled for your selected doors.');}
});
setInterval(()=>renderSensorTest(liveObservation),1000);

el('reset-bypass-reminders').onclick=()=>managementTask(el('reset-bypass-reminders'),async current=>{const result=await call('/settings/sensors/reminders');if(current())settingsNotice(result.message);});


async function loadSirens(current,keepSelection=false) {
  settingsNotice('Scanning Protect for adopted sirens…');
  const previous=keepSelection&&sirenData?new Set(sirenSelection):null;
  const duration=el('siren-duration').value;
  const data=await call('/settings/sirens');if(!current())return;
  sirenData=data;sirenSelection=previous?new Set([...previous].filter(id=>data.sirens.some(s=>s.id===id))):new Set(data.sirenIds);
  sirenPlan=data.pending||null;el('siren-review').hidden=!sirenPlan;
  text('siren-message',data.message);el('siren-duration').replaceChildren();
  for(const seconds of data.sirenDurations){const option=node('option',seconds+' seconds');option.value=String(seconds);el('siren-duration').append(option);}
  el('siren-duration').value=String(previous&&data.sirenDurations.includes(Number(duration))?duration:data.sirenDuration);
  renderSirens();if(sirenPlan)renderSirenReview(sirenPlan,true);settingsNotice('');
}
function renderSirens() {
  if(!sirenData)return;
  el('siren-list').replaceChildren();
  if(!sirenData.sirens.length)el('siren-list').append(node('p','No sirens found. Adopt your siren in Protect, then tap Scan Protect.','sub'));
  for(const siren of sirenData.sirens){
    const row=node('label',undefined,'settings-row siren-row'),check=node('input');check.type='checkbox';check.value=siren.id;check.checked=sirenSelection.has(siren.id);check.setAttribute('aria-label','Use '+siren.name+' for alarm');
    check.disabled=sirenBusy||!sirenData.connected||(!siren.available&&!sirenData.sirenIds.includes(siren.id));
    const detail=node('div');detail.append(node('strong',siren.name),node('p',siren.available?'Connected':siren.state==='MISSING'?'Missing from Protect':siren.state==='CONNECTED'?'Unavailable in Alarm Manager':'Offline','sub small'));
    check.onchange=()=>{if(sirenBusy)return;if(check.checked)sirenSelection.add(siren.id);else sirenSelection.delete(siren.id);sirenPlan=null;el('siren-review').hidden=true;renderSirens();};
    row.append(check,detail);el('siren-list').append(row);
  }
  el('siren-duration').disabled=sirenBusy||!sirenData.connected||!sirenData.sirenDurations.length;
  const changed=[...sirenSelection].sort().join('|')!==[...sirenData.sirenIds].sort().join('|') || (sirenSelection.size>0&&Number(el('siren-duration').value)!==sirenData.sirenDuration);
  el('review-sirens').disabled=sirenBusy||!sirenData.connected||!changed;
}
function renderSirenReview(plan,resume=false) {
  sirenPlan=plan;el('siren-review').hidden=false;el('siren-review-detail').replaceChildren();
  el('siren-review-detail').append(node('p',plan.sirens?.length?plan.sirens.join(' · ')+' · '+plan.sirenDuration+' seconds after the entry countdown.':'No siren sound. Phone alerts remain configured.'));
  if(resume){
    el('siren-review-detail').append(node('p','Finish the previous alarm setup: '+plan.doors.join(' · ')));
    const list=node('ul');for(const action of plan.actions)list.append(node('li',action));el('siren-review-detail').append(list);
  }
  text('save-sirens',resume?'Finish saved changes':'Save sirens');
}
async function sirenTask(button,operation) {
  if(sirenBusy)return;
  const revision=managementRevision;sirenBusy=true;renderSirens();
  el('refresh-sirens').disabled=true;el('cancel-sirens').disabled=true;
  await managementTask(button,operation);
  if(revision===managementRevision){sirenBusy=false;el('refresh-sirens').disabled=false;el('cancel-sirens').disabled=false;renderSirens();}
}
el('refresh-sirens').onclick=()=>sirenTask(el('refresh-sirens'),current=>loadSirens(current,true));
el('siren-duration').onchange=()=>{sirenPlan=null;el('siren-review').hidden=true;renderSirens();};
el('review-sirens').onclick=()=>sirenTask(el('review-sirens'),async current=>{
  settingsNotice('Checking sirens and alarm rules…');
  const plan=await call('/settings/sirens/review',{sirenIds:[...sirenSelection],sirenDuration:Number(el('siren-duration').value)||30});
  if(current()){renderSirenReview(plan);settingsNotice('');}
});
el('cancel-sirens').onclick=()=>{sirenPlan=null;el('siren-review').hidden=true;settingsNotice('');};
el('save-sirens').onclick=()=>sirenTask(el('save-sirens'),async current=>{
  if(!sirenPlan)return;
  settingsNotice('Saving and verifying alarm sound…');
  await call('/settings/sirens/apply',{planId:sirenPlan.planId});
  if(!current())return;
  await loadSirens(current);if(current())settingsNotice('Saved. Selected sirens will sound after the entry countdown.');
});
