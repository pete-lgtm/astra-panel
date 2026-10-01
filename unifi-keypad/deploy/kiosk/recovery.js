const screen=document.getElementById('recovery'),frame=document.getElementById('keypad'),continueButton=document.getElementById('boot-continue');
const preview=location.protocol==='file:' || location.pathname==='/preview';
const MINIMUM_BOOT_MS=15000,BACKEND='http://127.0.0.1:8765';
let instance=null,pending=false,started=performance.now(),healthy=false,loaded=false,shown=false,needsReload=false,skipIntro=false;
function revealWhenReady(){
  if(!healthy||!loaded)return;
  if(!shown&&!skipIntro&&performance.now()-started<MINIMUM_BOOT_MS)return;
  screen.hidden=true;frame.hidden=false;shown=true;
}
function unavailable(){
  healthy=false;loaded=false;needsReload=true;frame.hidden=true;screen.hidden=false;continueButton.hidden=true;
  const waiting=performance.now()-started>=MINIMUM_BOOT_MS;
  document.body.dataset.phase=shown?'reconnecting':waiting?'waiting':'starting';
  document.getElementById('detail').textContent=shown?'Restoring your connection.':waiting?'Your panel is taking a little longer to wake up.':'A little light. A connected home.';
  document.getElementById('loading-label').textContent=shown?'RECONNECTING YOUR PANEL':waiting?'WAITING FOR YOUR PANEL':'WAKING UP YOUR PANEL';
  document.getElementById('notice').hidden=!shown&&!waiting;
  window.astraAurora?.start();
}
async function poll(){
  if(pending)return;pending=true;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),2500);
  try{
    const response=await fetch(BACKEND+'/health',{cache:'no-store',signal:controller.signal,credentials:'omit'});
    const data=await response.json();
    if(!response.ok||data.service!=='loxicom-keypad'||data.processHealthy!==true||typeof data.instance!=='string')throw new Error('Unavailable');
    healthy=true;
    if(data.instance!==instance||needsReload){
      // Preload immediately while the independent animation is visible.
      loaded=false;frame.hidden=true;frame.src=BACKEND+'/';instance=data.instance;needsReload=false;
    }
    continueButton.hidden=false;document.getElementById('notice').hidden=true;
    document.body.dataset.phase='starting';
    document.getElementById('detail').textContent='Your home, in sync.';
    document.getElementById('loading-label').textContent=loaded?'YOUR PANEL IS READY':'CONNECTING YOUR HOME';
    revealWhenReady();
  }catch{unavailable();}
  finally{clearTimeout(timeout);pending=false;}
}
if(preview){
  // A preview has no connection to the actual panel, its health, or its controls.
  document.getElementById('preview-label').hidden=false;
}else{
  frame.addEventListener('load',()=>{if(healthy){loaded=true;revealWhenReady();}});
  continueButton.addEventListener('click',()=>{skipIntro=true;revealWhenReady();});
  window.addEventListener('message',event=>{
    if(event.origin!==BACKEND||event.source!==frame.contentWindow||event.data?.type!=='astra-startup')return;
    if(event.data.ready===true)loaded=true;
    // An active alarm takes priority over decorative startup time.
    if(event.data.urgent===true)skipIntro=true;
    revealWhenReady();
  });
  poll();setInterval(poll,2000);setTimeout(revealWhenReady,MINIMUM_BOOT_MS);
}
