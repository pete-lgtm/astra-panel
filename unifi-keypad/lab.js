let labToken, running = false;
const node = id => document.getElementById(id);
async function refreshLab(event) {
  if (!labToken || running) return;
  running = true;
  try {
    const response = await fetch('/lab/event', {method:'POST', headers:{'Content-Type':'application/json','X-Keypad-Token':labToken}, body:JSON.stringify({event})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    node('lab-state').textContent = data.state.replaceAll('_',' ').toUpperCase();
    node('countdown').textContent = data.remaining === null ? 'No pending countdown' : `${data.remaining} seconds remaining`;
    node('connection').textContent = data.connected ? 'Simulated connection available' : 'Simulated connection lost — state unverified';
    node('events').replaceChildren(...data.events.map(text => {const item=document.createElement('li');item.textContent=text;return item;}));
    node('error').textContent = '';
  } catch(error) {node('error').textContent=error.message; node('lab-state').textContent='CHECK FAILED / STATE UNVERIFIED';}
  finally {running=false;}
}
document.querySelectorAll('[data-event]').forEach(button => button.onclick=()=>refreshLab(button.dataset.event));
fetch('/session').then(r=>r.json()).then(data=>{labToken=data.token;refreshLab();});
setInterval(()=>refreshLab(),500);
