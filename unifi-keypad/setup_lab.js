'use strict';
const el=id=>document.getElementById(id);
let token='', pending=null, timer, generation=0, context=null, plan=null, resumePlan=null, pinSet=false, pinField='setup-pin', busy=false, keypadUrl='/';
let settingsOpen=false, settingsIdle, settingsExpiry, accessGeneration=0;
let browserSetup=false, apiConfigured=true, apiSupported=false, computerSetupAvailable=true, browserExpiry, checkTimer, ending=false;
let setupRequired=false, setupChoiceMade=false;
const ready=fetch('/session').then(async r=>{const data=await r.json();if(data.pairingRequired){closedBrowser(false,data.error);throw new Error(data.error);}return data;}).then(data=>{
  token=data.token;
  browserSetup=data.browserSetup===true;apiSupported=typeof data.apiConfigured==='boolean';apiConfigured=data.apiConfigured!==false;
  computerSetupAvailable=data.computerSetupAvailable!==false;
  setupRequired=data.setupRequired===true;
  el('keypad-link').hidden=!browserSetup&&data.setupRequired===true;
  el('api-host').value=data.consoleHost||'';el('host').value=data.consoleHost||'';
  if(!data.site)keypadUrl='http://127.0.0.1:8765/';
  el('keypad-link').href=keypadUrl;el('continue').href=keypadUrl;
  el('edit-api').hidden=!apiSupported;
  if(browserSetup){
    el('keypad-link').textContent='Close computer access';el('continue').textContent='Finish setup →';
    el('browser-session-note').hidden=false;el('browser-session-note').textContent='Connected to your panel · This browser session lasts 30 minutes. You can switch tabs to copy your API key.';
    el('completion-note').textContent='Your settings are saved on the panel. Check your doors below, then finish to close computer access.';
    document.querySelector('#settings-gate .fine').textContent='Settings lock after 10 minutes idle. Enter your administrator PIN to continue.';
    browserExpiry=setTimeout(()=>closedBrowser(false,'This setup session has expired. Start a new computer session on the panel to continue.'),data.browserExpiresIn*1000);
  }
});
function message(value,error=false){el('message').textContent=value;el('message').classList.toggle('error',error);}
async function call(path,body={}){
  return request('/installer/'+path,body);
}
async function request(path,body={}){
  const attempt=accessGeneration;
  await ready;
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Keypad-Token':token},body:JSON.stringify(body),signal:AbortSignal.timeout(60000)});
  const data=await response.json();
  if(data.pairingRequired)closedBrowser(false,data.error);
  if(data.settingsLocked)lockSettings(false);
  if(path.startsWith('/installer/')&&attempt!==accessGeneration)throw new Error('Settings locked. Enter your PIN again.');
  if(!response.ok){const error=new Error(data.error||'Setup could not complete.');error.reviewRequired=data.reviewRequired===true;throw error;}
  return data;
}
function clearLogin(){
  generation++;pending=null;clearTimeout(timer);
  el('password').value='';el('code').value='';
  el('password-field').hidden=false;el('password').required=true;
  el('code-field').hidden=true;el('code').required=false;el('cancel').hidden=true;
  el('host').disabled=false;el('username').disabled=false;el('submit').textContent='Connect to UniFi';
}
function showStep(step){
  if(step!==3)clearPinFields();
  clearTimeout(checkTimer);el('setup-start').hidden=true;el('api-page').hidden=true;el('computer-setup').hidden=true;
  el('complete').hidden=true;el('welcome').hidden=true;el('wizard').hidden=false;
  for(const [i,name]of ['doors','alerts','pin','review'].entries()){
    el(name+'-page').hidden=i+1!==step;
    if(i+1===step)el('step-'+(i+1)).setAttribute('aria-current','step');else el('step-'+(i+1)).removeAttribute('aria-current');
  }
  document.querySelector('#'+['doors','alerts','pin','review'][step-1]+'-page h1').focus({preventScroll:true});
  window.scrollTo(0,0);
}
function lockSettings(revoke=true){
  accessGeneration++;settingsOpen=false;clearTimeout(settingsIdle);clearTimeout(settingsExpiry);
  clearLogin();clearPinFields();el('settings-pin').value='';
  el('api-key').value='';clearTimeout(checkTimer);
  for(const id of ['welcome','wizard','complete','api-page','computer-setup','setup-start'])el(id).hidden=true;
  document.dispatchEvent?.(new Event('browser-pair-hide'));
  el('settings-gate').hidden=false;el('connection-state').textContent='';
  context=null;plan=null;resumePlan=null;
  for(const id of ['sensors','recipients','setup-siren-list','review-summary','review-actions'])el(id).replaceChildren();
  message('Enter your keypad PIN to open settings.');
  if(revoke)return request('/settings/lock').catch(()=>{});
}
function settingsActivity(){
  clearTimeout(settingsIdle);
  if(settingsOpen&&pinSet)settingsIdle=setTimeout(()=>lockSettings(),browserSetup?600000:120000);
}
function allowSettings(data){
  settingsOpen=true;pinSet=data.pinSet;el('settings-gate').hidden=true;
  clearTimeout(settingsExpiry);
  if(pinSet)settingsExpiry=setTimeout(()=>lockSettings(),data.expiresIn*1000);
  settingsActivity();
}
async function loadSettings(){
  const attempt=accessGeneration;
  if(setupRequired&&!browserSetup&&!setupChoiceMade){
    for(const id of ['api-page','welcome','wizard','complete'])el(id).hidden=true;
    el('setup-start').hidden=false;el('computer-setup').hidden=false;message('');return;
  }
  el('setup-start').hidden=true;
  if(apiSupported){
    if(!apiConfigured){showApi();return;}
    try{await request('/status');}
    catch(error){if(attempt===accessGeneration&&settingsOpen){showApi();message('The saved API connection could not be verified. Check the console is online and try the saved connection again, or update the key.',true);}return;}
  }
  try{const data=await call('context');if(attempt===accessGeneration&&settingsOpen)render(data);}
  catch(error){if(attempt===accessGeneration&&settingsOpen){el('api-page').hidden=true;el('welcome').hidden=false;el('computer-setup').hidden=browserSetup||setupChoiceMade||!apiSupported||!computerSetupAvailable;message('Connect your UniFi console to get started.');}}
}
el('setup-on-panel').onclick=()=>{setupChoiceMade=true;return loadSettings();};
el('setup-options').onclick=()=>{setupChoiceMade=false;return loadSettings();};
document.addEventListener('browser-setup-complete',()=>{
  // A remote save is now authoritative; load the normal root routing again.
  if(!browserSetup)window.location.href=keypadUrl;
});
function showApi(){
  clearLogin();for(const id of ['welcome','wizard','complete'])el(id).hidden=true;
  el('api-page').hidden=false;el('computer-setup').hidden=browserSetup||setupChoiceMade||!computerSetupAvailable;el('api-key').value='';
  el('setup-options').hidden=browserSetup||!setupRequired;
  el('api-back').hidden=!apiConfigured;el('api-retry').hidden=!apiConfigured;
  message(apiConfigured?'Your API key is saved on the panel. You can retry it or replace it here.':'Connect your UniFi Protect API key to get started.');
}
el('api-form').onsubmit=event=>{
  event.preventDefault();if(busy)return;
  let key=el('api-key').value.trim();el('api-key').value='';const host=el('api-host').value.trim(),attempt=accessGeneration;
  return run(el('api-submit'),async()=>{
    try{message('Checking and saving your API connection…');await request('/connect',{host,key});
      if(attempt!==accessGeneration||!settingsOpen)return;
      apiConfigured=true;el('host').value=host;el('api-page').hidden=true;await loadSettings();
    }finally{key='';}
  });
};
el('api-retry').onclick=()=>run(el('api-retry'),loadSettings);
el('api-back').onclick=()=>{el('api-key').value='';el('api-page').hidden=true;el('welcome').hidden=false;};
el('edit-api').onclick=showApi;
for(const event of ['pointerdown','keydown'])document.addEventListener(event,settingsActivity);
document.addEventListener('visibilitychange',()=>{if(!browserSetup&&document.hidden&&settingsOpen&&pinSet)lockSettings();});
for(const digit of ['1','2','3','4','5','6','7','8','9','Clear','0','⌫']){
  const button=document.createElement('button');button.type='button';button.textContent=digit;
  button.setAttribute('aria-label',digit==='⌫'?'Delete last digit':digit);
  button.onclick=()=>{if(busy)return;const input=el('settings-pin');if(digit==='Clear')input.value='';else if(digit==='⌫')input.value=input.value.slice(0,-1);else if(input.value.length<8)input.value+=digit;};
  el('settings-number-pad').append(button);
}
el('settings-unlock-form').onsubmit=event=>{
  event.preventDefault();if(busy)return;
  let pin=el('settings-pin').value;el('settings-pin').value='';
  const attempt=accessGeneration;
  return run(el('settings-unlock'),async()=>{
    try{
      const access=await request('/settings/unlock',{pin});
      if(attempt!==accessGeneration){await request('/settings/lock');return;}
      allowSettings(access);await loadSettings();
    }finally{pin='';}
  });
};
for(const id of ['keypad-link','continue'])el(id).onclick=async event=>{
  event.preventDefault();if(busy)return;
  if(browserSetup){return run(el(id),async()=>{await request('/browser/end');closedBrowser(id==='continue');});}
  await lockSettings();window.location.href=keypadUrl;
};
function closedBrowser(saved,detail){
  ending=true;clearTimeout(browserExpiry);lockSettings(false);token='';
  el('settings-gate').hidden=true;el('keypad-link').hidden=true;el('browser-session-note').hidden=true;
  el('browser-ended').hidden=false;el('browser-ended-title').textContent=saved?'Your panel is set up.':'Computer access closed.';
  el('browser-ended-detail').textContent=detail||'Your settings are saved on the panel. You can close this tab and return to your keypad.';
  message('');el('browser-ended-title').focus({preventScroll:true});
}
function selectedValues(container){return [...el(container).querySelectorAll('input:checked:not(:disabled)')].map(input=>input.value);}
function updateCount(){const n=selectedValues('sensors').length;el('selected-count').textContent=n+' door'+(n===1?'':'s')+' selected';el('next-doors').disabled=n===0||busy;}
function choices(){
  const profile=el('profile').value;
  return {name:el('home-name').value.trim(),sensorIds:selectedValues('sensors'),profileId:profile,
    profileName:profile?context.profiles.find(p=>p.id===profile).title:el('profile-name').value.trim(),receivers:selectedValues('recipients'),sirenIds:selectedValues('setup-siren-list'),sirenDuration:Number(el('setup-siren-duration').value)||30};
}
function checkboxRow(id,name,available,checked,detail,position){
  const row=document.createElement('label');row.className=position===undefined?'recipient':'sensor';
  const input=document.createElement('input');input.type='checkbox';input.value=id;input.checked=checked&&available;input.disabled=!available;input.setAttribute('aria-label',name);
  const label=document.createElement('strong');label.textContent=name;
  if(detail){const small=document.createElement('small');small.textContent=detail;label.append(small);}
  row.append(input,label);
  if(position!==undefined){const status=document.createElement('span');status.className='position';status.textContent=position;row.append(status);input.onchange=updateCount;}
  return row;
}
function render(data,keepChoices=false){
  const draft=keepChoices&&context?choices():data.choices;
  context=data;renderPin(data);resumePlan=data.pending||null;el('welcome').hidden=true;el('connection-state').textContent='CONSOLE CONNECTED';
  el('sensors').replaceChildren();
  for(const sensor of data.sensors){
    const position=sensor.isOpened===true?'Open':sensor.isOpened===false?'Closed':'Check sensor';
    el('sensors').append(checkboxRow(sensor.id,sensor.name||'Unnamed sensor',sensor.available,draft.sensorIds.includes(sensor.id),
      sensor.available?'Connected':sensor.state!=='CONNECTED'?'Offline · excluded':'Position unknown · excluded',position));
  }
  el('home-name').value=draft.name;el('profile-name').value=draft.profileName;
  el('profile').replaceChildren();
  for(const profile of data.profiles){const option=document.createElement('option');option.value=profile.id;option.textContent=profile.title;el('profile').append(option);}
  const fresh=document.createElement('option');fresh.value='';fresh.textContent='Create a new alarm profile';el('profile').append(fresh);el('profile').value=draft.profileId;
  el('new-profile-field').hidden=!!el('profile').value;
  el('recipients').replaceChildren();
  for(const user of data.users)el('recipients').append(checkboxRow(user.id,user.name,true,draft.receivers.includes(user.id)));
  renderSetupSirens(data,draft);
  updateCount();message(data.saved?'Saved setup loaded. You can update it here.':'Connected. Choose the doors you want to protect.');
  if(data.pending){plan=data.pending;if(pinSet)renderReview(plan,true);else showStep(3);}
  else showStep(1);
}
function renderSetupSirens(data,draft){
  el('setup-siren-list').replaceChildren();
  const selected=new Set(draft.sirenIds||[]);
  for(const siren of data.sirens||[])el('setup-siren-list').append(checkboxRow(siren.id,siren.name,siren.available||selected.has(siren.id),selected.has(siren.id),siren.available?'Connected':'Offline or unavailable in Protect'));
  if(!data.sirens?.length){const note=document.createElement('p');note.className='fine';note.textContent='No sirens found. Adopt one in Protect, then scan again.';el('setup-siren-list').append(note);}
  el('setup-siren-duration').replaceChildren();
  for(const seconds of data.sirenDurations||[]){const option=document.createElement('option');option.value=String(seconds);option.textContent=seconds+' seconds';el('setup-siren-duration').append(option);}
  el('setup-siren-duration').value=String(draft.sirenDuration||30);
}
el('scan-sirens').onclick=()=>run(el('scan-sirens'),async()=>{const current=accessGeneration;const data=await call('context');if(current===accessGeneration&&settingsOpen){render(data,true);showStep(2);}});
function renderReview(data,resume=false){
  plan=data;el('review-summary').replaceChildren();
  for(const [label,value]of [['Home',data.name],['Doors',data.doors.join(', ')],['Alarm profile',data.profile],['Exit / entry','1 minute / 1 minute'],['Phone alerts',data.recipients.join(', ')],['Sirens',data.sirens?.length?data.sirens.join(', ')+' · '+data.sirenDuration+' seconds':'None selected'],['Keypad PIN','Saved securely']]){
    const row=document.createElement('div');row.className='review-row';
    const name=document.createElement('span');name.textContent=label;const valueEl=document.createElement('strong');valueEl.textContent=value;row.append(name,valueEl);el('review-summary').append(row);
  }
  el('review-actions').replaceChildren();for(const action of data.actions){const li=document.createElement('li');li.textContent=action;el('review-actions').append(li);}
  el('save').textContent=resume?'Finish setup':'Save setup';el('back-pin').disabled=resume;
  showStep(4);message(resume?'Setup was interrupted. Resume verifies saved changes before continuing.':data.message);
}
function complete(data){
  clearPinFields();
  el('wizard').hidden=true;el('welcome').hidden=true;el('complete').hidden=false;
  el('saved-summary').textContent=data.name+' · '+data.doors.join(', ')+'. Your doors, timing, and phone alerts are saved.';
  message('Alarm rules verified and setup saved.');el('complete').querySelector('h1').focus({preventScroll:true});window.scrollTo(0,0);
  el('protection-check').hidden=!apiSupported;
  if(apiSupported)refreshProtection();
}
async function refreshProtection(){
  clearTimeout(checkTimer);const attempt=accessGeneration;
  try{
    const data=await request('/entry/status');if(attempt!==accessGeneration||!settingsOpen||el('complete').hidden)return;
    const doors=Object.entries(data.doors||{}),verified=new Set(data.verifiedDoors||[]);
    el('setup-door-checks').replaceChildren();
    for(const [name,door] of doors){
      const row=document.createElement('div');row.className='setup-door-check';
      const label=document.createElement('span');label.textContent=name;
      const state=document.createElement('span');state.textContent=!door.connected?'Unreachable':!verified.has(name)?'Awaiting open / close check':door.position==='closed'?'Checked · Closed':'Checked · '+door.position;
      row.append(label,state);el('setup-door-checks').append(row);
    }
    const checked=doors.length>0&&doors.every(([name,door])=>verified.has(name)&&door.connected&&door.position==='closed');
    el('setup-enable').disabled=busy||!data.fresh||data.state!=='disarmed'||data.liveDelayEnabled||!checked;
    el('setup-protection-message').textContent=!data.fresh?'Reconnect UniFi before checking protection.':data.liveDelayEnabled?'One-minute entry delay is enabled.':data.state!=='disarmed'?'Disarm from the keypad before checking new sensors.':checked?'All door checks passed. You can enable the entry delay.':'Door checks are still pending. You can finish setup and check them later from Settings → Sensors.';
  }catch(error){if(attempt===accessGeneration&&!ending){el('setup-protection-message').textContent=error.message;el('setup-enable').disabled=true;}}
  if(!ending&&settingsOpen&&!el('complete').hidden)checkTimer=setTimeout(refreshProtection,3000);
}
el('setup-check-refresh').onclick=refreshProtection;
el('setup-enable').onclick=()=>run(el('setup-enable'),async()=>{await request('/settings/sensors/enable');await refreshProtection();});
async function run(button,work){
  if(busy)return;busy=true;button.disabled=true;
  try{await work();}catch(error){message(error.name==='TimeoutError'?'Setup is taking longer than expected. Reopen this page to check and resume; do not start a second setup.':error.message,true);}
  finally{busy=false;button.disabled=false;updateCount();if(button===el('setup-enable')){button.disabled=true;refreshProtection();}}
}
el('profile').onchange=()=>{el('new-profile-field').hidden=!!el('profile').value;};
el('next-doors').onclick=()=>{if(selectedValues('sensors').length){showStep(2);message('Choose a name and who should receive phone alerts.');}};
el('back-doors').onclick=()=>{if(!busy)showStep(1);};
el('back-alerts').onclick=()=>{if(!busy)showStep(2);};
function clearPinFields(){el('setup-pin').value='';el('confirm-pin').value='';pinField='setup-pin';}
function renderPin(data){
  pinSet=data.pinSet===true;clearPinFields();
  el('pin-create').hidden=pinSet;el('existing-pin').hidden=!pinSet;
  for(const id of ['setup-pin','confirm-pin']){el(id).required=!pinSet;el(id).disabled=pinSet;}
  el('next-pin').textContent=pinSet?'Next · Review →':'Save PIN & continue →';
}
function validateSettings(){
  if(!el('home-name').reportValidity()||(!el('profile').value&&!el('profile-name').value.trim()))throw new Error('Enter a home name and alarm profile name.');
  if(!selectedValues('recipients').length)throw new Error('Choose who should receive phone notifications.');
}
el('next-alerts').onclick=()=>run(el('next-alerts'),async()=>{
  validateSettings();renderPin(await call('pin/status'));showStep(3);
  message(pinSet?'Your saved keypad PIN is ready. Continue to review.':'Choose and confirm your keypad PIN.');
});
el('back-pin').onclick=()=>{if(!busy){showStep(3);message('Your saved keypad PIN is ready.');}};
for(const id of ['setup-pin','confirm-pin'])el(id).onfocus=()=>{pinField=id;};
for(const button of el('pin-touchpad').querySelectorAll('[data-digit]'))button.onclick=()=>{
  if(busy||pinSet)return;
  const input=el(pinField);if(input.value.length<8)input.value+=button.dataset.digit;input.focus({preventScroll:true});
};
el('pin-next-field').onclick=()=>{if(!busy&&!pinSet){pinField='confirm-pin';el(pinField).focus({preventScroll:true});}};
el('pin-delete').onclick=()=>{if(!busy&&!pinSet){const input=el(pinField);input.value=input.value.slice(0,-1);input.focus({preventScroll:true});}};
el('pin-form').onsubmit=event=>{
  event.preventDefault();if(busy)return;
  return run(el('next-pin'),async()=>{
    validateSettings();
    if(!pinSet){
      let pin=el('setup-pin').value,confirmation=el('confirm-pin').value;
      clearPinFields();
      try{
        if(!/^[0-9]{4,8}$/.test(pin))throw new Error('Choose a PIN containing 4 to 8 digits.');
        if(pin!==confirmation)throw new Error('The PINs do not match. Enter the same PIN twice.');
        message('Saving your keypad PIN…');
        const result=await call('pin/create',{pin,confirmation});
        allowSettings(result);renderPin(result);
        if(!pinSet)throw new Error('Your PIN could not be verified. Please try again.');
      }finally{pin='';confirmation='';clearPinFields();}
    }
    message('Checking your selections and existing alarm rules…');
    if(resumePlan)renderReview(resumePlan,true);
    else renderReview(await call('review',choices()));
  });
};
el('save').onclick=()=>run(el('save'),async()=>{
  el('back-pin').disabled=true;message('Saving setup and checking each rule in Protect…');
  try{const data=await call('apply',{planId:plan.planId});complete(data);}
  catch(error){
    if(error.reviewRequired){plan=null;resumePlan=null;showStep(3);}
    else el('save').textContent='Finish setup';
    throw error;
  }
});
el('refresh').onclick=()=>run(el('refresh'),async()=>{message('Refreshing sensors…');render(await call('context'),true);});
el('edit').onclick=()=>run(el('edit'),async()=>render(await call('context')));
el('cancel').onclick=()=>{clearLogin();message('Sign-in cancelled.');};
el('login').onsubmit=async event=>{
  event.preventDefault();el('submit').disabled=true;
  const attempt=++generation;
  let credentials=pending||{host:el('host').value,username:el('username').value,password:el('password').value,expires:performance.now()+180000};
  const code=el('code').value;el('password').value='';el('code').value='';message('Connecting to your console…');
  try{
    if(performance.now()>=credentials.expires)throw new Error('Verification expired. Please sign in again.');
    const data=await call('login',{host:credentials.host,username:credentials.username,password:credentials.password,verificationCode:code});
    if(attempt!==generation)return;
    if(data.mfaRequired){
      pending=credentials;el('host').disabled=true;el('username').disabled=true;el('password-field').hidden=true;el('password').required=false;
      el('code-field').hidden=false;el('code').required=true;el('cancel').hidden=false;el('submit').textContent='Verify and connect';
      message('Enter the six-digit authenticator code for this UniFi account.');
      clearTimeout(timer);timer=setTimeout(()=>{clearLogin();message('Verification expired. Please sign in again.');},Math.max(0,credentials.expires-performance.now()));el('code').focus();
    }else{clearLogin();render(data);}
  }catch(error){if(attempt===generation){clearLogin();message(error.message,true);}}
  finally{credentials=null;el('submit').disabled=false;}
};
window.addEventListener('pagehide',()=>{clearLogin();clearPinFields();el('api-key').value='';clearTimeout(checkTimer);});
request('/settings/status').then(async data=>{
  pinSet=data.pinSet;
  if(data.settingsUnlocked){allowSettings(data);await loadSettings();}else lockSettings(false);
}).catch(error=>{if(!ending)lockSettings(false);message(error.message,true);});
