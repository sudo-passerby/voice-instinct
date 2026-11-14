// Pure numbers behind the orb's look: audio samples in, drawing parameters out. Unit-tested.

// Loudness (0..1) of an AnalyserNode's time-domain bytes (128 = silence). FLOOR keeps a quiet room
// from animating the orb; CEIL is loud speech close to the mic; the curve lifts quiet speech.
// Tune these two if the orb looks dead or pinned on a given microphone.
const FLOOR = 0.012, CEIL = 0.3;
function level(bytes) {
  if (!bytes.length) return 0;
  let sum = 0;
  for (let i = 0; i < bytes.length; i++) { const v = (bytes[i] - 128) / 128; sum += v * v; }
  const x = (Math.sqrt(sum / bytes.length) - FLOOR) / (CEIL - FLOOR);
  return x <= 0 ? 0 : Math.min(1, x ** 0.6);
}

// Frame-rate independent smoothing: up fast (a syllable shows at once), down slowly (no flicker).
function follow(current, target, dt, up = 18, down = 5) {
  return current + (target - current) * (1 - Math.exp(-dt * (target > current ? up : down)));
}

// The wake ignition (the star flares and the disc opens around it) and the sleep collapse: 0 closed .. 1 open,
// at a steady rate. The app waits COLLAPSE_S before it hides the window.
const IGNITE_S = 0.28, COLLAPSE_S = 0.15;
function ignite(current, open, dt) {
  return Math.min(1, Math.max(0, current + (open ? dt / IGNITE_S : -dt / COLLAPSE_S)));
}

// What each state looks like. flow: shell travel (-1 in toward the star = hearing you, +1 out = Nova
// speaking). warm: gold, Nova's own voice. shape: the outline moves with the voice. orbit: the
// thinking comets. broken: dashed shells, no connection. rest: small and dim, an errand is running while
// Nova is not awake. source: whose audio level drives it.
const LOOKS = {
  listening: { flow: -1, warm: 0, shape: 0, orbit: 0, broken: 0, rest: 0, source: "mic" },
  thinking: { flow: 0, warm: 0, shape: 0, orbit: 1, broken: 0, rest: 0, source: null },
  speaking: { flow: 1, warm: 1, shape: 1, orbit: 0, broken: 0, rest: 0, source: "bot" },
  resting: { flow: 0, warm: 0, shape: 0, orbit: 0, broken: 0, rest: 1, source: null },
  offline: { flow: 0, warm: 0, shape: 0, orbit: 0, broken: 1, rest: 0, source: null },
};
// errands: { running, asking } from the backend. bodies: one circles the star per running errand (three
// at most; a waiting question always has one). ask: a question waits, so a body parks at the top and the rim pulses.
function look(mode, muted, errands) {
  const known = LOOKS[mode] && mode !== "offline" && errands;
  const l = { ...(LOOKS[mode] || LOOKS.offline), // "hidden" is never drawn
    bodies: known ? Math.min(3, Math.max(errands.running || 0, errands.asking ? 1 : 0)) : 0,
    ask: known && errands.asking ? 1 : 0 };
  if (muted && mode === "listening") return { ...l, flow: 0, source: null, muted: 1 }; // must not look like it listens
  return { ...l, muted: muted ? 1 : 0 };
}

module.exports = { level, follow, look, ignite, IGNITE_S, COLLAPSE_S };
