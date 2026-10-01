const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const elements={};let timeout;
const element=()=>({dataset:{},classList:{toggle(){}},querySelectorAll(){return []},append(){},addEventListener(){},setAttribute(){},value:'',focus(){},showModal(){this.open=true},close(){this.open=false}});
const context=vm.createContext({document:{body:element(),getElementById:id=>elements[id]??=element(),addEventListener(){},createElement:element},performance:{now:()=>1000},Date,Math,setInterval(){},setTimeout(fn){timeout=fn},clearTimeout(){},fetch:()=>new Promise(()=>{}),confirm:()=>false});
vm.runInContext(fs.readFileSync('app.js','utf8'),context);
(async()=>{
 for(const id of ['local-host','local-user','local-password','verification-code']) elements[id]??=element();
 vm.runInContext("let sent=[];let replies=[{mfaRequired:true,message:'challenge'},{localConnected:true,profiles:[],message:'ok'}];call=async(p,b)=>{sent.push(b);return replies.shift()};refresh=async()=>{status={connected:true,pinSet:true,webhookReady:true,armAwayConfigured:true,armStayConfigured:true}};",context);
 elements['local-host'].value='192.168.0.1';elements['local-user'].value='test';elements['local-password'].value='test-only-password';
 await elements['local-login'].onsubmit({preventDefault(){}});
 assert.equal(elements['local-password'].value,'');assert.equal(elements['local-password-field'].hidden,true);assert.equal(elements['verification-code'].required,true);
 elements['verification-code'].value='123456';
 await elements['local-login'].onsubmit({preventDefault(){}});
 assert.equal(vm.runInContext('sent[1].password',context),'test-only-password');
 assert.equal(vm.runInContext('sent[1].verificationCode',context),'123456');
 assert.equal(vm.runInContext('pendingLogin',context),null);
 assert.equal(elements['signin-success'].open,true);
 assert.equal(elements['signin-finish-connection'].hidden,true);
 assert.equal(elements['local-login'].hidden,true);
 for(const ending of ['cancel','timeout','error']) {
  vm.runInContext("replies=[{mfaRequired:true,message:'challenge'}]",context);
  elements['local-password'].value='test-only-password';
  await elements['local-login'].onsubmit({preventDefault(){}});
  if(ending==='cancel') elements['cancel-verification'].onclick();
  if(ending==='timeout') timeout();
  if(ending==='error') { vm.runInContext("call=async()=>{throw new Error('Rejected')}",context);elements['verification-code'].value='123456';await elements['local-login'].onsubmit({preventDefault(){}}); }
  assert.equal(vm.runInContext('pendingLogin',context),null);assert.equal(elements['local-password'].value,'');
 }
 console.log('Single-password MFA flow and credential cleanup checks passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
