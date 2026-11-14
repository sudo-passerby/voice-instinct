const test = require("node:test");
const assert = require("node:assert/strict");
const { initial, reduce } = require("../renderer/state.js");

const run = (...events) => events.reduce((s, e) => reduce(s, typeof e === "string" ? { type: e } : e), initial());
const QUIET = { running: 0, asking: false, tucked: false }; // no errand, no question, not put away by hand
const errands = (running, asking = false) => ({ type: "errands", running, asking });

test("starts hidden and wakes on the server's wake message", () => {
  assert.deepEqual(initial(), { visible: false, mode: "hidden", connected: false, ...QUIET });
  assert.deepEqual(run("connected", "wake"), { visible: true, mode: "listening", connected: true, ...QUIET });
});

test("follows the conversation: user speaks, bot thinks, bot speaks, back to listening", () => {
  assert.equal(run("wake", "user-start").mode, "listening");
  assert.equal(run("wake", "user-start", "user-stop").mode, "thinking");
  assert.equal(run("wake", "user-stop", "bot-start").mode, "speaking");
  assert.equal(run("wake", "bot-start", "bot-stop").mode, "listening");
});

test("pre-wake speech does not show the orb", () => {
  assert.equal(run("connected", "user-start", "user-stop").visible, false);
});

test("sleep hides it; toggle shows and hides it", () => {
  assert.equal(run("wake", "sleep").visible, false);
  assert.equal(run("toggle").visible, true);
  assert.equal(run("toggle", "toggle").visible, false);
});

test("losing the backend hides the orb instead of freezing it", () => {
  assert.deepEqual(run("connected", "wake", "disconnected"), { visible: false, mode: "hidden", connected: false, ...QUIET });
});

test("unknown events change nothing", () => {
  assert.deepEqual(run("wake", "nonsense"), run("wake"));
});

const { statusLine } = require("../renderer/state.js");

test("the tray says whether it is connected and keeps saying why the microphone failed", () => {
  assert.equal(statusLine(true, null), "Connected");
  assert.equal(statusLine(false, null), "Not connected (is the backend running?)");
  // The mic error arrives before Connected (the transport connects anyway): it must not be overwritten.
  assert.equal(statusLine(true, "permissions"), "Connected, but the microphone is unavailable (permissions)");
  assert.equal(statusLine(false, "not-found"), "Not connected: the microphone is unavailable (not-found)");
});

test("showing the orb while disconnected marks it offline (the app hides it again shortly)", () => {
  const s = reduce(initial(), { type: "toggle" });
  assert.equal(s.visible, true);
  assert.equal(s.mode, "offline");
  const c = reduce(reduce(initial(), { type: "connected" }), { type: "toggle" });
  assert.equal(c.mode, "listening");
});

test("when the backend is up but the session will not connect, the tray points at the microphone", () => {
  // Without a usable mic the client fails before it reaches the backend, and no device error arrives.
  assert.equal(statusLine(false, null, true), "Not connected: the backend is running, so check the microphone");
  assert.equal(statusLine(false, "permissions", true), "Not connected: the microphone is unavailable (permissions)");
  assert.equal(statusLine(false, null, false), "Not connected (is the backend running?)");
});

// While an errand runs or a question waits, the orb stays up, small and dim ("resting"), instead of hiding.
test("falling asleep with an errand running leaves the orb resting; with none it hides as before", () => {
  const s = run("connected", "wake", errands(1), "sleep");
  assert.deepEqual([s.visible, s.mode, s.running], [true, "resting", 1]);
  assert.equal(run("connected", "wake", errands(0), "sleep").visible, false);
  assert.equal(run("connected", "wake", errands(0, true), "sleep").mode, "resting"); // a question waits
  assert.equal(run("connected", errands(2)).mode, "resting"); // found running when the orb reconnects
});

test("an errand's news while Nova is awake changes only the bodies, never the state", () => {
  const s = run("connected", "wake", "user-start", "user-stop", errands(1, true));
  assert.deepEqual([s.mode, s.running, s.asking], ["thinking", 1, true]);
});

test("a resting orb is not awake: the room's talk does not move it, Nova's own voice does", () => {
  const resting = ["connected", "wake", errands(1), "sleep"];
  assert.equal(run(...resting, "user-start").mode, "resting");
  assert.equal(run(...resting, "user-start", "user-stop").mode, "resting");
  assert.equal(run(...resting, "bot-stop").mode, "resting");
  assert.equal(run(...resting, "bot-start").mode, "speaking");
  assert.equal(run(...resting, "wake").mode, "listening");
});

test("when the last errand ends the resting orb lingers until it settles, then hides", () => {
  const done = ["connected", "wake", errands(1), "sleep", errands(0)];
  assert.equal(run(...done).mode, "resting"); // time for the body to fall into the star
  assert.equal(run(...done, "settle").visible, false);
  assert.equal(run(...done, errands(1), "settle").mode, "resting"); // another started meanwhile
  assert.equal(run("connected", "wake", "settle").mode, "listening"); // awake: nothing to settle
});

test("hiding the orb by hand keeps it hidden through errand news, until Nova is woken again", () => {
  const tucked = run("connected", "wake", errands(1), "sleep", "toggle");
  assert.deepEqual([tucked.visible, tucked.mode], [false, "hidden"]);
  assert.equal(reduce(tucked, errands(2, true)).visible, false);
  assert.equal(reduce(reduce(tucked, { type: "toggle" }), { type: "sleep" }).mode, "resting"); // shown by hand again
  assert.equal(reduce(reduce(tucked, { type: "wake" }), { type: "sleep" }).mode, "resting");
});

test("losing the backend forgets the errands: nothing rests on a dead connection", () => {
  const s = run("connected", "wake", errands(2, true), "sleep", "disconnected");
  assert.deepEqual(s, { visible: false, mode: "hidden", connected: false, ...QUIET });
  assert.equal(run("toggle", "sleep").visible, false); // the offline orb still hides after its moment
});

test("errand news with bad numbers is read as none", () => {
  const s = run("connected", { type: "errands", running: "lots", asking: 1 });
  assert.deepEqual([s.running, s.asking, s.mode], [0, true, "resting"]);
  assert.equal(run("connected", { type: "errands", running: -2 }).visible, false);
});
