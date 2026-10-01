const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('app.js','utf8');
const ready={fresh:true,state:'disarmed',remaining:null,notification:'not_sent',liveDelayEnabled:true,doors:Object.fromEntries(['Back Door','Front Door','Garage Door'].map(name=>[name,{connected:true,position:'closed'}]))};
const connected={connected:true,pinSet:true,webhookReady:true,armAwayConfigured:true,armStayConfigured:true};
function harness(mode) {
  let now=1000, rejectPin=false, rejectSend=false, holdUnlock;
  const elements={}, sent=[], timers=new Map();
  const element=()=>({dataset:{},classList:{toggle(){}},querySelectorAll(){return []},append(){},setAttribute(){},value:'',focus(){}});
  const body=element();
  const context=vm.createContext({document:{body,getElementById:id=>elements[id]??=element(),addEventListener(){},createElement:element},performance:{now:()=>now},Date,Math,setInterval(){},setTimeout(fn,ms){timers.set(fn,ms);return fn;},clearTimeout(fn){timers.delete(fn);},fetch:()=>new Promise(()=>{}),confirm(){throw new Error('Alarm commands must not open a browser confirmation dialog');}});
  vm.runInContext(source,context);
  context.fakeCall=async(path,data={})=>{
    sent.push({path,data});
    if(path==='/unlock') {
      if(rejectPin) throw new Error('Incorrect PIN.');
      if(holdUnlock) await holdUnlock;
      return {unlock:'test-unlock',expiresIn:60};
    }
    if(path==='/settings/status')return {pinSet:true,settingsUnlocked:false,expiresIn:0};
    if(path==='/settings/unlock'){
      if(rejectPin)throw new Error('Incorrect PIN.');
      if(holdUnlock)await holdUnlock;
      return {pinSet:true,settingsUnlocked:true,expiresIn:300};
    }
    if(path==='/settings/lock')return {settingsUnlocked:false};
    if(path==='/webhook/disarm') {
      if(rejectSend) throw new Error('Console unavailable.');
      return {...connected,exitSeconds:0,exitDeadline:0};
    }
    if(path==='/webhook/arm-away') return {...connected,exitSeconds:60,exitDeadline:now+60000};
    if(path==='/entry/status') return context.observation;
    if(path==='/lock') return {};
    throw new Error('Unexpected request: '+path);
  };
  context.observation={...ready,...(mode==='exit'?{state:'arming',exitRemaining:60}:['armed','disarmed'].includes(mode)?{state:mode}:{state:'breached',remaining:60})};
  context.connected=connected;
  vm.runInContext('call=fakeCall;render(connected);acceptObservation(observation);',context);
  return {context,elements,body,sent,run:code=>vm.runInContext(code,context),pinFail:value=>rejectPin=value,sendFail:value=>rejectSend=value,hold:value=>holdUnlock=value,timeout:ms=>{for(const [fn,delay]of [...timers])if(delay===ms)fn();},advance:seconds=>{now+=seconds*1000;vm.runInContext('exitDisplay()',context);}};
}
const submit=h=>{h.elements.pin.value='1111';return h.elements['unlock-form'].onsubmit({preventDefault(){}});};
(async()=>{
  for(const mode of ['exit','entry']) {
    const h=harness(mode), label=mode==='exit'?'Cancel arming':'Disarm';
    assert.equal(h.elements['wake-label'].textContent,label);
    assert.equal(h.elements['wake'].disabled,false);
    assert.equal(h.elements['arm-away'].hidden,true,'An active alarm should show one clear stop action');
    if(mode==='armed') {
      assert.equal(h.context.observation.remaining,null);
      assert.ok(Object.values(h.context.observation.doors).every(door=>door.position==='closed'));
      assert.equal(h.elements['standby-lock'].dataset.icon,'closed');
      assert.equal(h.elements['standby-display'].hidden,false);
      assert.equal(h.elements['alert-strip'].hidden,true,'No door event or countdown is needed');
    }
    h.elements.wake.onclick();
    assert.equal(h.run('activeScreen'),'pin-screen');
    assert.equal(h.elements['unlock-button'].textContent,label);
    assert.equal(h.elements['strip-time'].textContent,mode==='armed'?'':'1:00');
    assert.equal(h.sent.length,0,'Opening the PIN form must not send a command');
    h.pinFail(true);await submit(h);
    assert.equal(h.sent.filter(x=>x.path==='/webhook/disarm').length,0);
    assert.equal(h.body.dataset.alarmMode,mode,'Wrong PIN must not clear the countdown');
    h.pinFail(false);await submit(h);
    const commands=h.sent.filter(x=>x.path==='/webhook/disarm');
    assert.equal(commands.length,1);
    assert.equal(commands[0].data.unlock,'test-unlock');assert.equal(commands[0].data.confirm,true);
    assert.equal(h.run('activeScreen'),'sleep','PIN submission should send directly, without a second controls screen');
    assert.equal(h.body.dataset.alarmMode,mode,'Accepted request alone must not clear the countdown');
    h.context.observation={...ready};h.run('acceptObservation(observation)');
    assert.equal(h.body.dataset.alarmMode,'disarmed');assert.equal(h.body.dataset.tone,'blue');
    assert.equal(h.elements['alarm-display'].hidden,true);
    assert.equal(h.elements['message'].textContent,'');
  }
  const retry=harness('entry');retry.elements.wake.onclick();retry.sendFail(true);await submit(retry);
  assert.equal(retry.run('activeScreen'),'pin-screen');assert.equal(retry.run('unlockToken'),'');
  assert.equal(retry.body.dataset.alarmMode,'entry');assert.match(retry.elements.message.textContent,/unavailable/);
  retry.sendFail(false);await submit(retry);
  assert.equal(retry.sent.filter(x=>x.path==='/unlock').length,2,'Failed sends require a new PIN check');

  const back=harness('exit');back.elements.wake.onclick();back.elements.back.onclick();
  assert.equal(back.run('disarmOnUnlock'),false);assert.equal(back.body.dataset.alarmMode,'exit');
  assert.equal(back.sent.length,0,'Backing out must leave the countdown running');

  const expires=harness('entry');expires.elements.wake.onclick();expires.advance(61);
  assert.equal(expires.body.dataset.alarmMode,'breach');assert.equal(expires.elements['unlock-button'].textContent,'Disarm');
  await submit(expires);assert.equal(expires.sent.filter(x=>x.path==='/webhook/disarm').length,1);

  const duplicate=harness('exit');duplicate.elements.wake.onclick();
  let release;duplicate.hold(new Promise(resolve=>release=resolve));
  const first=submit(duplicate);await submit(duplicate);
  assert.equal(duplicate.sent.filter(x=>x.path==='/unlock').length,1);release();await first;
  assert.equal(duplicate.sent.filter(x=>x.path==='/webhook/disarm').length,1);

  const unlocked=harness('exit');unlocked.run("unlockToken='existing-unlock';show('home');");
  assert.equal(unlocked.elements.disarm.disabled,false);
  assert.equal(unlocked.elements['disarm-label'].textContent,'Cancel arming');
  await unlocked.elements.disarm.onclick();
  assert.equal(unlocked.sent.find(x=>x.path==='/webhook/disarm').data.unlock,'existing-unlock');
  const already=harness('disarmed');already.run("unlockToken='existing-unlock';show('home');");
  assert.equal(already.elements.disarm.disabled,true);
  assert.equal(already.elements['disarm-label'].textContent,'Disarmed');
  await already.elements.disarm.onclick();
  await already.run("sendAlarmCommand('disarm')");
  assert.equal(already.sent.length,0,'Already-disarmed must not send or invalidate the unlocked keypad');
  assert.equal(already.run('unlockToken'),'existing-unlock');
  assert.equal(already.elements['arm-away'].disabled,false,'Arm must remain available after redundant Disarm');
  assert.equal(already.run('busy'),false);
  const changedDuringPin=harness('entry');changedDuringPin.elements.wake.onclick();
  changedDuringPin.run("acceptObservation({...observation,state:'disarmed'})");
  await submit(changedDuringPin);
  assert.equal(changedDuringPin.sent.filter(x=>x.path==='/webhook/disarm').length,0);
  assert.equal(changedDuringPin.run('activeScreen'),'home','A system disarmed elsewhere should leave an unlocked, usable keypad');
  assert.equal(changedDuringPin.elements['arm-away'].disabled,false);

  const unknown=harness('armed');
  unknown.run("acceptObservation({fresh:false,state:'unknown',liveDelayEnabled:true,message:'Sign in to observe real door events.'})");
  assert.equal(unknown.elements['connection-alert'].hidden,false);
  assert.equal(unknown.elements.wake.hidden,true);
  assert.equal(unknown.elements['swipe-unlock'].hidden,false);
  unknown.elements['swipe-unlock'].onkeydown({key:'Enter',preventDefault(){}});await submit(unknown);await unknown.elements.disarm.onclick();
  assert.equal(unknown.sent.filter(x=>x.path==='/webhook/disarm').length,1,'Unknown status must not block PIN disarm');
  await unknown.elements.reconnect.onclick();
  assert.equal(unknown.run('activeScreen'),'pin-screen');
  assert.equal(unknown.elements['pin-title'].textContent,'Unlock settings');
  assert.equal(unknown.elements['setup-screen'].hidden,true);
  unknown.pinFail(true);await submit(unknown);
  assert.equal(unknown.run('activeScreen'),'pin-screen');
  unknown.pinFail(false);await submit(unknown);
  assert.equal(unknown.run('activeScreen'),'setup-screen');
  unknown.run('acceptObservation(observation)');
  assert.equal(unknown.elements['connection-alert'].hidden,true);
  await unknown.elements['close-setup'].onclick();
  assert.equal(unknown.run('settingsOpen'),false);
  assert.ok(unknown.sent.some(x=>x.path==='/settings/lock'));
  await unknown.elements.settings.onclick();
  assert.equal(unknown.run('activeScreen'),'pin-screen','Closing settings requires another PIN');
  const gear=harness('disarmed');gear.run("unlockToken='controls-only';show('home')");
  await gear.elements.settings.onclick();
  assert.equal(gear.run('activeScreen'),'pin-screen','A disarm unlock cannot bypass the settings PIN');
  await submit(gear);
  assert.equal(gear.run('activeScreen'),'setup-screen');
  assert.equal(gear.sent.filter(x=>x.path.startsWith('/webhook/')).length,0,'Settings PIN must never send an alarm command');
  gear.timeout(60000);
  assert.equal(gear.run('activeScreen'),'sleep','Idle settings must hide again');
  assert.equal(gear.run('settingsOpen'),false);
  await gear.elements.settings.onclick();await submit(gear);gear.timeout(300000);
  assert.equal(gear.run('activeScreen'),'sleep','Settings access has an absolute expiry');
  const late=harness('disarmed');await late.elements.settings.onclick();
  let finish;late.hold(new Promise(resolve=>finish=resolve));
  const waiting=submit(late);late.elements.back.onclick();finish();await waiting;
  assert.equal(late.run('activeScreen'),'sleep','A late PIN response must not reopen cancelled settings');
  assert.ok(late.sent.some(x=>x.path==='/settings/lock'));

  const inFlight=harness('disarmed');
  inFlight.run("unlockToken='existing-unlock';exitDeadline=performance.now()+60000;");
  await inFlight.elements.disarm.onclick();
  assert.equal(inFlight.sent.filter(x=>x.path==='/webhook/disarm').length,1,'An accepted Arm request must remain cancellable before the next status update');
  const armed=harness('armed');
  armed.elements.wake.onclick();await submit(armed);
  assert.equal(armed.run('activeScreen'),'home','Ordinary unlock goes to Security without disarming');
  assert.equal(armed.sent.filter(x=>x.path.startsWith('/webhook/')).length,0);
  assert.equal(armed.elements.disarm.disabled,false);
  await armed.elements.disarm.onclick();
  assert.equal(armed.sent.filter(x=>x.path==='/webhook/disarm').length,1);
  const arm=harness('disarmed');
  await arm.elements['arm-away'].onclick();
  assert.equal(arm.sent.filter(x=>x.path==='/webhook/arm-away').length,0,'Arm cannot send while locked');
  arm.elements.wake.onclick();await submit(arm);await arm.elements['arm-away'].onclick();
  const commands=arm.sent.filter(x=>x.path==='/webhook/arm-away');
  assert.equal(commands.length,1);
  assert.equal(commands[0].data.confirm,true);
  assert.equal(commands[0].data.unlock,'test-unlock');
  console.log('PIN unlock opens Security; Arm and Disarm send without browser confirmation dialogs.');
  console.log('Armed-with-closed-doors disarm, exit cancellation, entry disarm, PIN failures, retries, expiry, and duplicate-submit checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
