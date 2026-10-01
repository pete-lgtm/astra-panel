'use strict';
(()=>{
  const box=document.getElementById('computer-setup');if(!box)return;
  const begin=box.querySelector('[data-web-start]'),cancel=box.querySelector('[data-web-cancel]'),result=box.querySelector('[data-web-result]'),details=box.querySelector('[data-web-details]');
  const url=box.querySelector('[data-web-url]'),code=box.querySelector('[data-web-code]');
  let token='',poll,networkPoll,epoch=0,available=true,active=false;
  function showNetwork(session){
    available=session.computerSetupAvailable!==false;begin.disabled=!available||active;
    const info=session.networkSetup||{},ip=box.querySelector('[data-web-ip]'),address=box.querySelector('[data-web-address]');
    const row=box.querySelector('[data-web-address-row]'),unavailable=box.querySelector('[data-web-unavailable]');
    if(ip)ip.textContent=info.ipAddress||'Connect Wi-Fi or Ethernet';
    if(address)address.textContent=info.url||'';
    if(row)row.hidden=!info.url;
    if(unavailable)unavailable.hidden=available;
  }
  async function refreshNetwork(){
    try{const session=await(await fetch('/session')).json();if(session.browserSetup)return;token=session.token;showNetwork(session);}catch{}
    networkPoll=setTimeout(()=>{if(!box.hidden&&!active)refreshNetwork();else networkPoll=setTimeout(refreshNetwork,10000);},10000);
  }
  refreshNetwork();
  async function call(path){
    if(!token){const session=await(await fetch('/session')).json();if(session.browserSetup)throw new Error('You are already using computer setup.');token=session.token;}
    const response=await fetch('/browser-setup/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Keypad-Token':token},body:'{}',signal:AbortSignal.timeout(15000)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Computer setup is unavailable.');return data;
  }
  function hide(){epoch++;active=false;clearTimeout(poll);details.hidden=true;url.textContent='';code.textContent='';begin.disabled=!available;cancel.hidden=true;}
  function observe(attempt){
    poll=setTimeout(async()=>{
      try{
        const status=await call('status');if(attempt!==epoch)return;
        if(status.state==='connected'){
          details.hidden=true;code.textContent='';url.textContent='';
          result.textContent='Computer connected. Finish setup in that browser. The panel may lock while you work.';
        }else if(status.state!=='waiting'){
          hide();result.textContent=status.state==='complete'?'✓ Setup saved. Your panel is ready for its connection and door checks.':'Computer setup closed. Start a new session when you’re ready.';
          if(status.state==='complete')document.dispatchEvent(new Event('browser-setup-complete'));return;
        }
        observe(attempt);
      }catch(error){if(attempt===epoch){hide();result.textContent=error.message;}}
    },2000);
  }
  begin.onclick=async()=>{
    if(!available)return;
    hide();const attempt=epoch;active=true;begin.disabled=true;result.textContent='Opening computer setup…';
    try{
      const data=await call('start');if(attempt!==epoch)return;
      url.textContent=data.url;code.textContent=data.code;
      details.hidden=false;cancel.hidden=false;
      result.textContent='Pair within 10 minutes. You’ll then have 30 minutes to finish setup on your computer.';observe(attempt);
    }catch(error){if(attempt===epoch){hide();result.textContent=error.message;}}
  };
  cancel.onclick=async()=>{hide();try{await call('cancel');result.textContent='Computer access closed.';}catch(error){result.textContent=error.message;}};
  // Locking the touchscreen hides the code, without interrupting an authorized
  // computer session. Expiry and Finish setup still close server access.
  document.addEventListener('browser-pair-hide',hide);
  window.addEventListener('pagehide',()=>{hide();clearTimeout(networkPoll);});
})();
