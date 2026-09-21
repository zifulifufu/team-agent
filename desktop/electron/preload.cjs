// Hand the one-shot token and the backend address from the main process to the renderer.
// They are only used to reach the local Python backend.
const { contextBridge, ipcRenderer } = require("electron");

const argOf = (name) => {
  const a = process.argv.find((x) => x.startsWith(`--${name}=`));
  return a ? a.slice(name.length + 3) : "";
};

// The token is exposed only to the app's own pages (the packaged file:// page, or
// localhost:5173 in development). If the window is ever navigated elsewhere, that page
// cannot read it.
const isAppPage = location.protocol === "file:" || location.origin === "http://localhost:5173";

contextBridge.exposeInMainWorld("teamAgent", {
  token: isAppPage ? argOf("team-agent-token") : "",
  api: isAppPage ? argOf("team-agent-api") : "",
  // Opens the system folder picker and returns the path; null when it is cancelled
  pickFolder: isAppPage ? () => ipcRenderer.invoke("team-agent:pick-folder") : () => Promise.resolve(null),
});
