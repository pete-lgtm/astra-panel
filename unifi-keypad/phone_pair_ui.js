'use strict';
(()=>{
 const box=document.getElementById('phone-pair');if(!box)return;
 const begin=box.querySelector('[data-start]'),cancel=box.querySelector('[data-cancel]'),result=box.querySelector('[data-result]'),qr=box.querySelector('[data-qr]');
 let poll=null,active=false,epoch=0,token='';
 async function call(path,data={}){
  if(!token)token=(await (await fetch('/session')).json()).token;
  const response=await fetch('/phone-pair/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Keypad-Token':token},body:JSON.stringify(data)});
  const body=await response.json();if(!response.ok)throw new Error(body.error||'Phone setup unavailable.');return body;
 }
 function clear(){clearInterval(poll);qr.replaceChildren();cancel.hidden=true;active=false;begin.disabled=false;epoch++;}
 async function stop(){const wasActive=active;clear();if(wasActive)await call('cancel').catch(()=>{});}
 begin.onclick=async()=>{
  await stop();const attempt=epoch;
  begin.disabled=true;result.textContent='Preparing phone setup…';
  try{
   const host=document.getElementById('host').value.trim();
   const data=await call('start',{host});
   if(attempt!==epoch){await call('cancel');return;}
   // QR SVG contains only generated path geometry, no supplied markup.
   const doc=new DOMParser().parseFromString(data.qr,'image/svg+xml');
   const svg=document.importNode(doc.documentElement,true);svg.setAttribute('aria-label','Scan to connect your keypad');svg.setAttribute('role','img');svg.style.width='min(280px,100%)';qr.replaceChildren(svg);
   active=true;cancel.hidden=false;result.textContent='Scan with your phone, then paste your UniFi API key. Keep both devices on the same network. Expires in five minutes.';
   poll=setInterval(async()=>{
    try{const status=await call('status');if(attempt!==epoch)return;
     if(status.state==='saved'){clear();result.textContent='✓ API key saved. Your keypad is connected.';document.dispatchEvent(new Event('phone-api-connected'));}
     else if(status.state!=='waiting'){clear();result.textContent='Pairing closed. Create a new QR code to try again.';}
    }catch(error){await stop();result.textContent=error.message;}
   },1500);
  }catch(error){clear();result.textContent=error.message;}
 };
 cancel.onclick=()=>{stop();result.textContent='Phone setup cancelled.';};
 document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
 window.addEventListener('pagehide',()=>{if(active)fetch('/phone-pair/cancel',{method:'POST',headers:{'Content-Type':'application/json','X-Keypad-Token':token},body:'{}',keepalive:true}).catch(()=>{});clear();});
})();
