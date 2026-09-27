// Electron main process: start the Python backend (FastAPI + LiteLLM), then open the window.
const { app, BrowserWindow, dialog, ipcMain, shell, clipboard, nativeImage } = require("electron");
const { spawn, execFile } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { isAppUrl } = require("./navigation.cjs");

// What the app calls itself, before anything can ask. In a packaged build `productName` in
// package.json decides this; running from source the bundle is Electron's, so without this the menu
// bar, the About panel and every notification say "Electron" — a different name for the same app,
// right next to the one on screen.
app.setName("Team Agent");

// The icon, drawn by `scripts/make-icon.py` from the same mark the interface draws. macOS takes the
// Dock icon from the bundle, so a build from source shows Electron's unless it is set here.
const ICON = path.join(__dirname, "..", "build", "icon.png");

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
    icon: fs.existsSync(ICON) ? ICON : undefined,     // Linux and Windows take it from the window
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
  const guard = (e, url) => {
    if (isAppUrl(url, { dev: DEV, devUrl: DEV_URL,
                        indexPath: path.join(__dirname, "..", "dist", "index.html") })) return;
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

// Show a folder in the Finder (or the file manager). The renderer cannot do this itself, and it must
// not be able to hand over an arbitrary string either: the path is checked to be an existing
// directory, and the *answer* comes back so the panel can say what happened instead of assuming the
// window opened behind it.
ipcMain.handle("team-agent:open-path", async (_e, target) => {
  const given = typeof target === "string" ? target : "";
  if (!given || !path.isAbsolute(given)) return { ok: false, why: "not an absolute path" };
  try {
    if (!fs.statSync(given).isDirectory()) return { ok: false, why: "not a folder" };
  } catch {
    return { ok: false, why: "missing" };
  }
  const problem = await shell.openPath(given);      // "" means it opened
  return problem ? { ok: false, why: problem } : { ok: true };
});

// ---------------------------------------------------------------- one output file
// The 成果 column hands over **files**, and `open-path` above deliberately only accepts folders. Same
// discipline: the renderer names an absolute path, this side checks it is a real file, and the answer
// travels back so the row can report what happened instead of looking like it did nothing.
function outputFile(target) {
  const given = typeof target === "string" ? target : "";
  if (!given || !path.isAbsolute(given)) return "";
  try {
    return fs.statSync(given).isFile() ? given : "";
  } catch {
    return "";
  }
}

// In whatever program the system associates with this kind of file.
ipcMain.handle("team-agent:open-file", async (_e, target) => {
  const p = outputFile(target);
  if (!p) return { ok: false, why: "not an absolute path to an existing file" };
  const problem = await shell.openPath(p);
  return problem ? { ok: false, why: problem } : { ok: true };
});

// Reveal it in the Finder. This is the one action that works for *every* kind — including the ones
// no clipboard can carry (a 100 MB mp4) — which is why it is offered next to the share targets:
// from there the file can be dragged into any chat window.
ipcMain.handle("team-agent:reveal", (_e, target) => {
  const p = outputFile(target);
  if (!p) return { ok: false, why: "not an absolute path to an existing file" };
  shell.showItemInFolder(p);
  return { ok: true };
});

// Put the file on the clipboard **in whatever form this machine can carry**, and say which form that
// was. An image becomes an image, so it can be pasted straight into a chat; anything else becomes its
// path as text. ⚠️ The caller shows what actually happened: a button that says "copied" after copying
// a *path* when the user expected a picture is worse than one that says which of the two it was.
const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"]);
ipcMain.handle("team-agent:copy-file", (_e, target) => {
  const p = outputFile(target);
  if (!p) return { ok: false, why: "not an absolute path to an existing file" };
  if (IMAGE_EXT.has(path.extname(p).toLowerCase())) {
    const img = nativeImage.createFromPath(p);
    if (!img.isEmpty()) {
      clipboard.writeImage(img);
      return { ok: true, how: "image" };
    }
  }
  clipboard.writeText(p);
  return { ok: true, how: "path" };
});
ipcMain.handle("team-agent:copy-text", (_e, text) => {
  const s = typeof text === "string" ? text : "";
  if (!s) return { ok: false, why: "nothing to copy" };
  clipboard.writeText(s);
  return { ok: true, how: "text" };
});

// The applications this machine can hand a file to, **scanned rather than assumed**: a menu entry
// that opens nothing is worse than no entry, and which chat apps are installed differs per machine.
// Named in the order a person would look for them.
const SHARE_APPS = [
  ["wechat", ["/Applications/WeChat.app", "/Applications/WeChat.app"]],
  ["whatsapp", ["/Applications/WhatsApp.app"]],
  ["telegram", ["/Applications/Telegram.app"]],
  ["feishu", ["/Applications/Feishu.app", "/Applications/Lark.app"]],
  ["dingtalk", ["/Applications/DingTalk.app", "/Applications/钉钉.app"]],
  ["qq", ["/Applications/QQ.app"]],
  ["mail", ["/System/Applications/Mail.app", "/Applications/Mail.app"]],
  ["messages", ["/System/Applications/Messages.app", "/Applications/Messages.app"]],
  ["notes", ["/System/Applications/Notes.app", "/Applications/Notes.app"]],
];
ipcMain.handle("team-agent:share-targets", () => process.platform !== "darwin" ? [] :
  SHARE_APPS.map(([id, candidates]) => {
    const found = candidates.find((c) => fs.existsSync(c));
    return found ? { id, app: found } : null;
  }).filter(Boolean));

// Hand the file to a chosen application: `open -a <app> <file>`, which is exactly what Finder's
// "Open With" does. Whatever that application then does with a document (Mail composes an
// attachment, a chat app opens its send-file panel) is **its** behaviour, not something invented
// here — so the labels say "open with X", not "send to X".
// ⚠️ The application path comes from the renderer, so it is checked to be a bundle this machine has,
// inside the two directories applications live in — and the check is on the **resolved** path.
// A prefix test alone is not enough and this was measured: `/Applications/../tmp/Evil.app` starts
// with `/Applications/` and, if that file happens to exist, would have been accepted while the real
// bundle is somewhere else entirely.
const APP_ROOTS = ["/Applications", "/System/Applications"]
  .map((r) => { try { return fs.realpathSync(r); } catch { return r; } });
ipcMain.handle("team-agent:open-with", (_e, appPath, target) => {
  const p = outputFile(target);
  const app0 = typeof appPath === "string" ? appPath : "";
  if (!p) return { ok: false, why: "not an absolute path to an existing file" };
  let real = "";
  try { real = fs.realpathSync(app0); } catch { return { ok: false, why: "unknown application" }; }
  const inside = APP_ROOTS.some((root) => real === root || real.startsWith(root + "/"));
  if (!inside || !real.endsWith(".app")) return { ok: false, why: "unknown application" };
  return new Promise((resolve) => {
    execFile("open", ["-a", real, p], { timeout: 15000 }, (err, _out, stderr) => {
      resolve(err ? { ok: false, why: String(stderr || err.message).trim().slice(0, 300) } : { ok: true });
    });
  });
});

app.whenReady().then(async () => {
  // macOS only, and only needed for a run from source: `app.setName` fixes the menu, but the tile in
  // the Dock is the *asset* the app was launched from, and an unpackaged one inherits Electron's.
  // A packaged Team Agent.app already carries `build/icon.icns` and does not need this.
  let docked = "no (not macOS)";
  if (process.platform === "darwin" && app.dock && fs.existsSync(ICON)) {
    try { app.dock.setIcon(ICON); docked = "yes"; }
    catch (e) { docked = "failed: " + e.message; }   // a missing/odd icon must not stop the window
  }
  // One line, so "the app calls itself Team Agent and wears its own icon" can be *checked* rather
  // than hoped for — the menu bar, the About panel and the Dock are all outside the window, so
  // nothing in the UI can show whether they are right.
  console.log(`[team-agent] name="${app.getName()}" version=${app.getVersion()} dock-icon=${docked}`);
  startBackend();
  await waitBackend(); // Open the window even on timeout; the UI then shows "Backend not connected"
  await createWindow();
  app.on("activate", () => BrowserWindow.getAllWindows().length === 0 && createWindow());
});

app.on("before-quit", () => backend && backend.kill());
app.on("window-all-closed", () => process.platform !== "darwin" && app.quit());
