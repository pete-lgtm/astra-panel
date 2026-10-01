'use strict';
let pairingToken=location.hash.slice(1);
history.replaceState(null,'',location.pathname);
const form=document.getElementById('form'),key=document.getElementById('key'),message=document.getElementById('message');
if(!pairingToken){form.hidden=true;message.textContent='Scan a fresh QR code from your keypad to continue.';}
form.onsubmit=async event=>{
 event.preventDefault();
 const value=key.value.trim();key.value='';
 document.getElementById('submit').disabled=true;
 try{
  const response=await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+pairingToken},body:JSON.stringify({key:value}),signal:AbortSignal.timeout(30000)});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||'Connection failed.');
  form.hidden=true;message.textContent='Your keypad is connected! You can close this page and continue on your keypad.';
 }catch(error){form.hidden=true;message.textContent='Could not confirm the connection. Check your keypad before scanning a new QR code.';}
 finally{pairingToken='';}
};
window.addEventListener('pagehide',()=>{key.value='';pairingToken='';});
