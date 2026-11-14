// Preview page (preview.html): the real orb in every state, driven by fake voices. Not part of the app.
const { createOrb, meter } = require("./orb.js");

const q = new URLSearchParams(location.search);
const size = Number(q.get("size")) || 128;
const freeze = Number(q.get("t")) || 0;
document.body.classList.toggle("light", q.get("bg") === "light");

// A speech-like envelope: syllables with pauses.
const voice = () => {
  const t = performance.now() / 1000;
  return (0.3 + 0.7 * (Math.sin(t * 9) * 0.5 + 0.5)) * Math.min(1, Math.max(0, Math.sin(t * 1.7) * 2 + 1.4));
};
// One cell goes through the app's real path: a Web Audio tone -> MediaStreamTrack -> meter().
const ctx = new AudioContext();
const osc = ctx.createOscillator(), gain = ctx.createGain(), out = ctx.createMediaStreamDestination();
gain.gain.value = 0.2;
osc.connect(gain).connect(out);
osc.start();
const tone = meter(ctx, out.stream.getAudioTracks()[0]);
window.toneLevel = () => tone.level();

const one = { running: 1, asking: false }, two = { running: 2, asking: false }, ask = { running: 1, asking: true };
// [label, mode, muted, level source, errands, script(orb) run once for cells that change over time]
const cells = [
  ["listening", "listening", false, voice],
  ["thinking", "thinking", false, voice],
  ["speaking", "speaking", false, voice],
  ["offline", "offline", false, voice],
  ["muted", "listening", true, voice],
  ["speaking (metered tone)", "speaking", false, () => tone.level()],
  ["listening, 2 errands", "listening", false, voice, two],
  ["thinking, 1 errand", "thinking", false, voice, one],
  ["speaking, 2 errands", "speaking", false, voice, two],
  ["question waiting", "listening", false, voice, ask],
  ["resting, 1 errand", "resting", false, voice, one],
  ["resting, question waiting", "resting", false, voice, ask],
  ["resting, muted", "resting", true, voice, two],
  // wake, talk, sleep to resting, the errand ends, hidden: every 6 s
  ["wake / rest / sleep loop", "hidden", false, voice, null, (orb) => {
    const steps = [[0, "listening", one], [1500, "speaking", one], [2800, "resting", one], [4200, "resting", { running: 0, asking: false }], [5400, "hidden", null]];
    const run = () => { for (const [at, mode, e] of steps) setTimeout(() => orb.set(mode, false, e), at); };
    run();
    setInterval(run, 6000);
  }],
];
for (const [label, mode, muted, src, errands = null, script] of cells) {
  const fig = document.createElement("figure"), canvas = document.createElement("canvas");
  canvas.style.width = canvas.style.height = `${size}px`;
  fig.append(canvas, label);
  document.body.append(fig);
  const orb = createOrb(canvas, src);
  orb.set(mode, muted, errands);
  if (script) script(orb);
}
if (freeze) setTimeout(() => { window.requestAnimationFrame = () => 0; }, freeze * 1000); // every orb holds its frame
