// Renderer: a Pipecat JS client over SmallWebRTC (documented path) driving the orb.
import { LogLevel, PipecatClient, RTVIEvent, logger } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport, WavMediaManager } from "@pipecat-ai/small-webrtc-transport";

const { initial, reduce, statusLine } = require("./state.js");
const { makeReconnector, makeWatchdog } = require("./reconnect.js");
const { createOrb, meter } = require("./orb.js");

const backend = new URLSearchParams(location.search).get("backend") || "http://127.0.0.1:7860";
let state = initial();
let muted = false; // owned by the tray in main.js; re-applied after every connect

const meters = {}; // mic, bot: loudness of the two audio tracks
// The canvas is the drag handle (index.html). Drawn from our state machine and the two meters.
const orb = createOrb(document.getElementById("orb"), (who) => meters[who]?.level() ?? 0);
const render = () => orb.set(state.mode, muted);

let micError = null; // set by a device error, cleared once our mic track really starts
let backendUp = false; // after a failed connect: does the backend itself answer?
let lastLine = null;
function dispatch(type) {
  const before = state;
  state = reduce(state, { type });
  if (state.visible !== before.visible) window.vi.setVisible(state.visible);
  const line = statusLine(state.connected, micError, backendUp);
  if (line !== lastLine) window.vi.setStatus((lastLine = line)); // a rebuilt tray menu closes if open
  render();
}

// Shared by client-js and the transport; its default is DEBUG, which logs the bot's messages, replies included.
logger.setLevel(LogLevel.WARN);
const client = new PipecatClient({ transport: new SmallWebRTCTransport({ mediaManager: new WavMediaManager() }), enableMic: true, enableCam: false });
client.on(RTVIEvent.Connected, () => { backendUp = false; dispatch("connected"); if (client.isMicEnabled !== !muted) client.enableMic(!muted); });
client.on(RTVIEvent.BotReady, () => { if (state.visible) client.sendClientMessage("wake"); }); // shown before it connected
client.on(RTVIEvent.Disconnected, () => { dispatch("disconnected"); reconnector.trigger(); });
client.on(RTVIEvent.ServerMessage, (msg) => { if (msg && (msg.type === "wake" || msg.type === "sleep")) dispatch(msg.type); });
client.on(RTVIEvent.DeviceError, (err) => { micError = err?.type || "unknown"; dispatch("device-error"); });
// Our code plays the bot's audio (nothing else does: the transport only plays the WebRTC remote track
// if an element is attached, and WavMediaManager's player is fed by bufferBotAudio, which SmallWebRTC
// never calls). The same two tracks are metered, so the orb moves with the real voices.
const botAudio = new Audio(); // one element: a repeated trackStarted replaces the source, never adds a player
botAudio.autoplay = true;
const audioCtx = new AudioContext();
client.on(RTVIEvent.TrackStarted, (track, participant) => {
  if (track.kind !== "audio") return;
  const who = participant?.local ? "mic" : "bot"; // SmallWebRTC names only the local participant
  if (who === "bot") {
    botAudio.srcObject = new MediaStream([track]);
    botAudio.play().catch(() => {});
  } else if (micError) { micError = null; dispatch("mic-ok"); }
  meters[who]?.stop();
  meters[who] = meter(audioCtx, track);
  audioCtx.resume().catch(() => {});
});
// A stalled session (media stopped without a Disconnected) is torn down so the reconnector takes over.
// disconnect() emits no Disconnected when the peer is already gone, so trigger the reconnector here.
const watchdog = makeWatchdog(() => {
  console.warn("session stalled; reconnecting");
  client.disconnect().catch(() => {}).then(() => reconnector.trigger());
});
client.on(RTVIEvent.TransportStateChanged, (s) => watchdog.state(s));
client.on(RTVIEvent.UserStartedSpeaking, () => dispatch("user-start"));
client.on(RTVIEvent.UserStoppedSpeaking, () => dispatch("user-stop"));
client.on(RTVIEvent.BotStartedSpeaking, () => dispatch("bot-start"));
client.on(RTVIEvent.BotStoppedSpeaking, () => dispatch("bot-stop"));

window.vi.onToggle(() => {
  dispatch("toggle");
  if (state.mode === "offline") setTimeout(() => { if (state.mode === "offline") dispatch("sleep"); }, 4000); // tray says why
  if (state.connected) client.sendClientMessage(state.visible ? "wake" : "sleep"); // by hand: listen, or stop
});
window.vi.onMute((m) => {
  muted = m;
  render(); // a muted orb must not look like it listens
  if (client.isMicEnabled !== !muted) client.enableMic(!muted);
});
window.vi.setVisible(false); // a reloaded page starts hidden, so the window must too

// Our code owns connect/retry: the reconnector is the single caller of connect.
const connect = async () => {
  client.enableMic(!muted); // mic follows mute before any audio can flow
  await client.connect({ webrtcRequestParams: { endpoint: `${backend}/api/offer` } });
};
const LIVE = ["authenticating", "connecting", "connected", "ready"]; // client-js refuses connect() in these
const reconnector = makeReconnector(async () => {
  try {
    await connect();
  } catch (e) {
    backendUp = await fetch(`${backend}/gui-config`).then((r) => r.ok, () => false);
    dispatch("connect-failed");
    throw e;
  }
  reconnector.succeeded();
}, setTimeout, { connecting: () => LIVE.includes(client.state) });
reconnector.trigger();
