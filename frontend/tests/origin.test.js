const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// backend/guard.py lets a request without an Origin through only on /hook/. The page's requests get it from
// the session's onBeforeSendHeaders, but main.js's own fetch (Node's) bypasses the session: it must set it itself.
test("every fetch the main process makes stamps the orb's origin", () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const calls = src.match(/\bfetch\([^;]*/g) || [];
  assert.ok(calls.length >= 1);
  for (const c of calls) assert.match(c, /headers:\s*\{\s*Origin:\s*ORB_ORIGIN\s*\}/, c);
});
