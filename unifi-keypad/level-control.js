// Immediate slider feedback, with at most one write in flight per provider.
// Intermediate movements are replaced by the latest intent for that target.

// A range must not change on touch-down: that may be the start of a scroll.
// Pair this with touch-action:pan-y so the browser owns vertical scrolling.
function allowSliderScroll(slider){
  const handlers={};
  for(const name of ['pointerdown','pointerup','pointercancel','input','change','blur'])handlers[name]=slider['on'+name];
  let touch=null;
  function begin(event){
    slider.focus({preventScroll:true});
    handlers.pointerdown?.(event);
  }
  function direction(event){
    if(touch.axis)return;
    const dx=Math.abs(event.clientX-touch.x),dy=Math.abs(event.clientY-touch.y);
    if(Math.max(dx,dy)>=8)touch.axis=dy>=dx?'y':'x';
  }
  function update(event){
    const box=slider.getBoundingClientRect(),thumb=22;
    const fraction=Math.max(0,Math.min(1,(event.clientX-box.left-thumb/2)/Math.max(1,box.width-thumb)));
    const min=Number(slider.min),max=Number(slider.max),step=Number(slider.step)||1;
    const value=String(Math.max(min,Math.min(max,min+Math.round(fraction*(max-min)/step)*step)));
    if(value===slider.value)return;
    slider.value=value;touch.value=value;handlers.input?.(event);
  }
  slider.onpointerdown=event=>{
    if(event?.pointerType!=='touch'){handlers.pointerdown?.(event);return;}
    if(slider.disabled)return;
    // Cancel the range's native jump-to-position, not viewport panning.
    event.preventDefault();
    if(event.isPrimary===false)return;
    touch={id:event.pointerId,x:event.clientX,y:event.clientY,axis:'',value:slider.value};
  };
  slider.onpointermove=event=>{
    if(!touch||event.pointerId!==touch.id||slider.disabled)return;
    if(!touch.axis){
      direction(event);
      if(touch.axis==='x')begin(event);
    }
    if(touch.axis==='x'){event.preventDefault();update(event);}
  };
  slider.onpointerup=event=>{
    if(event?.pointerType!=='touch'){handlers.pointerup?.(event);return;}
    if(!touch||event.pointerId!==touch.id)return;
    const adjusting=touch.axis==='x';direction(event);
    if(touch.axis!=='y'&&!slider.disabled){
      if(!adjusting)begin(event);
      update(event);handlers.change?.(event);
    }
    touch=null;
  };
  slider.onpointercancel=event=>{
    if(event?.pointerType!=='touch'){handlers.pointercancel?.(event);return;}
    if(!touch||event.pointerId!==touch.id)return;
    const adjusting=touch.axis==='x';touch=null;
    // Native scrolling cancels pending touch without rendering or sending.
    if(adjusting)handlers.pointercancel?.(event);
  };
  for(const name of ['input','change'])slider['on'+name]=event=>{
    if(touch){slider.value=touch.value;return;}
    handlers[name]?.(event);
  };
  slider.onblur=event=>{touch=null;handlers.blur?.(event);};
}

function createLevelControl({active,start,send,render,refresh,failed}){
  const wanted=new Map(),queued=new Map();
  let generation=0,running=null,timer;
  const busy=()=>!!running||queued.size>0;
  function schedule(immediate=false){
    if(running||!queued.size||!active())return;
    if(immediate){clearTimeout(timer);timer=null;pump();}
    else if(!timer)timer=setTimeout(()=>{timer=null;pump();},150);
  }
  async function pump(){
    if(running||!queued.size||!active())return;
    const entry=queued.values().next().value,epoch=generation;
    queued.delete(entry.id);running=entry;
    try{
      await send(entry.command);
      if(epoch===generation&&wanted.get(entry.id)===entry)entry.until=Date.now()+6000;
    }catch(error){
      if(epoch===generation){
        // An uncertain write is never replayed, including queued movements.
        queued.clear();wanted.clear();failed(error.message);
      }
    }
    running=null;
    if(!active())return;
    if(queued.size)schedule();
    render();
    if(!busy())refresh();
  }
  return {
    busy,
    pending:()=>wanted.size>0,
    value:(id,actual)=>wanted.get(id)?.value??actual,
    requested:id=>wanted.has(id),
    set(id,value,command){
      if(!active()||!Number.isInteger(value)||value<0||value>100)return;
      if(wanted.get(id)?.value!==value){
        const entry={id,value,command,until:null};
        wanted.set(id,entry);queued.set(id,entry);start();render();
      }
      schedule();
    },
    flush(){schedule(true);},
    cancel(id){queued.delete(id);wanted.delete(id);if(!queued.size){clearTimeout(timer);timer=null;}},
    reset(){generation++;clearTimeout(timer);timer=null;queued.clear();wanted.clear();},
    reconcile(actual){
      for(const [id,entry] of wanted){
        if(entry.until===null)continue;
        const value=actual.get(id);
        if(value===entry.value)wanted.delete(id);
        else if(value==null||Date.now()>=entry.until){
          wanted.delete(id);
          failed('The device has not confirmed the requested level. Showing its reported status.');
        }
      }
    }
  };
}
