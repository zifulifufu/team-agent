// Electron main process: start the Python backend (FastAPI + LiteLLM), then open the window.
const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const PORT = process.env.TEAM_AGENT_PORT || "8765";
const DEV = !!process.env.TEAM_AGENT_DEV;
const DEV_URL = "http://localhost:5173";
// Where the UI reaches the backend: an external backend if one is given, otherwise this machine's PORT.
// The renderer receives it through the preload script instead of hardcoding it.
const API_BASE = (process.env.TEAM_AGENT_BACKEND_URL || `http://127.0.0.1:${PORT}`).replace(/\/+$/, "");
// Random per launch; the backend rejects requests without it, so other pages in the user's browser
// cannot reach the local backend.
const TOKEN = process.env.TEAM_AGENT_TOKEN || crypto.randomBytes(24).toString("hex");
let backend = null;

function pythonCommand() {
  if (process.env.TEAM_AGENT_PYTHON) return process.env.TEAM_AGENT_PYTHON;
  const root = path.join(__dirname, "..", "..");
  const venvs = [
    path.join(root, ".venv", "bin", "python"),
    path.join(root, ".venv", "Scripts", "python.exe"),
  ];
  return venvs.find((p) => fs.existsSync(p)) || (process.platform === "win32" ? "python" : "python3");
}

function startBackend() {
  if (process.env.TEAM_AGENT_BACKEND_URL) return; // An externally started backend is in use
  // Packaged: the backend executable lives in resources/backend/team-agent-backend
  // (built with PyInstaller — see the README)
  const packaged = path.join(process.resourcesPath || "", "backend", process.platform === "win32" ? "team-agent-backend.exe" : "team-agent-backend");
  if (app.isPackaged && fs.existsSync(packaged)) {
    backend = spawn(packaged, ["--port", PORT], { stdio: "inherit", env: { ...process.env, TEAM_AGENT_TOKEN: TOKEN } });
  } else {
    backend = spawn(pythonCommand(), ["-m", "app", "--port", PORT], {
      cwd: path.join(__dirname, "..", "..", "backend"),
      stdio: "inherit",
      env: { ...process.env, TEAM_AGENT_TOKEN: TOKEN },
    });
  }
  backend.on("exit", (code) => console.log("[backend] exited", code));
}

async function waitBackend(timeoutMs = 60000) {
  const url = `${API_BASE}/api/health`;
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(url);
      if (r.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 600,
    title: "Team Agent",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, "preload.cjs"),
      additionalArguments: [`--team-agent-token=${TOKEN}`, `--team-agent-api=${API_BASE}`],
    },
  });
  const openExternal = (url) => {
    // Only http(s) links go to the system browser. READMEs and release notes on GitHub may carry
    // other schemes, so nothing else is ever opened.
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
  };
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: "deny" };
  });
  // The window must stay on its own pages. A Markdown link in a message, or one in a GitHub note,
  // navigates the whole window by default when it has no target — and the page it lands on would
  // get the backend token. Everything else is blocked here and handed to the system browser.
  const isAppUrl = (url) => (DEV ? url.startsWith(DEV_URL) : url.startsWith("file://"));
  const guard = (e, url) => {
    if (isAppUrl(url)) return;
    e.preventDefault();
    openExternal(url);
  };
  win.webContents.on("will-navigate", guard);
  win.webContents.on("will-redirect", guard);
  if (DEV) await win.loadURL(DEV_URL);
  else await win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

ipcMain.handle("team-agent:pick-folder", async (e) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  const r = await dialog.showOpenDialog(win, { properties: ["openDirectory", "createDirectory"] });
  return r.canceled || r.filePaths.length === 0 ? null : r.filePaths[0];
});

app.whenReady().then(async () => {
  startBackend();
  await waitBackend(); // Open the window even on timeout; the UI then shows "Backend not connected"
  await createWindow();
  app.on("activate", () => BrowserWindow.getAllWindows().length === 0 && createWindow());
});

app.on("before-quit", () => backend && backend.kill());
app.on("window-all-closed", () => process.platform !== "darwin" && app.quit());
