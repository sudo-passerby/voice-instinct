// Tray menu items, pure so it can be tested. Labels say what a click does, because some tray hosts
// (waybar on Sway) do not draw checkmarks.
function trayItems({ status, muted }, { toggle, setMuted, quit }) {
  return [
    { label: muted ? `${status}, microphone muted` : status, enabled: false },
    { label: "Show / Hide", click: toggle },
    { label: muted ? "Unmute microphone" : "Mute microphone", click: () => setMuted(!muted) },
    { type: "separator" },
    { label: "Quit", click: quit },
  ];
}

// The tray icon says the same at a glance. Not connected wins over muted: Nova is not there at all.
function trayIcon({ status, muted }) {
  if (!status.startsWith("Connected")) return "icon-offline.png";
  return muted ? "icon-muted.png" : "icon.png";
}

module.exports = { trayItems, trayIcon };
