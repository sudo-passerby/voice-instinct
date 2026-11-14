const test = require("node:test");
const assert = require("node:assert/strict");
const { level, follow, look, ignite, IGNITE_S, COLLAPSE_S } = require("../renderer/orb-params.js");

// An AnalyserNode's time-domain bytes: 128 is silence, a sine of amplitude `a` (0..1) swings around it.
const wave = (a, n = 512) => Uint8Array.from({ length: n }, (_, i) => 128 + Math.round(127 * a * Math.sin(i / 3)));

test("silence and room noise read as zero; speech rises with loudness and never passes 1", () => {
  assert.equal(level(wave(0)), 0);
  assert.equal(level(wave(0.01)), 0); // below the noise floor: a quiet room must not animate the orb
  const quiet = level(wave(0.05)), normal = level(wave(0.2)), loud = level(wave(1));
  assert.ok(quiet > 0.1 && quiet < normal, `quiet speech is visible: ${quiet}`);
  assert.ok(normal > 0.5 && normal < 1, `normal speech fills most of the range: ${normal}`);
  assert.equal(loud, 1);
  assert.equal(level(new Uint8Array(0)), 0); // no samples yet
});

test("the level is followed fast on the way up and slowly on the way down", () => {
  const up = follow(0, 1, 1 / 60), down = follow(1, 0, 1 / 60);
  assert.ok(up > 0.2, `attack within a few frames: ${up}`);
  assert.ok(1 - down < up / 2, `release is slower than attack: ${down}`);
  assert.equal(follow(0.4, 0.4, 1 / 60), 0.4);
  assert.ok(follow(0, 1, 10) <= 1); // a long frame gap (window was hidden) never overshoots
});

test("each state has its own look: rings flow in when listening, out when speaking, stand still when thinking", () => {
  const l = look("listening", false), t = look("thinking", false), s = look("speaking", false), o = look("offline", false);
  assert.ok(l.flow < 0 && s.flow > 0);
  assert.equal(t.flow, 0);
  assert.deepEqual([l.orbit, t.orbit, s.orbit, o.orbit], [0, 1, 0, 0]); // only thinking has the orbiting arcs
  assert.deepEqual([l.warm, s.warm], [0, 1]); // gold is Nova's own voice, nothing else
  assert.deepEqual([l.shape, s.shape], [0, 1]); // the outline holds still to listen, moves to speak
  assert.deepEqual([l.broken, t.broken, s.broken, o.broken], [0, 0, 0, 1]);
  assert.equal(o.flow, 0);
});

test("the orb follows the mic while listening and the bot while speaking, and nothing otherwise", () => {
  assert.equal(look("listening", false).source, "mic");
  assert.equal(look("speaking", false).source, "bot");
  assert.equal(look("thinking", false).source, null);
  assert.equal(look("offline", false).source, null);
  assert.equal(look("hidden", false).source, null);
});

test("a muted orb must not look like it listens, but still shows Nova speaking", () => {
  const m = look("listening", true);
  assert.equal(m.muted, 1);
  assert.equal(m.source, null);
  assert.equal(m.flow, 0);
  const s = look("speaking", true);
  assert.equal(s.muted, 1);
  assert.equal(s.source, "bot");
  assert.ok(s.flow > 0);
  assert.equal(look("listening", false).muted, 0);
});

test("resting is small and still: no shells travel, no voice drives it", () => {
  const r = look("resting", false, { running: 1, asking: false });
  assert.deepEqual([r.rest, r.flow, r.orbit, r.source], [1, 0, 0, null]);
  for (const m of ["listening", "thinking", "speaking", "offline"]) assert.equal(look(m, false).rest, 0, m);
});

test("one body per running errand, three at most; a waiting question always has a body to park", () => {
  const bodies = (running, asking) => look("listening", false, { running, asking }).bodies;
  assert.deepEqual([bodies(0, false), bodies(1, false), bodies(2, false), bodies(7, false)], [0, 1, 2, 3]);
  assert.equal(bodies(0, true), 1);
  assert.equal(look("listening", false).bodies, 0); // no errand news yet
  assert.deepEqual([look("resting", false, { running: 1, asking: true }).ask, look("speaking", false, { running: 1, asking: false }).ask], [1, 0]);
  assert.equal(look("offline", false, { running: 2, asking: true }).bodies, 0); // not connected: nothing is known
});

test("the wake ignition opens in about a quarter second and the sleep collapse is quicker", () => {
  let x = 0, t = 0;
  while (x < 1) { x = ignite(x, true, 1 / 60); t += 1 / 60; }
  assert.equal(x, 1);
  assert.ok(Math.abs(t - IGNITE_S) < 0.03, `opens in ${t}`);
  for (t = 0; x > 0; t += 1 / 60) x = ignite(x, false, 1 / 60);
  assert.equal(x, 0);
  assert.ok(Math.abs(t - COLLAPSE_S) < 0.03 && COLLAPSE_S < IGNITE_S, `closes in ${t}`);
  assert.equal(ignite(0.5, true, 10), 1); // a long frame gap never overshoots
});
