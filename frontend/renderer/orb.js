// Nova's orb: a small night-sky disc with a star at its core and shells around it, drawn by one WebGL2
// fragment shader. The shells travel in toward the star while Nova hears you, stand dim while gold comets
// run round them as it thinks, and travel out in gold (the outline moving with the voice) while it speaks.
// A gold body circles the star for each errand that is running; when one has a question for you its body
// parks at the top and the rim pulses gold. Asleep with an errand still running, the orb rests: small, the
// star and its bodies only. Dashed and hollow means no connection; grey with a slash, the microphone is muted.
// Waking, the star flares and the disc opens around it; going to sleep, it closes.
const { level, follow, look, ignite } = require("./orb-params.js");

const VERT = `#version 300 es
in vec2 a; void main(){ gl_Position = vec4(a, 0.0, 1.0); }`;
const FRAG = `#version 300 es
precision highp float;
uniform vec2 u_res;
uniform float u_time, u_phase, u_orbT, u_level, u_shape, u_hear, u_warm, u_orbit, u_broken, u_mute;
uniform float u_rest, u_bodies, u_ask, u_ignite, u_flash;
out vec4 o;
const float R0 = 0.72, RINGS = 4.0, TAU = 6.2831853;
const vec3 INK = vec3(0.027, 0.035, 0.078), STAR = vec3(0.86, 0.90, 1.0), GOLD = vec3(1.0, 0.72, 0.22);
float hash(float n){ return fract(sin(n * 127.1) * 43758.5453); }
float cover(float d){ return clamp(d + 0.5, 0.0, 1.0); } // signed distance in pixels -> antialiased coverage
// Speaking: the voice's shape at one angle (three slow lobes, never a perfect repeat).
float lobes(float a){ return 0.5 * sin(a * 3.0 + u_time * 0.9) + 0.3 * sin(a * 5.0 - u_time * 1.4) + 0.2 * sin(a * 2.0 + u_time * 2.3); }
void main(){
  float m = min(u_res.x, u_res.y);
  float sc = mix(1.0, 0.55, u_rest);   // resting: the whole orb is small
  vec2 p = (gl_FragCoord.xy * 2.0 - u_res) / (m * sc);
  float px = 2.0 / (m * sc);   // one device pixel
  float lw = m / 128.0;        // line weights are for a 128 px orb and scale with it
  float r = length(p), a = atan(p.y, p.x);
  float open = 1.0 - pow(1.0 - u_ignite, 3.0);   // waking: the disc opens from the star outward
  float flare = max(u_flash, 1.0 - u_ignite);    // and the star flares; also when an errand's body falls in
  float R = max(R0 * open, 1e-3), k = R / R0;

  // Speaking: the outline and the shells take the voice's shape.
  float swell = u_shape * u_level * 0.2;
  float rr = r / (1.0 + swell * lobes(a) * smoothstep(0.08, 0.6, r));
  // Listening: your voice ripples the shells on their way in, finer and only inside; the outline holds still.
  float ripple = 0.6 * sin(a * 7.0 - u_time * 2.1) + 0.4 * sin(a * 4.0 + u_time * 1.3);
  rr /= 1.0 + u_hear * 0.11 * ripple * smoothstep(0.1, 0.36, r) * smoothstep(R, R * 0.62, r);
  float w = max(fwidth(rr), 1e-5);
  float body = cover((R - rr) / w);

  // Shells: iso-lines of the radius, 2 px on a 128 px orb so they survive a downscaled screen recording.
  // j names a shell for as long as it lives.
  float f = rr * RINGS / R - u_phase;
  float j = floor(f + 0.5);
  float dist = abs(f - j) * R / RINGS / w;                       // pixels to the nearest shell
  // Thinking: one comet per shell, each on its own period, thick at the head and thinning down its tail.
  float x = fract((a - u_orbT * (1.6 + 1.8 * hash(j))) / TAU + hash(j + 7.0));
  float comet = pow(x, 1.5) * smoothstep(1.0, 0.985, x) * u_orbit;
  float line = cover((1.0 + 0.5 * u_level + 0.9 * comet) * lw - dist);
  line *= smoothstep(0.15 * k, 0.30 * k, rr) * smoothstep(R - 0.02 * k, R - 0.08 * k, rr); // born at the rim, gone before the star
  float dash = fract(a / TAU * 6.0 + hash(j));
  line *= mix(1.0, smoothstep(0.0, 0.04, dash) * smoothstep(0.62, 0.58, dash), u_broken);
  line *= 1.0 - u_rest;

  // Light falls off the way the sound goes: toward the star when hearing, away from it when speaking.
  float t = clamp((rr - 0.15) / (R - 0.15), 0.0, 1.0);
  float glow = (0.5 + 0.5 * u_level) * mix(0.65, 1.0, mix(t, 1.0 - t, u_shape));
  glow = mix(glow, 0.16, u_orbit);                               // quiet shells while the comets run
  glow = mix(glow, 0.32, u_broken);
  vec3 voice = mix(STAR, GOLD, u_warm);
  vec3 col = mix(INK, mix(STAR, GOLD, max(u_warm, comet)), line * max(glow, comet));

  // Resting: the one orbit the errands' bodies ride.
  float orbitR = R * 0.84;
  col = mix(col, STAR, cover(0.5 * lw - abs(rr - orbitR) / w) * 0.3 * u_rest);

  // The star: a point with four thin spikes that lengthen with the voice; a hollow ring when there is no connection.
  float rc = (0.085 + 0.055 * u_level) * (1.0 + 0.7 * flare);
  float core = mix(cover((rc - r) / px), cover(0.8 * lw - abs(r - rc) / px), u_broken);
  vec2 q = abs(p);
  float len = 0.2 + 0.3 * u_level + 0.32 * flare, half_w = 1.1 * lw * px;
  float spike = cover(-min(q.y - half_w * (1.0 - q.x / len), q.x - half_w * (1.0 - q.y / len)) / px) * (1.0 - u_broken);
  float star = max(core * mix(1.0, 0.5, u_broken), spike * 0.85) * mix(1.0, 0.8, u_rest);
  col = mix(col, voice, star);

  // Rim, so the disc (and its moving outline) holds on dark wallpapers. A waiting question pulses it gold.
  float pulse = u_ask * (0.55 + 0.45 * sin(u_time * 5.0));
  float rim = cover((0.8 + 0.7 * pulse) * lw - abs(R - (0.9 + 0.7 * pulse) * w * lw - rr) / w);
  col = mix(col, mix(voice, GOLD, u_ask), rim * max(mix(0.6, 0.85, u_warm), pulse));
  col = mix(col, STAR, rim * (1.0 - u_ignite));                  // the opening edge is bright

  // Muted: no colour (the errands' bodies below keep theirs).
  col = mix(col, vec3(dot(col, vec3(0.3, 0.6, 0.1))) * 0.75, u_mute);

  // Errands: a gold body circles the star for each one running. It spirals in from the star as it starts and
  // falls back into it when it ends. With a question waiting, the first parks at the top and beats.
  for (int i = 0; i < 3; i++) {
    float fi = float(i), vis = clamp(u_bodies - fi, 0.0, 1.0);
    float ang = u_orbT * (0.8 + 0.27 * fi) + fi * 2.1;
    if (i == 0) ang = mix(mod(ang, TAU), TAU * 0.25, u_ask);
    float size = (0.062 + 0.03 * u_rest + (i == 0 ? 0.03 * pulse : 0.0)) * vis;
    float at = orbitR * mix(0.2, 1.0, vis) * (1.0 + swell * lobes(ang));   // it rides the outline when that moves
    float d = (length(p - at * vec2(cos(ang), sin(ang))) - size) / px;
    col = mix(col, INK, cover(2.0 * lw - d) * step(0.01, vis));  // a gap, so it reads over the shells
    col = mix(col, GOLD, cover(-d) * step(0.01, vis));
  }

  // Muted: a slash through it.
  float across = abs(p.x + p.y) * 0.7071 / px, along = (R0 * 0.66 - abs(p.x - p.y) * 0.7071) / px;
  col = mix(col, INK, cover(4.2 * lw - across) * cover(along + 3.0 * lw) * u_mute);
  col = mix(col, vec3(0.74, 0.76, 0.82), cover(1.3 * lw - across) * cover(along) * u_mute);

  float alpha = max(body, star * step(0.001, u_ignite));         // the flaring star shows before the disc has opened
  o = vec4(col * alpha, alpha); // premultiplied: the window behind is transparent
}`;

// A loudness meter on one audio track. Nothing is connected to the speakers here.
function meter(ctx, track) {
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  const source = ctx.createMediaStreamSource(new MediaStream([track]));
  source.connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  return { level: () => (analyser.getByteTimeDomainData(buf), level(buf)), stop: () => source.disconnect() };
}

const EASED = ["flow", "warm", "shape", "orbit", "broken", "rest", "ask", "muted"];
const UNIFORMS = ["res", "time", "phase", "orbT", "level", "shape", "hear", "warm", "orbit", "broken", "mute", "rest", "bodies", "ask", "ignite", "flash"];

// levelOf("mic" | "bot") -> 0..1 right now. Returns set(mode, muted, errands); nothing is drawn while hidden.
function createOrb(canvas, levelOf) {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const s = { flow: 0, warm: 0, shape: 0, orbit: 0, broken: 0, rest: 0, ask: 0, muted: 0, bodies: 0, flash: 0, ignite: 0, level: 0, time: 0, phase: 0, orbT: 0 };
  let gl = null, U = null, raf = 0, last = 0, mode = "hidden", muted = false, errands = null;
  let drawn = "offline"; // the last state shown: what the sleep collapse closes
  let lost = 0;

  // No usable WebGL2 (none, or a shader that will not build): index.html draws a plain disc whose ring says
  // the state (.flat), so Nova is never invisible.
  function init() {
    gl = lost > 3 ? null : canvas.getContext("webgl2", { antialias: false, powerPreference: "low-power" });
    // A lost context leaves Chromium's placeholder on the canvas, which cannot be drawn over: start again on a
    // fresh canvas, at once. After a few losses, stay with the plain disc until the page reloads.
    canvas.addEventListener("webglcontextlost", () => {
      lost++;
      const fresh = canvas.cloneNode();
      canvas.replaceWith(fresh);
      canvas = fresh;
      init();
      set(mode, muted, errands);
    }, { once: true });
    if (gl) {
      const prog = gl.createProgram();
      for (const [type, src] of [[gl.VERTEX_SHADER, VERT], [gl.FRAGMENT_SHADER, FRAG]]) {
        const sh = gl.createShader(type);
        gl.shaderSource(sh, src);
        gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(sh));
        gl.attachShader(prog, sh);
      }
      gl.linkProgram(prog);
      if (gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        gl.useProgram(prog);
        gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW); // one full-screen triangle
        gl.enableVertexAttribArray(0);
        gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
        U = {};
        for (const n of UNIFORMS) U[n] = gl.getUniformLocation(prog, `u_${n}`);
      } else {
        console.error(`orb shader did not link: ${gl.getProgramInfoLog(prog)}`);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl = null;
      }
    }
    canvas.classList.toggle("flat", !gl);
  }
  init();

  function frame(now) {
    raf = 0;
    if (!gl) return;
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    const still = reduced.matches; // reduced motion: states and loudness still show, nothing travels, spins, opens or closes
    const shown = mode !== "hidden";
    s.ignite = still ? Number(shown) : ignite(s.ignite, shown, dt);
    if (!shown && s.ignite <= 0) { gl.clear(gl.COLOR_BUFFER_BIT); return; } // closed: the next wake starts from nothing
    const t = look(drawn, muted, errands);
    for (const k of EASED) s[k] = follow(s[k], t[k], dt, 8, 8);
    s.bodies = still ? t.bodies : follow(s.bodies, t.bodies, dt, 5, 2.5);
    s.flash = follow(s.flash, 0, dt, 18, 4);
    s.level = follow(s.level, t.source ? levelOf(t.source) * (still ? 0.4 : 1) : 0, dt);
    if (!still) {
      s.time += dt;
      s.orbT += dt;
      s.phase += dt * s.flow * (0.4 + 2.2 * s.level); // louder travels faster
    }
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const size = Math.round(Math.min(canvas.clientWidth, canvas.clientHeight) * dpr);
    if (size && canvas.width !== size) { canvas.width = canvas.height = size; gl.viewport(0, 0, size, size); }
    gl.uniform2f(U.res, canvas.width, canvas.height);
    for (const n of ["time", "phase", "orbT", "level", "shape", "warm", "orbit", "broken", "rest", "bodies", "ask", "ignite", "flash"]) gl.uniform1f(U[n], s[n]);
    gl.uniform1f(U.hear, s.level * (1 - s.shape));
    gl.uniform1f(U.mute, s.muted);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    raf = requestAnimationFrame(frame);
  }

  function set(nextMode, nextMuted, nextErrands = null) {
    if (nextMode !== "hidden") {
      const before = look(drawn, muted, errands), t = look(nextMode, nextMuted, nextErrands);
      if (mode === "hidden") { // it appears already in its state, not easing in from the last one
        for (const k of EASED) s[k] = t[k];
        Object.assign(s, { bodies: t.bodies, level: 0, flash: 0 });
        last = performance.now();
      } else if (t.bodies < before.bodies || t.rest < before.rest) {
        s.flash = 1; // an errand's body fell into the star, or Nova woke from resting
      }
      drawn = nextMode;
    }
    if (nextMode === "hidden" && gl && reduced.matches) { s.ignite = 0; gl.clear(gl.COLOR_BUFFER_BIT); } // no collapse to wait for
    mode = nextMode;
    muted = nextMuted;
    errands = nextErrands;
    canvas.dataset.mode = muted ? "muted" : mode; // for the no-WebGL fallback
    canvas.dataset.ask = nextErrands?.asking ? "1" : "0";
    if (!raf && (mode !== "hidden" || s.ignite > 0)) raf = requestAnimationFrame(frame);
  }
  return { set };
}

module.exports = { createOrb, meter };
