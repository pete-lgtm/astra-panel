/* Sonos status and explicit room controls. Polls never renew an idle PIN grant. */
let audioData=null,audioSelected='',audioTimer,audioGeneration=0,audioLoading=false,audioGrant='';
let audioControlBusy=false,audioControlGeneration=0,audioControlNotice='',audioVolumeRoom='';
let audioGrouping=null;
const audioRows=new Map();
const audioImages=new Map();
const audioDisplayedImages=new WeakMap();
let audioArtGeneration=0,audioArtActive=0,audioArtQueue=[];
const audioStates={playing:'Playing',paused:'Paused',stopped:'Stopped',idle:'Idle',loading:'Connecting',offline:'Unavailable',unknown:'Status unknown'};
function audioCoverKey(zone){return zone?.playback!=='offline'&&/^[a-f0-9]{64}$/.test(zone?.artworkId||'')?zone.artworkId:'';}
function showAudioCover(img,frame,key){
  const image=audioImages.get(key)?.image||'';
  if(audioDisplayedImages.get(img)===image)return;
  audioDisplayedImages.set(img,image);img.hidden=true;frame.dataset.cover='';img.onload=null;img.onerror=null;
  if(!image){img.removeAttribute('src');return;}
  img.onload=()=>{if(audioDisplayedImages.get(img)===image){img.hidden=false;frame.dataset.cover='ready';}};
  img.onerror=()=>{if(audioDisplayedImages.get(img)===image){img.hidden=true;frame.dataset.cover='';img.removeAttribute('src');}};
  img.src=image;
}
function renderAudioArtwork(){
  const zones=audioData?.zones||[],selected=zones.find(z=>z.id===audioSelected);
  showAudioCover(el('audio-cover'),el('audio-art'),audioCoverKey(selected));
  for(const zone of zones){const parts=audioRows.get(zone.id);if(parts)showAudioCover(parts.cover,parts.thumb,audioCoverKey(zone));}
}
function clearAudioArtwork(){
  audioArtGeneration++;audioArtActive=0;audioArtQueue=[];audioImages.clear();
  showAudioCover(el('audio-cover'),el('audio-art'),'');
  for(const parts of audioRows.values())showAudioCover(parts.cover,parts.thumb,'');
}
function pumpAudioArtwork(){
  if(!unlockToken||activeScreen!=='audio'||document.hidden)return;
  while(audioArtActive<2&&audioArtQueue.length){
    const key=audioArtQueue.shift(),entry=audioImages.get(key);
    if(!entry||entry.state!=='queued')continue;
    entry.state='loading';audioArtActive++;
    const generation=audioArtGeneration,grant=unlockToken;
    (async()=>{
      let image='';
      try{
        const result=await call('/hub/audio/artwork',{unlock:grant,artworkId:key});
        if(result.artworkId===key&&typeof result.image==='string'&&result.image.length<1400000&&/^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(result.image))image=result.image;
      }catch(_error){/* Artwork failure must never hide room or playback status. */}
      if(generation!==audioArtGeneration||grant!==unlockToken)return;
      if(audioImages.get(key)===entry){entry.state='ready';entry.image=image;entry.expires=Date.now()+(image?900000:60000);}
      audioArtActive--;
      if(activeScreen==='audio'&&!document.hidden){renderAudioArtwork();pumpAudioArtwork();}
    })();
  }
}
function loadAudioArtwork(){
  const zones=audioData?.zones||[],selected=zones.find(z=>z.id===audioSelected);
  // Prioritize the selected cover and bound memory/network work on large systems.
  const wanted=[...new Set([audioCoverKey(selected),...zones.map(audioCoverKey)].filter(Boolean))].slice(0,12);
  for(const key of audioImages.keys())if(!wanted.includes(key))audioImages.delete(key);
  for(const key of wanted){
    const entry=audioImages.get(key);
    if(!entry||(entry.state==='ready'&&entry.expires<Date.now())){audioImages.set(key,{state:'queued',image:''});}
  }
  audioArtQueue=wanted.filter(key=>audioImages.get(key)?.state==='queued');
  renderAudioArtwork();pumpAudioArtwork();
}
function clearAudioHub(){
  closeAudioGrouping();
  clearTimeout(audioTimer);audioGeneration++;audioLoading=false;audioGrant='';audioData=null;audioSelected='';
  audioControlGeneration++;audioControlBusy=false;audioControlNotice='';audioVolumeRoom='';renderAudioControls();
  clearAudioArtwork();
  audioRows.clear();
  el('audio-rooms').textContent='';el('audio-layout').hidden=true;el('audio-empty').hidden=false;
  el('audio-notice').hidden=true;el('audio-discover').disabled=false;
  for(const id of ['audio-track-title','audio-track-artist','audio-track-album','audio-room-name','audio-group-detail','audio-now-state','audio-source'])text(id,'');
}
function syncAudioScreen(){
  clearTimeout(audioTimer);
  if(activeScreen!=='audio'||!unlockToken)closeAudioGrouping();
  if(activeScreen!=='audio'||!unlockToken||document.hidden){audioGeneration++;audioLoading=false;audioGrant='';clearAudioArtwork();return;}
  if(audioGrant===unlockToken && audioLoading)return;
  audioGrant=unlockToken;loadAudio();
}
function audioTrackTitle(zone){
  if(zone.playback==='offline')return 'Room unavailable';
  return zone.title || (zone.playback==='playing'?'Audio playing':zone.playback==='loading'?'Connecting…':'Nothing playing');
}
function renderAudioControls(){
  const zone=audioData?.zones?.find(z=>z.id===audioSelected),actions=zone?.transportActions||[];
  const usable=!!unlockToken&&!!zone&&zone.playback!=='offline'&&!audioControlBusy;
  el('audio-link-rooms').disabled=!usable||!zone.groupRevision||!zone.groupMembers?.some(m=>m.id===zone.groupId)||!audioGroupingAllowed();
  const playing=zone?.playback==='playing',verb=playing?'pause':'play';
  el('audio-play-pause').disabled=!usable||!actions.includes(verb);
  text('audio-play-label',audioControlBusy?'Updating…':playing?'Pause':'Play');text('audio-play-symbol',playing?'Ⅱ':'▶');
  el('audio-play-pause').setAttribute('aria-label',(playing?'Pause ':'Play ')+(zone?.name||'room')+(zone?.groupRooms?.length>1?' group':''));
  el('audio-stop').disabled=!usable||!actions.includes('stop');
  el('audio-stop').setAttribute('aria-label','Stop '+(zone?.name||'room')+(zone?.groupRooms?.length>1?' group':''));
  el('audio-mute').disabled=!usable||typeof zone?.muted!=='boolean';
  text('audio-mute',zone?.muted?'Unmute':'Mute');el('audio-mute').setAttribute('aria-pressed',String(!!zone?.muted));
  el('audio-mute').setAttribute('aria-label',(zone?.muted?'Unmute ':'Mute ')+(zone?.name||'room'));
  const level=Number.isInteger(zone?.volume)?zone.volume:null,volumeReady=usable&&level!==null&&!zone.fixedVolume;
  el('audio-volume-slider').disabled=!volumeReady;
  el('audio-volume-down').disabled=!volumeReady||level<=0;el('audio-volume-up').disabled=!volumeReady||level>=100;
  if(audioVolumeRoom!==audioSelected){
    audioVolumeRoom='';el('audio-volume-slider').value=String(level??0);
    text('audio-volume-value',level===null?'—':level+'%');
    el('audio-volume-slider').setAttribute('aria-valuetext',zone?.fixedVolume?'Fixed output':level===null?'Unavailable':level+' percent');
  }
}
function audioGroupingAllowed(){return typeof liveObservation==='undefined'||!liveObservation||!['exit','entry','breach'].includes(alarmPresentation(liveObservation).mode);}
function closeAudioGrouping(){
  audioGrouping=null;
  const dialog=el('audio-group-dialog');if(dialog.open)dialog.close();
  el('audio-group-options').textContent='';
}
function renderAudioGrouping(){
  const view=audioGrouping;if(!view)return;
  const current=audioData?.zones?.find(z=>z.id===view.roomId);
  const stale=!current||current.playback==='offline'||current.groupRevision!==view.revision;
  const changed=[...view.wanted].sort().join('|')!==view.original;
  for(const option of view.options){
    option.input.checked=option.ids.every(id=>view.wanted.has(id));
    option.input.disabled=audioControlBusy||stale||option.disabled;
  }
  text('audio-group-count',view.wanted.size+' room'+(view.wanted.size===1?'':'s')+' selected');
  text('audio-group-save',audioControlBusy?'Linking…':'Save links');
  el('audio-group-save').disabled=audioControlBusy||stale||!changed;
  el('audio-group-close').disabled=audioControlBusy;
  text('audio-group-message',stale?'Room links changed. Close and reopen Link rooms to review them.':'');
  el('audio-group-message').hidden=!stale;
}
function openAudioGrouping(){
  if(el('audio-link-rooms').disabled||!unlockToken||activeScreen!=='audio'||!audioGroupingAllowed())return;
  const zones=audioData?.zones||[],selected=zones.find(z=>z.id===audioSelected);
  if(!selected?.groupRevision)return;
  const peers=zones.filter(z=>z.groupScope===selected.groupScope),source=peers.find(z=>z.id===selected.groupId);
  if(!source)return;
  closeAudioGrouping();activity();
  const initial=peers.filter(z=>z.groupId===source.id).map(z=>z.id);
  const view={roomId:selected.id,groupId:source.id,revision:selected.groupRevision,wanted:new Set(initial),original:initial.sort().join('|'),options:[]};
  audioGrouping=view;text('audio-group-source','Audio from '+source.name);
  const choices=peers.filter(z=>z.groupId===source.id).sort((a,b)=>(b.id===source.id)-(a.id===source.id)||a.name.localeCompare(b.name)).map(z=>[z]);
  const otherGroups=new Map();
  for(const zone of peers.filter(z=>z.groupId!==source.id)){
    if(!otherGroups.has(zone.groupId))otherGroups.set(zone.groupId,[]);
    otherGroups.get(zone.groupId).push(zone);
  }
  choices.push(...otherGroups.values());
  for(const rooms of choices){
    const ids=rooms.map(z=>z.id),isSource=ids.includes(source.id),unavailable=rooms.some(z=>z.playback==='offline');
    const row=hubNode('label','audio-group-choice'),input=hubNode('input','');input.type='checkbox';
    const copy=hubNode('span','audio-group-choice-copy'),name=rooms.map(z=>z.name).join(' + ');
    copy.append(hubNode('strong','',name));
    const duplicate=rooms.length===1&&zones.some(z=>z.id!==rooms[0].id&&z.name===rooms[0].name);
    const detail=isSource?'Audio source':unavailable?'Room unavailable':rooms.length>1?rooms.length+' linked rooms · move together':duplicate?rooms[0].model||'Sonos room':rooms[0].groupId===source.id?'Linked now':'Add to this group';
    copy.append(hubNode('small','',detail));row.append(input,copy);el('audio-group-options').append(row);
    input.setAttribute('aria-label',name+(duplicate?' · '+(rooms[0].model||'Sonos room'):'')+(isSource?' · Audio source':''));
    input.onchange=()=>{
      if(input.disabled||audioGrouping!==view)return;
      for(const id of ids){if(input.checked)view.wanted.add(id);else view.wanted.delete(id);}
      activity();renderAudioGrouping();
    };
    view.options.push({input,ids,disabled:isSource||unavailable});
  }
  renderAudioGrouping();el('audio-group-dialog').showModal();el('audio-group-close').focus();
}
async function saveAudioGrouping(){
  const view=audioGrouping;
  if(!view||el('audio-group-save').disabled||audioControlBusy||!unlockToken||activeScreen!=='audio'||!audioGroupingAllowed())return;
  const grant=unlockToken,generation=++audioControlGeneration;
  clearTimeout(audioTimer);audioGeneration++;audioLoading=false;audioControlBusy=true;audioControlNotice='';activity();
  renderAudioControls();renderAudioGrouping();
  try{
    const result=await call('/hub/audio/group',{unlock:grant,roomId:view.roomId,groupId:view.groupId,revision:view.revision,roomIds:[...view.wanted].sort()});
    if(!result.confirmed)throw new Error('Sonos has not confirmed the room links. Refresh before trying again.');
    if(generation===audioControlGeneration&&grant===unlockToken)audioControlNotice='Room links updated.';
  }catch(error){if(generation===audioControlGeneration&&grant===unlockToken)audioControlNotice=error.message;}
  if(generation!==audioControlGeneration||grant!==unlockToken)return;
  audioControlBusy=false;closeAudioGrouping();
  if(activeScreen==='audio'){renderAudioControls();await loadAudio();}
}
async function controlAudio(action,settings={}){
  const zone=audioData?.zones?.find(z=>z.id===audioSelected);
  if(audioControlBusy||!unlockToken||activeScreen!=='audio'||!zone||zone.playback==='offline')return;
  const grant=unlockToken,generation=++audioControlGeneration;
  const request={unlock:grant,roomId:zone.id,groupId:zone.groupId,action,...settings};
  clearTimeout(audioTimer);audioGeneration++;audioLoading=false;audioControlBusy=true;audioControlNotice='';
  el('audio-notice').hidden=true;el('audio-discover').disabled=true;activity();renderAudioControls();
  try{
    const result=await call('/hub/audio/control',request);
    if(!result.accepted)throw new Error('Sonos did not confirm the change. Refresh before trying again.');
  }catch(error){
    if(generation===audioControlGeneration&&grant===unlockToken)audioControlNotice=error.message;
  }
  if(generation!==audioControlGeneration||grant!==unlockToken)return;
  audioControlBusy=false;
  if(activeScreen==='audio'){renderAudioControls();await loadAudio();}
}
function renderAudio(){
  const zones=audioData?.zones||[],playing=zones.filter(z=>z.playback==='playing').length;
  el('audio-layout').hidden=!zones.length;el('audio-empty').hidden=!!zones.length;
  text('audio-summary',zones.length?zones.length+' rooms · '+playing+' playing':audioLoading?'Finding Sonos…':'Your Sonos rooms');
  text('audio-room-count',String(zones.length));
  text('audio-empty-title',audioLoading?'Finding your rooms…':audioData?.state==='unavailable'?'Sonos unavailable':'No rooms found');
  text('audio-empty-detail',audioLoading?'Looking for Sonos on your local network.':audioData?.message||'Set up your speakers in the Sonos app, then tap Find rooms.');
  el('audio-discover').disabled=audioLoading||audioControlBusy;text('audio-discover',audioLoading?'Checking…':'Find rooms');
  if(audioControlNotice){text('audio-notice',audioControlNotice);el('audio-notice').hidden=false;}
  if(!zones.length){clearAudioArtwork();renderAudioControls();renderAudioGrouping();return;}
  if(!zones.some(z=>z.id===audioSelected))audioSelected=(zones.find(z=>z.playback==='playing')||zones[0]).id;
  const list=el('audio-rooms'),top=list.scrollTop;
  const orderChanged=[...audioRows.keys()].join('|')!==zones.map(z=>z.id).join('|');
  if(orderChanged){list.textContent='';audioRows.clear();}
  for(const zone of zones){
    let parts=audioRows.get(zone.id);
    if(!parts){
      const row=hubNode('button','audio-room');row.type='button';row.dataset.audioRoom=zone.id;
      const signal=hubNode('span','audio-signal');signal.setAttribute('aria-hidden','true');
      for(let i=0;i<4;i++)signal.append(hubNode('i',''));
      const thumb=hubNode('span','audio-thumb'),cover=hubNode('img','audio-room-cover');
      thumb.setAttribute('aria-hidden','true');cover.alt='';cover.decoding='async';cover.hidden=true;thumb.append(signal,cover);
      const copy=hubNode('span','audio-room-copy'),name=hubNode('strong',''),title=hubNode('span',''),links=hubNode('span','audio-room-links'),detail=hubNode('span','audio-room-state');
      copy.append(name,title,links);row.append(thumb,copy,detail);
      row.onclick=()=>{if(!unlockToken||activeScreen!=='audio')return;audioSelected=zone.id;audioControlNotice='';el('audio-notice').hidden=true;activity();renderAudio();};
      parts={row,signal,thumb,cover,name,title,links,detail};audioRows.set(zone.id,parts);list.append(row);
    }
    const {row,signal,name,title,links,detail}=parts;
    row.setAttribute('aria-pressed',String(zone.id===audioSelected));
    row.setAttribute('aria-label',zone.name+', '+(audioStates[zone.playback]||'Status unknown')+', '+audioTrackTitle(zone));
    const duplicate=zones.some(other=>other.id!==zone.id&&other.name===zone.name);
    signal.dataset.playback=zone.playback;name.textContent=zone.name;
    title.textContent=(duplicate&&zone.model?zone.model.replace(/^Sonos /,'')+' · ':'')+audioTrackTitle(zone);
    detail.textContent=audioStates[zone.playback]||'Unknown';
    const linked=zone.playback!=='offline'&&zone.groupMembers?.length>1;
    const linkedNames=linked?zone.groupMembers.filter(m=>m.id!==zone.id).map(m=>m.name).join(' + '):'';
    links.hidden=!linked;links.textContent=linked?'Linked: '+linkedNames:'';
    row.dataset.linked=String(!!linked);row.title=linked?'Linked with '+linkedNames:'';
    if(linked)row.setAttribute('aria-label',zone.name+', '+(audioStates[zone.playback]||'Status unknown')+', '+audioTrackTitle(zone)+', linked with '+linkedNames);
  }
  list.scrollTop=top;
  const zone=zones.find(z=>z.id===audioSelected);
  text('audio-now-state',zone.playback==='playing'?'Now playing':audioStates[zone.playback]||'Status unknown');
  el('audio-now-state').dataset.playback=zone.playback;
  text('audio-source',zone.source||'Sonos');
  text('audio-art-symbol',zone.source==='TV'?'▣':zone.source==='Line-in'?'↗':'♪');
  text('audio-track-title',audioTrackTitle(zone));
  text('audio-track-artist',zone.playback==='offline'?'Check its power and network connection.':zone.artist||'');
  text('audio-track-album',zone.album||'');
  text('audio-room-name',zone.name);
  text('audio-group-detail',zone.groupMembers?.length>1?'Linked with '+zone.groupMembers.filter(m=>m.id!==zone.id).map(m=>m.name).join(' · '):zone.groupRooms?.length>1?'Linked with '+zone.groupRooms.filter(n=>n!==zone.name).join(' · '):zone.model||'Sonos');
  renderAudioControls();
  renderAudioGrouping();
  loadAudioArtwork();
}
async function loadAudio(rescan=false){
  if(!unlockToken||activeScreen!=='audio'||document.hidden||audioLoading||audioControlBusy)return;
  clearTimeout(audioTimer);audioLoading=true;const generation=++audioGeneration,grant=unlockToken;
  if(!audioData)renderAudio();else{el('audio-discover').disabled=true;text('audio-discover',rescan?'Finding…':'Checking…');}
  try{
    const data=await call('/hub/audio',{unlock:grant,...(rescan?{rescan:true}:{})});
    if(generation!==audioGeneration||grant!==unlockToken||activeScreen!=='audio')return;
    audioData=data;el('audio-notice').hidden=true;
  }catch(error){
    if(generation!==audioGeneration||grant!==unlockToken||activeScreen!=='audio')return;
    // Never leave a cached song labeled as currently playing after a failed read.
    if(audioData?.zones?.length){
      audioData={...audioData,zones:audioData.zones.map(z=>({...z,playback:'offline',title:'',artist:'',album:'',source:'',artworkId:null}))};
      text('audio-notice','Could not refresh Sonos. Tap Find rooms to try again.');el('audio-notice').hidden=false;
    }else audioData={state:'unavailable',zones:[],message:'Could not reach Sonos. Check the local network and try Find rooms.'};
  }
  if(generation!==audioGeneration||grant!==unlockToken||activeScreen!=='audio')return;
  audioLoading=false;renderAudio();audioTimer=setTimeout(()=>loadAudio(),8000);
}
el('audio-discover').onclick=()=>{activity();audioControlNotice='';loadAudio(true);};
el('audio-play-pause').onclick=()=>{const z=audioData?.zones?.find(z=>z.id===audioSelected);if(!el('audio-play-pause').disabled)controlAudio(z?.playback==='playing'?'pause':'play');};
el('audio-stop').onclick=()=>{if(!el('audio-stop').disabled)controlAudio('stop');};
el('audio-mute').onclick=()=>{const z=audioData?.zones?.find(z=>z.id===audioSelected);if(!el('audio-mute').disabled)controlAudio('mute',{muted:!z.muted});};
el('audio-volume-down').onclick=()=>{if(!el('audio-volume-down').disabled)controlAudio('volume_down');};
el('audio-volume-up').onclick=()=>{if(!el('audio-volume-up').disabled)controlAudio('volume_up');};
el('audio-volume-slider').onpointerdown=()=>{if(!el('audio-volume-slider').disabled)audioVolumeRoom=audioSelected;};
el('audio-volume-slider').oninput=()=>{if(el('audio-volume-slider').disabled)return;audioVolumeRoom=audioVolumeRoom||audioSelected;text('audio-volume-value',el('audio-volume-slider').value+'%');};
el('audio-volume-slider').onchange=()=>{const room=audioVolumeRoom,value=Number(el('audio-volume-slider').value);audioVolumeRoom='';if(room===audioSelected&&!el('audio-volume-slider').disabled)controlAudio('volume',{volume:value});};
el('audio-volume-slider').onpointercancel=()=>{audioVolumeRoom='';renderAudioControls();};
el('audio-volume-slider').onblur=()=>{audioVolumeRoom='';renderAudioControls();};
el('audio-link-rooms').onclick=openAudioGrouping;
el('audio-group-save').onclick=saveAudioGrouping;
el('audio-group-close').onclick=()=>{if(!audioControlBusy){activity();closeAudioGrouping();}};
el('audio-group-dialog').oncancel=event=>{if(audioControlBusy)event.preventDefault();else closeAudioGrouping();};
document.addEventListener('visibilitychange',()=>syncAudioScreen());
syncAudioScreen();
