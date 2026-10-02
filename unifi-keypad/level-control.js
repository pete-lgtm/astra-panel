// Immediate slider feedback, with at most one write in flight per provider.
// Intermediate movements are replaced by the latest intent for that target.
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
