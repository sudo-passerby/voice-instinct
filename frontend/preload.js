// The only bridge between the sandboxed page and the main process.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("vi", {
  setVisible: (visible) => ipcRenderer.send("visible", Boolean(visible)),
  setStatus: (text) => ipcRenderer.send("status", String(text)),
  setErrands: (running, asking) => ipcRenderer.send("errands", Number(running) || 0, Boolean(asking)),
  onToggle: (fn) => ipcRenderer.on("toggle", () => fn()),
  onTalk: (fn) => ipcRenderer.on("talk", () => fn()),
  onMute: (fn) => ipcRenderer.on("mute", (_e, muted) => fn(Boolean(muted))),
});
