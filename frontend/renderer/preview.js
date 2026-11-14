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

const cells = [
  ["listening", "listening", false, voice],
  ["thinking", "thinking", false, voice],
  ["speaking", "speaking", false, voice],
  ["offline", "offline", false, voice],
  ["muted", "listening", true, voice],
  ["speaking (metered tone)", "speaking", false, () => tone.level()],
];
for (const [label, mode, muted, src] of cells) {
  const fig = document.createElement("figure"), canvas = document.createElement("canvas");
  canvas.style.width = canvas.style.height = `${size}px`;
  fig.append(canvas, label);
  document.body.append(fig);
  const orb = createOrb(canvas, src);
  orb.set(mode, muted);
  if (freeze) setTimeout(() => orb.set("hidden", muted), freeze * 1000);
}
