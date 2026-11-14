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

// What each state looks like. flow: ring travel (-1 in toward the core = hearing you, +1 out = Nova
// speaking). warm: gold, Nova's own voice. shape: the outline moves with the voice. orbit: the
// thinking arcs. broken: dashed rings, no connection. source: whose audio level drives it.
const LOOKS = {
  listening: { flow: -1, warm: 0, shape: 0, orbit: 0, broken: 0, source: "mic" },
  thinking: { flow: 0, warm: 0, shape: 0, orbit: 1, broken: 0, source: null },
  speaking: { flow: 1, warm: 1, shape: 1, orbit: 0, broken: 0, source: "bot" },
  offline: { flow: 0, warm: 0, shape: 0, orbit: 0, broken: 1, source: null },
};
function look(mode, muted) {
  const l = LOOKS[mode] || LOOKS.offline; // "hidden" is never drawn
  if (muted && mode === "listening") return { ...l, flow: 0, source: null, muted: 1 }; // must not look like it listens
  return { ...l, muted: muted ? 1 : 0 };
}

module.exports = { level, follow, look };
