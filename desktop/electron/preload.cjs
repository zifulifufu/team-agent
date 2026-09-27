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
  // Show a folder in the Finder. Present only in the desktop app: a browser tab has no way to, and
  // the sidebar hides the button when this is missing rather than offering one that cannot work.
  openPath: isAppPage ? (p) => ipcRenderer.invoke("team-agent:open-path", p) : null,
  // One **file** from the 成果 column: right-clicking a row offers these. Same rule as `openPath` —
  // absent in a browser tab, and the menu simply does not show the actions it cannot perform.
  openFile: isAppPage ? (p) => ipcRenderer.invoke("team-agent:open-file", p) : null,
  reveal: isAppPage ? (p) => ipcRenderer.invoke("team-agent:reveal", p) : null,
  copyFile: isAppPage ? (p) => ipcRenderer.invoke("team-agent:copy-file", p) : null,
  copyText: isAppPage ? (text) => ipcRenderer.invoke("team-agent:copy-text", text) : null,
  // Which applications this machine can hand a file to (scanned in the main process).
  shareTargets: isAppPage ? () => ipcRenderer.invoke("team-agent:share-targets") : null,
  openWith: isAppPage ? (app, p) => ipcRenderer.invoke("team-agent:open-with", app, p) : null,
});
