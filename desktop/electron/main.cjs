// Electron 主进程:启动 Python 后端(FastAPI + LiteLLM),再打开窗口。
const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const PORT = process.env.TEAM_AGENT_PORT || "8765";
const DEV = !!process.env.TEAM_AGENT_DEV;
const DEV_URL = "http://localhost:5173";
// 界面访问后端的地址:指定了外部后端就用它,否则是本机的 PORT 端口(渲染进程通过预加载脚本拿到,不再各自写死)
const API_BASE = (process.env.TEAM_AGENT_BACKEND_URL || `http://127.0.0.1:${PORT}`).replace(/\/+$/, "");
// 每次启动随机生成;后端拒绝没带它的请求,避免用户浏览器里的其它网页访问本机后端
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
  if (process.env.TEAM_AGENT_BACKEND_URL) return; // 使用外部已启动的后端
  // 打包后:后端可执行文件放在 resources/backend/team-agent-backend(用 PyInstaller 构建,见 README)
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
    // 只交给系统浏览器打开 http(s) 链接;GitHub 上的 README / 更新说明里可能带别的协议,一律不放行
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
  };
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: "deny" };
  });
  // 应用窗口只能停留在自己的页面上:消息里的 Markdown 链接、GitHub 说明里的链接不带 target 时,
  // 默认会把整个窗口导航过去(并让那个外部页面拿到后端令牌)。这里一律拦下,改交系统浏览器。
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
  await waitBackend(); // 超时也继续打开窗口,界面会显示"后端未连接"
  await createWindow();
  app.on("activate", () => BrowserWindow.getAllWindows().length === 0 && createWindow());
});

app.on("before-quit", () => backend && backend.kill());
app.on("window-all-closed", () => process.platform !== "darwin" && app.quit());
