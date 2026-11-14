// Nova's orb: a small night-sky disc with a star at its core and thin shells around it, drawn by one
// WebGL2 fragment shader. The shells travel in toward the star while Nova hears you, stand still with
// gold arcs orbiting while it thinks, and travel out in gold (the outline moving with the voice) while
// it speaks. Dashed and hollow means no connection; grey with a slash means the microphone is muted.
const { level, follow, look } = require("./orb-params.js");

const VERT = `#version 300 es
in vec2 a; void main(){ gl_Position = vec4(a, 0.0, 1.0); }`;
const FRAG = `#version 300 es
precision highp float;
uniform vec2 u_res;
uniform float u_time, u_phase, u_orbT, u_level, u_shape, u_hear, u_warm, u_orbit, u_broken, u_mute;
out vec4 o;
const float R = 0.72, RINGS = 6.0, TAU = 6.2831853;
const vec3 INK = vec3(0.027, 0.035, 0.078), STAR = vec3(0.86, 0.90, 1.0), GOLD = vec3(1.0, 0.72, 0.22);
float hash(float n){ return fract(sin(n * 127.1) * 43758.5453); }
float cover(float d){ return clamp(d + 0.5, 0.0, 1.0); } // signed distance in pixels -> antialiased coverage
void main(){
  float m = min(u_res.x, u_res.y);
  vec2 p = (gl_FragCoord.xy * 2.0 - u_res) / m;
  float px = 2.0 / m;          // one device pixel
  float lw = m / 128.0;        // line weight: 1 px on a 128 px orb, scaled with it
  float r = length(p), a = atan(p.y, p.x);

  // Speaking: the outline and the shells take the voice's shape (three slow lobes, never a perfect repeat).
  float lobe = 0.5 * sin(a * 3.0 + u_time * 0.9) + 0.3 * sin(a * 5.0 - u_time * 1.4) + 0.2 * sin(a * 2.0 + u_time * 2.3);
  float rr = r / (1.0 + u_shape * u_level * 0.2 * lobe * smoothstep(0.08, 0.6, r));
  // Listening: your voice ripples the shells on their way in, finer and only inside; the outline holds still.
  float ripple = 0.6 * sin(a * 7.0 - u_time * 2.1) + 0.4 * sin(a * 4.0 + u_time * 1.3);
  rr /= 1.0 + u_hear * 0.11 * ripple * smoothstep(0.1, 0.36, r) * smoothstep(R, R * 0.62, r);
  float w = max(fwidth(rr), 1e-5);
  float body = cover((R - rr) / w);

  // Shells: crisp iso-lines of the radius. j names a shell for as long as it lives.
  float f = rr * RINGS / R - u_phase;
  float j = floor(f + 0.5);
  float dist = abs(f - j) * R / RINGS / w;                       // pixels to the nearest shell
  float line = cover((0.62 + 0.45 * u_level) * lw - dist);
  line *= smoothstep(0.15, 0.30, rr) * smoothstep(R - 0.02, R - 0.11, rr); // born at the rim, gone before the star
  float dash = fract(a / TAU * 6.0 + hash(j));
  line *= mix(1.0, smoothstep(0.0, 0.04, dash) * smoothstep(0.62, 0.58, dash), u_broken);

  // Thinking: one comet per shell, each on its own period.
  float x = fract((a - u_orbT * (1.2 + 1.6 * hash(j))) / TAU + hash(j + 7.0));
  float comet = pow(x, 3.0) * smoothstep(1.0, 0.985, x) * u_orbit;

  // Light falls off the way the sound goes: toward the star when hearing, away from it when speaking.
  float t = clamp((rr - 0.15) / (R - 0.15), 0.0, 1.0);
  float glow = (0.5 + 0.5 * u_level) * mix(0.4, 1.0, mix(t, 1.0 - t, u_shape));
  glow = mix(glow, 0.2, u_orbit);                                // quiet shells while the comets run
  glow = mix(glow, 0.32, u_broken);
  vec3 col = mix(INK, mix(STAR, GOLD, max(u_warm, comet)), line * max(glow, comet));

  // The star: a point that swells with the voice; a hollow ring when there is no connection.
  float rc = 0.085 + 0.055 * u_level;
  float star = mix(cover((rc - r) / px), cover(0.6 * lw - abs(r - rc) / px), u_broken);
  col = mix(col, mix(STAR, GOLD, u_warm), star * mix(1.0, 0.5, u_broken));

  // Hairline rim, so the disc (and its moving outline) holds on dark wallpapers.
  col = mix(col, mix(STAR, GOLD, u_warm), cover(0.5 * lw - abs(R - 0.75 * w * lw - rr) / w) * mix(0.3, 0.6, u_warm));

  // Muted: no colour, and a slash through it.
  col = mix(col, vec3(dot(col, vec3(0.3, 0.6, 0.1))) * 0.75, u_mute);
  float across = abs(p.x + p.y) * 0.7071 / px, along = (R * 0.66 - abs(p.x - p.y) * 0.7071) / px;
  col = mix(col, INK, cover(4.2 * lw - across) * cover(along + 3.0 * lw) * u_mute);
  col = mix(col, vec3(0.74, 0.76, 0.82), cover(1.3 * lw - across) * cover(along) * u_mute);

  o = vec4(col * body, body); // premultiplied: the window behind is transparent
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

const EASED = ["flow", "warm", "shape", "orbit", "broken", "muted"];

// levelOf("mic" | "bot") -> 0..1 right now. Returns set(mode, muted); nothing is drawn while hidden.
function createOrb(canvas, levelOf) {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const s = { flow: 0, warm: 0, shape: 0, orbit: 0, broken: 0, muted: 0, level: 0, time: 0, phase: 0, orbT: 0 };
  let gl = null, U = null, raf = 0, last = 0, mode = "hidden", muted = false;

  function init() {
    gl = canvas.getContext("webgl2", { antialias: false, powerPreference: "low-power" });
    canvas.classList.toggle("flat", !gl); // no WebGL2: index.html draws a plain disc, so Nova is never invisible
    if (!gl) return;
    const prog = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, VERT], [gl.FRAGMENT_SHADER, FRAG]]) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(sh));
      gl.attachShader(prog, sh);
    }
    gl.linkProgram(prog);
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW); // one full-screen triangle
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    U = {};
    for (const n of ["res", "time", "phase", "orbT", "level", "shape", "hear", "warm", "orbit", "broken", "mute"]) U[n] = gl.getUniformLocation(prog, `u_${n}`);
  }
  init();
  canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); gl = null; });
  canvas.addEventListener("webglcontextrestored", () => { init(); set(mode, muted); });

  function frame(now) {
    raf = 0;
    if (mode === "hidden" || !gl) return;
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    const t = look(mode, muted);
    for (const k of EASED) s[k] = follow(s[k], t[k], dt, 8, 8);
    const still = reduced.matches; // reduced motion: states and loudness still show, nothing travels or spins
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
    gl.uniform1f(U.time, s.time);
    gl.uniform1f(U.phase, s.phase);
    gl.uniform1f(U.orbT, s.orbT);
    gl.uniform1f(U.level, s.level);
    gl.uniform1f(U.shape, s.shape);
    gl.uniform1f(U.hear, s.level * (1 - s.shape));
    gl.uniform1f(U.warm, s.warm);
    gl.uniform1f(U.orbit, s.orbit);
    gl.uniform1f(U.broken, s.broken);
    gl.uniform1f(U.mute, s.muted);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    raf = requestAnimationFrame(frame);
  }

  function set(nextMode, nextMuted) {
    if (mode === "hidden" && nextMode !== "hidden") { // it appears already in its state, not fading in from the last one
      Object.assign(s, look(nextMode, nextMuted), { level: 0 });
      last = performance.now();
    }
    mode = nextMode;
    muted = nextMuted;
    canvas.dataset.mode = muted ? "muted" : mode; // for the no-WebGL fallback
    if (!raf && mode !== "hidden") raf = requestAnimationFrame(frame);
  }
  return { set };
}

module.exports = { createOrb, meter };
