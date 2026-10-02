const assert = require('node:assert/strict');
const {createCountdownSound} = require('./countdown-sound.js');

function harness({state='running', fail=false}={}) {
  let now=0, visible=true, created=0;
  const tones=[], resumes=[];
  class AudioContext {
    constructor() { created++; if(fail)throw new Error('No output'); this.state=state; this.destination={}; }
    get currentTime(){return now/1000;}
    resume(){return new Promise(resolve=>resumes.push(()=>{this.state='running';resolve();}));}
    createOscillator(){
      const tone={frequency:{setValueAtTime(value){tone.hz=value;}},connect(){},disconnect(){tone.disconnected=true;},
        start(at){tone.startAt=at;tones.push(tone);},stop(at){tone.stopAt=at;}};
      return tone;
    }
    createGain(){return {gain:{setValueAtTime(){},linearRampToValueAtTime(){}},connect(){},disconnect(){}};}
  }
  const sound=createCountdownSound({AudioContext,now:()=>now,visible:()=>visible});
  return {sound,tones,resumes,time:value=>{now=value;},hide:value=>{visible=!value;},created:()=>created};
}
const exit=remaining=>({mode:'exit',remaining}),entry=remaining=>({mode:'entry',remaining});

(async()=>{
  const h=harness();
  h.sound.update({mode:'disarmed'});assert.equal(h.created(),0,'Idle does not open an audio output');
  h.sound.update(exit(60));assert.equal(h.tones.length,1);
  assert.equal(h.tones[0].hz,660);assert.ok(h.tones[0].stopAt-h.tones[0].startAt<.15);
  for(const t of [0,100,250,500,999]){h.time(t);h.sound.update(exit(60));}
  assert.equal(h.tones.length,1,'Polls and redraws must not repeat the beep');
  h.time(1000);h.sound.update(exit(59));assert.equal(h.tones.length,2);
  h.time(9000);h.sound.update(exit(51));assert.equal(h.tones.length,3,'A delayed frame cannot replay missed beeps');
  h.time(50000);h.sound.update(exit(10));assert.equal(h.tones.length,4);
  h.time(50499);h.sound.update(exit(10));assert.equal(h.tones.length,4);
  h.time(50500);h.sound.update(exit(10));assert.equal(h.tones.length,5,'Final ten seconds beep twice per second');
  h.sound.update(exit(0));assert.ok(h.tones.every(t=>t.disconnected));
  h.time(51000);h.sound.update(exit(null));assert.equal(h.tones.length,5,'Unknown timing must be silent');

  h.sound.update(entry(60));assert.equal(h.tones.at(-1).hz,880);
  const count=h.tones.length;
  h.time(52000);h.sound.update({...entry(59),fresh:false});
  assert.equal(h.tones.length,count+1,'An already displayed entry timer keeps its bounded warning after connection loss');
  h.sound.update({mode:'disarmed'});assert.ok(h.tones.at(-1).disconnected,'Confirmed disarm cancels the tone immediately');
  for(const view of [{mode:'armed'},{mode:'breach'},{mode:'unknown'},entry(NaN),entry(Infinity),entry(-1)])h.sound.update(view);
  assert.equal(h.tones.length,count+1,'No countdown sounds after expiry or without finite positive timing');

  h.sound.update(entry(20));h.hide(true);h.sound.update(entry(19));assert.ok(h.tones.at(-1).disconnected);
  const hiddenCount=h.tones.length;h.time(100000);h.sound.update(entry(1));assert.equal(h.tones.length,hiddenCount);
  h.hide(false);h.sound.update(entry(1));assert.equal(h.tones.length,hiddenCount+1);
  h.sound.stop();assert.ok(h.tones.at(-1).disconnected);

  const missing=harness({fail:true});
  for(let i=0;i<20;i++){missing.time(i*250);assert.doesNotThrow(()=>missing.sound.update(entry(60-i)));}
  assert.equal(missing.created(),1,'Broken outputs back off instead of disrupting every redraw');
  assert.equal(missing.tones.length,0);missing.sound.stop();assert.equal(await missing.sound.test(),'unavailable');
  const unsupported=createCountdownSound({now:()=>0,visible:()=>true});
  assert.doesNotThrow(()=>unsupported.update(exit(60)));unsupported.stop();assert.equal(await unsupported.test(),'unavailable');

  const blocked=harness({state:'suspended'});
  blocked.sound.update(entry(60));assert.equal(blocked.resumes.length,1);assert.equal(blocked.tones.length,0);
  // A real touch retries resume even if an earlier autoplay request remains pending.
  const gesture=blocked.sound.prime(true);assert.equal(blocked.resumes.length,2);
  blocked.resumes[1]();await gesture;
  blocked.time(1000);blocked.sound.update(entry(59));assert.equal(blocked.tones.length,1);
  blocked.sound.stop();blocked.resumes[0]();await Promise.resolve();
  assert.equal(blocked.tones.length,1,'An old resume completion must never create a late beep');

  const p=harness();assert.equal(await p.sound.test(),'playing');assert.equal(p.tones.length,1);
  p.sound.cancelPreview();assert.ok(p.tones[0].disconnected);
  p.sound.update(entry(30));assert.equal(await p.sound.test(),'countdown','A sound test cannot replace countdown cues');
  const late=harness({state:'suspended'});const test=late.sound.test();
  late.sound.cancelPreview();late.resumes[0]();assert.equal(await test,'cancelled');assert.equal(late.tones.length,0);
  const alarm=harness({state:'suspended'});const testBeforeAlarm=alarm.sound.test();
  alarm.sound.update(entry(30));alarm.resumes[0]();assert.equal(await testBeforeAlarm,'cancelled');
  assert.equal(alarm.tones.length,0,'Starting an alarm invalidates the queued test sound');
  const leaving=harness({state:'suspended'});let current=true;const left=leaving.sound.test(()=>current);
  current=false;leaving.resumes[0]();assert.equal(await left,'cancelled');assert.equal(leaving.tones.length,0);
  console.log('Countdown sound: cadence, expiry, disarm, hidden state, unavailable output, touch resume and cancelled tests passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
