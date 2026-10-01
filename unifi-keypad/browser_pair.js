'use strict';
(()=>{
  const form=document.getElementById('pair-form'),input=document.getElementById('pair-code'),button=document.getElementById('pair-submit'),message=document.getElementById('pair-message');
  let pairingToken=window.location.hash.slice(1),busy=false;
  // Remove the one-use token before any navigation or request. It is never a query string.
  window.history.replaceState(null,'','/');
  async function pair(body){
    if(busy)return;busy=true;button.disabled=true;message.classList.remove('error');message.textContent='Connecting to your panel…';input.value='';
    try{
      const response=await fetch('/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not connect to your panel.');
      window.location.replace('/setup');
    }catch(error){message.textContent=error.name==='TimeoutError'?'Connection timed out. Check that both devices are on the same network, then try again.':error.message;message.classList.add('error');}
    finally{pairingToken='';body=null;busy=false;button.disabled=false;}
  }
  form.onsubmit=event=>{event.preventDefault();return pair({code:input.value});};
  if(pairingToken)pair({token:pairingToken});
  else fetch('/session').then(r=>r.ok?r.json():null).then(data=>{if(data?.browserSetup){form.hidden=true;document.getElementById('resume').hidden=false;message.textContent='This browser is already connected.';}}).catch(()=>{});
  window.addEventListener('pagehide',()=>{input.value='';pairingToken='';});
})();
