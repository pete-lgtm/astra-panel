/* A local music remote, independent of the PIN grant and idle timer. */
let standbyData=null,standbyPreferred='',standbyTimer,standbyGeneration=0,standbyLoading=false;
let standbyRunning=false,standbyBusy=false,standbyNotice='',standbyArtKey='',standbyArtGeneration=0;
let standbyArtRetry=0;
const standbyLevels=createLevelControl({
  active:()=>standbyActive()&&!!standbyData?.mediaToken&&!standbyBusy,
  start:()=>{clearTimeout(standbyTimer);standbyTimer=null;standbyGeneration++;standbyLoading=false;standbyNotice='';},
  send:async command=>{
    const result=await call('/standby/audio/control',command);
    if(!result.accepted)throw new Error('Sonos did not confirm the volume.');
  },
  render:()=>renderStandbyAudio(),refresh:()=>loadStandbyAudio(),
  failed:message=>{standbyNotice=message;renderStandbyAudio();}
});
function standbyActive(){
  return activeScreen==='sleep'&&!document.hidden&&!!token&&pinConfigured&&
    !['exit','entry','breach'].includes(document.body.dataset.alarmMode);
}
function rememberStandbyRoom(id){if(id)standbyPreferred=id;}
function clearStandbyCover(){
  standbyArtGeneration++;standbyArtKey='';standbyArtRetry=0;
  const img=el('standby-cover');img.onload=null;img.onerror=null;img.hidden=true;img.removeAttribute('src');el('standby-art').dataset.cover='';
}
async function loadStandbyCover(){
  const key=audioCoverKey(standbyData?.zone),grant=standbyData?.mediaToken;
  if(!key){if(standbyArtKey)clearStandbyCover();return;}
  if(key===standbyArtKey&&Date.now()<standbyArtRetry)return;
  if(key!==standbyArtKey)clearStandbyCover();
  standbyArtKey=key;standbyArtRetry=Infinity;
  const generation=++standbyArtGeneration;
  try{
    const result=await call('/standby/audio/artwork',{mediaToken:grant,artworkId:key});
    if(generation!==standbyArtGeneration||!standbyActive()||key!==audioCoverKey(standbyData?.zone))return;
    if(result.artworkId!==key||typeof result.image!=='string'||result.image.length>=1400000||!/^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(result.image))throw new Error('No cover');
    const img=el('standby-cover');
    img.onload=()=>{if(generation===standbyArtGeneration){img.hidden=false;el('standby-art').dataset.cover='ready';}};
    img.onerror=()=>{if(generation===standbyArtGeneration){img.hidden=true;el('standby-art').dataset.cover='';standbyArtRetry=Date.now()+60000;}};
    img.src=result.image;
  }catch(_error){if(generation===standbyArtGeneration)standbyArtRetry=Date.now()+60000;}
}
function renderStandbyAudio(){
  const zone=standbyData?.zone,visible=standbyActive()&&!!zone;
  el('standby-music').hidden=!visible;el('sleep').dataset.music=String(visible);
  const actions=zone?.transportActions||[],level=standbyLevels.value(zone?.id,Number.isInteger(zone?.volume)?zone.volume:null);
  const volumeReady=visible&&!standbyBusy&&level!==null&&!zone.fixedVolume;
  const usable=visible&&!standbyBusy&&!standbyLevels.busy();
  el('standby-volume-down').disabled=!volumeReady||level<=0;
  el('standby-volume-up').disabled=!volumeReady||level>=100;
  text('standby-volume',level===null?'—':level+'%');
  const playing=zone?.playback==='playing',verb=playing?'pause':'play';
  el('standby-play').disabled=!usable||!actions.includes(verb);
  el('standby-play').setAttribute('aria-label',(playing?'Pause ':'Play ')+(zone?.name||'music'));
  text('standby-play',playing?'Ⅱ':'▶');
  el('standby-next').hidden=!actions.includes('next');el('standby-next').disabled=!usable||!actions.includes('next');
  text('standby-music-notice',standbyNotice);el('standby-music-notice').hidden=!standbyNotice;
  if(!visible)return;
  el('standby-music').dataset.playback=zone.playback;
  text('standby-room-name',zone.name+(zone.groupRooms?.length>1?' + '+(zone.groupRooms.length-1):''));
  musicText('standby-track-title',zone.title||'Music playing');
  musicText('standby-track-artist',zone.artist||zone.source||'Sonos');
}
function syncStandbyAudio(){
  if(!standbyActive()){
    if(standbyRunning){
      standbyRunning=false;clearTimeout(standbyTimer);standbyTimer=null;standbyGeneration++;standbyLoading=false;
      standbyLevels.reset();standbyBusy=false;standbyData=null;standbyNotice='';clearStandbyCover();
    }
    renderStandbyAudio();return;
  }
  standbyRunning=true;renderStandbyAudio();
  if(!standbyTimer&&!standbyLoading&&!standbyBusy&&!standbyLevels.busy())loadStandbyAudio();
}
async function loadStandbyAudio(){
  if(!standbyActive()||standbyLoading||standbyBusy||standbyLevels.busy())return;
  clearTimeout(standbyTimer);standbyTimer=null;standbyLoading=true;
  const generation=++standbyGeneration;
  try{
    const result=await call('/standby/audio',standbyPreferred?{preferredRoom:standbyPreferred}:{});
    if(generation!==standbyGeneration||!standbyActive())return;
    standbyData=result;
    const zone=result.zone;
    standbyLevels.reconcile(new Map(zone?[[zone.id,zone.fixedVolume?null:zone.volume]]:[]));
    if(zone)standbyPreferred=zone.id;
    loadStandbyCover();
  }catch(_error){
    if(generation!==standbyGeneration||!standbyActive())return;
    // Never leave live controls or an old playing label after a failed refresh.
    standbyData=null;standbyLevels.reset();clearStandbyCover();
  }
  if(generation!==standbyGeneration||!standbyActive())return;
  standbyLoading=false;renderStandbyAudio();
  standbyTimer=setTimeout(()=>{standbyTimer=null;loadStandbyAudio();},standbyLevels.pending()?1000:8000);
}
async function controlStandbyAudio(action){
  if(!standbyActive()||!standbyData?.zone||standbyBusy||standbyLevels.busy())return;
  const grant=standbyData.mediaToken,zone=standbyData.zone;
  if(!zone.transportActions?.includes(action))return;
  clearTimeout(standbyTimer);standbyTimer=null;standbyLoading=false;standbyBusy=true;standbyNotice='';
  const generation=++standbyGeneration;
  if(action==='pause'||action==='play')zone.playback=action==='pause'?'paused':'playing';
  renderStandbyAudio();
  try{
    const result=await call('/standby/audio/control',{mediaToken:grant,action});
    if(!result.accepted)throw new Error('Sonos did not confirm the change.');
  }catch(error){if(generation===standbyGeneration)standbyNotice=error.message;}
  if(generation!==standbyGeneration||!standbyActive())return;
  standbyBusy=false;await loadStandbyAudio();
}
function changeStandbyVolume(delta){
  const zone=standbyData?.zone;
  if(!standbyActive()||!zone||standbyBusy||zone.fixedVolume||!Number.isInteger(zone.volume))return;
  const value=Math.max(0,Math.min(100,standbyLevels.value(zone.id,zone.volume)+delta));
  standbyLevels.set(zone.id,value,{mediaToken:standbyData.mediaToken,action:'volume',volume:value});
}
el('standby-volume-down').onclick=()=>{if(!el('standby-volume-down').disabled)changeStandbyVolume(-2);};
el('standby-volume-up').onclick=()=>{if(!el('standby-volume-up').disabled)changeStandbyVolume(2);};
el('standby-play').onclick=()=>{if(!el('standby-play').disabled)controlStandbyAudio(standbyData?.zone?.playback==='playing'?'pause':'play');};
el('standby-next').onclick=()=>{if(!el('standby-next').disabled)controlStandbyAudio('next');};
document.addEventListener('visibilitychange',syncStandbyAudio);
syncStandbyAudio();
