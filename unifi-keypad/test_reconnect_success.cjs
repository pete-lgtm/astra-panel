const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function harness({api=true,mfa=false}={}){
  const nodes={},requests=[];let challenge=mfa,fail=false,heldStatus=null;
  const make=()=>({value:'',open:false,hidden:false,dataset:{},classList:{toggle(){}},querySelectorAll(){return []},append(){},setAttribute(){},focus(){this.focused=true},showModal(){this.open=true},close(){this.open=false},scrollIntoView(){}});
  const get=id=>nodes[id]??=make();
  const ctx=vm.createContext({document:{body:make(),getElementById:get,createElement:make,addEventListener(){}},Date,Math,performance:{now:()=>1000},fetch:()=>new Promise(()=>{}),setTimeout(){},clearTimeout(){},setInterval(){}});
  vm.runInContext(fs.readFileSync('app.js','utf8'),ctx);
  ctx.fakeCall=async(path,data={})=>{
    requests.push(path);
    if(path==='/local/login'){
      if(fail)throw Error('Verification rejected.');
      if(challenge){challenge=false;return {mfaRequired:true,localConnected:false,message:'Verification needed'}}
      return {localConnected:true,profiles:[{title:'Arm',state:'disarmed'}],message:'Login and profile read succeeded.'};
    }
    if(path==='/local/status')return {localConnected:true,profiles:[],message:'Account verified.'};
    if(path==='/local/logout')return {localConnected:false,message:'Signed out.'};
    if(path==='/status'){
      if(heldStatus)await heldStatus;
      if(!api)throw Error('Connect to your console first.');
      return {connected:true,pinSet:true,webhookReady:true,armAwayConfigured:true,armStayConfigured:true};
    }
    if(path==='/settings/lock')return {settingsUnlocked:false};
    throw Error('Unexpected request '+path);
  };
  const run=code=>vm.runInContext(code,ctx);
  run("call=fakeCall;settingsOpen=true;pinConfigured=true;show('setup-screen');let managedSetup=true;");
  const submit=async()=>{
    get('local-host').value='192.168.1.1';get('local-user').value='test-user';get('local-password').value='test-password';
    await get('local-login').onsubmit({preventDefault(){}});
  };
  return {get,run,submit,requests,fail:()=>fail=true,holdStatus:promise=>heldStatus=promise};
}
(async()=>{
  // Exact reported bug: account login succeeded, but the API key is not connected.
  const partial=harness({api:false});await partial.submit();
  assert.equal(partial.get('signin-success').open,true,'Successful account login must be acknowledged independently of API readiness');
  assert.match(partial.get('signin-success-detail').textContent,/account is connected/);
  assert.match(partial.get('signin-success-next').textContent,/Reconnect the API connection/);
  assert.equal(partial.get('signin-finish-connection').hidden,false);
  assert.match(partial.get('connection').textContent,/UniFi signed in · API connection needed/);
  assert.equal(partial.get('local-login').hidden,true,'Hide the cleared password form after login to avoid repeated submission');
  assert.equal(partial.get('local-connected-note').hidden,false);
  partial.get('signin-finish-connection').onclick();
  assert.equal(partial.get('signin-success').open,false);
  assert.equal(partial.get('advanced-api').open,true);
  assert.equal(partial.get('key').focused,true);
  await partial.get('local-refresh').onclick();
  assert.equal(partial.get('signin-success').open,true,'An explicit connection check can confirm an existing login');
  partial.get('signin-continue').onclick();
  assert.equal(partial.run('activeScreen'),'sleep');
  assert.equal(partial.get('signin-success').open,false);
  assert.equal(partial.run('unlockToken'),'','Continue must not bypass the keypad PIN');
  assert.equal(partial.requests.some(path=>path.startsWith('/webhook/')),false);

  const complete=harness();await complete.submit();
  assert.equal(complete.get('signin-success').open,true,'Managed installations also receive the popup');
  assert.equal(complete.get('signin-finish-connection').hidden,true);
  assert.equal(complete.get('local-password').value,'');
  await complete.get('local-logout').onclick();
  assert.equal(complete.get('signin-success').open,false);
  assert.equal(complete.get('local-login').hidden,false);
  assert.equal(complete.run('localConnection'),false);

  const mfa=harness({mfa:true});await mfa.submit();
  assert.equal(mfa.get('signin-success').open,false,'MFA challenge is not a successful sign-in');
  mfa.get('verification-code').value='123456';
  await mfa.get('local-login').onsubmit({preventDefault(){}});
  assert.equal(mfa.get('signin-success').open,true);
  const failed=harness();failed.fail();await failed.submit();
  assert.equal(failed.get('signin-success').open,false);
  assert.match(failed.get('local-result').textContent,/rejected/);

  const late=harness();let release;late.holdStatus(new Promise(resolve=>release=resolve));
  const pending=late.submit();await new Promise(resolve=>setImmediate(resolve));
  late.run('lock()');release();await pending;
  assert.equal(late.run('activeScreen'),'sleep');
  assert.equal(late.get('signin-success').open,false,'A late readiness check cannot reopen the popup after locking');
  console.log('Reconnect confirmation: missing API, managed sites, MFA, failed login, redirect, logout, and lock races passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
