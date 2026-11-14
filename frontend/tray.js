// Tray menu items, pure so it can be tested. Labels say what a click does, because some tray hosts
// (waybar on Sway) do not draw checkmarks.
// running, asking: the backend's errand news, the same the orb shows as bodies.
function statusLabel({ status, muted, running = 0, asking = false }) {
  return [status, muted && "microphone muted", running > 0 && `${running} errand${running > 1 ? "s" : ""} running`,
    asking && "a question is waiting for you"].filter(Boolean).join(", ");
}

function trayItems(state, { toggle, setMuted, quit }) {
  return [
    { label: statusLabel(state), enabled: false },
    { label: "Show / Hide", click: toggle },
    { label: state.muted ? "Unmute microphone" : "Mute microphone", click: () => setMuted(!state.muted) },
    { type: "separator" },
    { label: "Quit", click: quit },
  ];
}

// The tray icon says the same at a glance. Not connected wins over muted: Nova is not there at all.
// Muted wins over a running errand: that Nova cannot hear you matters more.
function trayIcon({ status, muted, running = 0, asking = false }) {
  if (!status.startsWith("Connected")) return "icon-offline.png";
  if (muted) return "icon-muted.png";
  return running > 0 || asking ? "icon-busy.png" : "icon.png";
}

const trayTooltip = (name, state) => `${name}: ${statusLabel(state)}`;

module.exports = { trayItems, trayIcon, trayTooltip };
