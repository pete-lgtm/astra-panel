/* Local countdown cues only. This module never sends an alarm or Sonos command. */
function createCountdownSound(options = {}) {
  const now = options.now || (() => performance.now());
  const visible = options.visible || (() => typeof document === 'undefined' || !document.hidden);
  const Context = options.AudioContext || globalThis.AudioContext || globalThis.webkitAudioContext;
  let context = null, mode = '', lastTick = -Infinity, previewRevision = 0;
  let resumePending = null, retryAt = 0;
  const voices = new Set();

  function audio() {
    if (!Context || now() < retryAt) return null;
    try {
      if (!context || context.state === 'closed') context = new Context({latencyHint:'interactive'});
      return context;
    } catch {
      retryAt = now() + 5000;
      return null;
    }
  }
  function prime(fromGesture = false) {
    const output = audio();
    if (!output) return Promise.resolve(false);
    if (output.state === 'running') return Promise.resolve(true);
    if (!resumePending || fromGesture) {
      try {
        const attempt = Promise.resolve(output.resume()).then(() => output.state === 'running', () => false)
          .finally(() => { if (resumePending === attempt) resumePending = null; });
        resumePending = attempt;
      } catch { return Promise.resolve(false); }
    }
    return resumePending;
  }
  function silence(kind) {
    for (const voice of Array.from(voices)) {
      if (kind && voice.kind !== kind) continue;
      try { voice.oscillator.stop(); } catch { /* An ended tone is already silent. */ }
      try { voice.oscillator.disconnect(); voice.gain.disconnect(); } catch { /* Device disconnected. */ }
      voices.delete(voice);
    }
  }
  function tone(kind, frequency, duration = .09) {
    const output = audio();
    if (!output || output.state !== 'running') return false;
    let oscillator, gain;
    try {
      oscillator = output.createOscillator(); gain = output.createGain();
      const voice = {kind, oscillator, gain}, start = output.currentTime;
      oscillator.type = 'sine'; oscillator.frequency.setValueAtTime(frequency, start);
      // Short ramps avoid clicks; gain stays bounded independently of Sonos volume.
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(.12, start + .008);
      gain.gain.setValueAtTime(.12, start + duration - .02);
      gain.gain.linearRampToValueAtTime(0, start + duration);
      oscillator.connect(gain); gain.connect(output.destination); voices.add(voice);
      oscillator.onended = () => {
        voices.delete(voice);
        try { oscillator.disconnect(); gain.disconnect(); } catch { /* Device disconnected. */ }
      };
      oscillator.start(start); oscillator.stop(start + duration);
      return true;
    } catch {
      try { oscillator?.stop(); oscillator?.disconnect(); gain?.disconnect(); } catch { /* No audio output. */ }
      for (const voice of voices) if (voice.oscillator === oscillator) voices.delete(voice);
      return false;
    }
  }
  function cancelPreview() {
    previewRevision++;
    silence('test');
  }
  function stop() {
    mode = ''; lastTick = -Infinity;
    cancelPreview(); silence();
  }
  function update(view) {
    if (!visible()) { stop(); return; }
    const counting = ['exit','entry'].includes(view?.mode) && Number.isFinite(view.remaining) && view.remaining > 0;
    if (!counting) {
      mode = ''; lastTick = -Infinity; silence('countdown');
      return;
    }
    if (mode !== view.mode) {
      stop(); mode = view.mode;
    }
    const tick = now(), interval = view.remaining <= 10 ? 500 : 1000;
    if (tick - lastTick < interval) return;
    // No backlog: a delayed browser frame produces one current cue, never a burst.
    lastTick = tick;
    const output = audio();
    if (output && output.state !== 'running') { void prime(); return; }
    tone('countdown', mode === 'entry' ? 880 : 660);
  }
  async function test(current = () => true) {
    if (mode) return 'countdown';
    cancelPreview();
    const revision = previewRevision;
    if (!Context) return 'unavailable';
    let timer;
    const ready = await Promise.race([prime(true), new Promise(resolve => { timer = setTimeout(() => resolve(false), 3000); })])
      .finally(() => clearTimeout(timer));
    // A late audio permission/device response cannot interrupt a new alarm or PIN screen.
    if (revision !== previewRevision || mode || !visible() || !current()) return 'cancelled';
    if (!ready) return 'unavailable';
    return tone('test', 880, .2) ? 'playing' : 'unavailable';
  }
  return {update, prime, test, stop, cancelPreview};
}

const countdownSound = createCountdownSound();
if (typeof document !== 'undefined') {
  // Passive capture primes browser audio without eating the first touchscreen tap.
  for (const event of ['pointerup','keydown']) document.addEventListener(event, () => { void countdownSound.prime(true); }, {capture:true,passive:true});
  document.addEventListener('visibilitychange', () => { if (document.hidden) countdownSound.stop(); });
}
if (typeof window !== 'undefined') window.addEventListener('pagehide', () => countdownSound.stop());
if (typeof module !== 'undefined' && module.exports) module.exports = {createCountdownSound};
