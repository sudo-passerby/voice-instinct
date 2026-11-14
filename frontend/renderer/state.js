// Pure orb state machine: RTVI events and server messages in, what to show out. Unit-tested.
// running, asking: the backend's errand news (gui.py). tucked: the user hid the orb by hand, so it stays
// away until Nova is woken again.
function initial() {
  return { visible: false, mode: "hidden", connected: false, running: 0, asking: false, tucked: false };
}

// Where the orb goes when Nova is not awake: resting (small and dim) while an errand runs or a question
// waits, hidden otherwise.
function asleep(state) {
  const rests = state.connected && !state.tucked && (state.running > 0 || state.asking);
  return { ...state, visible: rests, mode: rests ? "resting" : "hidden" };
}

function reduce(state, event) {
  const shown = (mode) => ({ ...state, visible: true, mode, tucked: false });
  const awake = state.visible && state.mode !== "resting"; // a resting orb does not follow the room's talk
  switch (event.type) {
    case "connected":
      return { ...state, connected: true };
    case "disconnected":
      return initial();
    case "wake":
      return shown("listening");
    case "sleep":
      return asleep(state);
    case "toggle":
      if (state.visible) return { ...state, visible: false, mode: "hidden", tucked: true };
      return shown(state.connected ? "listening" : "offline");
    case "errands": {
      const next = { ...state, running: Number.isInteger(event.running) && event.running > 0 ? event.running : 0, asking: Boolean(event.asking) };
      if (awake) return next;
      const rest = asleep(next);
      return state.mode === "resting" && !rest.visible ? { ...next } : rest; // lingers until "settle"
    }
    case "settle": // a moment after the last errand ended: its body has fallen into the star
      return state.mode === "resting" ? asleep(state) : state;
    case "user-start":
      return awake ? shown("listening") : state;
    case "user-stop":
      return awake ? shown("thinking") : state;
    case "bot-start":
      return shown("speaking");
    case "bot-stop":
      return awake ? shown("listening") : state;
    default:
      return state;
  }
}

// What the tray says: the connection, and a microphone error that a later "connected" must not hide.
// backendUp: the backend answered after a failed connect, so the problem is on this side.
function statusLine(connected, micError, backendUp = false) {
  if (!connected && micError) return `Not connected: the microphone is unavailable (${micError})`;
  if (!connected && backendUp) return "Not connected: the backend is running, so check the microphone";
  if (!connected) return "Not connected (is the backend running?)";
  return micError ? `Connected, but the microphone is unavailable (${micError})` : "Connected";
}

module.exports = { initial, reduce, statusLine };
