#!/usr/bin/env node
/**
 * Render the built UI in a headless Chrome and check the layout invariants that only exist on
 * screen. Reading the bundle proves the code shipped; it does not prove the user sees the right
 * thing — and the two member lists this check exists for disagreed in plain sight for exactly that
 * reason (the sidebar printed one group's count while its entry opened the app-wide list).
 *
 * How it runs without any new dependency: the Chrome already on this machine is driven over the
 * DevTools protocol with a raw WebSocket, and `dist/` is served from a temporary loopback server so
 * the bundle's ES modules load (Chrome refuses modules over `file://`). The token the Electron
 * preload normally injects is read from the running app's own process arguments and injected before
 * any page script runs, so the UI talks to the real backend with real data.
 *
 * Usage:  node desktop/scripts/ui-smoke.cjs [--shot out.png] [--port 0]
 * Requires the app to be running (that is where the token comes from). Exits non-zero on failure.
 */
const { spawn, spawnSync, execSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const SHOT = arg("--shot", "");
const PORT = Number(arg("--port", "0"));   // 0 = let the OS pick, so two runs never collide
const DIST = path.join(__dirname, "..", "dist");
const APP = process.env.TEAM_AGENT_BACKEND_URL || "http://127.0.0.1:8765";

const CHROMES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
];
const chromePath = CHROMES.find((p) => fs.existsSync(p));
if (!chromePath) fail("no Chrome/Chromium found; install one or adapt the CHROMES list");

function fail(msg) {
  console.error("FAIL: " + msg);
  process.exit(1);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** The token the Electron preload injects (`--team-agent-token=…` on the renderer process).
 *
 *  Every matching line is scanned, not just the first one. `pgrep` also matches the GPU/renderer
 *  helpers, whose output order is not fixed, and only some of them carry the flag — with `head -1`
 *  this read as "could not read the app token — is the app running?" while the app was running and
 *  the token was right there in the next line.
 */
function token() {
  // A backend started by hand (no Electron window to read it from) can name its own token: start it
  // with TEAM_AGENT_TOKEN=… and pass the same variable to this check.
  const env = (process.env.TEAM_AGENT_TOKEN || "").trim();
  if (env.length >= 8) return env;
  const out = execSync(
    // `\{8,\}` is load-bearing: this command line *contains* the text `--team-agent-token=`
    // (inside the sed expression itself), so `pgrep -f` matches the shell running it, and the
    // substitution then succeeds on that line with an empty capture. `head -1` took that empty
    // match and the script reported "could not read the app token — is the app running?" while the
    // token was on the next line. Requiring a real token length skips it.
    "/usr/bin/pgrep -fl 'team-agent-token' 2>/dev/null | sed -n 's/.*--team-agent-token=\\([A-Za-z0-9_-]\\{8,\\}\\).*/\\1/p' | head -1",
    { shell: "/bin/zsh" },
  ).toString().trim();
  if (!out) fail("could not read the app token — is the app running?");
  return out;
}

/** A loopback server for dist/, so the bundle's module scripts are same-origin. */
function serveDist() {
  const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent((req.url || "/").split("?")[0]);
    const file = path.join(DIST, rel === "/" ? "index.html" : rel.replace(/^\/+/, ""));
    if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404).end("not found");
      return;
    }
    res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
    res.end(fs.readFileSync(file));
  });
  // Port 0: the OS hands back a free one, so a stray run from earlier cannot collide.
  return new Promise((ok) => server.listen(PORT, "127.0.0.1", () => ok(server)));
}

const get = (port, p) => new Promise((ok, no) => http.get({ host: "127.0.0.1", port, path: p },
  (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => ok(JSON.parse(b))); }).on("error", no));

// -------------------------------------------------------- a throwaway backend, for the states
// that cannot be found in stored history
const FIXTURE_DIR = "/tmp/ta-ui-fixture";
// Not one fixed port: this machine already has things on loopback (the embedding sidecar sits on
// 8799, and it answers 404 to everything else — which reads as "the fixture never started"). The
// first one that refuses a connection is taken.
const FIXTURE_PORTS = [8917, 8923, 8929, 8941, 8951];
const FIXTURE_TOKEN = "ui-smoke-fixture-token";

/** The fixture's database: one exchange holding a tool call that is *still running*, with output
 *  arriving, and the working behind the reply. Built with the app's own code, so the schema is
 *  whatever this checkout expects — not a hand-written INSERT that goes stale.
 *
 *  A directory of its own under /tmp, never the user's data directory. */
const FIXTURE_BUILD = `
import struct, wave
from pathlib import Path
from PIL import Image

from app.store import Store

st = Store()
g = st.list_groups()[0]
who = st.list_agents()[0]
ws = Path(st.workspace_dir(g["id"]))
st.add_message(g["id"], "user", None, "我", "把这段渲染一下,顺便看看要跑多久。")
st.add_message(
    g["id"], "agent", who["id"], who["name"], "跑完了:300 帧,成片在 out/demo.mp4。",
    model_id="deepseek/deepseek-flash",
    meta={
        "thinking": "用户要的是渲染成片。先确认帧数,再决定分辨率:"
                    "1) 先按 300 帧试水,别一上来就 4K;2) 跑完再核对成片大小。",
        "tools": [
            {"name": "Bash",
             "args": {"command": "npx remotion render src/index.tsx out/demo.mp4",
                      "description": "render the clip"},
             "status": "ok", "ms": 41230,
             "preview": "Rendered 300/300 frames\\nEncoded out/demo.mp4 (2.1 MB)"},
            {"name": "run_code",
             "args": {"language": "python", "code": "import time\\ntime.sleep(2)\\nprint('ok')"},
             "status": "running",
             "live": "frame 118/300  (39%)\\nframe 119/300  (40%)\\nframe 120/300  (40%)"},
        ],
    },
)

# The file a review looked at, really on disk: the chat fetches it through the API to show it, so a
# fixture without the bytes would prove the request was made and nothing about what the user sees.
(ws / "figures").mkdir(parents=True, exist_ok=True)
Image.new("RGB", (320, 200), (180, 40, 60)).save(ws / "figures" / "shot.png")
audio = ws / "narration.wav"
with wave.open(str(audio), "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"".join(struct.pack("<h", int(9000 * __import__("math").sin(i / 12.0)))
                           for i in range(16000)))          # one second of tone: long enough to play
st.add_message(
    g["id"], "agent", who["id"], who["name"], "看过画面了,也听过旁白。",
    model_id="deepseek/deepseek-flash",
    meta={"tools": [
        {"name": "review_picture",
         "args": {"path": "figures/shot.png", "question": "管子是不是圆的?"},
         "status": "ok", "ms": 7100,
         "preview": "Looked at \\"shot.png\\" with \\"gemini-3.1-flash-lite\\": 一根淡蓝色的直管,管壁均匀。",
         "files": [{"kind": "image", "name": "figures/shot.png", "bytes": 281, "where": "workspace"}]},
        {"name": "review_audio",
         "args": {"path": "narration.wav"},
         "status": "ok", "ms": 12900,
         "preview": "\\"narration.wav\\": 1.0s of sound. What is said in it: 这是旁白的第一句。",
         "files": [{"kind": "audio", "name": "narration.wav", "bytes": 32044, "where": "workspace"}]},
    ]},
)
print(g["id"])
`;

const health = (port, token) => new Promise((ok, no) => {
  const req = http.get({ host: "127.0.0.1", port, path: "/api/health", headers: { "x-team-agent-token": token } },
    (r) => { r.resume(); r.on("end", () => ok(r.statusCode)); });
  req.on("error", no);
  req.setTimeout(3000, () => req.destroy(new Error("timed out")));
});

/** Is anything listening on this port? A refusal means no — which is what the fixture needs. */
const portFree = (port) => new Promise((ok) => {
  const req = http.get({ host: "127.0.0.1", port, path: "/" }, (r) => { r.resume(); ok(false); });
  req.on("error", () => ok(true));
  req.setTimeout(1500, () => { req.destroy(); ok(false); });
});

/** Start the throwaway backend and open a second page on it. Returns { session, stop } or null.
 *  `send` and `ui` come from the caller: this script drives one Chrome over one socket. */
async function startFixture(send, ui) {
  const repo = path.join(__dirname, "..", "..");
  const py = path.join(repo, ".venv", "bin", "python");
  const backend = path.join(repo, "backend");
  if (!fs.existsSync(py)) {
    console.error("  no interpreter at " + py + " — cannot build the fixture");
    return null;
  }
  // The directory must NOT exist yet: it is created by the app, and under a sandbox the app's own
  // `mkdir(exist_ok=True)` fails on a directory that is already there — which reads as a broken app.
  fs.rmSync(FIXTURE_DIR, { recursive: true, force: true });
  const env = Object.assign({}, process.env, { TEAM_AGENT_DATA: FIXTURE_DIR, TEAM_AGENT_TOKEN: FIXTURE_TOKEN });
  // One of the environments this check runs in proxies file operations through CODEBUDDY_* environment
  // variables, and there `mkdir(exist_ok=True)` fails (EEXIST) on a directory that already exists —
  // which is exactly the case for a second process on the same data directory, and reads as "the app
  // is broken". They belong to the shell this check was launched from, not to the app under test.
  for (const k of Object.keys(env)) if (k.startsWith("CODEBUDDY_")) delete env[k];
  const built = spawnSync(py, ["-c", FIXTURE_BUILD], { cwd: backend, env, encoding: "utf8" });
  if (built.status !== 0) {
    console.error("  the fixture database could not be built:\n" + String(built.stderr || built.stdout || "").slice(-900));
    return null;
  }
  let port = 0;
  for (const candidate of FIXTURE_PORTS) { if (await portFree(candidate)) { port = candidate; break; } }
  if (!port) { console.error("  no free port among " + FIXTURE_PORTS.join(", ")); return null; }
  const srv = spawn(py, ["-m", "app", "--port", String(port)], { cwd: backend, env, stdio: "ignore" });
  let code = 0;
  for (let i = 0; i < 80 && code !== 200; i++) {
    try { code = await health(port, FIXTURE_TOKEN); } catch { code = 0; }
    if (code !== 200) await sleep(500);
  }
  if (code !== 200) {
    console.error("  the fixture backend never answered on port " + port);
    try { srv.kill(); } catch {}
    return null;
  }
  const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
  const { result: { sessionId } } = await send("Target.attachToTarget", { targetId, flatten: true });
  await send("Page.enable", {}, sessionId);
  await send("Runtime.enable", {}, sessionId);
  // ⚠️ 这一页**装作有外壳**:只有 Electron 主进程给得出 `openPath`,而「点文件夹进那个目录」的
  // 断言必须验到「交给外壳的是**哪个**路径」—— 在真的 Chromium 里那个入口会(正确地)不存在,
  // 于是断言只能写成「按钮不在」,等于什么都没验。
  // 成果栏那条右键菜单更甚:浏览器里它**只会剩一项**(在这一栏里看它),所以它列出的分享目标、
  // 以及「交给外壳的绝对路径对不对」,只有在装了外壳的这里才验得到。每个动作把调用记进
  // `window.__shell`,断言据此核对**参数**,而不是核对「点了没报错」。
  await send("Page.addScriptToEvaluateOnNewDocument",
    { source: "window.teamAgent = { token: " + JSON.stringify(FIXTURE_TOKEN) +
              ", api: 'http://127.0.0.1:" + port + "'" +
              ", openPath: function (p) { window.__openedPath = p; return Promise.resolve({ ok: true }); }" +
              ", openFile: function (p) { (window.__shell = window.__shell || []).push(['openFile', p]);" +
              "    return Promise.resolve({ ok: true }); }" +
              ", reveal: function (p) { (window.__shell = window.__shell || []).push(['reveal', p]);" +
              "    return Promise.resolve({ ok: true }); }" +
              // 复制这一路**故意分成两种**:图片复制的是图,别的复制的是路径 —— 断言要能看见是哪种。
              ", copyFile: function (p) { (window.__shell = window.__shell || []).push(['copyFile', p]);" +
              "    return Promise.resolve({ ok: true, how: /\\\\.(png|jpe?g|webp|gif)$/i.test(p) ? 'image' : 'path' }); }" +
              ", copyText: function (t) { (window.__shell = window.__shell || []).push(['copyText', t]);" +
              "    return Promise.resolve({ ok: true }); }" +
              // 固定的两个目标:它们是不是**这台机器真装了**由主进程扫描,这里只验渲染与调用。
              ", shareTargets: function () { return Promise.resolve([" +
              "    { id: 'wechat', app: '/Applications/WeChat.app' }," +
              "    { id: 'mail', app: '/System/Applications/Mail.app' }]); }" +
              ", openWith: function (a, p) { (window.__shell = window.__shell || []).push(['openWith', a, p]);" +
              "    return Promise.resolve({ ok: true }); } };" },
    sessionId);
  await send("Page.navigate", { url: "http://127.0.0.1:" + ui + "/" }, sessionId);
  await sleep(7000);
  const stop = () => {
    if (stop.done) return;
    stop.done = true;
    try { srv.kill(); } catch {}
    try { send("Target.closeTarget", { targetId }); } catch {}
  };
  return { session: sessionId, stop, dir: FIXTURE_DIR };
}

async function main() {
  if (!fs.existsSync(path.join(DIST, "index.html"))) fail("dist/ is missing — run the frontend build first");
  // ⚠️ 机械守卫：注入进页面的那几段模板必须是**括号配对**的。
  //
  // 为什么值得单查一次（2026-09-27 实测）：给那段查询补一个字段时手滑丢掉了返回对象的 `}`，
  // 于是页面里抛 SyntaxError，而下游每一条断言都只说「找不到 .zone / 找不到那个标题」——
  // 「我删了个括号」看起来跟「页面根本没渲染」一模一样，白跑了一整轮五分钟。
  // 这个形状的自伤（模板字符串里结构不完整）在第 5 分钟才被发现，第 5 毫秒就能发现。
  {
    const src = fs.readFileSync(__filename, "utf8");
    const re = /val\(`([^`\\]*(?:\\.[^`\\]*)*)`\)/g;
    const bad = [];
    for (let m; (m = re.exec(src)); ) {
      const body = m[1].trim();
      if (!body.startsWith("(function")) continue;
      const open = (body.match(/\{/g) || []).length;
      const close = (body.match(/\}/g) || []).length;
      if (!body.endsWith("})()") || open !== close)
        bad.push("line " + src.slice(0, m.index).split("\n").length + ": { ×" + open + " / } ×" + close);
    }
    if (bad.length)
      fail("一段注入页面的模板括号不配对，页面里会抛 SyntaxError（而不是「找不到元素」）:\n  " + bad.join("\n  "));
  }
  const server = await serveDist();
  const ui = server.address().port;
  // A fixed port, deliberately. This used to be `ui + 1000`, and the UI port comes from the ephemeral
  // range (49152+ on macOS), so the sum was routinely above 65535: Chrome was handed an impossible
  // port, never opened a debugging one, and the check failed with `Chrome never opened a debugging
  // port` — which is how this script sat unrun.
  const dbg = Number(arg("--cdp", "19223"));
  const chromeLog = "/tmp/ta-ui-smoke-chrome.log";
  const chrome = spawn(chromePath, [
    "--headless=new", "--remote-debugging-port=" + dbg, "--user-data-dir=/tmp/ta-ui-smoke",
    "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--disable-dev-shm-usage",
    // Chrome's own sandbox cannot start inside a sandboxed shell (it dies with SIGTRAP); it is a
    // throwaway profile reading a local page, so this costs nothing here.
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-crash-reporter", "--noerrdialogs",
    // Headless Chrome refuses to start playback without a user gesture, and a call made over the
    // DevTools protocol is not one — so "read this out loud" would look broken in the check while
    // working for a person. The page is local and the audio is this machine talking.
    "--autoplay-policy=no-user-gesture-required",
    "--window-size=1680,1050", "about:blank",
  ], { stdio: ["ignore", fs.openSync(chromeLog, "w"), fs.openSync(chromeLog, "w")] });
  const stop = () => { try { chrome.kill(); } catch {} try { server.close(); } catch {} };
  process.on("exit", stop);

  let ver; for (let i = 0; i < 40 && !ver; i++) { try { ver = await get(dbg, "/json/version"); } catch { await sleep(250); } }
  if (!ver) {
    let why = ""; try { why = fs.readFileSync(chromeLog, "utf8").slice(-600); } catch {}
    fail("Chrome never opened a debugging port (" + dbg + ")" + (why ? "\n" + why : ""));
  }
  // Node's own WebSocket (stable since 22), so this script needs no dependency at all — the `ws`
  // package it used to require is not installed here, which is how a check stops being run. The
  // standard event API is used throughout so it works with either implementation.
  if (typeof WebSocket !== "function") fail("this check needs Node 22+ (built-in WebSocket)");
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((ok, no) => { ws.addEventListener("open", ok); ws.addEventListener("error", (e) => no(e)); });
  let id = 0; const waiting = new Map();
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data.toString());
    if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  });
  const send = (method, params, sessionId) => new Promise((ok) => {
    const n = ++id; waiting.set(n, ok);
    ws.send(JSON.stringify(Object.assign({ id: n, method, params: params || {} }, sessionId ? { sessionId } : {})));
  });

  const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
  const { result: { sessionId } } = await send("Target.attachToTarget", { targetId, flatten: true });
  await send("Page.enable", {}, sessionId);
  await send("Runtime.enable", {}, sessionId);
  await send("Page.addScriptToEvaluateOnNewDocument",
    { source: "window.teamAgent = { token: " + JSON.stringify(token()) + ", api: " + JSON.stringify(APP) + " };" },
    sessionId);
  await send("Page.navigate", { url: "http://127.0.0.1:" + ui + "/" }, sessionId);
  await sleep(7000);

  /** `Runtime.evaluate` inside one target. Split out from `val` because this check drives two pages:
   *  the running app, and a throwaway backend whose data is built to show states that history cannot
   *  contain (see the "one message" section). */
  const valIn = (sid) => async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sid);
    if (r.result && r.result.exceptionDetails) return { __exc: JSON.stringify(r.result.exceptionDetails).slice(0, 200) };
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
  const val = valIn(sessionId);
  /** What the sidebar says, and how the main area is divided up — as text and as geometry. */
  const state = () => val(`(function () {
    var side = document.querySelector('.sidebar');
    var main = document.querySelector('.main');
    var chat = document.querySelector('.chat');
    var rail = document.querySelector('.mrail');
    var page = document.querySelector('.page-cols');
    var box = function (e) { if (!e) return null; var r = e.getBoundingClientRect();
      return { x: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height),
               top: Math.round(r.top), bottom: Math.round(r.bottom) }; };
    return {
      sidebarMemberLists: document.querySelectorAll('.sidebar .mdock, .sidebar .mc, .sidebar .list-item').length,
      sidebarMemberEntry: ([].filter.call(side ? side.querySelectorAll('.nav-item') : [],
        function (b) { return /\\u6210\\u5458|Members/.test(b.textContent); })[0] || {}).textContent,
      railPresent: !!rail,
      railW: rail ? Math.round(rail.getBoundingClientRect().width) : 0,
      railGapFromSidebar: (function () {
        var r = rail ? rail.getBoundingClientRect() : null;
        var sr = side ? side.getBoundingClientRect() : null;
        return (r && sr) ? Math.round(r.left - sr.right) : null; })(),
      railTitle: rail ? ((rail.querySelector('.mrail-title') || {}).textContent || '') : '',
      railSub: rail ? ((rail.querySelector('.mrail-sub') || {}).textContent || '') : '',
      railCards: rail ? rail.querySelectorAll('.mc').length : 0,
      railEmptyHint: rail ? !!rail.querySelector('.mrail-empty') : false,
      railFoot: rail ? ((rail.querySelector('.mrail-foot button') || {}).textContent || '') : '',
      sideW: side ? Math.round(side.getBoundingClientRect().width) : 0,
      mainW: box(main) ? box(main).w : 0,
      chatW: box(chat) ? box(chat).w : 0,
      chatX: box(chat) ? box(chat).x : 0,
      titleCount: (function () {
        var m = (document.querySelector('.chat-title .muted') || {}).textContent || '';
        var n = m.match(/\\((\\d+)\\)/); return n ? Number(n[1]) : null; })(),
      // The chat header must have NO row of member faces any more — that strip is exactly what the
      // user asked to be rid of, and "Members" opens from the sidebar instead.
      headerFaces: document.querySelectorAll('.chat-head .ava-stack, .chat-head .mpop, .chat-head .mfly').length,
      headerButtons: document.querySelectorAll('.chat-head .head-actions button').length,
      // The column's own list is what scrolls; the header and footer are fixed rows.
      railScrolls: rail ? (function () {
        var b = rail.querySelector('.mrail-body');
        if (!b) return null;
        return { overflowY: getComputedStyle(b).overflowY,
                 scrollable: b.scrollHeight > b.clientHeight + 1 }; })() : null,
      // The number printed on the sidebar entry, read from the entry itself.
      sideItemCount: (function () {
        var b = [].filter.call(document.querySelectorAll('.sidebar .nav-item'), function (x) {
          return /\\u6210\\u5458|Members/.test(x.textContent); })[0];
        var n = b ? (String(b.textContent).match(/\\((\\d+)\\)/) || [])[1] : null;
        return n ? Number(n) : null; })(),
      vh: innerHeight, vw: innerWidth,
      wsHeaderIcon: document.querySelectorAll('.head-actions button[title="\u5de5\u4f5c\u7a7a\u95f4"], .head-actions button[title="Workspace"]').length,
      wsChip: (function () {
        var b = document.querySelector('.wspick-btn');
        if (!b) return null;
        var r = b.getBoundingClientRect(), bar = document.querySelector('.composer-bar');
        return { name: (b.querySelector('.wspick-name') || {}).textContent,
                 below: bar ? Math.round(r.top) >= Math.round(bar.getBoundingClientRect().top) : null,
                 chev: !!b.querySelector('.wspick-chev'),
                 expanded: b.getAttribute('aria-expanded') };
      })(),
      wsPop: (function () {
        var p = document.querySelector('.wspick-pop');
        if (!p) return null;
        var r = p.getBoundingClientRect(), cs = getComputedStyle(p), b = p.querySelector('.wspick-body');
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height),
                 maxHeight: cs.maxHeight,
                 bodyOverflow: b ? getComputedStyle(b).overflowY : null,
                 opensUpward: (function () {
                   var chip = document.querySelector('.wspick-btn').getBoundingClientRect();
                   return r.bottom <= chip.top; })() };
      })(),
      memberPage: !!page,
      memberPageItems: page ? page.querySelectorAll('.list-item').length : 0
    };
  })()`);
  const click = (selector) => val(`(function () { var b = document.querySelector(${JSON.stringify(selector)});
    if (!b) return 'missing: ' + ${JSON.stringify(selector)}; b.click(); return 'clicked'; })()`);
  const clickByText = (where, re) => val(`(function () {
    var b = [].filter.call(document.querySelectorAll(${JSON.stringify(where)}),
      function (x) { return ${re}.test(x.textContent); })[0];
    if (!b) return 'missing in ' + ${JSON.stringify(where)}; b.click(); return 'clicked: ' + b.textContent.trim(); })()`);
  const esc = () => val(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) && 'sent'`);

  const problems = [];
  const expect = (ok, what) => { console.log((ok ? "  ok   " : "  FAIL ") + what); if (!ok) problems.push(what); };
  /** A picture of the current screen, next to the one --shot names. Sections that have something worth
   *  looking at (the filter panel open, the two settings pages) take one while it is on screen. */
  const shotPath = (suffix) => SHOT.replace(/\.png$/i, "") + suffix + ".png";
  const shotTo = async (file, sid = sessionId) => {
    // A plain viewport capture — **not** `captureBeyondViewport`. With a clip, Chrome re-lays the page
    // out for the capture, which resets the inner scroll container these settings pages use, and the
    // picture then shows the top of the page however carefully the caller scrolled first.
    // `sid` because this check drives two pages: the running app and the fixture's own backend
    // (`Page.captureScreenshot` without a session captures the *first* target, which silently
    // produced two byte-identical pictures of somewhere else entirely).
    const r = await send("Page.captureScreenshot", {}, sid);
    if (r.result && r.result.data) { fs.writeFileSync(file, Buffer.from(r.result.data, "base64")); console.log("screenshot → " + file); }
  };

  // ---------------------------------------------------------------- 左栏顶部那一条
  // The strip the user asked about, checked as geometry rather than as markup. The middle size it
  // went through is why the overflow check is here: the name and the buttons genuinely do not fit in
  // a 280px sidebar once the 82px macOS traffic-light gutter is taken (measured 309 > 279), so a
  // version badge in that row pushed the last button off the edge of the sidebar — which no
  // "does the button exist" assertion would have caught.
  // The panel's top corner, checked as geometry. Two facts are load-bearing here and both were
  // wrong before: the buttons belong *above* the name, and the toggle must not move when the panel
  // collapses — the way back used to live in the main area, 173px from the button that had just been
  // pressed, so pressing the old spot did nothing.
  /** Exactly what a brand mark is drawn as: every shape, its geometry, and its painted colours.
   *  A helper rather than inline JSON because this is read for two elements and compared to each
   *  other — see the "one mark" assertions. */
  const drawMark = `function (svg) {
    if (!svg) return null;
    var parts = [].map.call(svg.querySelectorAll('rect, circle'), function (n) {
      var cs = getComputedStyle(n);
      return [n.tagName, n.getAttribute('cx') || n.getAttribute('x'), n.getAttribute('cy') || n.getAttribute('y'),
              n.getAttribute('r') || n.getAttribute('width'), n.getAttribute('rx') || '', cs.fill].join(':');
    });
    return { parts: parts, box: [svg.getAttribute('width'), svg.getAttribute('height')] };
  }`;

  const topBar = () => val(`(function () {
    var drawMark = ${drawMark};
    var side = document.querySelector('.sidebar');
    if (!side) return { missing: true };
    var row = side.querySelector('.side-top');
    var btns = [].slice.call(row.querySelectorAll('button')).filter(function (b) {
      return getComputedStyle(b).display !== 'none'; });
    var tg = btns[0];
    var tr = tg ? tg.getBoundingClientRect() : null;
    var rr = row.getBoundingClientRect(), sr = side.getBoundingClientRect();
    var brand = side.querySelector('.brand-row');
    var br = brand ? brand.getBoundingClientRect() : null;
    var name = side.querySelector('.brand-name');
    var main = document.querySelector('.main');
    // The header block's own height, and the bottoms of the two headers beside it: the members
    // column's and the chat's. All three have to end on one line — see the assertions below.
    var rail = document.querySelector('.mrail-head');
    var chat = document.querySelector('.chat-head');
    var btnBox = btns.map(function (b) { return b.getBoundingClientRect(); });
    var gaps = [];
    for (var i = 1; i < btnBox.length; i++) { gaps.push(Math.round(btnBox[i].left - btnBox[i - 1].right)); }
    var logo = side.querySelector('.brand-logo');
    var ver = side.querySelector('.brand-ver');
    var nb = name ? name.getBoundingClientRect() : null;
    var sr2 = sr;
    return {
      buttons: btns.map(function (b) { return b.getAttribute('aria-label'); }),
      toggle: tr ? { x: Math.round(tr.left), y: Math.round(tr.top) } : null,
      name: name ? name.textContent : null,
      nameRight: name ? Math.round(name.getBoundingClientRect().right) : null,
      firstBtnLeft: tr ? Math.round(tr.left) : null,
      // Above the name, not beside it.
      buttonsAboveName: (br && br.height > 0) ? Math.round(rr.bottom) <= Math.round(br.top) : null,
      nameRowVisible: !!br && br.height > 0,
      collapsed: /(^|\\s)collapsed(\\s|$)/.test(side.className),
      sideW: Math.round(sr.width),
      sideRight: Math.round(sr.right),
      mainLeft: main ? Math.round(main.getBoundingClientRect().left) : null,
      overflowRight: btns.length ? Math.round(btns[btns.length - 1].getBoundingClientRect().right - sr.right) : null,
      // --- the top block's own geometry (the layout the user asked for, 2026-09-25) ---
      rowH: Math.round(rr.height),
      rowBottom: Math.round(rr.bottom),
      railHeadBottom: rail ? Math.round(rail.getBoundingClientRect().bottom) : null,
      chatHeadBottom: chat ? Math.round(chat.getBoundingClientRect().bottom) : null,
      btnGaps: gaps,
      // The gap that is actually visible: from the buttons' own bottom edge to the top of the
      // wordmark. Measuring "the brand row's top" would read 0 — that row's spacing is its own
      // padding, so its box starts flush against the header band above it.
      btnAboveBrandGap: (br && br.height > 0 && nb && btnBox.length)
        ? Math.round(nb.top - Math.round(Math.min.apply(null, btnBox.map(function (b) { return b.bottom; }))))
        : null,
      logoBeforeName: (logo && nb) ? Math.round(logo.getBoundingClientRect().right) <= Math.round(nb.left) : null,
      // The mark and the wordmark are centred **as one lockup**: the name cannot be centred on its own
      // while the mark sits to its left, and a lockup centred by its whole width is what "centred
      // logo + name" means. So this measures from the mark's left edge to the **right edge of the
      // whole lockup** — the version's right edge, now that the version sits on that same line
      // (before it moved there, the version was the row below and this stopped at the name).
      // ⚠️ 要的是元素**算出来的矩形**的 .right,不是元素本身:元素上没有 .right,拿它会得到
      // logo.left + undefined → NaN → 断言里显示成 null,看起来像「量不到」而不是「量错了」。
      // (这段注释在模板字符串里,所以**一个反引号都不能写** —— 写了就把字符串提前闭合,报
      //  missing ) after argument list。)
      nameCentreOff: (logo && nb)
        ? Math.round((logo.getBoundingClientRect().left
                      + (ver ? ver.getBoundingClientRect().right : nb.right)) / 2
                     - (sr2.left + sr2.right) / 2)
        : null,
      // ---- one mark, not two
      // The sidebar and the middle panel introduce the app on the same screen, and they used to draw
      // *different pictures* — an outline of three dots against a filled square with a sparkle in it.
      // Comparing the node coordinates alone does not catch that (both had three dots by then), so
      // this reads the whole drawing: every shape, its geometry, and the colour it is actually
      // painted in. Size is allowed to differ; nothing else is.
      mark: drawMark(logo), heroMark: drawMark(document.querySelector('.hero-logo')),
      version: ver ? ver.textContent.trim() : null,
      // 版本号与名字**同一条线**,而且在名字右边 —— 用户明确要求「写在 Team Agent 后,不用分两行」。
      // 所以断言量两件事:竖直方向在同一行、水平方向在名字之后(只量文字,不量容器)。
      versionOnNameLine: (ver && nb)
        ? Math.abs((ver.getBoundingClientRect().top + ver.getBoundingClientRect().height / 2)
                   - (nb.top + nb.height / 2)) <= 3
        : null,
      versionAfterName: (ver && nb)
        ? Math.round(ver.getBoundingClientRect().left) >= Math.round(nb.right) - 2
        : null,
      floatingExpandBtn: document.querySelectorAll('.expand-btn').length,
      brandRowGone: !document.querySelector('.brand'),
      newChatEntries: [].slice.call(document.querySelectorAll('button')).filter(function (b) {
        return /\u65b0\u5efa\u7fa4\u804a|New group chat/.test(b.textContent || '') ||
               b.getAttribute('aria-label') === 'New group chat'; }).length
    };
  })()`);

  // ---------------------------------------------------------------- 项目筛选 / 归档 / 搜索
  // What the user asked for: "a status of all projects, completed ones filed away, and searchable
  // after that", and then "去掉项目和状态标签, 筛选按钮放在顶部那一行". Asserted as the things that can
  // silently break — where the button is, what each state's number is against the rows it actually
  // shows, and a filed-away project still being findable.
  const projects = () => val(`(function () {
    var rows = [].slice.call(document.querySelectorAll('.conv')).map(function (c) {
      // 状态不再是单独一个点(2026-09-25 并进了文件夹图标):跑着的读那个会呼吸的点,
      // 其余读文件夹图标的可读名 —— 两条路都要有,否则「正在运行」那一档会读成空字符串。
      var dot = c.querySelector('.conv-run') || c.querySelector('.cv-ico');
      return { name: (c.querySelector('.conv-name') || {}).textContent,
               // ⚠️ 列表里显示的名字是**截断版**(最多 10 个字 + 省略号),完整名字在 title 上。
               // 凡是要拿「后端那只群的名字」去比的地方(归档),都得比这一项 —— 比截断过的那一项,
               // 一条本来就对的断言会因为截断而变红。
               full: (c.querySelector('.conv-name') || {}).getAttribute
                 ? (c.querySelector('.conv-name').getAttribute('title') || '') : '',
               // 状态是一个点,所以「状态词」只能从它的可读名读回来 —— 这正是这个点必须带
               // aria-label 的原因(否则屏幕阅读器和这条断言都读不到状态)。
               state: dot ? (dot.getAttribute('aria-label') || '') : '',
               archived: /(^|\\s)arch(\\s|$)/.test(c.className) }; });
    var top = document.querySelector('.side-top'), brand = document.querySelector('.brand-row');
    var btns = top ? [].slice.call(top.querySelectorAll('button')).map(function (b) {
      var r = b.getBoundingClientRect();
      return { label: b.getAttribute('aria-label') || b.title || '', x: Math.round(r.left), y: Math.round(r.top),
               on: b.classList.contains('on') }; }) : [];
    return { rows: rows, empty: (document.querySelector('.side-empty') || {}).textContent || '',
             // 状态标签不再摊在正文里(它们进了顶部筛选按钮的面板);列表的标题在 layout() 里查。
             tabsLeft: document.querySelectorAll('.sp-tab, .sp-tabs').length,
             searchBtns: document.querySelectorAll('.sidebar button[aria-label="Search group chats"]').length,
             topBtns: btns,
             rowAboveName: top && brand ? Math.round(top.getBoundingClientRect().bottom) <= Math.round(brand.getBoundingClientRect().top) : null };
  })()`);
  /** The filter panel, which is where the four states live — now one row of icon chips. */
  const filterPanel = () => val(`(function () {
    var p = document.querySelector('.side-filter');
    if (!p) return { open: false };
    var all = [].slice.call(p.querySelectorAll('.sf-chip')).map(function (b) {
      var m = String(b.textContent).match(/(\\d+)\\s*$/);
      var br = b.getBoundingClientRect();
      return { all: b.classList.contains('sf-all'), y: Math.round(br.top), h: Math.round(br.height),
               // 芯片上只有图标和数字,所以标签只能从 tooltip 读回来 —— 这是它必须带 title 的原因
               label: b.getAttribute('title') || '',
               n: m ? Number(m[1]) : null, on: b.getAttribute('aria-checked') === 'true' }; });
    var r = p.getBoundingClientRect(), top = document.querySelector('.side-top').getBoundingClientRect();
    return { open: true, all: all[0], states: all.slice(1),
             // 一行:五个芯片的 y 必须一致,而且整块面板只占一行的高度(旧的竖排菜单比列表还高)
             oneRow: all.length > 1 && all.every(function (x) { return x.y === all[0].y; }),
             panelH: Math.round(r.height),
             belowRow: Math.round(r.top) >= Math.round(top.bottom) - 1,
             inWindow: Math.round(r.left) >= 0 && Math.round(r.right) <= innerWidth && Math.round(r.bottom) <= innerHeight };
  })()`);
  /** Open it if it is not open (the button toggles, so ask first). */
  const openFilter = async () => {
    const p = await filterPanel();
    if (p.open) return p;
    await click(".side-top .filter-btn");
    await sleep(600);
    return filterPanel();
  };
  /** Tick one panel row by position: 0 = 全部项目, 1..4 = running / in progress / completed / archived. */
  const tickRow = async (i) => {
    await val(`(function () {
      var rows = document.querySelectorAll('.side-filter .sf-chip');
      var b = rows[${i}]; if (!b) return 'missing row';
      b.click(); return 'clicked'; })()`);
    await sleep(1300);
  };
  /** Exactly one state ticked — 全部项目 clears everything first, so this is "only the i-th state". */
  const onlyState = async (i) => {
    await openFilter();
    await tickRow(0);
    await tickRow(i + 1);
    return filterPanel();
  };
  /** Ticked, then closed again: the state the user is left looking at. */
  const onlyAndClose = async (i) => {
    const p = await onlyState(i);
    await click(".side-top .filter-btn");
    await sleep(400);
    return p;
  };
  /** 侧栏的上下排布:导航(成员/工具)在上,项目列表在下 —— 用户明确要求恢复成这样。 */
  const layout = () => val(`(function () {
    var sec = document.querySelector('.sidebar .side-section');
    var list = document.querySelector('.sidebar .side-list');
    var tools = document.querySelector('.sidebar .side-nav[aria-label]');
    var userRow = document.querySelector('.sidebar .user-row');
    var box = function (e) { if (!e) return null; var r = e.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height) }; };
    var m = String((sec || {}).textContent || '').match(/\\((\\d+)\\)/);
    return { section: sec ? String(sec.textContent).replace(/\\s+/g, ' ').trim() : null,
             sectionCount: m ? Number(m[1]) : null,
             navBottom: tools ? box(tools).bottom : null,
             sectionTop: sec ? box(sec).top : null,
             listTop: list ? box(list).top : null,
             listBottom: list ? box(list).bottom : null,
             listOverflow: list ? getComputedStyle(list).overflowY : null,
             userTop: userRow ? box(userRow).top : null,
             rows: document.querySelectorAll('.sidebar .conv').length };
  })()`);
  /** An authenticated API call made from the page, so this check uses the same token the app does. */
  const apiCall = (method, path, body) => val(`(async function () {
    var h = { 'X-Team-Agent-Token': (window.teamAgent || {}).token || '' };
    var opts = { method: ${JSON.stringify(method)}, headers: h };
    ${body === undefined ? "" : `opts.headers['Content-Type'] = 'application/json';
    opts.body = ${JSON.stringify(JSON.stringify(body))};`}
    var r = await fetch('http://127.0.0.1:8765' + ${JSON.stringify(path)}, opts);
    return { status: r.status, text: await r.text() };
  })()`);
  const searchBox = (text) => val(`(function () {
    var b = [].filter.call(document.querySelectorAll('.side-top button'), function (x) {
      return /Search group chats|\\u641c\\u7d22/.test(x.getAttribute('aria-label') || ''); })[0];
    if (b && !document.querySelector('.search-pal')) b.click();
    var i = document.querySelector('.search-pal input');
    if (!i) return 'no search box';
    var set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    set.call(i, ${JSON.stringify(text)}); i.dispatchEvent(new Event('input', { bubbles: true }));
    return 'typed'; })()`);
  /** The floating search box: where it is, what it lists, and whether the sidebar still has a field
   *  of its own (it must not — one search, one place). */
  const searchPal = () => val(`(function () {
    var p = document.querySelector('.search-pal');
    if (!p) return { open: false, inlineFields: document.querySelectorAll('.sidebar .side-search').length };
    var r = p.getBoundingClientRect();
    var rows = [].slice.call(p.querySelectorAll('.search-pal-row')).map(function (b) {
      return { name: (b.querySelector('.conv-name') || {}).textContent,
               state: (b.querySelector('.conv-state') || {}).textContent,
               sel: b.classList.contains('sel'), cur: b.classList.contains('on') }; });
    return { open: true, rows: rows, q: (p.querySelector('input') || {}).value,
             rect: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)],
             cx: Math.round((r.left + r.right) / 2), cy: Math.round((r.top + r.bottom) / 2),
             vw: innerWidth, vh: innerHeight,
             // 正中间:两个轴的中心都落在窗口中心 ±2px
             centred: Math.abs((r.left + r.right) / 2 - innerWidth / 2) <= 2 && Math.abs((r.top + r.bottom) / 2 - innerHeight / 2) <= 2,
             inWindow: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
             headCursor: getComputedStyle(p.querySelector('.search-pal-head')).cursor,
             inlineFields: document.querySelectorAll('.sidebar .side-search').length,
             backdrop: !!document.querySelector('.search-pal-back') };
  })()`);
  const openSearch = async () => {
    if ((await searchPal()).open) return;
    await val(`(function () {
      var b = [].filter.call(document.querySelectorAll('.side-top button'), function (x) {
        return /Search group chats|\\u641c\\u7d22/.test(x.getAttribute('aria-label') || ''); })[0];
      if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
    await sleep(600);
  };
  // 真鼠标事件(CDP),不是 JS 造事件:拖动这条路径必须走浏览器自己的 pointer 事件链
  const mouse = (type, x, y, buttons) => send("Input.dispatchMouseEvent",
    { type, x, y, button: "left", clickCount: 1, pointerType: "mouse",
      buttons: buttons === undefined ? (type === "mouseReleased" ? 0 : 1) : buttons }, sessionId);
  /** 只是把光标移过去(不按键)——用来制造/消除行上的 hover */
  const hoverAt = (x, y) => mouse("mouseMoved", x, y, 0);

  console.log("— 每个项目下面的工作文件夹与任务");
  // ⚠️ 先等侧栏**把项目渲染出来**再量。这一段量的是「一行里有什么」,列表还没上来时它量到的是一片
  // 空、所有断言都"通过"在一个空集合上 —— 上一轮就是这么红的(「侧栏列出了项目 (0)」)。
  for (let i = 0; i < 30; i++) {
    if (await val(`document.querySelectorAll('.side-list .conv').length`) > 0) break;
    await sleep(500);
  }
  // 这一段量的是两件事:①侧栏那个文件夹 = 群聊的工作空间(同一个目录、同一个名字规则);
  // ②**每一行都是两行、而且一样高** —— 这正是「排列紊乱」要治的东西,所以断言全落在几何上,
  // 不看截图:行里子块的数量、行高、以及「文件夹名和项目名一样时不再重复写一遍」。
  const folders = await val(`(function () {
    var rows = [].slice.call(document.querySelectorAll('.conv')).map(function (c) {
      var head = c.querySelector('.conv-head');
      var t = c.querySelector('.conv-task');
      var dir = c.querySelector('.conv-dir');
      var ico = c.querySelector('.cv-ico');
      var task = t ? t.querySelector('.ct-title') : null;
      return {
        project: (c.querySelector('.conv-name') || {}).textContent,
        // 列表里显示的名字 vs 完整名字(title 上):截断只发生在显示这一层
        shownName: (c.querySelector('.conv-name') || {}).textContent || '',
        fullName: (c.querySelector('.conv-name') || {}).getAttribute
          ? (c.querySelector('.conv-name').getAttribute('title') || '') : '',
        // 「名字挡住下面功能键」的可量形式:第一行的名字必须在第二行**上面**,不重叠
        nameBottom: (c.querySelector('.conv-name') || {}).getBoundingClientRect
          ? Math.round(c.querySelector('.conv-name').getBoundingClientRect().bottom) : 0,
        taskTop: t ? Math.round(t.getBoundingClientRect().top) : 0,
        rowRight: Math.round(c.getBoundingClientRect().right),
        nameRight: (c.querySelector('.conv-name') || {}).getBoundingClientRect
          ? Math.round(c.querySelector('.conv-name').getBoundingClientRect().right) : 0,
        // 目录只有一条出口 —— 挂在文件夹图标上的 title,和群聊工作空间是同一个值
        folderPath: ico ? (ico.getAttribute('title') || '') : '',
        dirName: dir ? String(dir.textContent).replace(/^·\\s*/, '') : '',
        blocks: c.children.length,
        headH: head ? Math.round(head.getBoundingClientRect().height) : 0,
        subH: t ? Math.round(t.getBoundingClientRect().height) : 0,
        h: Math.round(c.getBoundingClientRect().height),
        // 「进目录」那个入口现在就是文件夹图标自己(第一行),有外壳时它是个真按钮
        canOpen: !!c.querySelector('.cv-ico.cv-open'),
        openTag: (c.querySelector('.cv-ico') || {}).tagName || '',
        // 折叠箭头:只有**当前打开的那一个**项目才有
        canFold: !!c.querySelector('.cv-fold'),
        isActive: c.classList.contains('on'),
        hasRename: !!c.querySelector('.cf-rename'),
        canRename: !(c.querySelector('.cf-rename') || {}).disabled,
        hasEmpty: !!c.querySelector('.cf-empty'),
        task: task ? task.textContent : '',
        taskCounts: t ? ((t.querySelector('.ct-owner') || {}).textContent || '') : '',
        // 第二行要缩进到**项目名**那一列 —— 参考样式里那层层次感就是靠这个缩进读出来的,
        // 两层起点一样会因为「看起来没缩进」而被读成一堆。所以它是可量的,断言量它。
        nameX: (c.querySelector('.conv-name') || {}).getBoundingClientRect
          ? Math.round(c.querySelector('.conv-name').getBoundingClientRect().left) : 0,
        // 缩进的锚点是这一行的第一个**文字**元素,也就是标题。(那个状态点当年是悬在文字列外面、
        // 靠 dot+gap 把标题顶到项目名那一列的;2026-09-25 换成状态词之后它挪到了右边那组元信息里,
        // 所以标题仍然是第一个文字元素 —— 别去量 .ct-state。)
        // ⚠️ 这段注释在 val 的模板字符串**里面** —— 注释里写反引号会把模板提前闭合,后面几行就变回
        // 真的 JS 在 main() 里跑,报一个和界面毫无关系的 ReferenceError(或者直接语法错)。
        titleX: task ? Math.round(task.getBoundingClientRect().left) : 0,
        // 运行状态必须是**读得出来的词**(用户 2026-09-25:「显示运行状态」),不是那个点。
        taskState: t ? (((t.querySelector('.ct-state') || {}).textContent) || '') : '',
        stateX: (t && t.querySelector('.ct-state'))
          ? Math.round(t.querySelector('.ct-state').getBoundingClientRect().left) : -1,
        hasDot: !!(t && t.querySelector('.ct-dot')),
        // 有没有任务板 —— 按结构判断,不按占位文案:这一页在 headless Chrome 里是**英文界面**,
        // 「No task board yet」和中文占位对不上,按文案过滤会把它当成「有任务板却没写状态」。
        // 这一行是**任务板**还是「最近一句」?两种形状后端都能给,差别就在有没有完成数。
        hasBoard: !!(t && t.querySelector('.ct-owner')),
        // 横向溢出:列表出现横向滚动条时,行里的文字会互相压在一起 —— 截图里就是这样
        // (codex 读旧截图时看到的「重叠」)。它是可量的,所以断言量它,不靠眼睛。
        overflowX: c.scrollWidth - c.clientWidth,
      };
    });
    var list = document.querySelector('.side-list');
    return { rows: rows, thirdLine: document.querySelectorAll('.conv-folder').length,
             listOverflowX: list ? (list.scrollWidth - list.clientWidth) : 0,
             // 「在访达里打开」这个按钮只有 Electron 外壳能提供(渲染进程自己开不了访达),
             // 所以在这台 headless Chrome 里它**本来就该缺席** —— 断言要认这个前提,而不是当成缺失。
             shellOpen: !!(window.teamAgent && window.teamAgent.openPath) };
  })()`);
  const api_groups = await val(`(async function () {
    var r = await fetch('http://127.0.0.1:8765/api/groups', { headers: { 'X-Team-Agent-Token': (window.teamAgent || {}).token || '' } });
    return await r.json();
  })()`);
  const shown = folders.rows || [];
  expect(shown.length >= 1, "侧栏列出了项目 (" + shown.length + ")");
  // 按**路径**配对,不按名字:群名会重名(有三个「视频制作」),用名字当键会把不同的行配错。
  const byPath = new Map((api_groups || []).map((g) => [(g.folder || {}).path, g]));
  const paths = shown.map((r) => r.folderPath).filter(Boolean);
  const wrong = paths.filter((p) => !byPath.has(p));
  expect(paths.length === shown.length && wrong.length === 0,
    "侧栏显示的文件夹 = 该群的工作空间(同一个路径)。对不上的:" + JSON.stringify(wrong));
  // ① 两层,而且只有两层:项目行 + 任务行。
  const bad = shown.filter((r) => r.blocks !== 2);
  expect(bad.length === 0, "每个项目恰好两层(项目行 + 任务行):" + JSON.stringify(bad));
  expect(folders.thirdLine === 0, "那条重复的「文件夹行」没有了 (.conv-folder = " + folders.thirdLine + ")");
  // ② 一样高 —— 一行有多高不再取决于它有没有任务板。
  const hs = shown.map((r) => r.h);
  expect(Math.max(...hs) - Math.min(...hs) <= 1, "所有项目行一样高 (" + JSON.stringify(hs) + ")");
  // ②b 不横向溢出:列表里不该出现横向滚动条,行里也不该有被压在一起的文字。这一条是截图里
  // 那条横条的直接对应物(旧排版把名字 + 状态词 + 时间 + 文件夹名挤在一条线上)。
  expect(folders.listOverflowX <= 1, "项目列表不横向溢出 (scrollWidth - clientWidth = " + folders.listOverflowX + ")");
  const overflow = shown.filter((r) => r.overflowX > 1);
  expect(overflow.length === 0, "没有一行横向溢出:" + JSON.stringify(overflow.map((r) => [r.project, r.overflowX])));
  // ②c 第二行缩进到项目名那一列(±2px):两层之间要有层次,不是两行字一起从左边顶出来。
  const offIndent = shown.filter((r) => Math.abs(r.titleX - r.nameX) > 2);
  expect(offIndent.length === 0,
    "任务行缩进到与项目名同一列:" + JSON.stringify(offIndent.map((r) => [r.project, r.nameX, r.titleX])));
  // ③ 文件夹名只在**和项目名不一样**时才显示:一样时再写一遍就是那行重复的字。
  const mismatched = [];
  for (const r of shown) {
    const g = byPath.get(r.folderPath);
    if (!g) continue;
    const differs = ((g.folder || {}).name || "") !== g.name;
    if (differs !== (r.dirName === (g.folder || {}).name)) {
      mismatched.push({ project: g.name, folder: (g.folder || {}).name, shown: r.dirName });
    }
  }
  expect(mismatched.length === 0, "文件夹名与项目名相同时不再重复显示:" + JSON.stringify(mismatched));
  // ④ 文件夹的操作仍在(收进 hover 了,不是删了),改名仍然只有自己的目录能用。
  //    「进目录」现在挂在**第一行那个文件夹图标**上(用户 2026-09-25:「workbuddy 直接点击文件夹
  //    就可以进入对应的目录」),而它是有前提的:只有 Electron 外壳给了 openPath,它才是个按钮;
  //    没有就该是个 span —— 一个点了没反应的按钮不是入口。(真正点到目录的断言在临时后端那一页,
  //    那边把外壳桩上了。)
  for (const r of shown) {
    const g = byPath.get(r.folderPath);
    expect(r.hasRename && r.hasEmpty, "改名/清空两个操作都还在:" + r.project);
    expect(r.canOpen === folders.shellOpen && (r.canOpen ? r.openTag === "BUTTON" : r.openTag === "SPAN"),
      "文件夹图标跟着外壳走:有外壳=可点 (shell=" + folders.shellOpen + ", button=" + r.canOpen +
      ", tag=" + r.openTag + "):" + r.project);
    expect(r.canRename === !!((g || {}).folder || {}).mine,
      "改名按钮与「是不是自己的目录」一致 (" + r.project + ": mine=" + !!((g || {}).folder || {}).mine + " canRename=" + r.canRename + ")");
  }
  // ⑤ 任务行:每一个项目都有一行(没有任务板时是占位文案),有任务板时带完成数。
  expect(shown.every((r) => String(r.task || "").trim().length > 0),
    "每个项目都写了它在做什么(没有任务时也有占位):" + JSON.stringify(shown.map((r) => r.task)));
  const withCounts = shown.filter((r) => /^\d+\/\d+$/.test(String(r.taskCounts || "").trim()));
  expect(withCounts.length >= 1, "任务行带完成数:" + JSON.stringify(shown.map((r) => r.taskCounts)));
  // ④ 第二行要**说出运行状态**(用户 2026-09-25:「下面是项目重新开始时最新的主要任务,并且显示运行状态」)。
  //    它以前是一个点:有颜色、可读名里也有状态,但没读过的人看不出那是什么意思 —— 而用户要的就是读得出来。
  //    ⚠️ 有任务板的行就**必须**有序,而且只能是这几个词之一(多一个别的词说明状态映射漏了一档)。
  const STATES = ["正在运行", "排队中", "完成", "已失败", "失败", "已停止", "已跳过",
                  "Running now", "Queued", "Done", "Failed", "Stopped", "Skipped"];
  const withBoard = shown.filter((r) => r.hasBoard);
  expect(withBoard.length >= 1, "至少有一个项目已经有任务板(否则这一段等于没量)");
  const badState = withBoard.filter((r) => !STATES.includes((r.taskState || "").trim()));
  expect(badState.length === 0,
    "有任务板的那几行写出了运行状态:" +
    JSON.stringify(shown.map((r) => [r.project, r.taskState])) + " 说不出的:" + JSON.stringify(badState.map((r) => r.project)));
  // 状态词在**右边**那组元信息里:它要是跑到标题前面,标题就会被从「项目名那一列」推开。
  const wrongSide = withBoard.filter((r) => !(r.stateX > r.titleX));
  expect(wrongSide.length === 0,
    "状态词在标题右边,没把标题挤走:" + JSON.stringify(withBoard.map((r) => [r.project, r.titleX, r.stateX])));
  expect(shown.every((r) => !r.hasDot), "那个状态点已经换成词了:" + JSON.stringify(shown.map((r) => [r.project, r.hasDot])));

  // ⑤ 没有任务板的项目,那一行**不能写「还没有任务」** —— 要说它最近做了什么。
  //    用户 2026-09-25 报的就是这个:一个跑过一轮、出过图出过 30 秒视频的项目,左栏写着「还没有任务」
  //    (因为任务板只在「没点名别人」的那一轮里才生成)。
  //    判据从**后端**拿(哪些项目的 task.board 是空的),再逐条对界面上那一行 —— 两边对不上的话,
  //    不管是后端没给还是前端没渲染,这一条都会红。
  const apiGroups = JSON.parse((await apiCall("GET", "/api/groups")).text || "[]");
  const fallbacks = apiGroups.filter((g) => g.task && !g.task.board);
  // ⚠️ 没有这种项目时这一段是空跑(数据会变)。所以先把它**数出来**打进日志,别让它悄悄变成空断言。
  console.log("     (没有任务板、靠「最近做了什么」撑着的项目:" + fallbacks.length + " 个)");
  for (const f of fallbacks) {
    const row = shown.filter((r) => (r.fullName || "") === f.name)[0];
    if (!row) continue;                       // 不在当前筛选/这一屏里,跳过
    expect(!row.hasBoard && !row.taskState && !String(row.taskCounts || "").trim(),
      "没有任务板的行不显示完成数、也不显示状态词 (" + f.name + "):" + JSON.stringify([row.hasBoard, row.taskState, row.taskCounts]));
    expect(row.task && row.task !== "还没有任务",
      "它说的是「最近做了什么」,不是「还没有任务」 (" + f.name + "):" + JSON.stringify(row.task));
  }
  // ③ 名字必须短(用户 2026-09-25 先说 10 个字,当天又收紧:「这个项目名称都太长,限制 8 个字以内,
  //    系统自动使用最核心的关键词命名」)。三件事分开量,因为它们会各自坏掉:
  //      ①**显示**的名字不超过 8 个字(+省略号 = 最多 9 个字符),而且完整名字还在 title 上;
  //      ②名字不越过这一行的右边界(撑满整行就是"压到右边的控件上");
  //      ③名字在**第二行上面**,不和它重叠 —— 这正是"挡住下面功能键"的可量形式。
  const tooLong = shown.filter((r) => (r.shownName || "").length > 9);
  expect(tooLong.length === 0,
    "列表里的项目名都不超过 8 个字+省略号:" + JSON.stringify(tooLong.map((r) => r.shownName)));
  const lostFull = shown.filter((r) => (r.fullName || "").length < (r.shownName || "").replace("…", "").length);
  expect(lostFull.length === 0, "被截断的名字,完整版还在 title 上:" + JSON.stringify(lostFull.map((r) => r.shownName)));
  const overRight = shown.filter((r) => r.nameRight > r.rowRight + 1);
  expect(overRight.length === 0, "名字没有越过行的右边界:" + JSON.stringify(overRight.map((r) => [r.project, r.nameRight, r.rowRight])));
  const overlap = shown.filter((r) => r.nameBottom > r.taskTop + 1);
  expect(overlap.length === 0,
    "名字在第二行上面,不会盖住它(下面的功能键):" + JSON.stringify(overlap.map((r) => [r.project, r.nameBottom, r.taskTop])));
  if (SHOT) await shotTo(shotPath(".sidebar-folders"));

  console.log("— 老项目那些长名字:显示也得短");
  // 上面那一段量的是**现在**这些项目的名字。真正的用户库里还留着建群时按旧规则(前 14 个字)起的名字 ——
  // 它们同样不能把这一行撑满,所以单独造一个长名字的临时项目来量(量完就删,不碰真实项目)。
  {
    const longName = "ZZ 复现:勾选成员后建群,检查他顺便再看一眼长名字会不会挡住功能键";
    const made = await apiCall("POST", "/api/groups", { name: longName, member_ids: [] });
    const gid = made.status === 200 ? JSON.parse(made.text).id : "";
    expect(made.status === 200 && !!gid, "造了一个长名字的临时项目 (" + made.status + ")");
    await send("Page.navigate", { url: "http://127.0.0.1:" + ui + "/" }, sessionId);
    await sleep(7500);
    const rowInfo = await val(`(function () {
      var out = null;
      [].slice.call(document.querySelectorAll('.side-list .conv')).forEach(function (c) {
        var n = c.querySelector('.conv-name');
        if (n && (n.getAttribute('title') || '').indexOf('ZZ ') === 0) {
          var r = n.getBoundingClientRect();
          out = { shown: n.textContent, full: n.getAttribute('title') || '',
                  w: Math.round(r.width), right: Math.round(r.right),
                  rowRight: Math.round(c.getBoundingClientRect().right) };
        }
      });
      return out; })()`);
    expect(rowInfo && rowInfo.shown.length <= 9 && rowInfo.shown.slice(-1) === "…",
      "一个 26 字的名字在列表里只显示 8 个字 + 省略号:" + JSON.stringify(rowInfo));
    expect(rowInfo && rowInfo.full === longName, "完整名字在 title 上:" + JSON.stringify((rowInfo || {}).full));
    expect(rowInfo && rowInfo.right <= rowInfo.rowRight + 1,
      "它也没有越过行的右边界 (" + (rowInfo || {}).right + " ≤ " + (rowInfo || {}).rowRight + ")");

    // ④ 点这一行 → 右侧三个按钮(用户 2026-09-25:「点击项目时,右侧出现三个按钮,一个是归档、
    //    一个是重命名、一个是删除按钮」)。四件事分开量,因为它们会各自坏掉:
    //      ①**三个**都在,而且那个「标记完成」的勾**不在了**(用户:只要那三个,完成不要了);
    //      ②点了就看得见(opacity=1)——「点击项目时出现」的意思就是点完它就在;
    //      ③顺序就是用户说的:归档 / 重命名 / 删除;
    //      ④它们不和名字重叠(盖住名字的尾巴就是又一个"挡住功能键"的变种)。
    await val(`(function () {
      var rows = [].slice.call(document.querySelectorAll('.side-list .conv'));
      for (var i = 0; i < rows.length; i++) {
        var n = rows[i].querySelector('.conv-name');
        if (n && (n.getAttribute('title') || '').indexOf('ZZ ') === 0) {
          (rows[i].querySelector('.conv-main') || n).click(); return 'clicked';
        }
      }
      return 'missing'; })()`);
    await sleep(2600);
    const acts = await val(`(function () {
      var row = null;
      [].slice.call(document.querySelectorAll('.side-list .conv')).forEach(function (c) {
        var n = c.querySelector('.conv-name');
        if (n && (n.getAttribute('title') || '').indexOf('ZZ ') === 0) row = c; });
      if (!row) return { err: 'row gone' };
      var box = row.querySelector('.conv-acts');
      var name = row.querySelector('.conv-name');
      var btns = box ? [].slice.call(box.querySelectorAll('button')) : [];
      return {
        on: row.classList.contains('on'),
        n: btns.length,
        labels: btns.map(function (b) { return b.getAttribute('aria-label') || b.title || ''; }),
        opacity: box ? Number(getComputedStyle(box).opacity) : 0,
        // ⚠️ 「有没有一块加深的方块」= 这块补丁**自己**的底色是不是全透明。别用眼睛判、也别用
        // 「和行底色一样吗」判:半透明黑**叠**在行底色上会更深(--active 8.5% 叠 8.5% ≈ 16%),
        // 看起来就是一块方块 —— 用户 2026-09-25 就是这么发现它的。
        // ⚠️ 这个注释在模板字符串**里面**,不许出现反引号(踩过:提前闭合模板,报的错跟界面毫无关系)。
        bg: box ? getComputedStyle(box).backgroundColor : null,
        // 补丁透明了就盖不住名字,于是名字要在它底下**淡出**(mask)。这是替代品,得一起守。
        mask: (function () {
          var m = row.querySelector('.conv-main'); if (!m) return null;
          var cs = getComputedStyle(m); return cs.webkitMaskImage || cs.maskImage || ''; })(),
        // ⚠️ 量的是**字的范围**,不是名字那个盒子。名字是 flex 子项,盒子会被撑到整行剩下的宽度
        // (实测 216px 而字只有 121px),量盒子会误报「按钮盖住名字」—— 而真正要守的是**字**别被盖住。
        gap: (box && name) ? Math.round((function () {
          var r = document.createRange(); r.selectNodeContents(name);
          return r.getBoundingClientRect().right - box.getBoundingClientRect().left; })()) : null,
        doneBtn: btns.filter(function (b) {
          return /Mark as|标记/.test(b.getAttribute('aria-label') || b.title || ''); }).length
      }; })()`);
    expect(acts.n === 3 && !acts.err, "点过的项目右侧正好三个按钮:" + JSON.stringify(acts.labels));
    expect(acts.doneBtn === 0, "「标记完成」那个勾已经不在了:" + JSON.stringify(acts.labels));
    expect(acts.on === true && acts.opacity === 1,
      "点完不用悬停就看得见 (" + JSON.stringify([acts.on, acts.opacity]) + ")");
    // 用户 2026-09-25:「重命名放在第一,归档放在第二,删除放第三个」。
    expect(/改名|Rename/.test(acts.labels[0]) && /归档|Archive/.test(acts.labels[1]) && /删除|Delete/.test(acts.labels[2]),
      "顺序就是「重命名 / 归档 / 删除」:" + JSON.stringify(acts.labels));
    // 用户 2026-09-25:「出现三个按钮的时候,透明色就行了,不用看起来加深有个方框」。
    const alphaOf = (c) => {
      const m = /rgba?\(([^)]+)\)/.exec(String(c || ""));
      if (!m) return null;
      const p = m[1].split(",").map((x) => Number(x.trim()));
      return p.length < 4 ? 1 : p[3];
    };
    expect(alphaOf(acts.bg) === 0,
      "那三个按钮的补丁没有底色(不再是一块加深的方块):" + JSON.stringify(acts.bg));
    expect(/linear-gradient/.test(String(acts.mask)),
      "补丁透明了,名字改在它底下淡出(mask):" + JSON.stringify(String(acts.mask).slice(0, 72)));
    expect(acts.gap === null || acts.gap <= 0, "三个按钮不盖住名字 (名字右边界 - 补丁左边界 = " + acts.gap + ")");
    if (SHOT) {
      // 这一张是留给「那块方块到底有没有了」的**人眼/视觉模型**复核用的。⚠️ 先把鼠标挪开那三个按钮:
      // 拍到的是按钮自己的 hover 底色的话,那是「可点击」的反馈,跟补丁那块深色方块不是一回事。
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 3, y: 3 }, sessionId);
      await sleep(250);
      await shotTo(shotPath(".conv-acts"));
    }

    // ⑤ 那个「重命名」改的是**这个项目的名字**,而且真的落到后端去(不是只改了界面上的字)。
    //    桩掉 prompt 来点它:顺便验对话框里的**预填值就是当前名字**、以及提示里写明了 8 个字。
    await val(`(function () {
      window.__asked = null;
      window.prompt = function (msg, def) { window.__asked = { msg: msg, def: def }; return 'ZZ 新名字'; };
      return 'stubbed'; })()`);
    const clickedRename = await val(`(function () {
      var b = document.querySelector('.side-list .conv.on .conv-acts button.cv-rename');
      if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
    expect(clickedRename === "clicked", "点到了那个「重命名」(" + clickedRename + ")");
    await sleep(2200);
    const asked = await val(`window.__asked || null`);
    expect(asked && asked.def === longName, "对话框预填的是当前名字:" + JSON.stringify(asked));
    expect(asked && /8/.test(String(asked.msg)), "对话框里写明了 8 个字的上限:" + JSON.stringify(asked && asked.msg));
    const atApi = JSON.parse((await apiCall("GET", "/api/groups")).text || "[]").filter((g) => g.id === gid)[0] || {};
    expect(atApi.name === "ZZ 新名字", "后端那只项目的名字真的改了 (name=" + JSON.stringify(atApi.name) + ")");
    const shownNow = await val(`(function () {
      var n = document.querySelector('.side-list .conv.on .conv-name');
      return n ? { shown: n.textContent, full: n.getAttribute('title') } : null; })()`);
    expect(shownNow && shownNow.shown === "ZZ 新名字",
      "列表里跟着变了 (显示 " + JSON.stringify(shownNow) + ")");

    const gone2 = await apiCall("DELETE", "/api/groups/" + gid);
    expect(gone2.status === 200, "临时项目已删除 (" + gone2.status + ")");
    await send("Page.navigate", { url: "http://127.0.0.1:" + ui + "/" }, sessionId);
    await sleep(7000);
  }

  console.log("— 项目行:折叠成一行(箭头只在当前这个项目上)");
  // 用户 2026-09-25:「项目命名不能太长,后面加一个可折叠的下拉符号,点击这个项目的时候才会出现」。
  // 这一段量四件事:①箭头**只有一行有**(当前打开的那一个);②点它,那一行的第二行真的没了、
  // 整行变矮;③别的行一点没动;④再点一下回得来(行高、第二行都回来)—— **进得去也要出得来**。
  const foldState = () => val(`(function () {
    var rows = [].slice.call(document.querySelectorAll('.side-list .conv'));
    return rows.map(function (c) {
      var b = c.querySelector('.cv-fold');
      return { name: (c.querySelector('.conv-name') || {}).textContent || '',
               active: c.classList.contains('on'),
               chev: !!b, expanded: b ? b.getAttribute('aria-expanded') : null,
               task: !!c.querySelector('.conv-task'),
               h: Math.round(c.getBoundingClientRect().height) }; }); })()`);
  const clickFold = () => val(`(function () {
    var on = document.querySelector('.side-list .conv.on');
    var b = on ? on.querySelector('.cv-fold') : null;
    if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
  // ⚠️ 先把一个项目**打开**:箭头的前提是「这个是当前项目」,而侧栏刚起来时一个都没打开 ——
  // 第一版就是这么红的(0 个箭头),而页面其实是对的。
  await val(`(function () { var c = document.querySelector('.side-list .conv-main'); if (c) c.click(); return 'ok'; })()`);
  await sleep(2600);
  let fl = await foldState();
  const withChev = (fl || []).filter((r) => r.chev);
  expect(withChev.length === 1 && withChev[0] && withChev[0].active,
    "只有当前打开的那一个项目有折叠箭头 (" + withChev.length + " 个:" +
    JSON.stringify(withChev.map((r) => r.name)) + ")");
  expect((fl || []).every((r) => r.task),
    "这一层里每个项目都还有第二行(没折之前)");
  const beforeFold = withChev[0];
  expect(await clickFold() === "clicked", "点了那个箭头");
  await sleep(500);
  let af = (await foldState()).filter((r) => r.name === beforeFold.name)[0];
  expect(af && af.task === false && af.h < beforeFold.h,
    "点一下:第二行收起来了,整行变矮 (" + beforeFold.h + " → " + (af || {}).h + ")");
  expect(af && af.expanded === "false", "箭头自己说收起来了 (aria-expanded=" + (af || {}).expanded + ")");
  const others = (await foldState()).filter((r) => r.name !== beforeFold.name);
  expect(others.every((r) => r.task),
    "别的项目的第二行一点没动 (" + JSON.stringify(others.map((r) => [r.name, r.task])) + ")");
  if (SHOT) await shotTo(shotPath(".sidebar-folded"));
  expect(await clickFold() === "clicked", "再点一下");
  await sleep(500);
  const back2 = (await foldState()).filter((r) => r.name === beforeFold.name)[0];
  expect(back2 && back2.task === true && back2.h === beforeFold.h && back2.expanded === "true",
    "再点一下:展开回来,行高回到原样 (" + JSON.stringify(back2) + " vs " + JSON.stringify(beforeFold) + ")");

  console.log("— 项目筛选(侧栏顶部那一行)");
  let pr = await projects();
  // No row of state tabs in the panel body: the states are the filter button in the top row (their
  // counts live in the panel). The list's own heading is checked in the layout section below.
  expect(pr.tabsLeft === 0, "四个状态标签不像以前那样摊在正文里 (" + pr.tabsLeft + " 个)");
  expect(pr.searchBtns === 1, "侧栏里只有一个查找按钮,在顶部那一行 (" + pr.searchBtns + " 个)");
  // Where the button is: the top row, on one line with the collapse toggle and the search, above the
  // app name. All three are geometry, because "the button exists somewhere" was never the question.
  expect(pr.topBtns.length === 3, "顶部那一行有三个按钮:" + JSON.stringify(pr.topBtns.map((b) => b.label)));
  expect(pr.topBtns.every((b) => b.y === pr.topBtns[0].y),
    "三个按钮在同一行 (y " + JSON.stringify(pr.topBtns.map((b) => b.y)) + ")");
  expect(pr.topBtns.some((b) => /Filter projects|\u7b5b\u9009\u9879\u76ee/.test(b.label)), "其中有筛选按钮");
  const collapseBtn = pr.topBtns.filter((b) => /Collapse sidebar|Expand the sidebar/.test(b.label))[0];
  expect(collapseBtn && collapseBtn.x <= Math.min(...pr.topBtns.map((b) => b.x)),
    "收缩开关仍在这一行最左边 (x " + (collapseBtn ? collapseBtn.x : "?") + ")");
  expect(pr.rowAboveName === true, "这一行在 Team Agent 名字上方");
  expect(pr.topBtns.some((b) => /Filter projects|\u7b5b\u9009\u9879\u76ee/.test(b.label) && b.on),
    "有筛选生效时按钮是高亮态");

  // The four states live in the panel the button opens.
  let fp = await openFilter();
  expect(fp.open && fp.states.length === 4, "筛选面板里是四个状态:" + JSON.stringify((fp.states || []).map((x) => x.label)));
  expect(/\u6b63\u5728\u8fd0\u884c|Running now/.test(fp.states[0].label), "第一项是「正在运行」:" + fp.states[0].label);
  expect(!fp.states[0].on, "默认不勾「正在运行」(它不是项目的归属状态)");
  expect(fp.states[1].on && /\u8fdb\u884c\u4e2d|In progress/.test(fp.states[1].label),
    "默认勾「进行中」:" + JSON.stringify(fp.states.map((x) => x.label + (x.on ? "*" : ""))));
  expect(fp.belowRow && fp.inWindow, "面板开在这一行下方、完整落在窗口内");
  // 一行图标:五个芯片的 y 一致,而且整块面板只有一行的高度 —— 旧的竖排菜单比它过滤的列表还高。
  expect(fp.oneRow === true, "五个筛选项排成一行 (y " + JSON.stringify((fp.states || []).map((x) => x.y)) + ")");
  expect(fp.panelH > 0 && fp.panelH <= 48, "筛选面板只有一行高 (" + fp.panelH + "px)");
  if (SHOT) await shotTo(shotPath(".sidebar-filter"));   // the panel is on screen right here
  expect(fp.all && !fp.all.on, "「全部项目」没被勾上(勾上它才是不过滤)");
  expect(pr.rows.length === fp.states[1].n,
    "「进行中」的数字 = 列表条数 (" + fp.states[1].n + " vs " + pr.rows.length + ")");

  // The row-level dot and the panel count must be the same fact, both ways.
  const rowsTotal = await val(`(async function () {
    var r = await fetch('http://127.0.0.1:8765/api/groups', { headers: { 'X-Team-Agent-Token': (window.teamAgent || {}).token || '' } });
    var gs = await r.json();
    return { total: gs.length, busy: gs.filter(function (g) { return g.busy; }).length }; })()`);
  fp = await onlyState(0);                       // 只看「正在运行」
  pr = await projects();
  expect(pr.rows.length === fp.states[0].n, "「正在运行」的数字 = 它显示的条数 (" + fp.states[0].n + " vs " + pr.rows.length + ")");
  expect(fp.states[0].n === rowsTotal.busy, "「正在运行」的数字 = 后端正忙的项目数 (" + fp.states[0].n + " vs " + rowsTotal.busy + ")");
  expect(pr.rows.every((r) => !r.archived), "正在运行的列表里不含已归档的项目");
  // The three reaching states account for every project; running is on top of them.
  const summed = fp.states.slice(1).reduce((a, t) => a + (t.n || 0), 0);
  expect(summed === rowsTotal.total, "进行中+已完成+已归档 = 项目总数 (" + summed + " vs " + rowsTotal.total + ")");
  expect(fp.all.n === rowsTotal.total, "「全部项目」的数字 = 总数 (" + fp.all.n + " vs " + rowsTotal.total + ")");

  // Nothing ticked means every project, archived ones included — the thing a row of tabs could not say.
  await tickRow(0);
  fp = await filterPanel();
  pr = await projects();
  expect(pr.rows.length === rowsTotal.total, "勾「全部项目」= 列出全部(含归档) (" + pr.rows.length + " vs " + rowsTotal.total + ")");
  expect(pr.rows.filter((r) => r.archived).length === fp.states[3].n,
    "「全部项目」里含全部已归档的项目 (" + fp.states[3].n + " 个)");

  // Back to the default, then the archived-only view.
  fp = await onlyState(1);
  pr = await projects();
  // 状态不再是行上的一个词,而是行首一个点 —— 所以断言的是「那个点在,而且带可读的状态名」:
  // 屏幕阅读器读到的状态和视觉上那个点必须是同一件事。
  expect(pr.rows.every((r) => r.state && !r.archived), "默认列表都是未归档的,每行都有一个带可读名的状态点");
  fp = await onlyState(3);
  pr = await projects();
  expect(pr.rows.length === fp.states[3].n, "「已归档」的数字 = 它显示的条数 (" + fp.states[3].n + " vs " + pr.rows.length + ")");
  expect(pr.rows.every((r) => r.archived), "只勾「已归档」时列表里没有未归档的项目");
  await onlyAndClose(1);                          // 回到默认(只看「进行中」)

  // ---------------------------------------------------------------- 搜索框:窗口正中间、可拖动
  // The user asked for this in place of the field that used to unfold inside the sidebar: "a separate
  // search box in the middle of the screen that can be moved". Asserted as geometry (it really is
  // centred, and it stays where it is dropped), as behaviour (the sidebar's list must NOT move while
  // you type in it — that is the whole point of taking it out) and for the promise archiving makes
  // (a query still reaches a filed-away project).
  console.log("— 搜索框(浮在窗口正中间、可拖动)");
  expect((await searchPal()).inlineFields === 0, "侧栏里已经没有内嵌的搜索框");
  await openSearch();
  let pal = await searchPal();
  expect(pal.open && pal.backdrop, "点查找后出现浮层搜索框");
  expect(pal.centred, "开在屏幕正中间 (中心 " + pal.cx + "," + pal.cy + " / 窗口 " + Math.round(pal.vw / 2) + "," + Math.round(pal.vh / 2) + ")");
  expect(pal.inWindow, "完整落在窗口内");
  expect(pal.headCursor === "move", "标题栏显示可拖动 (" + pal.headCursor + ")");
  expect(pal.rows.length > 0, "没输入时列出当前项目 (" + pal.rows.length + " 条)");
  if (SHOT) await shotTo(shotPath(".search"));

  // 拖动:按住标题栏左侧的把手往右下拖,松手后应停在那里
  const was = pal.rect;
  const grabX = was[0] + 10, grabY = was[1] + 20;
  await mouse("mousePressed", grabX, grabY);
  await sleep(150);
  const pinned = (await searchPal()).rect;             // 按下时会先钉在原地(不能跳)
  await mouse("mouseMoved", grabX + 90, grabY + 60);
  await sleep(120);
  const mid = (await searchPal()).rect;
  await mouse("mouseMoved", grabX + 180, grabY + 120);
  await sleep(120);
  const dragged = (await searchPal()).rect;
  await mouse("mouseReleased", grabX + 180, grabY + 120);
  await sleep(300);
  const moved = await searchPal();
  expect(Math.abs(pinned[0] - was[0]) <= 2 && Math.abs(pinned[1] - was[1]) <= 2,
    "按下的瞬间不跳 (起点 " + was.join(",") + " → 按下后 " + pinned.join(",") + ")");
  expect(Math.abs(moved.rect[0] - (was[0] + 180)) <= 5 && Math.abs(moved.rect[1] - (was[1] + 120)) <= 5,
    "拖到哪就停在哪 (起点 " + was.join(",") + " → 中途 " + mid.join(",") + " → 拖到 " + dragged.join(",") + " → 松手 " + moved.rect.join(",") + ")");
  expect(!moved.centred && moved.inWindow, "拖过以后不再居中,也没有跑出窗口");
  if (SHOT) await shotTo(shotPath(".search-moved"));

  // 打字只收窄搜索框里的结果,侧栏那份列表一步都不动
  const sideBefore = (await projects()).rows.length;
  const needle = String(moved.rows[0].name).slice(0, 2);
  await searchBox(needle);
  await sleep(500);
  const narrowed = await searchPal();
  expect(narrowed.rows.length > 0 && narrowed.rows.length <= moved.rows.length,
    "输入后结果被收窄 (" + moved.rows.length + " → " + narrowed.rows.length + ")");
  // ⚠️ 原来断言「**每一条**结果都含关键字」。这是个**搜索框从未承诺过**的性质：它匹配的不只是项目名
  // （还有任务板摘要、最近发言），所以按名字取的关键字当然可能出现在一条名字不含它的结果里 ——
  // 于是这条**常驻变红**。用户真正要的是「我打了名字的一截，那个项目还在结果里」。
  expect(narrowed.rows.some((r) => String(r.name).includes(needle)),
    "按关键字找的那个项目还在结果里 (关键字「" + needle + "」→ " +
    JSON.stringify(narrowed.rows.map((r) => r.name)) + ")");
  expect((await projects()).rows.length === sideBefore,
    "侧栏列表不因为搜索框里打字而变 (" + sideBefore + " → " + (await projects()).rows.length + ")");

  // 键盘:↓ 移光标,回车打开选中的那个项目。光标会跟着鼠标走(onMouseEnter),所以先把鼠标
  // 从列表上挪开,否则「往下移一行」会从光标所在那行开始算。
  await searchBox("");
  await sleep(400);
  await hoverAt(moved.rect[0] + 250, moved.rect[1] + 18);
  await sleep(200);
  const selBefore = (await searchPal()).rows.findIndex((r) => r.sel);
  await val(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })) && 'sent'`);
  await sleep(250);
  const pal2 = await searchPal();
  const selIdx = pal2.rows.findIndex((r) => r.sel);
  expect(selIdx === Math.min(selBefore + 1, pal2.rows.length - 1),
    "↓ 把选中项往下移一行 (" + selBefore + " → " + selIdx + " / 共 " + pal2.rows.length + " 行)");
  const target = pal2.rows[selIdx].name;
  await val(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })) && 'sent'`);
  await sleep(2000);
  const opened = await val(`(function () {
    var t = document.querySelector('.chat-title');
    return { chat: !!document.querySelector('.chat'), title: t ? t.textContent : '' }; })()`);
  expect(!(await searchPal()).open, "回车之后搜索框自己关掉");
  expect(opened.chat && String(opened.title).includes(target),
    "回车打开了选中的项目 (期望「" + target + "」,实际「" + opened.title + "」)");

  // 点框外面关掉
  await openSearch();
  await mouse("mousePressed", 24, 24);
  await mouse("mouseReleased", 24, 24);
  await sleep(400);
  expect(!(await searchPal()).open, "点搜索框外面能关掉");
  await openSearch();

  await esc();
  await sleep(300);
  expect(!(await searchPal()).open, "Esc 也能关掉");
  if (SHOT) await shotTo(shotPath(".search-closed"));

  console.log("— 侧栏排布:导航在上、项目列表在下");
  // 用户的原话:「项目任务都在侧栏下面,现在(原来)下面的成员工具都在侧栏上面」。
  // 所以这里断言的不是「元素在不在」,而是两块的**上下顺序**(rect 比较)与列表能滚。
  let lz = await layout();
  expect(!!lz.section && /\u9879\u76ee|Projects/.test(String(lz.section)), "项目列表有标题:" + lz.section);
  expect(/\(\d+\)/.test(String(lz.section)), "标题上带数量:" + lz.section);
  expect(lz.sectionCount === lz.rows, "标题上的数字 = 列表条数 (" + lz.sectionCount + " vs " + lz.rows + ")");
  expect(lz.navBottom !== null && lz.sectionTop >= lz.navBottom - 1,
    "项目标题在「工具」那组下面 (标题 top " + lz.sectionTop + " >= 工具组 bottom " + lz.navBottom + ")");
  expect(lz.listTop >= lz.sectionTop && lz.listBottom <= lz.userTop + 1,
    "列表在标题下面、用户行上面 (" + lz.listTop + " / " + lz.listBottom + " vs 用户行 " + lz.userTop + ")");
  expect(lz.listOverflow === "auto", "列表自己滚 (" + lz.listOverflow + ")");
  if (SHOT) await shotTo(shotPath(".sidebar-order"));
  // 标题点一下能折起来,再点一下回来
  await click(".sidebar .side-section");
  await sleep(500);
  expect((await layout()).rows === 0, "点标题能把列表折起来");
  await click(".sidebar .side-section");
  await sleep(500);
  const back = await layout();
  expect(back.rows === lz.rows, "再点一下列表回来了 (" + back.rows + " 条)");

  console.log("— 归档与搜索(用临时项目,不碰真实项目)");
  const name = "ZZ archive check";
  const made = await apiCall("POST", "/api/groups", { name: name, member_ids: [] });
  const gid = made.status === 200 ? JSON.parse(made.text).id : "";
  expect(made.status === 200 && !!gid, "建了一个临时项目 (" + made.status + ")");
  const arch = await apiCall("PATCH", "/api/groups/" + gid, { status: "done", archived: true });
  expect(arch.status === 200, "归档请求成功 (" + arch.status + ")");

  // Reload the page rather than nudging a nav item: the list has to be read the way the app reads it
  // on a cold start, and nothing else guarantees `useData` refetches after an API call made outside
  // the app. (The token is injected on every new document, so a reload stays authenticated.)
  await send("Page.navigate", { url: "http://127.0.0.1:" + ui + "/" }, sessionId);
  await sleep(7500);
  const archPanel = await onlyAndClose(3);      // 只看「已归档」
  pr = await projects();
  expect(pr.rows.some((r) => (r.full || r.name) === name),
    "归档后的项目出现在「已归档」里(按完整名字比 —— 列表里显示的是截断版):" +
    JSON.stringify(pr.rows.map((r) => [r.name, r.full])));
  expect(archPanel.states[3].n === pr.rows.length, "归档计数跟着变 (" + archPanel.states[3].n + " vs " + pr.rows.length + ")");

  // A search has to find it: that is the promise archiving makes.
  await openSearch();
  await searchBox("ZZ archive");
  await sleep(700);
  const hitArchived = await searchPal();
  expect(hitArchived.rows.some((r) => r.name === name && /\u5df2\u5f52\u6863|Archived/.test(r.state || "")),
    "搜索框能搜到已归档的项目(" + hitArchived.rows.length + " 条命中)");
  await esc();
  await sleep(300);

  const gone = await apiCall("DELETE", "/api/groups/" + gid);
  expect(gone.status === 200, "临时项目已删除 (" + gone.status + ")");
  await send("Page.navigate", { url: "http://127.0.0.1:" + ui + "/" }, sessionId);
  await sleep(7500);                     // back to a clean list for the checks that follow

  console.log("— 左栏顶部:按钮在上、名字在下");
  let tb = await topBar();
  expect(!tb.missing, "左栏和它的顶部那一行都在");
  const tgOpen = tb.toggle;
  if (!tb.missing) {
    expect(tb.buttons.length === 3, "顶部这一行是三个按钮(收缩/筛选/查找):" + JSON.stringify(tb.buttons));
    expect(tb.buttonsAboveName === true,
      "按钮整行在「" + tb.name + "」的上方 (按钮行底 <= 名字行顶)");
    expect(tb.nameRowVisible, "展开时名字行是显示的");
    expect(tb.overflowRight <= -8, "最后一个按钮不越出左栏右边界 (溢出 " + tb.overflowRight + "px)");
    // 顶部这一块的高度是定值 52:左边是成员栏的头部、右边是聊天头部,两个都是 52 且带下边框,
    // 所以三栏的第一条横线必须落在同一条线上(它们真的对上,见下面成员栏那一节)。
    expect(tb.rowH === 52, "顶部块高 52,和旁边两栏的头部一样 (实测 " + tb.rowH + "px)");
    // 三个按钮等距:间距是量出来的,不是看出来的。
    expect(tb.btnGaps.length === 2 && tb.btnGaps[0] === tb.btnGaps[1] && tb.btnGaps[0] >= 5,
      "三个按钮等距且留出呼吸 (实测间距 " + JSON.stringify(tb.btnGaps) + "px)");
    // 按钮行和名字之间要有间距,否则两行粘在一起。
    expect(tb.btnAboveBrandGap !== null && tb.btnAboveBrandGap >= 10,
      "按钮行与名字之间有间距 (实测 " + tb.btnAboveBrandGap + "px)");
    expect(tb.logoBeforeName === true, "logo 在名字左边");
    expect(tb.nameCentreOff !== null && Math.abs(tb.nameCentreOff) <= 2,
      "logo+名字这一组在左栏里居中 (偏心 " + tb.nameCentreOff + "px)");
    expect(!!tb.version && /^v\d+\.\d+/.test(tb.version), "名字旁边有版本号:" + tb.version);
    // 同一个 mark:侧栏和中间面板顶上那一块画的必须是同一组节点(见 BrandMark.tsx)。
    expect(tb.mark && tb.mark.parts.length === 4,
      "侧栏那个 mark 是一块底 + 三个节点(实测 " + (tb.mark ? tb.mark.parts.length : 0) + " 个形状)");
    expect(tb.heroMark && tb.heroMark.parts.length === 4,
      "中间面板顶部也是同一套(实测 " + (tb.heroMark ? tb.heroMark.parts.length : 0) + " 个形状)");
    // 形状、坐标、实际画出来的颜色全都要一样 —— 只比节点坐标是不够的:两边都「有三个点」时,
    // 一边是描边、一边是实心底,这种差别正是用户看到的那一个。
    expect(tb.mark && tb.heroMark && JSON.stringify(tb.mark.parts) === JSON.stringify(tb.heroMark.parts),
      "两处画的是同一张图(形状+坐标+颜色):" + JSON.stringify([tb.mark && tb.mark.parts, tb.heroMark && tb.heroMark.parts]));
    expect(tb.mark && tb.heroMark && tb.mark.box.join() !== tb.heroMark.box.join(),
      "允许不同的只有大小 (侧栏 " + JSON.stringify(tb.mark && tb.mark.box) +
      " / 中间 " + JSON.stringify(tb.heroMark && tb.heroMark.box) + ")");
    expect(tb.versionOnNameLine === true && tb.versionAfterName === true,
      "版本号与名字同一行、且在名字右边 (同行:" + tb.versionOnNameLine + " 在右:" + tb.versionAfterName + ")");
    expect(tb.newChatEntries === 1, "「新建群聊」只有一处入口(实测 " + tb.newChatEntries + " 处)");
    expect(tb.floatingExpandBtn === 0, "主区域左上角没有那个浮层展开按钮(" + tb.floatingExpandBtn + " 个)");
  }

  console.log("— 收起侧栏:同一个按钮、同一个位置");
  // By aria-label, not by text: the button's content is an icon, so textContent is empty.
  const collapsedIt = await val(`(function () {
    var b = document.querySelector('.sidebar .side-top button[aria-label="Collapse sidebar"]')
         || document.querySelector('.sidebar .side-top button');
    if (!b) return 'missing'; b.click(); return 'clicked: ' + b.getAttribute('aria-label'); })()`);
  await sleep(900);
  expect(collapsedIt.indexOf("clicked") === 0, "点到了收起按钮 (" + collapsedIt + ")");
  const tc = await topBar();
  expect(tc.collapsed, "侧栏进入收起状态");
  expect(!tc.nameRowVisible, "名字行随内容一起收起");
  expect(tc.buttons.length === 1, "收起后只剩一个按钮:" + JSON.stringify(tc.buttons));
  // Fully retracted, not a strip: the panel is gone and the main area starts at the window's edge.
  expect(tc.sideW === 0, "侧栏完全收回,没有留下一条窄栏 (宽度 " + tc.sideW + ")");
  expect(tc.sideRight === 0 && tc.mainLeft === 0,
    "主区域从窗口左缘开始 (侧栏右 " + tc.sideRight + ", 主区域左 " + tc.mainLeft + ")");
  // …and the control that did it hugs the edge and has not moved.
  expect(tc.toggle && tc.toggle.x === tgOpen.x && tc.toggle.y === tgOpen.y,
    "按钮没有移动 (展开 " + JSON.stringify(tgOpen) + " → 收起 " + JSON.stringify(tc.toggle) + ")");
  expect(tc.toggle && tc.toggle.x <= 20, "按钮靠窗口左边缘 (x=" + (tc.toggle ? tc.toggle.x : "?") + ")");
  // Clicking where the button is — which is where it was a moment ago — has to bring the sidebar back.
  const samePlace = await val(`(function () {
    var b = document.querySelector('.sidebar .side-top button');
    var r = b.getBoundingClientRect();
    var el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    var hit = el && el.closest ? el.closest('button') : null;
    if (hit !== b) return 'hit: ' + (hit ? hit.getAttribute('aria-label') : (el ? el.className : 'nothing'));
    b.click(); return 'clicked'; })()`);
  await sleep(900);
  const td = await topBar();
  expect(samePlace === "clicked" && !td.collapsed, "在同一个位置再点一次就回来了 (" + samePlace + ")");
  expect(td.sideW === tb.sideW, "侧栏宽度回到原样 (" + td.sideW + ")");
  expect(td.nameRowVisible, "名字行也回来了");

  console.log("— 首页,未打开任何群聊");
  let s = await state();
  expect(s.sidebarMemberLists === 0, "左栏里没有任何成员列表");
  expect(s.railPresent && s.railEmptyHint, "成员栏在,并说明还没有打开群聊");
  // ⚠️ 文案随界面改过（"Every member…" → "All members and tools…"），断言却写死了旧的 —— 又是一条
  // **常驻变红**。要守的是「底部有入口，而且它开的是全应用成员页」，不是它印哪几个字。
  expect(/Every member|All members|全部成员/i.test(s.railFoot || ""),
    "成员栏底部有「全部成员…」入口:" + JSON.stringify(s.railFoot));

  // 这一段守的是「新建群聊时勾了成员、那些成员却没入群」这条链上最硬的一处断裂。
  // 三件事都是实测出来的,不是设想的:
  //   ① 面板锚在输入框上方向上展开,34 个候选时有 368px 高、而输入框顶边在 y=306 —— 面板顶部
  //      跑到 y=-62,它自己的表头和第一行名字在窗口外,点都点不到;
  //   ② 候选里约四成不是「人」而是工具(生成模型 9 个 + 外部智能体 4 个),它们不能带队;而勾选的
  //      第一个原本会被当成群主 → 后端 400 → 整个建群失败。用户看到的就是「我选的成员没入群」;
  //   ③ 前端标的「不带队」必须与后端 `may_host` 一模一样 —— 这条判据只能有一处,抄一份到
  //      TypeScript 里迟早会和 API 的答案分家。
  // 只开面板、只勾选,不发消息,所以真实项目不受影响。
  console.log("— 首页选成员:谁能带队、面板不得长出窗口");
  await clickByText(".sidebar .nav-item", "/\\u65b0\\u5efa\\u7fa4\\u804a|New group chat/");
  await sleep(1300);
  await val(`(function () { var b = document.querySelectorAll('.hsetup .wspick-btn')[1]; if (b) b.click(); return 'ok'; })()`);
  await sleep(900);
  const pn = await val(`(function () {
    var p = document.querySelector('.wspick-pop');
    if (!p) return { open: false };
    var ms = [].slice.call(p.querySelectorAll('.ng-mem'));
    var body = p.querySelector('.wspick-body');
    var r = p.getBoundingClientRect();
    return { open: true, n: ms.length, solo: ms.filter(function (m) { return m.classList.contains('solo'); }).length,
             top: Math.round(r.top), bottom: Math.round(r.bottom), winH: innerHeight,
             bodyH: body ? Math.round(body.getBoundingClientRect().height) : 0 };
  })()`);
  expect(pn.open && pn.n > 0, "首页能打开成员面板 (" + pn.n + " 个候选)");
  expect(pn.top >= 0, "面板没有长出窗口顶部 (top=" + pn.top + ")");
  expect(pn.bottom <= pn.winH, "面板底部也在窗口内 (bottom=" + pn.bottom + " ≤ " + pn.winH + ")");
  expect(pn.solo > 0, "候选里标出了「不带队」的成员 (" + pn.solo + " 个)");
  // 滚到底之后最后一名要够得到 —— 否则「看得到的才选得到」就等于「有成员永远选不到」。
  const reach = await val(`(function () {
    var p = document.querySelector('.wspick-pop'); if (!p) return null;
    var body = p.querySelector('.wspick-body'); if (!body) return null;
    body.scrollTop = body.scrollHeight;
    var ms = [].slice.call(body.querySelectorAll('.ng-mem'));
    var last = ms[ms.length - 1]; if (!last) return null;
    var pr = p.getBoundingClientRect(), lr = last.getBoundingClientRect();
    return { last: last.textContent.trim().slice(0, 14), lastGap: Math.round(lr.bottom - pr.bottom),
             lastVisible: lr.bottom <= pr.bottom + 1 && lr.top >= pr.top - 1 };
  })()`);
  expect(reach && reach.lastVisible, "滚到底之后最后一名成员也够得到 (" + JSON.stringify(reach) + ")");
  // 前端标记的集合必须等于后端 `may_host === false` 的集合(生成成员与外部智能体)。
  const agentsApi = await val(`(async function () {
    var r = await fetch(${JSON.stringify(APP)} + '/api/agents',
      { headers: { 'X-Team-Agent-Token': (window.teamAgent || {}).token || '' } });
    return await r.json();
  })()`);
  const cannotHost = (agentsApi || []).filter((a) => a.may_host === false && a.origin !== "model").length;
  expect(agentsApi && agentsApi.length > 0 && typeof agentsApi[0].may_host === "boolean",
    "后端每个成员都回答了「能不能带队」");
  expect(pn.solo === cannotHost, "标为「不带队」的数量 = 后端说不能带队的人数 (" + pn.solo + " vs " + cannotHost + ")");
  // 勾上之后 chip 上必须出现一个**显式的移除键**。点整块 chip 确实也能取消，但一个只能靠"再点一下"
  // 撤销的勾，就是没有撤销。
  // ⚠️ 必须**分开三拍**：点 → 等 React 重渲染 → 再读。React 的更新是异步批处理的，在同一个同步块里
  // 紧接着 `.click()` 读 `classList` 拿到的是**旧 DOM**（我先写成那样，三条断言全红，看起来像功能没做）。
  const pick = await val(`(function () {
    var p = document.querySelector('.wspick-pop'); if (!p) return { err: 'no panel' };
    var first = [].slice.call(p.querySelectorAll('.ng-mem')).filter(function (m) {
      return !m.classList.contains('on'); })[0];
    if (!first) return { err: 'no member' };
    first.setAttribute('data-smoke-pick', '1');
    first.click();
    return { name: first.textContent.trim().slice(0, 12) };
  })()`);
  await sleep(400);
  const ticked = await val(`(function () {
    var m = document.querySelector('.ng-mem[data-smoke-pick="1"]'); if (!m) return { err: 'gone' };
    var x = m.querySelector('.ng-x');
    var b = x ? x.getBoundingClientRect() : null;
    return { on: m.classList.contains('on'), hasX: !!x,
             w: b ? Math.round(b.width) : 0, h: b ? Math.round(b.height) : 0 };
  })()`);
  expect(ticked.on, "成员被勾上 (" + (pick.name || "") + ")");
  expect(ticked.hasX, "勾选的成员上有显式移除键 (.ng-x)");
  expect(ticked.w >= 12 && ticked.h >= 12, "移除键点得到 (" + ticked.w + "×" + ticked.h + "px)");
  await val(`(function () {
    var m = document.querySelector('.ng-mem[data-smoke-pick="1"]');
    var x = m && m.querySelector('.ng-x'); if (x) x.click(); return 'ok';
  })()`);
  await sleep(400);
  const unticked = await val(`(function () {
    var m = document.querySelector('.ng-mem[data-smoke-pick="1"]');
    if (!m) return { err: 'gone' };
    var r = { on: m.classList.contains('on'), hasX: !!m.querySelector('.ng-x') };
    m.removeAttribute('data-smoke-pick');
    return r;
  })()`);
  expect(!unticked.on && !unticked.hasX, "点它就把该成员移出勾选 (" + JSON.stringify(unticked) + ")");
  if (SHOT) await shotTo(shotPath(".home-members"));
  await val(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
  await sleep(400);
  const closed = await val(`!document.querySelector('.wspick-pop')`);
  expect(closed === true, "Esc 关掉面板");

  console.log("— 打开一个群聊");
  await click(".sidebar .conv-main");
  await sleep(3000);
  s = await state();
  expect(s.sidebarMemberLists === 0, "左栏里仍然没有任何成员列表");
  expect(s.railPresent && s.railCards > 0, "成员栏列出了本群成员 (" + s.railCards + " 个)");
  expect(s.railGapFromSidebar === 0, "成员栏紧贴左栏右侧 (相对位置 " + s.railGapFromSidebar + ")");
  expect(s.titleCount !== null && s.titleCount > 0, "群名旁写着成员数 (" + s.titleCount + ")");
  expect(s.titleCount === s.railCards, "群名旁的数字 = 栏里的卡片数 (" + s.titleCount + " vs " + s.railCards + ")");
  expect(/·\s*\d+/.test(String(s.railSub)), "成员栏自己写明是「本群成员 · n」:" + s.railSub);
  expect(s.headerFaces === 0, "聊天头部已没有那排成员头像 (" + s.headerFaces + " 个)");
  expect(s.headerButtons > 0, "聊天头部仍保留它自己的动作图标 (" + s.headerButtons + " 个)");
  // The column is a real column, so the conversation sits to its right — not underneath it.
  expect(s.chatX === s.sideW + s.railW,
    "聊天区紧贴成员栏右侧 (" + s.chatX + " = " + s.sideW + " + " + s.railW + ")");
  expect(Math.abs(s.chatW - s.mainW) <= 2,
    "聊天区占满主区域 (聊天 " + s.chatW + " / 主区域 " + s.mainW + ")");

  console.log("— 用左栏「成员」那一项开合成员栏");
  await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
  await sleep(1200);
  s = await state();
  expect(!s.railPresent, "点一下收起成员栏");
  expect(s.chatX === s.sideW, "收起后聊天区紧贴左栏 (" + s.chatX + " = " + s.sideW + ")");
  expect(s.chatW === s.mainW, "收起后聊天区仍是满宽 (" + s.chatW + ")");
  await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
  await sleep(1600);
  s = await state();
  expect(s.railPresent && s.railCards > 0, "再点一下展开 (" + s.railCards + " 张卡片)");
  expect(s.sideItemCount === s.railCards,
    "左栏项上的数字 = 栏里的卡片数 (" + s.sideItemCount + " vs " + s.railCards + ")");
  if (s.railScrolls) {
    expect(s.railScrolls.overflowY === "auto", "成员栏的列表能滚 (overflow-y " + s.railScrolls.overflowY + ")");
  }
  // 三栏的头部下沿必须落在同一条线上 —— 这是「侧栏顶部和中间块同等高度」那条要求的落点，
  // 而它只有三栏同时在屏幕上时才量得到（所以放在这里，不放在打开群聊之前）。
  const align = await topBar();
  expect(align.railHeadBottom !== null && align.chatHeadBottom !== null &&
         align.rowBottom === align.railHeadBottom && align.rowBottom === align.chatHeadBottom,
    "左栏/成员栏/主区域三栏头部下沿同线 (左栏 " + align.rowBottom + " / 成员栏 " +
    align.railHeadBottom + " / 聊天 " + align.chatHeadBottom + ")");

  console.log("— 用成员栏自己的收起按钮");
  // By aria-label, not by position: the header also holds the add-member button.
  const hidden = await val(`(function () {
    var b = [].filter.call(document.querySelectorAll('.mrail-head button'), function (x) {
      return /column|\\u6210\\u5458\\u680f/.test(x.getAttribute('aria-label') || ''); })[0];
    if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
  await sleep(900);
  s = await state();
  expect(hidden === "clicked" && !s.railPresent, "栏里的收起按钮也能关掉");

  console.log("— 成员栏里的「全部成员…」");
  await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
  await sleep(1600);
  await click(".mrail-foot button");
  await sleep(2600);
  s = await state();
  expect(s.memberPage && s.memberPageItems > 0, "打开了全应用成员页 (" + s.memberPageItems + " 个成员)");
  expect(s.sidebarMemberLists === 0, "即使在这一页,左栏也没有第二份成员列表");

  console.log("— 成员栏:只剩「成员」这一件事");
  // ⚠️ 2026-09-25 用户的原话是「**成果不适放在这边**,像 WorkBuddy 一样,把输出结果放在**中间框的
  // 右侧栏**里」。所以这一段量的是「这里不再有成果」:那一栏原来有「成员 / 成果」两个页签,现在
  // 只剩成员一件事,页签整个消失。**成果栏自己的断言在临时后端那一节**(那边的工作目录里一定有
  // 真文件:一张 png 和一段 wav),因为它必须验到「点一条就在这里看得见内容」。
  // 这一段只关心成员栏,所以先把筛选清成「全部」并进第一个项目 —— 否则「当前项目」这句话没有对象。
  await openFilter();
  await tickRow(0);                      // 0 = 全部项目
  await click(".side-top .filter-btn");  // 关掉弹层,别挡住列表
  await sleep(500);
  await val(`(function () { var c = document.querySelector('.conv-main'); if (c) c.click(); return 'ok'; })()`);
  await sleep(2600);
  // ⚠️ 成员栏这时应该是开着的(上一段刚用过它)。**不要无条件点侧栏那个「成员」入口** ——
  // 它是「已经开着再点一下 = 收起」的开关,无条件点会把整栏关掉,下面全部断言就红了。
  const railOpen = await val(`!!document.querySelector('.mrail')`);
  if (!railOpen) {
    await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
    await sleep(1600);
  }
  const railOnly = () => val(`(function () {
    var rail = document.querySelector('.mrail'), chat = document.querySelector('.chat');
    var main = document.querySelector('.main');
    var body = rail ? rail.querySelector('.mrail-body') : null;
    var mx = document.querySelector('.mrail .rail-max');
    var mr = mx ? mx.getBoundingClientRect() : null;
    return {
      present: !!rail,
      tabs: document.querySelectorAll('.mrail-tab').length,
      outputsHere: document.querySelectorAll('.mrail .out-row, .mrail .op-head').length,
      title: rail ? ((rail.querySelector('.mrail-title') || {}).textContent || '') : '',
      sub: rail ? ((rail.querySelector('.mrail-sub') || {}).textContent || '') : '',
      cards: rail ? rail.querySelectorAll('.mc').length : 0,
      foot: rail ? !!rail.querySelector('.mrail-foot button') : false,
      railRight: rail ? Math.round(rail.getBoundingClientRect().right) : null,
      chatLeft: chat ? Math.round(chat.getBoundingClientRect().left) : null,
      maxBtn: mr ? { x: Math.round(mr.left), y: Math.round(mr.top) } : null,
      mainVisible: !!(main && main.getBoundingClientRect().width > 0),
      winW: innerWidth,
      overflowX: body ? (body.scrollWidth - body.clientWidth) : 0
    }; })()`);
  let rf = await railOnly();
  expect(rf.present, "成员栏开着");
  console.log("— 四条栏头的第一行文字必须在同一条水平线上");
  // 用户 2026-09-25:「这两个地方的字体不在一个平面上,需要调整到和中间栏同等高度」。
  // 「一个平面」只能量:**每栏标题第一行文字的水平中线**必须相同(以聊天栏为基准)。量的是文字
  // 自己的盒子,不是它所在的头部 —— 头部等高(都是 52px)并不代表**字**在同一条线上:两行标题
  // (标题 + 副标题)作为一个块垂直居中时,它的第一行会比单行标题更靠上。这正是用户看到的东西。
  const headLines = () => val(`(function () {
    var box = function (sel) { var e = document.querySelector(sel); if (!e) return null;
      var r = e.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom),
               mid: Math.round((r.top + r.bottom) / 2), h: Math.round(r.height),
               text: (e.textContent || '').trim().slice(0, 10) }; };
    return { brand: box('.brand-name'), band: box('.side-top'),
             railTitle: box('.mrail-title'), railSub: box('.mrail-sub'),
             chatTitle: box('.chat-title'),
             outTitle: box('.outpanel .op-title'), outSub: box('.outpanel .op-sub') }; })()`);
  const sameLine = (a, b) => !!(a && b) && Math.abs(a.mid - b.mid) <= 1;
  let hl = await headLines();
  console.log("— 本群设置面板已经没有了(技能/MCP/提示词只在全局页里)");
  // 用户 2026-09-25:「技能、MCP、提示词都一样,在这个地方不合适」→ **整个面板删掉**。
  // 所以这里量的是两件事:①聊天头部不再有打开它的入口;②整个面板(含那个弹层)不在 DOM 里。
  // ⚠️ 同时要认住「别的入口还在」—— 删掉一个功能最容易连带删掉旁边的,所以顺手量一下成果那个入口。
  const gpGone = await val(`(function () {
    var btns = [].slice.call(document.querySelectorAll('.chat-head .head-actions button'));
    return {
      entry: btns.filter(function (x) {
        return /Group settings|\u672c\u7fa4\u8bbe\u7f6e/.test(x.getAttribute('aria-label') || ''); }).length,
      panel: document.querySelectorAll('.gp-back').length + document.querySelectorAll('.gp-pane').length,
      actions: btns.map(function (x) { return x.getAttribute('aria-label') || x.title || ''; })
    }; })()`);
  expect(gpGone.entry === 0 && gpGone.panel === 0,
    "「本群设置」的入口和面板都不在了:" + JSON.stringify(gpGone));
  expect(gpGone.actions.some((l) => /Outputs|\u6210\u679c/.test(l)),
    "但聊天头部别的入口还在:" + JSON.stringify(gpGone.actions));

  expect(hl.chatTitle, "聊天栏有标题可量");
  expect(sameLine(hl.railTitle, hl.chatTitle),
    "成员栏标题与聊天栏标题同线 (" + JSON.stringify(hl.railTitle) + " vs " + JSON.stringify(hl.chatTitle) + ")");
  expect(sameLine(hl.outTitle, hl.chatTitle),
    "成果栏标题与聊天栏标题同线 (" + JSON.stringify(hl.outTitle) + " vs " + JSON.stringify(hl.chatTitle) + ")");
  // ⚠️ 侧栏那个名字**故意不在这条线上**:它和窗口按钮分属两带(用户自己定的「窗口按钮一行在上、
     // 「Team Agent」在它下面」),而且 macOS 的红绿灯占了左上角 70×26 —— 名字想挤进那一条 52px 的带子,
     // 就要么压住红绿灯、要么压住三个按钮(240px 宽放不下)。所以这里只**记录**它的位置,不断言:
  console.log("     (侧栏名字在 y " + JSON.stringify(hl.brand) + ",另占一行 —— 这是刻意的)");

  expect(rf.tabs === 0, "成员栏里已经没有页签了 (" + rf.tabs + " 个)");
  expect(rf.outputsHere === 0, "成员栏里没有成果列表(它搬去聊天区右侧了," + rf.outputsHere + " 个残留)");
  expect(rf.cards > 0 || rf.sub.length > 0,
    "成员栏讲的是这个群的成员:" + rf.title + " / " + rf.sub);
  expect(rf.foot, "页脚「全部成员…」还在");
  // ⚠️⚠️ 加人入口：验**意图**，不验**位置**。
  //
  // 用户 2026-09-25 用红圈指出的是「成员栏里要有加人入口」（他此前否掉的是**聊天区**右上角那一个）。
  // 这段断言原来把位置写死成 `.mrail-head` 里的**三个**按钮（加人/最大化/收起），于是成员栏被重构成
  // 「Members / Tools 两个分区、各自拥有邀请控件」（`MemberDock.tsx` 的注释就是这么写的）之后，
  // 这里就**常驻变红** —— 控件一个没少，红的是断言。
  // ⚠️ 常驻变红的验证工具比没有验证工具更糟：真出问题时它淹在固定的一堆红里。
  // 所以改成验「这一栏里加人入口存在、看得见、点得开、弹层完整落在窗口内」，位置只**记录**不断言。
  const addEntry = await val(`(function () {
    var rail = document.querySelector('.mrail');
    if (!rail) return { n: 0, visible: 0, headBtnCount: 0, sections: 0, perSection: [], where: '', y: -1, railTop: 0 };
    var want = /Add group member|Add group tool|Add member|\\u6dfb\\u52a0\\u7fa4\\u6210\\u5458|\\u6dfb\\u52a0\\u7fa4\\u5de5\\u5177/;
    var all = [].filter.call(rail.querySelectorAll('button'), function (b) {
      return want.test(b.getAttribute('aria-label') || b.title || ''); });
    var vis = all.filter(function (b) { var r = b.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(b).visibility !== 'hidden'; });
    var perSection = [].map.call(rail.querySelectorAll('.roster-heading'), function (h) {
      return [].filter.call(h.querySelectorAll('button'), function (b) {
        return want.test(b.getAttribute('aria-label') || b.title || ''); }).length; });
    var first = vis[0];
    return { n: all.length, visible: vis.length,
             headBtnCount: rail.querySelectorAll('.mrail-head button').length,
             sections: rail.querySelectorAll('.roster-section').length, perSection: perSection,
             where: first ? ((first.parentElement && first.parentElement.className) || '') : '',
             y: first ? Math.round(first.getBoundingClientRect().top) : -1,
             railTop: Math.round(rail.getBoundingClientRect().top) };
  })()`);
  expect(addEntry.n >= 1 && addEntry.visible === addEntry.n,
    "成员栏里的加人入口都在、都看得见 (共 " + addEntry.n + ", 可见 " + addEntry.visible +
    ", 分区 " + addEntry.sections + " → " + JSON.stringify(addEntry.perSection) + ")");
  expect(addEntry.perSection.length > 0 && addEntry.perSection.every((n) => n === 1),
    "每个分区标题行恰好一个邀请控件:" + JSON.stringify(addEntry.perSection));
  expect(addEntry.headBtnCount === 2,
    "栏头保持「最大化 / 收起」两个(加人入口在分组标题行上,不在栏头):" + addEntry.headBtnCount);
  console.log("       (加人入口在 ." + addEntry.where + ",距栏顶 " + (addEntry.y - addEntry.railTop) +
              "px —— 位置只记录不断言,它搬过一次)");
  const headAdd = await val(`(function () {
    var b = [].filter.call(document.querySelectorAll('.chat-head .head-actions button'),
      function (x) { return /Add member|\u6dfb\u52a0\u6210\u5458/.test(x.getAttribute('aria-label') || ''); });
    return b.length; })()`);
  expect(headAdd === 0, "聊天头部不再有加人按钮(它搬去成员栏了):" + headAdd);
  // 而且它真的能开:点一下,弹层出现、并且**完整落在窗口内**(最大化之后这个按钮贴着窗口右缘,
  // 弹层要是向右开就会跑出去 —— 这条就是为那个方向写的)。
  await val(`(function () {
    var rail = document.querySelector('.mrail');
    var want = /Add group member|Add group tool|Add member|\\u6dfb\\u52a0\\u7fa4\\u6210\\u5458|\\u6dfb\\u52a0\\u7fa4\\u5de5\\u5177/;
    var b = [].filter.call(rail.querySelectorAll('button'), function (x) {
      return want.test(x.getAttribute('aria-label') || x.title || ''); })[0];
    if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
  await sleep(700);
  // ⚠️ 弹层是**共用 `Modal`**（portal 到 body：`.modal-mask > .modal`，标题在 `.modal-head h3`），
  // 不再是它自己那套 `.madd-pop`。组件改用共用弹层时，这里若继续找老类名，会把
  // 「打开了，只是类名换了」误报成「点了没反应」—— 与成员栏那次是同一类错。
  const addPop = await val(`(function () {
    var p = document.querySelector('.modal-mask .modal');
    var railName = ((document.querySelector('.mrail-title') || {}).textContent || '').trim();
    if (!p) return { open: false, railName: railName };
    var r = p.getBoundingClientRect();
    return { open: true, railName: railName,
             left: Math.round(r.left), right: Math.round(r.right),
             top: Math.round(r.top), bottom: Math.round(r.bottom),
             winW: innerWidth, winH: innerHeight,
             title: ((p.querySelector('.modal-head h3') || {}).textContent) || '',
             inWindow: r.left >= 0 && r.top >= 0 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1 }; })()`);
  expect(addPop.open === true, "点它真的开出了加人面板:" + JSON.stringify(addPop));
  expect(addPop.inWindow === true, "面板完整落在窗口内:" + JSON.stringify(addPop));
  // ⚠️ 标题要点着**这个群的名字**。判据按**数据**取（栏头那个群名），不写死任何群的名称 ——
  // 写死过一次（「未破裂…」），而那不是所有群都能满足的。
  expect(!!addPop.title && (!addPop.railName || addPop.title.includes(addPop.railName)),
    "面板标题点明了加到哪个群 (" + JSON.stringify(addPop.title) + " ⊇ " + JSON.stringify(addPop.railName) + ")");
  if (SHOT) await shotTo(shotPath(".memberrail-add"));
  await esc();
  await sleep(400);
  // ⚠️ 栏在聊天的**左边** —— 这一条是「成果搬到右侧」这件事的另一半:搬走的不能只是名义上搬走。
  expect(rf.railRight <= rf.chatLeft + 1,
    "成员栏仍在聊天区左侧 (" + rf.railRight + " ≤ " + rf.chatLeft + ")");
  expect(rf.overflowX <= 1, "成员列表不横向溢出 (" + rf.overflowX + ")");
  // 最大化:占满主区域、聊天让位,再点一下回来 —— **进得去也要出得来**,而且那个按钮不换位置。
  const maxAtStart = rf.maxBtn;
  await click(".mrail .rail-max");
  await sleep(700);
  rf = await railOnly();
  expect(rf.mainVisible === false, "成员栏最大化后聊天让位");
  expect(rf.maxBtn && rf.maxBtn.y === maxAtStart.y,
    "最大化按钮仍在同一行 (" + JSON.stringify(maxAtStart) + " → " + JSON.stringify(rf.maxBtn) + ")");
  if (SHOT) await shotTo(shotPath(".memberrail-max"));
  await click(".mrail .rail-max");
  await sleep(700);
  rf = await railOnly();
  expect(rf.mainVisible === true, "再点一下回到聊天");
  expect(rf.maxBtn && rf.maxBtn.x === maxAtStart.x && rf.maxBtn.y === maxAtStart.y,
    "还原后按钮回到原坐标 (" + JSON.stringify(maxAtStart) + " → " + JSON.stringify(rf.maxBtn) + ")");
  if (SHOT) await shotTo(shotPath(".memberrail"));
  // 栏里那个收起按钮也能关掉整栏,聊天回来
  await val(`(function () {
    var b = [].filter.call(document.querySelectorAll('.mrail-head button'), function (x) {
      return (x.getAttribute('aria-label') || '').indexOf('column') >= 0 ||
             (x.getAttribute('aria-label') || '').indexOf('\\u6210\\u5458\\u680f') >= 0; })[0];
    if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
  await sleep(900);
  const railGone = await val(`(function () {
    return { rail: !!document.querySelector('.mrail'),
             main: !!(document.querySelector('.main') || {}).getBoundingClientRect().width > 0 }; })()`);
  expect(!railGone.rail && railGone.main !== false, "栏里的收起按钮也能关掉整栏,聊天回来");

  console.log("— 工作空间:在输入框下面,弹层向上开");
  await click(".sidebar .conv-main");
  await sleep(2600);
  s = await state();
  expect(!!s.wsChip, "输入框那一行有工作空间的 chip");
  if (s.wsChip) {
    expect(s.wsChip.below === true, "它在输入框那一行里(不占主面板)");
    expect(s.wsChip.chev === true, "chip 上有可展开的指示");
    expect(s.wsChip.expanded === "false", "默认是收起的");
    expect((s.wsChip.name || "").length > 0, "chip 写明了是哪个目录:" + s.wsChip.name);
  }
  expect(!s.wsPop, "没点开之前没有弹层");
  expect(s.wsHeaderIcon === 0, "头部那个工作空间图标已经不在了(" + s.wsHeaderIcon + " 个)");
  await click(".wspick-btn");
  await sleep(1500);
  s = await state();
  expect(!!s.wsPop, "点 chip 后弹出工作空间面板");
  if (s.wsPop) {
    expect(s.wsPop.opensUpward, "弹层开在 chip 上方(在 composer 里 bottom:100%)");
    expect(s.wsPop.top >= 0 && s.wsPop.bottom <= s.vh, "弹层完整落在窗口内 (top " + s.wsPop.top + ", 底 " + s.wsPop.bottom + " / " + s.vh + ")");
    expect(s.wsPop.maxHeight !== "none" && s.wsPop.bodyOverflow === "auto",
      "弹层有上界且文件列表可滚 (max-height " + s.wsPop.maxHeight + ", 列表 overflow-y " + s.wsPop.bodyOverflow + ")");
  }
  expect(s.chatW === s.mainW, "工作空间也没有挤窄聊天区 (" + s.chatW + ")");
  await esc();
  await sleep(700);
  s = await state();
  expect(!s.wsPop, "Esc 能关掉工作空间面板");

  console.log("— 回到群聊:成员栏里是本群成员,不是全应用成员页");
  await click(".sidebar .conv-main");
  await sleep(2600);
  s = await state();
  if (!s.railPresent) {
    await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
    await sleep(1600);
    s = await state();
  }
  expect(s.railPresent && !s.memberPage, "看到的是本群成员栏,不是全应用成员页");
  expect(s.sideItemCount === s.railCards && s.railCards > 0,
    "左栏项的数字 = 栏里的卡片数 (" + s.sideItemCount + " vs " + s.railCards + ")");
  // ⚠️ 原来的判据是 `/本群成员|members/` —— **大小写敏感**，而栏头的副标题现在印的是
  // "Members and tools · 9"，于是这条永远红。要守的是「这一栏说的是本群」：标题是**群名本身**，
  // 副标题给出人数。所以按**数据**比（群名从页面上的标题取，人数与卡片数一致），不写死文案。
  const railGroupName = String(s.railTitle || "").trim();
  expect(railGroupName.length > 0 && railGroupName !== "Members",
    "栏头写的是**群名**,不是泛称:" + JSON.stringify(railGroupName));
  expect(/members|tools|成员|工具/i.test(String(s.railSub)) && /\d/.test(String(s.railSub)),
    "副标题说明这一栏是什么、有几个人:" + JSON.stringify(s.railSub));
  expect(String(s.railSub).includes(String(s.sideItemCount)),
    "副标题里的人数 = 左栏那个数字 (" + JSON.stringify(s.railSub) + " vs " + s.sideItemCount + ")");

  // ---------------------------------------------------------------- @ 提及候选
  // This is the check that was missing, and the bug it exists for is invisible to any test that only
  // reads the code: the popup is anchored *above* the composer, so with no height limit it grows
  // upward. Typing "@" in the 10-member group listed 37 entries (members + every file and document),
  // stood 1305px tall in a 733px window, and put its own first 733px — exactly the 11 member rows —
  // off the top of the screen. The members were not hidden by a style; they were outside the window,
  // and with `overflow: visible` they could not even be scrolled to. So the invariant asserted here is
  // about geometry, not content: a bounded box, and every member row inside the viewport.
  console.log("— @ 提及候选");
  await click(".sidebar .conv-main");       // the member page unmounted the chat; go back to it
  await sleep(3000);
  const typeAt = async (text) => {
    await val(`(function () {
      var ta = document.querySelector('.composer textarea');
      if (!ta) return 'no composer';
      var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, ${JSON.stringify(text)});
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      return 'typed';
    })()`);
    await sleep(900);
  };
  const picker = () => val(`(function () {
    var pop = document.querySelector('.mention-pop');
    if (!pop) return { exists: false };
    var r = pop.getBoundingClientRect(), cs = getComputedStyle(pop), vh = innerHeight, vw = innerWidth;
    var rows = [].slice.call(pop.children).map(function (b) {
      var br = b.getBoundingClientRect();
      return { member: /(^|\\s)mp-member(\\s|$)/.test(b.className),
               onScreen: br.bottom > 0 && br.top < vh && br.right > 0 && br.left < vw };
    });
    return { exists: true, rows: rows.length, rect: [r.left, r.top, r.width, r.height],
             maxHeight: cs.maxHeight, overflowY: cs.overflowY, vh: vh, vw: vw,
             members: rows.filter(function (x) { return x.member; }).length,
             membersOnScreen: rows.filter(function (x) { return x.member && x.onScreen; }).length };
  })()`);

  await typeAt("@");
  const pk = await picker();
  expect(pk.exists, "@ 之后弹出候选列表");
  if (pk.exists) {
    expect(pk.membersOnScreen === pk.members && pk.members > 0,
      "每个成员都在屏幕内 (" + pk.membersOnScreen + "/" + pk.members + ")");
    expect(pk.maxHeight !== "none" && pk.overflowY === "auto",
      "列表有上界且可滚动 (max-height " + pk.maxHeight + ", overflow-y " + pk.overflowY + ")");
    expect(Math.round(pk.rect[1]) >= 0 && pk.rect[3] <= pk.vh * 0.7,
      "列表完整落在窗口内 (top " + Math.round(pk.rect[1]) + ", 高 " + Math.round(pk.rect[3]) + "/" + pk.vh + ")");
    expect(pk.rect[0] >= 0 && pk.rect[0] + pk.rect[2] <= pk.vw,
      "列表没有横向溢出 (" + Math.round(pk.rect[0] + pk.rect[2]) + " <= " + pk.vw + ")");
  }
  // A typed query must still bring the files and documents back — hiding them when the box is empty
  // is a de-cluttering decision, not a removal.
  await typeAt("@a");
  const typed = await picker();
  expect(typed.exists && typed.rows > 0, "输入字符后仍有候选 (" + (typed.rows || 0) + " 项)");
  expect(typed.members === typed.membersOnScreen, "有查询词时成员同样都在屏幕内");
  await typeAt("");

  // ---------------------------------------------------------------- 设置里的「发现」与「版本更新」
  // These two were one page ("Updates") until the app's own version was given its own entry, because
  // the two ask different things of the reader: a skill or a catalog can be updated in one click,
  // while the app is only ever *reported* — you download the installer and replace it by hand.
  // Asserted: two entries in the settings nav (and in the user menu), each landing on its own page,
  // and the version page's own settings (the repository the current version is compared against)
  // living only there. A label is not enough — a page that renders the wrong component still has one.
  console.log("— 设置:发现 / 软件升级");
  const menuItems = () => val(`(function () {
    var m = document.querySelector('.user-menu');
    return m ? [].slice.call(m.querySelectorAll('button')).map(function (b) {
      return b.textContent.replace(/\\s+/g, ' ').trim(); }) : null; })()`);
  const settingsPage = () => val(`(function () {
    var c = document.querySelector('.settings-content');
    var nav = [].slice.call(document.querySelectorAll('.settings .nav-item')).map(function (b) {
      var n = b.querySelector('.count-badge');
      return { label: b.textContent.replace(/\\s+/g, ' ').replace(/\\d+\\s*$/, '').trim(),
               badge: n ? Number(n.textContent) : 0, on: b.classList.contains('on') }; });
    return { nav: nav, title: ((c && c.querySelector('.sp-title')) || {}).textContent || '',
             hasRepo: !!(c && c.querySelector('#upd-repo')), hasToken: !!(c && c.querySelector('#upd-token')),
             hasCatalog: !!(c && c.querySelector('#upd-cat')),
             sections: c ? [].slice.call(c.querySelectorAll('.sec')).map(function (x) {
               return x.textContent.replace(/\\s+/g, ' ').trim(); }) : [] };
  })()`);

  if (!(await menuItems())) { await click(".user-row"); await sleep(300); }
  const menu = await menuItems();
  expect(Array.isArray(menu) && menu.some((x) => /\u53d1\u73b0|^Discover/.test(x)), "用户菜单里有「发现」:" + JSON.stringify(menu));
  expect(Array.isArray(menu) && menu.some((x) => /\u8f6f\u4ef6\u5347\u7ea7|Software update/.test(x)), "用户菜单里有「软件升级」");
  await clickByText(".user-menu button", "/\\u8f6f\\u4ef6\\u5347\\u7ea7|Software update/");
  await sleep(1200);
  let sp = await settingsPage();
  expect(/\u8f6f\u4ef6\u5347\u7ea7|Software update/.test(sp.title), "「软件升级」打开的是自己的页面:" + sp.title);
  expect(sp.nav.some((i) => /\u53d1\u73b0|Discover/.test(i.label)) && sp.nav.some((i) => /\u8f6f\u4ef6\u5347\u7ea7|Software update/.test(i.label)),
    "设置导航里两项都在:" + JSON.stringify(sp.nav.map((i) => i.label + (i.badge ? "(" + i.badge + ")" : ""))));
  expect(!sp.nav.some((i) => /^(\u66f4\u65b0|Updates)$/.test(i.label)), "旧的「更新」一项已不再出现");
  expect(sp.hasRepo && sp.hasToken, "升级来源设置在这一页(程序仓库 + Token)");
  expect(sp.sections.some((x) => /\u7248\u672c\u72b6\u6001|Version status/.test(x)), "有「版本状态」区块:" + JSON.stringify(sp.sections));
  if (SHOT) await shotTo(shotPath(".version"));

  await clickByText(".settings .nav-item", "/\\u53d1\u73b0|Discover/");
  await sleep(900);
  sp = await settingsPage();
  expect(/\u53d1\u73b0|Discover/.test(sp.title), "「发现」打开的是自己的页面:" + sp.title);
  expect(sp.hasCatalog && !sp.hasRepo && !sp.hasToken, "程序仓库只留在软件升级页(发现页不该有)");
  expect(sp.sections.some((x) => /\u5f85\u5904\u7406\u7684\u63d0\u9192|Reminders to review/.test(x)),
    "「发现」仍列出待处理提醒:" + JSON.stringify(sp.sections));
  if (SHOT) await shotTo(shotPath(".discover"));


  // Both pages are rows inside one bordered panel, so the failure mode that matters on screen is a
  // row wider than that panel (a long value, or a select next to a switch), which "the section is
  // there" never catches. Checked as geometry: nothing spills out, nothing scrolls sideways.
  const panelRooms = () => val(`(function () {
    var c = document.querySelector('.settings-content');
    if (!c) return null;
    var cr = c.getBoundingClientRect(), spill = 0, rows = 0;
    [].forEach.call(c.querySelectorAll('.setting-row, .ext-form-row, .ext-upd'), function (r) {
      rows++; var b = r.getBoundingClientRect();
      spill = Math.max(spill, Math.round(b.right - cr.right), Math.round(cr.left - b.left));
    });
    return { rows: rows, spill: spill, hScroll: c.scrollWidth - c.clientWidth };
  })()`);
  const room = await panelRooms();
  expect(room && room.rows > 0 && room.spill <= 1 && room.hScroll <= 1,
    "两页的行都在面板内、不横向滚动 (" + (room ? room.rows + " 行, 溢出 " + room.spill + "px, 横向 " + room.hScroll + "px" : "没有面板") + ")");

  // ---------------------------------------------------------------- 通用页:流程工程师的两只旋钮
  // A settings row that renders but whose field is missing is the failure that only shows up when a
  // member needs it, so both are asserted where they live: the command (which may be empty, meaning
  // "use whichever CLI is installed") and the ceiling in seconds. The chip matters as much as the
  // field — "nothing found" is the difference between "the tool is there" and "it never appears".
  console.log("— 设置:通用页的流程工程师旋钮");
  await clickByText(".settings .nav-item", "/\\u901a\\u7528|General/");
  await sleep(1000);
  sp = await settingsPage();
  expect(/\\u901a\\u7528|General/.test(sp.title), "打开「通用」:" + sp.title);
  const knobs = await val(`(function () {
    var want = [/\\u8be2\\u95ee\\u7fa4\\u5916\\u7684 AI|Ask an AI outside/, /\\u7b49\\u5f85\\u90a3\\u4e2a\\u56de\\u7b54|How long to wait/];
    var rows = [].slice.call(document.querySelectorAll('.settings-content .setting-row'));
    return want.map(function (re) {
      var r = rows.filter(function (x) { return re.test(x.textContent); })[0];
      if (!r) return { found: false };
      var b = r.getBoundingClientRect();
      return { found: true, field: !!r.querySelector('input, .num-input'),
               chip: [].slice.call(r.querySelectorAll('.chip')).map(function (c) { return c.textContent.trim(); }),
               right: Math.round(b.right) };
    }); })()`);
  expect(knobs[0].found && knobs[0].field, "「询问群外的 AI」有输入框:" + JSON.stringify(knobs[0]));
  expect(knobs[0].chip.length > 0, "它旁边有状态 chip(装了哪个/没找到):" + JSON.stringify(knobs[0].chip));
  expect(knobs[1].found && knobs[1].field, "「等待那个回答多久」有数字输入:" + JSON.stringify(knobs[1]));
  const general = await panelRooms();
  expect(general && general.spill <= 1 && general.hScroll <= 1,
    "通用页的行也不横向溢出 (" + (general ? general.rows + " 行, 溢出 " + general.spill + "px" : "没有面板") + ")");
  if (SHOT) {
    // Bring the row into view before shooting: the assertions above read the whole document, so they
    // passed while the picture showed the top of the page — and a picture that does not contain the
    // row is a picture nobody can check. (Caught by looking at the screenshot with the app's own
    // vision model, which reported "no such row" for exactly this reason.)
    await val(`(function () {
      var r = [].slice.call(document.querySelectorAll('.settings-content .setting-row')).filter(function (x) {
        return /Ask an AI outside|\\u8be2\\u95ee\\u7fa4\\u5916/.test(x.textContent); })[0];
      if (r) r.scrollIntoView({ block: 'center' });
      return 'ok'; })()`);
    await sleep(300);
    await shotTo(shotPath(".general"));
  }

  // ---------------------------------------------------------------- 流程工程师（隐身）
  // It is in every group and invisible in all of them, so this panel is the only place it exists on
  // screen — which makes it exactly the kind of thing that can be shipped broken without anyone
  // noticing. Asserted as data (the three switches are there, the chip counts real groups) and as
  // geometry (the section is on the page, not overflowing it).
  console.log("— 设置:流程工程师面板");
  const panel = await val(`(function () {
    var rows = [].slice.call(document.querySelectorAll('.settings-content .setting-row'));
    var want = [/Keep it in every group|\\u6bcf\\u4e2a\\u7fa4\\u90fd\\u653e\\u4e00\\u4e2a/,
                /Record what this app can measure|\\u628a\\u7a0b\\u5e8f\\u91cf\\u5f97\\u51fa\\u6765\\u7684\\u8bb0\\u4e0b\\u6765/,
                /Ask a model for the cause|\\u8ba9\\u6a21\\u578b\\u8865\\u6839\\u56e0/,
                /What it has found|\\u5b83\\u53d1\\u73b0\\u4e86\\u4ec0\\u4e48/];
    return {
      sections: [].slice.call(document.querySelectorAll('.settings-content .sec')).map(function (x) {
        return x.textContent.trim(); }),
      rows: want.map(function (re) {
        var r = rows.filter(function (x) { return re.test(x.textContent); })[0];
        if (!r) return { found: false };
        var b = r.getBoundingClientRect();
        return { found: true, switch: !!r.querySelector('.switch, [role=switch], input[type=checkbox]'),
                 chip: [].slice.call(r.querySelectorAll('.chip')).map(function (c) { return c.textContent.trim(); }),
                 inWindow: b.top >= 0 && b.bottom <= innerHeight + 1, right: Math.round(b.right) };
      }),
      recent: document.querySelectorAll('.settings-content .card .setting-row .sr-title').length };
  })()`);
  expect(panel.sections.some((x) => /Process engineer|\\u6d41\\u7a0b\\u5de5\\u7a0b\\u5e08/.test(x)),
    "通用页有「流程工程师」区块:" + JSON.stringify(panel.sections));
  expect(panel.rows[0].found && panel.rows[0].switch, "「每个群都放一个」是开关:" + JSON.stringify(panel.rows[0]));
  expect(panel.rows[1].found && panel.rows[1].switch, "「把程序量得出来的记下来」是开关:" + JSON.stringify(panel.rows[1]));
  expect(panel.rows[2].found && panel.rows[2].switch, "「让模型补根因与修法」是开关:" + JSON.stringify(panel.rows[2]));
  expect(panel.rows[3].found && panel.rows[3].chip.length > 0,
    "「它发现了什么」有状态 chip:" + JSON.stringify(panel.rows[3]));
  // A single backslash here, not the doubled one template literals need: this regex is read by node,
  // not by the page, and `/\\d+/` would look for a literal backslash and fail on every real chip.
  expect(/\d+/.test(panel.rows[3].chip[0] || ""), "chip 里是真实数字:" + JSON.stringify(panel.rows[3].chip));
  const procRoom = await panelRooms();
  expect(procRoom && procRoom.spill <= 1 && procRoom.hScroll <= 1,
    "流程工程师这些行也不横向溢出 (" + (procRoom ? procRoom.rows + " 行, 溢出 " + procRoom.spill + "px" : "没有") + ")");
  if (SHOT) {
    // ⚠️ The settings page scrolls inside its own container, so `scrollIntoView` alone left the row
    // below the fold and the screenshot showed the *previous* page position — a picture that looked
    // right and contained none of what it was taken for. So: bring the row into view, and assert it
    // actually is inside the viewport before shooting.
    const shown = await val(`(function () {
      var box = document.querySelector('.settings-content');
      var row = [].slice.call(document.querySelectorAll('.settings-content .setting-row')).filter(function (x) {
        return /Keep it in every group|\\u6bcf\\u4e2a\\u7fa4\\u90fd\\u653e\\u4e00\\u4e2a/.test(x.textContent); })[0];
      if (!box || !row) return { ok: false };
      // Scroll to *the row*, not to the bottom of whatever container scrolls. Scrolling the container
      // to its end puts this row off the top as soon as the panel below it is taller than the
      // viewport — which is exactly what the panel does once the ledger has a handful of entries, so
      // that version of this check passed only while the log happened to be short.
      row.scrollIntoView({ block: "center" });
      var scroller = box;
      while (scroller && scroller.scrollHeight <= scroller.clientHeight + 1) scroller = scroller.parentElement;
      var b = row.getBoundingClientRect();
      return { ok: true, scrolledTo: scroller ? Math.round(scroller.scrollTop) : 0,
               h: scroller ? scroller.scrollHeight : 0, vh: innerHeight,
               top: Math.round(b.top), bottom: Math.round(b.bottom),
               inWindow: b.top >= 0 && b.bottom <= innerHeight + 1 };
    })()`);
    console.log("  流程面板滚动:", JSON.stringify(shown));
    expect(shown.ok && shown.inWindow,
      "滚动后「每个群都放一个」这一行真的在屏幕内 (" + JSON.stringify(shown) + ")");
    // ⚠️ Scrolling first is not enough on this page: the settings content has its own scroll
    // container, and by the time the capture happens the panel has re-rendered and the scroll is back
    // at the top — a picture that looks right and shows the wrong part of the page. So the viewport
    // itself is made tall enough for the whole panel to be on screen, and restored afterwards.
    await send("Emulation.setDeviceMetricsOverride",
      { width: 1280, height: 1750, deviceScaleFactor: 1, mobile: false }, sessionId);
    await sleep(350);
    const fits = await val(`(function () {
      var row = [].slice.call(document.querySelectorAll('.settings-content .setting-row')).filter(function (x) {
        return /Keep it in every group|\u6bcf\u4e2a\u7fa4\u90fd\u653e\u4e00\u4e2a/.test(x.textContent); })[0];
      if (!row) return null;
      var b = row.getBoundingClientRect();
      return { top: Math.round(b.top), bottom: Math.round(b.bottom), vh: innerHeight,
               inWindow: b.top >= 0 && b.bottom <= innerHeight + 1 }; })()`);
    console.log("  加高视口后的位置:", JSON.stringify(fits));
    expect(fits && fits.inWindow, "加高视口后这一行确实在画面内 (" + JSON.stringify(fits) + ")");
    await shotTo(shotPath(".process"));
    await send("Emulation.clearDeviceMetricsOverride", {}, sessionId);
    await sleep(200);
  }

  // 按意思检索:这张卡片长在「资料库」页上,因为它要说的是「这一堆文档能不能按意思搜到」。
  // 断言落在几何上 —— 「有没有这个元素」抓不到「它跑到屏幕外面去了」或「它把卡片撑出一条横向
  // 滚动条」,而这两种都真的发生过(浮层曾在 733px 窗口里长到 1305px)。状态行的内容也逐字取:
  // 一条「模型没装」却不说怎么装的状态行,比没有状态行更糟。
  console.log("— 资料库:按意思检索");
  await clickByText(".settings .nav-item", "/Library|\\u8d44\\u6599\\u5e93/");
  // 这一页要先把几千篇文档拉回来才渲染正文,所以等它,而不是睡一个固定秒数 ——
  // 「固定 sleep + 一次查询」在多大数据集上一定会偶发失败,而偶发失败会被当成代码问题。
  const vecProbe = () => val(`(function () {
    var card = document.querySelector('.kn-vec');
    if (!card) return null;
    var head = card.querySelector('.kn-shelf-head');
    var lines = [].slice.call(card.querySelectorAll('.kn-vec-line'));
    var hr = head ? head.getBoundingClientRect() : null;
    var r = card.getBoundingClientRect();
    return { title: (head && head.textContent) || '',
             headTop: hr ? Math.round(hr.top) : -1, headBottom: hr ? Math.round(hr.bottom) : -1,
             lines: lines.length,
             texts: lines.map(function (l) { return l.textContent.replace(/\\s+/g, ' ').trim(); }),
             height: Math.round(r.height),
             overflow: card.scrollWidth - card.clientWidth,
             wide: card.scrollWidth > Math.round(r.width) + 1,
             vw: window.innerWidth, vh: window.innerHeight };
  })()`);
  let vec = null;
  for (let i = 0; i < 25 && !vec; i++) {
    await sleep(400);
    vec = await vecProbe();
  }
  if (!vec) {
    // 失败的现场要能读出「页面长什么样」,否则「缺一个元素」只能靠猜。
    const here = await val(`(function () {
      var c = document.querySelector('.settings-content');
      return { title: ((c && c.querySelector('.sp-title')) || {}).textContent || '',
               shelves: document.querySelectorAll('.kn-shelf').length,
               head: !!document.querySelector('.kn-head'),
               text: ((c && c.innerText) || '').replace(/\\s+/g, ' ').slice(0, 220) };
    })()`);
    expect(false, "资料库设置页上有那张「按意思检索」的卡片;现场:" + JSON.stringify(here));
  }
  expect(/按意思检索|Searching by meaning/.test(vec.title), "卡片标题对:" + vec.title);
  expect(vec.headTop >= 0 && vec.headBottom <= vec.vh + 1,
    "卡片标题在视口里:顶部 " + vec.headTop + ", 底部 " + vec.headBottom + " / " + vec.vh);
  expect(vec.height <= vec.vh * 0.65, "卡片没有高过半屏(" + vec.height + " / " + vec.vh + ")");
  expect(vec.overflow <= 1, "卡片内容没有被撑出一条横向滚动条(" + vec.overflow + "px)");
  expect(!vec.wide, "最长的命令行在卡片宽度内换行,而不是把卡片撑宽");
  expect(vec.lines >= 2, "至少两行状态(服务 + 覆盖率):" + vec.lines);
  expect(vec.texts.some((x) => /\u6bb5|passages/.test(x)), "有一行在说索引覆盖率:" + JSON.stringify(vec.texts));
  if (SHOT) await shotTo(shotPath(".vector"));

  console.log("— 资料库:按归类分区、来源写在每一行上、项目材料默认收起");
  // 这一页有两块用同一个类名的面板(向量那块在文档顺序里更靠前),所以取「里面真的有知识库表格」的那一块 ——
  // 取错的表现就是「一个行都没有」,而那看起来正像功能没做。
  // (这段在模板字符串里,所以注释里不要出现反引号:一个就够把模板提前截断。)
  const shelfProbe = () => val(`(function () {
    var shelf = [].slice.call(document.querySelectorAll('.kn-shelf'))
      .filter(function (s) { return s.querySelector('.kn-docs'); })[0];
    if (!shelf) return null;
    var rows = [].slice.call(shelf.querySelectorAll('.kn-doc-row'));
    var grp = rows.filter(function (r) { return r.classList.contains('kn-grp'); });
    var kb = rows.filter(function (r) {
      return !r.classList.contains('kn-grp') && !r.classList.contains('kn-doc-th'); });
    var table = shelf.querySelector('.kn-doc-th');
    var box = function (e) { var r = e.getBoundingClientRect();
      return { l: Math.round(r.left), t: Math.round(r.top) }; };
    return {
      groups: grp.map(function (g) {
        var t = g.querySelector('.kn-grp-toggle');
        return { name: (g.querySelector('.kn-grp-name') || {}).textContent || '',
                 note: (g.querySelector('.kn-grp-note') || {}).textContent || '',
                 expanded: t ? t.getAttribute('aria-expanded') : null, left: box(g).l };
      }),
      kbs: kb.map(function (r) {
        return { name: (r.querySelector('.kn-doc-title') || {}).textContent || '',
                 tags: [].slice.call(r.querySelectorAll('.kn-doc-sub .tag')).map(function (x) {
                   return x.textContent.trim(); }),
                 sub: (r.querySelector('.kn-doc-sub') || {}).textContent || '' };
      }),
      tableLeft: table ? box(table).l : null,
      overflow: shelf.scrollWidth - shelf.clientWidth };
  })()`);
  let shelf = null;
  for (let i = 0; i < 25 && !shelf; i++) { await sleep(400); shelf = await shelfProbe(); }
  expect(!!shelf, "资料库架子上有知识库那一块");
  if (shelf) {
    if (shelf.kbs.length === 0) {
      // 一台还没放过任何资料的机器:这里没有可断言的分区,说出来而不是静默跳过。
      console.log("   (这个资料库里还没有知识库,分区断言本次不适用)");
    } else {
      expect(shelf.groups.length >= 1, "架子按归类分了区 (" + shelf.groups.length + " 组)");
      // 分区行是跨列的,所以它必须和表头左边缘对齐 —— 错位说明网格没接上,而那只有真渲染才看得见。
      if (shelf.tableLeft !== null) {
        expect(shelf.groups.every((g) => Math.abs(g.left - shelf.tableLeft) <= 1),
          "每个分区行和表头左对齐 (" + shelf.tableLeft + ", 实际 " +
          JSON.stringify(shelf.groups.map((g) => g.left)) + ")");
      }
      // 「项目材料」默认收起:那是唯一会自动长大的那一栏(第一次往群里放东西就有一份),
      // 所以它不占屏幕 —— 这条断言是「项目不进资料库」那个决定留在界面上的痕迹。
      for (const g of shelf.groups.filter((x) => /项目材料|Project material/.test(x.name))) {
        expect(g.expanded === "false", "「" + g.name + "」默认收起 (aria-expanded " + g.expanded + ")");
      }
      // 每一行都要说清「材料从哪来」。归类是用户自己的选择(可能还没选),来源是每一行都必须有的。
      for (const r of shelf.kbs) {
        expect(r.tags.length >= 2, "「" + r.name + "」行上有归类与来源两个词:" + JSON.stringify(r.tags));
        expect((r.tags[r.tags.length - 1] || "").length > 0, "「" + r.name + "」的来源词不是空的");
      }
      expect(shelf.overflow <= 1, "架子没有被撑出一条横向滚动条 (" + shelf.overflow + "px)");
    }
    if (SHOT) await shotTo(shotPath(".library-shelf"));
  }

  console.log("— 资料库:几千篇怎么读(分页 + 分类 + 筛选,不再一次展开)");
  // The document table lives outside any `.kn-shelf`; the knowledge-base table lives inside one.
  // Both use `.kn-doc-row`, so the scope has to be chosen by where they are, not by the class.
  const libProbe = () => val(`(function () {
    var flat = [].slice.call(document.querySelectorAll('.kn-docs'))
      .filter(function (d) { return !d.closest('.kn-shelf'); });
    var rows = flat.length ? [].slice.call(flat[0].querySelectorAll('.kn-doc-row')) : [];
    var data = rows.filter(function (r) { return !r.classList.contains('kn-doc-th'); });
    var title = function (r) { return (r.querySelector('.kn-doc-title') || {}).textContent || ''; };
    var pagers = [].slice.call(document.querySelectorAll('.kn-pager'));
    var stat = ((document.querySelector('.kn-lib-bar .kn-stats') || {}).textContent || '').replace(/\\s+/g, ' ');
    var chips = [].slice.call(document.querySelectorAll('.kn-chip'));
    return {
      rows: data.length,
      first: data.length ? title(data[0]) : '',
      last: data.length ? title(data[data.length - 1]) : '',
      stat: stat,
      total: Number((stat.match(/([\\d,]+)/) || [])[1] ? (stat.match(/([\\d,]+)/) || [])[1].replace(/,/g, '') : 0),
      pager: pagers.length ? (pagers[0].innerText || '').replace(/\\s+/g, ' ').trim() : '',
      page: pagers.length ? ((pagers[0].querySelector('.kn-pager-page') || {}).textContent || '') : '',
      pagerW: pagers.length ? Math.round(pagers[0].getBoundingClientRect().width) : 0,
      chips: chips.map(function (c) { return c.textContent.replace(/\\s+/g, ' ').trim(); }),
      // 每一行 chip 里「选中」的那些。两个维度各有一枚「全部」,所以「选中几个」只能按行看。
      onRows: [].slice.call(document.querySelectorAll('.kn-chips')).map(function (r) {
        return [].filter.call(r.querySelectorAll('.kn-chip'), function (c) {
          return c.classList.contains('on'); })
          .map(function (c) { return c.textContent.replace(/\\s+/g, ' ').trim(); }); }),
      classes: [].slice.call(document.querySelectorAll('.kn-class')).map(function (c) {
        return { label: ((c.querySelector('.kn-class-head b') || {}).textContent || ''),
                 count: ((c.querySelector('.kn-class-head .small') || {}).textContent || '').replace(/\\s+/g, ' ').trim(),
                 rows: c.querySelectorAll('.kn-doc-row:not(.kn-doc-th)').length }; }),
      tables: flat.length,
    };
  })()`);
  const libRow = (i) => val(`(function () {
    var flat = [].slice.call(document.querySelectorAll('.kn-docs'))
      .filter(function (d) { return !d.closest('.kn-shelf'); });
    var rows = flat.length ? [].slice.call(flat[0].querySelectorAll('.kn-doc-row'))
      .filter(function (r) { return !r.classList.contains('kn-doc-th'); }) : [];
    var r = rows[${i}];
    return r ? ((r.querySelector('.kn-doc-title') || {}).textContent || '') : '';
  })()`);
  const clickPager = (which) => val(`(function () {
    var p = document.querySelector('.kn-pager');
    if (!p) return 'missing';
    var b = [].filter.call(p.querySelectorAll('button'), function (x) {
      return /Next page|\\u4e0b\\u4e00\\u9875/.test(x.textContent); })[0];
    if (!b) return 'missing-next';
    b.click(); return 'clicked';
  })()`);
  // The `src` arguments below are regex *sources*: the pattern is built inside the page (`new
  // RegExp`), because a regex literal cannot be carried across from here — passing one as a string
  // makes `"...".test(...)` the evaluated code, which throws instead of matching.
  const clickChip = (src) => val(`(function () {
    var re = new RegExp(${JSON.stringify(src)}, 'i');
    var c = [].filter.call(document.querySelectorAll('.kn-chip'), function (x) {
      return re.test(x.textContent); })[0];
    if (!c) return 'missing';
    c.click(); return 'clicked: ' + c.textContent.replace(/\\s+/g, ' ').trim();
  })()`);
  /** Click a chip **inside the row whose label is `rowLabel`**. Three rows now carry a chip called
   *  "All", so clicking by text alone hits the first row every time — which silently leaves the
   *  other dimension's filter in place and reads as "the reset did not work". */
  const clickChipIn = (rowLabel, src) => val(`(function () {
    var label = new RegExp(${JSON.stringify(rowLabel)}, 'i');
    var chip = new RegExp(${JSON.stringify(src)}, 'i');
    var row = [].slice.call(document.querySelectorAll('.kn-chips')).filter(function (r) {
      var l = r.querySelector('.kn-chips-label');
      return l && label.test(l.textContent); })[0];
    if (!row) return 'missing-row';
    var c = [].filter.call(row.querySelectorAll('.kn-chip'), function (x) {
      return chip.test(x.textContent); })[0];
    if (!c) return 'missing-chip';
    c.click(); return 'clicked: ' + c.textContent.replace(/\\s+/g, ' ').trim();
  })()`);
  const setSelect = (src, value) => val(`(function () {
    var re = new RegExp(${JSON.stringify(src)}, 'i');
    var sel = [].filter.call(document.querySelectorAll('.kn-groupby select'), function (s) {
      return re.test(s.getAttribute('aria-label') || ''); })[0];
    if (!sel) return 'missing';
    var set = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
    set.call(sel, ${JSON.stringify(value)});
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    return 'set';
  })()`);
  const typeFilter = (text) => val(`(function () {
    var i = document.querySelector('.kn-filter input');
    if (!i) return 'missing';
    var set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    set.call(i, ${JSON.stringify(text)});
    i.dispatchEvent(new Event('input', { bubbles: true }));
    return 'typed';
  })()`);

  let lib = null;
  for (let i = 0; i < 20 && !lib; i++) { await sleep(400); lib = await libProbe(); }
  expect(!!lib && lib.total > 0, "资料库读到了总数:" + (lib && lib.stat));
  if (lib && lib.total > 0) {
    // 1) 一页,不是全部:这是用户报的那条「一下子全部展开到面板里」的落点。
    expect(lib.rows <= 200, "面板里只有一页的条数 (" + lib.rows + " 行)");
    if (lib.total > 500) {
      expect(lib.rows < lib.total / 5, "渲染的行数远少于总数 (" + lib.rows + " / " + lib.total + ")");
    }
    expect(/\\d+\\s*[–-]\\s*\\d+|–/.test(lib.pager) || /\\d/.test(lib.pager),
      "分页条写着这是第几篇到第几篇:" + JSON.stringify(lib.pager));
    expect(lib.total.toLocaleString().length > 0 && lib.stat.indexOf(String(lib.total)) >= 0,
      "统计写着总数 " + lib.total + ":" + lib.stat);
    expect(lib.page.length > 0, "分页条写着第几页 / 共几页:" + lib.page);
    expect(lib.pagerW > 0 && lib.pagerW < 1680, "分页条在窗口内 (" + lib.pagerW + "px)");

    // 2) 分类:来源与类型各一行 chip,带计数 —— 这批数字加起来就是总数。
    const originChips = lib.chips.filter((c) => /本机|导入|抓取|上传|附件|工作目录|Import|Fetch|Capture|Upload/i.test(c));
    expect(lib.chips.length >= 4, "有分类 chip(来源 + 类型):" + JSON.stringify(lib.chips.slice(0, 8)));
    // 注意:这一行是 **node 里的正则字面量**,要 `\d` 单反斜杠 —— 写成 `\\d` 会去匹配一个真的反斜杠,
    // 然后每个 chip 的数字都读成 0(模板字符串里那几处才需要双反斜杠)。
    const numOf = (s) => Number(((s.match(/([\d,]+)\s*$/) || [])[1] || "0").replace(/,/g, ""));
    const nums = lib.chips.map(numOf);
    expect(nums.some((n) => n === lib.total), "有一个 chip 的数就是总数(「全部」):" + JSON.stringify(nums.slice(0, 10)));

    // 3) 翻页真的换了一批文档
    const firstOnPage1 = lib.first;
    expect((await clickPager("next")) === "clicked", "点了「下一页」");
    await sleep(1200);
    const p2 = await libProbe();
    expect(p2.first !== firstOnPage1 && p2.rows > 0,
      "第二页是另一批文档 (" + JSON.stringify(firstOnPage1.slice(0, 18)) + " → " +
      JSON.stringify((p2.first || "").slice(0, 18)) + ")");
    expect(p2.page !== lib.page, "页码变了 (" + lib.page + " → " + p2.page + ")");

    // 4) 点一个来源 chip 就是筛到那一类
    if (originChips.length >= 2) {
      const target = originChips.find((c) => !/全部|All/.test(c));
      const clicked = await clickChip(target.split(" ")[0]);
      await sleep(1200);
      const pf = await libProbe();
      expect(typeof clicked === "string" && clicked.startsWith("clicked")
             && pf.onRows[0] && pf.onRows[0].length === 1,
        "点「" + target + "」后来源那一行只有它被选中:" + JSON.stringify(pf.onRows) + " / " + JSON.stringify(clicked));
      expect(pf.total <= lib.total && pf.total > 0, "筛选后的总数变小了 (" + pf.total + " ≤ " + lib.total + ")");
      if (SHOT) await shotTo(shotPath(".library-filtered"));
      await clickChipIn("^(Source|\\u6765\\u6e90)$", "^(All|全部)");
      await sleep(1000);
    }

    // 5) 分区视图:每一类只露几条,数字写在类名旁边,「看全部」是入口而不是把 6160 行铺开
    expect((await setSelect("Arrange|\\u6392\\u5217", "origin")) === "set", "切到「按来源分区」");
    await sleep(1500);
    const cls = await libProbe();
    expect(cls.classes.length >= 2, "分区视图里有 " + cls.classes.length + " 个类");
    if (cls.classes.length) {
      // 同样是 node 里的正则(单反斜杠):`\\D` 会去匹配真的反斜杠,于是每个数字都变成 NaN。
      const sum = cls.classes.reduce((a, c) => a + Number(c.count.replace(/\D/g, "") || 0), 0);
      expect(sum === lib.total, "各分区数量之和 = 总数 (" + sum + " = " + lib.total + ")");
      expect(cls.classes.every((c) => c.rows > 0 && c.rows <= 10),
        "每个类只露头几条,没有整个铺开:" + JSON.stringify(cls.classes.map((c) => c.rows)));
      expect(cls.classes.every((c) => c.count.length > 0 && c.label.length > 0),
        "每一类都写着叫什么、有多少篇:" + JSON.stringify(cls.classes.slice(0, 3)));
      expect(cls.tables >= cls.classes.length, "每个类是一张自己的小表 (" + cls.tables + ")");
      if (SHOT) await shotTo(shotPath(".library-classes"));
    } else {
      console.log("   (这个资料库只有一类,分区断言本次不适用)");
    }

    // 6) 按标题 / 文件名找:这是用户说的「找也不好找」
    expect((await setSelect("Arrange|\\u6392\\u5217", "")) === "set", "切回平铺");
    await sleep(1200);
    const kw = (lib.first || "").slice(0, 4);
    expect((await typeFilter(kw)) === "typed", "在筛选框里输入 " + JSON.stringify(kw));
    // ⚠️ 等**答案**而不是等一段固定时间。库长到 6160 篇之后,一次筛选查询有时超过 1.5 秒,
    // 于是「筛完的数字应当变小」这条会偶发变红(而紧接着那条「列表里确实是匹配的文档」是过的 ——
    // 说明功能没问题,是我读得太早)。
    let hit = await libProbe();
    for (let i = 0; i < 12 && !(hit.total > 0 && hit.total < lib.total); i++) {
      await sleep(800);
      hit = await libProbe();
    }
    expect(hit.rows >= 1 && hit.total > 0 && hit.total < lib.total,
      "筛选后只剩匹配的那些 (" + hit.total + " < " + lib.total + ")");
    expect(hit.rows === 0 || hit.first.indexOf(kw) >= 0 || hit.last.indexOf(kw) >= 0,
      "列表里确实是匹配的文档:" + JSON.stringify((hit.first || "").slice(0, 20)));
    await typeFilter("");
    await sleep(800);

    // 7) 抓来的素材按「用处」归类:名字里写着它是干什么的,所以它不该是一堆灰。
    const catRow = lib.onRows.length > 2
      ? lib.chips.filter((c) => /Camera work|\\u8fd0\\u955c/.test(c)) : [];
    expect(lib.onRows.length >= 3, "有第三行 chip:按用处 (" + lib.onRows.length + " 行)");
    expect(catRow.length >= 1, "用处那一行里有「运镜与镜头」:" + JSON.stringify(catRow));
    if (catRow.length) {
      const n = numOf(catRow[0]);
      expect(n > 0 && n < lib.total, "「运镜与镜头」带着它自己的数量:" + catRow[0]);
      const clicked = await clickChip("Camera work|\\u8fd0\\u955c");
      await sleep(1300);
      const pc = await libProbe();
      expect(typeof clicked === "string" && clicked.startsWith("clicked") && pc.total === n,
        "点它就只留这一类 (" + pc.total + " = " + n + ") / " + JSON.stringify(clicked));
      expect(pc.total < lib.total, "这一类比总数小 (" + pc.total + " < " + lib.total + ")");
      if (SHOT) await shotTo(shotPath(".library-category"), sessionId);
      expect((await clickChipIn("^(For|\\u7528\\u5904)$", "^(All|全部)")).startsWith("clicked"), "清掉用处筛选");
      await sleep(1000);
    }

    // 8) 按用处分区 = 一张「这个资料库有什么」的目录,每一类只露头几条
    expect((await setSelect("Arrange|\\u6392\\u5217", "category")) === "set", "切到「按用处分区」");
    await sleep(1500);
    const cat = await libProbe();
    expect(cat.classes.length >= 3, "用处分区里有 " + cat.classes.length + " 类");
    if (cat.classes.length) {
      const counted = cat.classes.map((c) => Number(c.count.replace(/\D/g, "") || 0));
      expect(counted.every((n) => n > 0), "每一类都有数量:" + JSON.stringify(cat.classes.slice(0, 4)));
      expect(cat.classes[0].label.length > 0 && !/未分类|Not classified/.test(cat.classes[0].label),
        "第一类是它真正的用处,不是「未分类」:" + cat.classes[0].label);
      expect(cat.classes.every((c) => c.rows > 0 && c.rows <= 10), "每一类只露几条:" + JSON.stringify(cat.classes.map((c) => c.rows)));
      if (SHOT) await shotTo(shotPath(".library-categories"), sessionId);
    }
    await setSelect("Arrange|\\u6392\\u5217", "");
    await sleep(800);
  }

  // --------------------------------------------- 群聊里的一条发言(临时数据目录里造出来的)
  // The states this app is *supposed* to show during a turn cannot be found in stored history: a
  // tool call that is still running is finished by the time anything is saved, and the working
  // behind a reply only exists while the reply is being written. So they are constructed — in a
  // throwaway data directory of its own, never the user's — and what is asserted is what those
  // states look like on screen. Everything above runs against the real app.
  console.log("— 群聊里的一条发言:它此刻在做什么、思路、不用框、随栏宽变宽");
  const fixture = await startFixture(send, ui);
  if (!fixture) {
    expect(false, "临时后端没起来(见上面的报错),这一节什么都没验证");
  } else {
    const fval = valIn(fixture.session);
    await fval("document.querySelector('.sidebar .conv-main') && document.querySelector('.sidebar .conv-main').click(), 'ok'");
    await sleep(3000);
    // One round trip per state: an assertion that needs three evaluate() calls is three chances to
    // assert on a stale DOM.
    const snap = await fval(`(function () {
      var rect = function (e) { var r = e.getBoundingClientRect(); return { w: Math.round(r.width), x: Math.round(r.left) }; };
      var theirs = [].slice.call(document.querySelectorAll('.msg.theirs'));
      var withTools = null;
      for (var i = 0; i < theirs.length; i++) if (theirs[i].querySelector('.tool-pill')) withTools = theirs[i];
      var think = document.querySelector('.think');
      var pills = [].slice.call(document.querySelectorAll('.tool-pill'));
      var bubble = withTools ? withTools.querySelector('.bubble') : null;
      var body = withTools ? withTools.querySelector('.msg-body') : null;
      var cs = bubble ? getComputedStyle(bubble) : null;
      var inner = document.querySelector('.msg-inner');
      var ics = inner ? getComputedStyle(inner) : null;
      var mine = document.querySelector('.msg.mine .bubble');
      var mcs = mine ? getComputedStyle(mine) : null;
      return {
        bubbleBg: cs ? cs.backgroundColor : null,
        bubbleBorder: cs ? cs.borderTopWidth : null,
        bubblePad: cs ? cs.paddingLeft : null,
        bubbleW: bubble ? rect(bubble).w : null,
        bodyW: body ? rect(body).w : null,
        innerContentW: inner ? Math.round(inner.clientWidth - parseFloat(ics.paddingLeft) - parseFloat(ics.paddingRight)) : null,
        mineBg: mcs ? mcs.backgroundColor : null,
        think: think ? { title: (think.querySelector('.think-title') || {}).textContent,
                         open: think.classList.contains('open'),
                         live: think.classList.contains('live') } : null,
        pills: pills.map(function (p) {
          return { name: ((p.querySelector('.tp-name') || {}).textContent || '').trim(),
                   arg: ((p.querySelector('.tp-arg') || {}).textContent || '').trim(),
                   argTitle: (p.querySelector('.tp-arg') || {}).title || '',
                   open: p.classList.contains('open'),
                   liveText: ((p.querySelector('.tp-pre.live') || {}).textContent || ''),
                   ms: ((p.querySelector('.tp-ms') || {}).textContent || '').trim() };
        }) };
    })()`);
    expect(!!snap && !snap.__exc, "这一页能读到 DOM" + (snap && snap.__exc ? ":" + snap.__exc : ""));
    if (!snap || snap.__exc) {
      fixture.stop();
    } else {
      // ---- 思路:它怎么想的
      expect(!!snap.think, "成员的思路渲染出来了");
      if (snap.think) {
        expect(/思路|Reasoning/.test(snap.think.title || ""), "标题写着「思路」而不是「思考中」:" + snap.think.title);
        expect(snap.think.live === false && snap.think.open === false,
          "答完的消息里它自动收起 (live=" + snap.think.live + " open=" + snap.think.open + ")");
        // 收起时正文不在 DOM 里 —— 所以「有没有内容」必须在展开之后再看,否则读到的是空字符串,
        // 看起来像「思路是空的」,其实是没展开。
        await fval("(function(){var b=document.querySelector('.think-head'); if(b) b.click(); return 'ok';})()");
        await sleep(300);
        const opened = await fval(`(function () {
          var t = document.querySelector('.think');
          if (!t) return null;
          var b = t.querySelector('.think-body');
          return { open: t.classList.contains('open'),
                   text: b ? b.textContent : '',
                   h: b ? Math.round(b.getBoundingClientRect().height) : 0 }; })()`);
        expect(opened && opened.open && opened.h > 0,
          "点一下能展开看全文 (open=" + (opened && opened.open) + " 高 " + (opened && opened.h) + "px)");
        expect(opened && opened.text.indexOf("先确认帧数") >= 0,
          "展开后读到的是它自己的话:" + JSON.stringify((opened && opened.text || "").slice(0, 40)));
      }
      // ---- 工具调用:不用点开就知道在做什么
      expect(snap.pills.length >= 2, "两处工具调用都渲染成了胶囊 (" + snap.pills.length + " 个)");
      for (const p of snap.pills) expect(p.name.length > 0, "每个胶囊都有动作名(" + p.name + ")");
      const bash = snap.pills.find((p) => p.arg.indexOf("npx remotion") >= 0);
      expect(!!bash, "外部的 Bash 调用把命令写在了脸上:" + JSON.stringify(snap.pills.map((p) => p.name + "|" + p.arg)));
      if (bash) {
        expect(/运行命令|Run a command/.test(bash.name), "Bash 被翻成一句人话,不是工具名:" + bash.name);
        expect(bash.argTitle.indexOf("npx remotion") >= 0, "完整命令留在 title 里,长了也丢不掉");
        expect(/\d+\s*ms/.test(bash.ms), "跑完的胶囊带耗时:" + bash.ms);
      }
      const live = snap.pills.filter((p) => p.liveText.length > 0)[0];
      expect(!!live, "正在跑的那次调用把实时输出显示出来了");
      if (live) {
        expect(live.open === true, "跑着的时候胶囊自动展开,不用点");
        expect(/frame 1\d\d\/300/.test(live.liveText),
          "实时输出里是它此刻打印的那几行:" + JSON.stringify(live.liveText.slice(-40)));
      }
      // ---- 被「看过/听过」的那份东西本身,渲染出来了才算数
      // 「元素在 DOM 里」不等于用户看得见:图片要真的解出像素、音频要真的读出时长。所以这里量的是
      // naturalWidth 和 duration,不是 .msg-look 的个数 —— 内容类型不对时它们都是 0,而页面上照样有个元素。
      let seen = null;
      for (let i = 0; i < 12; i++) {
        seen = await fval(`(function () {
          var img = document.querySelector('.msg-look img');
          var au = document.querySelector('.msg-look audio');
          return {
            img: img ? { w: img.naturalWidth, h: img.naturalHeight } : null,
            audio: au ? { dur: isFinite(au.duration) ? au.duration : -1 } : null,
            files: document.querySelectorAll('.tool-pill-files .msg-look').length,
            cap: ((document.querySelector('.msg-look figcaption') || {}).textContent || '').trim(),
          }; })()`);
        if ((seen && seen.img && seen.img.w > 0) && (seen && seen.audio && seen.audio.dur > 0)) break;
        await sleep(500);
      }
      expect(seen && seen.img && seen.img.w > 0,
        "被看过的图真的显示了像素 (" + JSON.stringify(seen && seen.img) + ")");
      expect(seen && seen.audio && seen.audio.dur > 0.5,
        "被听过的录音真的能播 (时长 " + (seen && seen.audio && seen.audio.dur) + "s)");
      expect(seen && seen.files >= 2, "两处产物都挂在各自的胶囊下面 (" + (seen && seen.files) + " 个)");
      expect(seen && /shot\.png/.test(seen.cap || "") && !/workspace/.test(seen.cap || ""),
        "文件名照原样写在下面,没有把内部记号漏给用户:" + JSON.stringify(seen && seen.cap));
      // 小图不能显示成「0 KB」—— 看起来像文件没存下来。
      expect(seen && !/·\s*0\s*KB/.test(seen.cap || ""),
        "大小写成人看得懂的数:" + JSON.stringify(seen && seen.cap));
      // ---- 发言上的功能键:复制 / 评价 / 转发 / 朗读 / 引用
      const acts = await fval(`(function () {
        var rows = [].slice.call(document.querySelectorAll('.msg.theirs .msg-acts'));
        var keys = rows.length ? [].slice.call(rows[rows.length - 1].querySelectorAll('.act'))
          .map(function (b) { return (b.textContent || '').trim(); }) : [];
        return { rows: rows.length, keys: keys };
      })()`);
      expect(acts && acts.rows >= 1, "成员发言下面有功能键那一行 (" + (acts && acts.rows) + " 行)");
      expect(acts && acts.keys.length >= 5, "键是齐的:" + JSON.stringify(acts && acts.keys));
      for (const want of ["Copy", "Helpful", "Not helpful", "Forward", "Read aloud", "Quote"]) {
        expect(acts && acts.keys.join("|").indexOf(want) >= 0 || acts.keys.join("|").indexOf(
          ({ Copy: "复制", Helpful: "有用", "Not helpful": "没用", Forward: "转发",
             "Read aloud": "朗读", Quote: "引用" })[want]) >= 0,
          "有「" + want + "」这个键:" + JSON.stringify(acts && acts.keys));
      }

      // ⚠️⚠️ 这一节的每一步都作用在**同一条发言**上,所以先把那一条**钉住**。
      // 原来每一步都重新 `.pop()`(取最后一条成员消息),等于假设这几次之间列表没变 —— 而应用
      // 一直在轮询并重渲染,DOM 一换,「点击的那条」和「读亮灯的那条」就不是同一条了。
      // 实测:后端确实记下了这条好评,而断言读到「0 个亮」。按 `data-mid` 选才稳定。
      const mid = await fval(`(function () {
        var m = [].slice.call(document.querySelectorAll('.msg.theirs[data-mid]'));
        return m.length ? m[m.length - 1].getAttribute('data-mid') : ''; })()`);
      expect(!!mid, "钉住了要操作的那一条发言 (data-mid=" + mid + ")");
      const rowSel = '.msg.theirs[data-mid="' + mid + '"] .msg-acts';

      // 复制:真的进了剪贴板,而且拿到的是原文(不是按钮上的字)。
      const copied = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        var b = row.querySelector('[aria-label="Copy this message"]')
             || row.querySelector('[aria-label="复制这条发言"]');
        if (!b) return 'missing';
        b.click();
        return 'clicked';
      })()`);
      await sleep(400);
      expect(copied === "clicked", "点了复制:" + copied);

      // 评价:点了赞要变亮,并且**落进后端**(重新拉一次反馈还在,才算真进了反馈体系)。
      const liked = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        var b = row.querySelector('[aria-label="Good answer"]') || row.querySelector('[aria-label="这条回答有用"]');
        if (!b) return 'missing';
        b.click(); return 'clicked';
      })()`);
      await sleep(900);
      const lit = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        return row ? row.querySelectorAll('.act.on').length : -1;
      })()`);
      expect(liked === "clicked" && lit === 1, "点赞后那个键是亮着的 (" + lit + " 个亮)");
      // 从页面自己问一次后端(它注入过 token 与地址),而不是相信按钮变亮 —— 亮可以是本地的乐观状态。
      const stored = await fval(`(async function () {
        var r = await fetch(window.teamAgent.api + '/api/feedback?limit=5',
          { headers: { 'x-team-agent-token': window.teamAgent.token } });
        return await r.json();
      })()`);
      expect(stored && stored.totals && stored.totals.up === 1,
        "这次评价进了反馈体系(后端记着 1 条好评):" + JSON.stringify(stored && stored.totals));

      // 朗读:本机合成的 WAV,而且播放状态真的起来了(按钮变成「停止」)。
      const spoke = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        var b = row.querySelector('[aria-label="Read this message out loud"]')
             || row.querySelector('[aria-label="朗读这条发言"]');
        if (!b) return 'missing';
        b.click(); return 'clicked';
      })()`);
      await sleep(2500);
      // 只看朗读那个键有没有变成「停止」:评价那一步已经让赞的键亮着了,拿「第一个亮着的键」
      // 来断言会读到它,看起来像朗读没开始。
      const speaking = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        if (!row) return '';
        var b = row.querySelector('[aria-label="Stop reading"]')
             || row.querySelector('[aria-label="\u505c\u6b62\u6717\u8bfb"]');
        return b ? (b.textContent || '').trim() : '';
      })()`);
      expect(spoke === "clicked", "点了朗读:" + spoke);
      expect(/Stop|停止/.test(speaking || ""),
        "朗读真的开始播了(键变成「停止」):" + JSON.stringify(speaking));

      // 转发:目标群是选出来的,列表里没有当前这个群。
      const clickedFwd = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        var b = row.querySelector('[aria-label="Forward this message"]')
             || row.querySelector('[aria-label="转发这条发言"]');
        if (!b) return 'missing';
        b.click(); return 'clicked';
      })()`);
      // 弹层要等 React 重渲染:同一个 evaluate 里立刻读 DOM,读到的是还没更新的那一帧。
      await sleep(400);
      const fwd = await fval(`(function () {
        var row = document.querySelector(${JSON.stringify(rowSel)});
        if (!row) return 'missing';
        var list = row ? row.querySelector('.act-list') : null;
        return { open: !!list, rows: list ? list.querySelectorAll('.act-row').length : -1 };
      })()`);
      expect(clickedFwd === "clicked", "点了转发:" + clickedFwd);
      expect(fwd && fwd.open === true, "转发弹出了目标群列表:" + JSON.stringify(fwd));

      // ---- 项目行那个文件夹图标:点它 = 进这个项目的工作目录(2026-09-25)
      // 这一页把一个外壳桩上了(见 `startFixture`),所以这里能验到**真正交给外壳的路径** ——
      // 「有个可以点的图标」和「点它进的是这个项目的目录」是两件事。
      const folderIcon = await fval(`(function () {
        var b = document.querySelector('.sidebar .cv-ico.cv-open');
        var all = document.querySelectorAll('.sidebar .cv-ico').length;
        return { button: !!b, tag: b ? b.tagName : '', all: all,
                 label: b ? (b.getAttribute('aria-label') || '') : '' }; })()`);
      expect(folderIcon.button && folderIcon.tag === "BUTTON",
        "有外壳时,文件夹图标是个真按钮:" + JSON.stringify(folderIcon));
      expect(folderIcon.all >= 1, "项目行里有文件夹图标 (" + folderIcon.all + " 个)");
      await fval(`(function () {
        document.querySelector('.sidebar .cv-ico.cv-open').click(); return 'ok'; })()`);
      await sleep(500);
      const openedPath = await fval(`window.__openedPath || ''`);
      // 它必须是**磁盘上的绝对路径**,而且就是这一行自己那个目录(图标上的 title 就是那个值 ——
      // 全应用只有一处解析它:后端 `Store.workspace_path`)。
      const iconTitle = await fval(`(function () {
        var b = document.querySelector('.sidebar .cv-ico');
        return b ? (b.getAttribute('title') || '') : ''; })()`);
      expect(openedPath.length > 0 && openedPath === iconTitle && openedPath.indexOf("/") === 0,
        "点文件夹 = 打开**这个项目**的工作目录 (" + openedPath + " vs " + iconTitle + ")");

      // ---- 成果栏:挂在**聊天区的右侧**,点一条就在这里看得见内容(2026-09-25)
      // 用户的原话:「成果不适放在这边,像 WorkBuddy 一样,把输出结果放在中间框的右侧栏里」。
      // ⚠️ 这一节必须用临时后端跑:它的工作目录里**一定**有一张真 png 和一段真 wav 落盘了
      // (见 `FIXTURE_BUILD`)。真库里「第一个项目有没有产物」是不确定的 —— 用户刚建的那个群排在
      // 列表第一、里面什么文件都没有 —— 那样「点一条、看内容」就只能写成条件断言,等于没验。
      const outInfo = () => fval(`(function () {
        var p = document.querySelector('.outpanel');
        var pr = p ? p.getBoundingClientRect() : null;
        var chat = document.querySelector('.chat');
        var cr = chat ? chat.getBoundingClientRect() : null;
        var main = document.querySelector('.main');
        var head = document.querySelector('.outpanel .op-head');
        var chr = document.querySelector('.chat-head');
        var rows = [].slice.call(document.querySelectorAll('.outpanel .out-row'));
        var btn = [].filter.call(document.querySelectorAll('.chat-head .head-actions button'),
          function (x) { return /Outputs|\\u6210\\u679c/.test(x.getAttribute('aria-label') || ''); })[0];
        var marked = btn ? (btn.querySelector('.n') || {}).textContent : null;
        return {
          present: !!p,
          left: pr ? Math.round(pr.left) : null,
          right: pr ? Math.round(pr.right) : null,
          winW: innerWidth,
          chatRight: cr ? Math.round(cr.right) : null,
          headH: head ? Math.round(head.getBoundingClientRect().height) : null,
          headBottom: head ? Math.round(head.getBoundingClientRect().bottom) : null,
          chatHeadBottom: chr ? Math.round(chr.getBoundingClientRect().bottom) : null,
          title: (document.querySelector('.outpanel .op-title') || {}).textContent || '',
          sub: (document.querySelector('.outpanel .op-sub') || {}).textContent || '',
          rows: rows.length,
          names: rows.map(function (r) { return (r.querySelector('.out-name') || {}).textContent || ''; }),
          hasBack: !!document.querySelector('.outpanel .op-back'),
          view: (function () {
            if (document.querySelector('.outpanel img')) return 'img';
            if (document.querySelector('.outpanel video')) return 'video';
            if (document.querySelector('.outpanel audio')) return 'audio';
            if (document.querySelector('.outpanel .op-pre')) return 'pre';
            if (document.querySelector('.outpanel .md')) return 'md';
            if (document.querySelector('.outpanel .op-note')) return 'note';
            return ''; })(),
          // 图片真的画出来了吗 —— 「元素在」和「用户看得到」是两件事(CSP 曾经把 blob: 挡在外面)
          painted: (function () { var i = document.querySelector('.outpanel img');
            return i ? { w: Math.round(i.getBoundingClientRect().width), h: Math.round(i.getBoundingClientRect().height),
                         loaded: i.complete && i.naturalWidth > 0 } : null; })(),
          bodyOverflowX: (function () { var b = document.querySelector('.outpanel .op-body');
            return b ? (b.scrollWidth - b.clientWidth) : null; })(),
          mainVisible: !!(main && main.getBoundingClientRect().width > 0),
          mainW: main ? Math.round(main.getBoundingClientRect().width) : 0,
          marked: marked === null || marked === undefined ? null : String(marked).trim(),
          btnPressed: btn ? btn.getAttribute('aria-pressed') : null
        }; })()`);
      // 按名字点一条产物(名字拼进表达式里,不做字符串魔术)。
      const clickRow = (name) => fval(`(function () {
        var want = ${JSON.stringify(name)};
        var rows = [].slice.call(document.querySelectorAll('.outpanel .out-row'));
        for (var i = 0; i < rows.length; i++) {
          var n = (rows[i].querySelector('.out-name') || {}).textContent || '';
          if (n === want) {
            var b = rows[i].querySelector('.out-pick');
            if (!b) return 'no-button';
            b.click(); return 'clicked';
          }
        }
        return 'missing'; })()`);
      // ⚠️ 这一页是**第二个页面**(临时后端),所以每一次交互都必须用 `fval`。用主页面的 `click()`
      // 会点在另一个页面上 —— 那正是这一段第一版红的九条的原因:「收起」没关掉这一页的面板,
      // 而断言里看到的宽度一直没变。
      const fclick = (sel) => fval(`(function () {
        var e = document.querySelector(${JSON.stringify(sel)});
        if (!e) return 'missing'; e.click(); return 'clicked'; })()`);
      let op = await outInfo();
      if (!op.present) {
        // 没开着就用聊天头部那个入口打开 —— 那**就是**用户找它的路径,顺便把它验了。
        await fval(`(function () {
          var b = [].filter.call(document.querySelectorAll('.chat-head .head-actions button'),
            function (x) { return /Outputs|\\u6210\\u679c/.test(x.getAttribute('aria-label') || ''); })[0];
          if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
        await sleep(1200);
        op = await outInfo();
      }
      expect(op.present, "成果栏在聊天这一页上");
      // ⚠️ 两条几何判据,这就是「搬到右侧」这句话的全部内容:左边界不早于聊天区右边界,
      // 右边界贴住窗口右缘。它原来在**左**边(和成员挤在同一根柱子上),这两条会精确地红。
      expect(op.left !== null && op.chatRight !== null && op.left >= op.chatRight - 1,
        "成果栏在聊天区的**右侧** (面板左 " + op.left + " ≥ 聊天右 " + op.chatRight + ")");
      expect(op.right === op.winW, "成果栏贴住窗口右缘 (" + op.right + " = " + op.winW + ")");
      expect(op.headH === 52 && op.headBottom === op.chatHeadBottom,
        "成果栏头部与聊天头部同线、同高 (" + op.headH + "px, " + op.headBottom + " vs " + op.chatHeadBottom + ")");
      // ⚠️ 这一节靠的就是临时后端里那两个真文件:它们不在,说明夹具坏了,不能条件跳过。
      expect(op.rows >= 2, "列出来的都是真文件:" + JSON.stringify(op.names));
      expect(op.names.indexOf("shot.png") >= 0 && op.names.indexOf("narration.wav") >= 0,
        "那张图和那段音频都在列表里:" + JSON.stringify(op.names));
      // 聊天头部那个入口上的数字 = 面板里数出来的条数(**一个数只有一处算**)
      expect(op.marked === String(op.rows),
        "聊天头部入口上的数字 = 列表条数 (" + JSON.stringify(op.marked) + " vs " + op.rows + ")");
      expect(op.bodyOverflowX !== null && op.bodyOverflowX <= 1,
        "成果列表不横向溢出 (" + op.bodyOverflowX + ")");
      if (SHOT) await shotTo(shotPath(".outputs-list"), fixture.session);
      // ⚠️ 清单态的标题先记下来,回来时和它比 —— **不要写死「成果」**:这一页(headless Chrome)
      // 是英文界面,标题是 "Outputs"。写死一种语言 = 一条永远在另一种语言下变红的断言。
      const outputsListTitle = op.title;
      // ---- 点一条:在这里看它(而不是把文件交给别的程序、答案跑到别处去)
      const clickedPng = await clickRow("shot.png");
      expect(clickedPng === "clicked", "点了一条产物:" + clickedPng);
      for (let i = 0; i < 10; i++) { await sleep(500); op = await outInfo(); if (op.view) break; }
      expect(op.view === "img", "图片在这个栏里显示出来了 (view=" + op.view + ")");
      // 「元素在」≠「用户看得到」:它必须真的解码出来、有尺寸。
      expect(op.painted && op.painted.loaded && op.painted.w > 50 && op.painted.h > 30,
        "图真的画出来了:" + JSON.stringify(op.painted));
      expect(op.title === "shot.png", "头部标题就是文件名 (" + op.title + ")");
      expect(op.hasBack, "有一条**显式的**返回键(否则清单像是没了)");
      if (SHOT) await shotTo(shotPath(".outputs-preview"), fixture.session);
      // 回到清单再点下一条 —— **预览态里没有清单**("点一条看内容"就意味着列表让位),所以
      // 「点第二条」必须先回来。第一版在这里直接点第二条,点了个空(missing),而断言写的是
      // `expect(clickedWav, ...)` —— 一个非空字符串是**真值**,于是它"通过"了,却什么都没验。
      expect(await fclick(".outpanel .op-back") === "clicked", "点了返回");
      await sleep(500);
      const clickedWav = await clickRow("narration.wav");
      expect(clickedWav === "clicked", "音频那一行也点得到:" + clickedWav);
      for (let i = 0; i < 10; i++) { await sleep(500); op = await outInfo(); if (op.view === "audio") break; }
      expect(op.view === "audio", "音频在这个栏里变成了播放器 (view=" + op.view + ")");
      // ---- 返回清单
      expect(await fclick(".outpanel .op-back") === "clicked", "点了返回");
      await sleep(500);
      op = await outInfo();
      expect(op.rows >= 2 && op.title === outputsListTitle,
        "返回键回到清单 (" + op.title + " vs " + outputsListTitle + ", " + op.rows + " 条)");
      // ---- 收起:面板消失、聊天变宽。**再点聊天头部那个入口要能把它叫回来。**
      const wideBefore = op.mainW;
      expect(await fclick(".outpanel .op-close") === "clicked", "点了收起");
      await sleep(700);
      op = await outInfo();
      expect(!op.present, "收起后成果栏不在了");
      expect(op.mainW > wideBefore, "收起后聊天变宽 (" + wideBefore + " → " + op.mainW + ")");
      await fval(`(function () {
        var b = [].filter.call(document.querySelectorAll('.chat-head .head-actions button'),
          function (x) { return /Outputs|\\u6210\\u679c/.test(x.getAttribute('aria-label') || ''); })[0];
        if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
      await sleep(1000);
      op = await outInfo();
      expect(op.present && op.mainW === wideBefore,
        "聊天头部的入口能把成果栏叫回来,宽度回到原样 (" + op.mainW + " vs " + wideBefore + ")");
      // ---- 最大化:占满主区域,聊天让位;再点一下回来
      expect(await fclick(".outpanel .op-max") === "clicked", "点了最大化");
      await sleep(700);
      const omax = await outInfo();
      expect(!omax.mainVisible, "最大化后聊天让位");
      expect(omax.right === omax.winW, "最大化后成果栏占满到窗口右缘 (" + omax.right + ")");
      if (SHOT) await shotTo(shotPath(".outputs-max"), fixture.session);
      expect(await fclick(".outpanel .op-max") === "clicked", "再点一下");
      await sleep(700);
      op = await outInfo();
      expect(op.mainVisible && op.present, "再点一下回到聊天");
      expect(op.left >= op.chatRight - 1, "回来后仍然在聊天区右侧 (" + op.left + " ≥ " + op.chatRight + ")");

      // ---- ⭐ 有成果就自动展开右栏(用户 2026-09-26:「一旦有成果输出自动显示右侧栏」)
      // ⚠️ 这一节**只能在这里验**:真库里往工作目录写文件会污染用户的资料库(`watch_workspace` 会把
      // 工作目录里的文档收进去),而 fixture 是一次性的。而且这一节比别的都严 —— fixture 的群
      // **不会有新消息**,所以面板自己那条「按群消息时间戳刷新」的路根本不会响:
      // 它能自己回来,就只可能是新加的那个轮询做到的。
      const findWs = (root) => {
        for (const entry of fs.readdirSync(root)) {
          const p = path.join(root, entry);
          try {
            if (!fs.statSync(p).isDirectory()) continue;
            if (fs.existsSync(path.join(p, "narration.wav"))) return p;
            const deeper = findWs(p);
            if (deeper) return deeper;
          } catch { /* 读不了的目录跳过:这不是被测的东西 */ }
        }
        return "";
      };
      const wsDir = findWs(fixture.dir);
      // ⚠️⚠️ 比的必须是**规范路径**。后端给的是解析过的那个(/private/tmp/...),而 fixture 目录是
      // 用 /tmp/... 这个名字起的 —— /tmp 是符号链接,两个拼写指向同一个文件却**不相等**。
      // 这个项目为同一件事踩过一次(coderun 的路径守卫),这里踩的是第二次。
      const canon = (q) => { try { return fs.realpathSync(q); } catch { return q; } };
      expect(!!wsDir, "找到了 fixture 的工作目录 (" + wsDir + ")");
      const autoAdded = wsDir ? path.join(wsDir, "zz-autoopen.txt") : "";
      try {
        // 1) 收起 → 往工作目录里放一个新文件 → 面板应当**自己**回来
        expect(await fclick(".outpanel .op-close") === "clicked", "先收起成果栏");
        await sleep(700);
        expect(!(await outInfo()).present, "收起后确实不在");
        fs.writeFileSync(autoAdded, "written by ui-smoke to ask for the outputs column\n");
        // ⚠️ 等到**面板在**、而且**新文件已经在它的列表里**为止。第一版一看到面板在就断言「新文件就在
        // 列表里」,于是读到空数组 —— 面板先挂上去、工作目录那一趟请求随后才回来。断言要等**要验的那
        // 两件事都成立**,不是「差不多到了」。
        let back = null;
        for (let i = 0; i < 20; i++) {
          await sleep(700);
          back = await outInfo();
          if (back.present && back.names.indexOf("zz-autoopen.txt") >= 0) break;
        }
        expect(back && back.present, "有新成果时成果栏**自己**回来了(轮询 ≤12 秒,没靠任何新消息)");
        expect(back && back.names.indexOf("zz-autoopen.txt") >= 0,
          "新文件就在列表里:" + JSON.stringify(back && back.names));
        if (SHOT) await shotTo(shotPath(".outputs-autoopened"), fixture.session);
        // 2) 反过来:没有新文件时**不许**自己弹出来 —— 否则每收一条没有产出的消息都弹一次,
        //    那就成了「关不掉」,而这个功能的判据本来就是「只在真的多出文件时开」。
        expect(await fclick(".outpanel .op-close") === "clicked", "再收起一次");
        await sleep(9000);                                   // 跨过两个轮询周期
        expect(!(await outInfo()).present, "没有新成果时它不会自己弹回来");

        // ---- ⭐ 右键一条成果:用对应的工具打开 / 送给微信等
        // 上一个断言把面板收起来了,这里显式叫回来 —— 那**就是**用户找它的路径。
        await fval(`(function () {
          var b = [].filter.call(document.querySelectorAll('.chat-head .head-actions button'),
            function (x) { return /Outputs|\\u6210\\u679c/.test(x.getAttribute('aria-label') || ''); })[0];
          if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
        await sleep(1400);
        /** 右键第一行（只派事件，**不读结果**）。 */
        const rightClickRow = () => fval(`(function () {
          var row = document.querySelector('.outpanel .out-row');
          if (!row) return { ok: false, why: 'no-row' };
          var rel = row.getAttribute('title') || '';
          var r = row.getBoundingClientRect();
          row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true,
            clientX: Math.round(r.left + 30), clientY: Math.round(r.top + r.height / 2) }));
          return { ok: true, rel: rel }; })()`);
        /**
         * ⚠️⚠️ **派事件和读 DOM 必须分成两次往返。** 第一版把两件事写在同一个同步块里,于是
         * 永远读到 `open:false` —— React 的状态更新是**异步批处理**的(这个项目已经为同一件事
         * 踩过一次),同步块里 querySelector 拿到的是**上一次**渲染的 DOM,菜单根本还没挂上去。
         * 断言要「点 → 等一拍 → 再读」。
         */
        const readMenu = () => fval(`(function () {
          var m = document.querySelector('.om');
          if (!m) return { open: false };
          var b = m.getBoundingClientRect();
          return { open: true,
            acts: [].map.call(m.querySelectorAll('[data-act]'), function (x) { return x.getAttribute('data-act'); }),
            left: Math.round(b.left), top: Math.round(b.top), right: Math.round(b.right),
            bottom: Math.round(b.bottom), w: Math.round(b.width), h: Math.round(b.height),
            winW: innerWidth, winH: innerHeight,
            inWindow: b.left >= 0 && b.top >= 0 && b.right <= innerWidth + 1 && b.bottom <= innerHeight + 1,
            // 「元素在」≠「用户看得到」:菜单中心那个点上最上面的一层,必须还是菜单自己
            onTop: (function () { var el = document.elementFromPoint(
              Math.round((b.left + b.right) / 2), Math.round((b.top + b.bottom) / 2));
              return !!(el && (m.contains(el) || el === m)); })(),
            z: getComputedStyle(m).zIndex,
            // 菜单顶上要写清对**哪一条**可以做事
            head: (m.querySelector('.om-head') || {}).textContent || '' };
        })()`);
        const openMenu = async () => {
          const rc = await rightClickRow();
          if (!rc.ok) return { open: false, why: rc.why };
          await sleep(400);                     // ← 等一拍,等 React 把菜单挂上
          const got = await readMenu();
          return Object.assign({ rel: rc.rel }, got);
        };
        let menu = await openMenu();
        expect(menu.open === true, "右键弹出了菜单:" + JSON.stringify(menu.why || ""));
        expect(menu.inWindow === true, "菜单完整落在窗口内:" +
          JSON.stringify([menu.left, menu.top, menu.right, menu.bottom, menu.winW, menu.winH]));
        expect(menu.onTop === true, "菜单没有被列表或预览压住 (z-index " + menu.z + ")");
        expect(menu.head === menu.rel, "菜单顶上写着**对这一条**:" + JSON.stringify([menu.head, menu.rel]));
        const acts = menu.acts || [];
        const want = ["view", "open", "reveal", "copy", "copy-path"];
        expect(want.every((a) => acts.indexOf(a) >= 0),
          "五个动作都在(看/打开/找到/复制/复制路径):" + JSON.stringify(acts));
        expect(acts.filter((a) => a.indexOf("share:") === 0).length === 2,
          "装了的外壳应用都列出来了:" + JSON.stringify(acts));
        if (SHOT) await shotTo(shotPath(".outputs-menu"), fixture.session);
        // 点「用它的默认程序打开」:交给外壳的必须是**绝对路径**(工作目录 + 相对路径),不是相对路径
        const rel = menu.rel;
        // 之后三次比较都用它:外壳拿到的应当是**绝对路径**,而且要和 fixture 目录拼出来的那个
        // **规范**路径相等(见上面 `canon` 的注释)。
        const wantAbs = canon(wsDir + "/" + rel);
        await fval(`(function () { var b = document.querySelector('.om [data-act="open"]');
          if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
        await sleep(700);
        const shell1 = await fval("window.__shell || []");
        expect(shell1.length === 1 && shell1[0][0] === "openFile" &&
          canon(shell1[0][1]) === wantAbs,
          "「用它的默认程序打开」给外壳的是绝对路径 (" + JSON.stringify(shell1) + " vs " + wantAbs + ")");
        expect(await fval("!!document.querySelector('.om')") === false, "点完一项菜单自己收起");
        // 「每个动作都回话」是这块菜单的设计之一(复制那条尤其:不是图片时复制的是**路径**,
        // 界面必须说清是哪一个)。菜单一收起就什么都不说,用户只能猜 —— 所以这条要验。
        const toast = await fval("((document.querySelector('.toast') || {}).textContent || '').trim()");
        expect(toast.length > 0, "动作做完有一条回话:" + JSON.stringify(toast));
        // 分享那一路:交给外壳的是(**哪个应用**,**哪个文件**)两个参数
        menu = await openMenu();
        expect(menu.open === true, "第二次右键也弹出菜单");
        await fval(`(function () { var b = document.querySelector('.om [data-act="share:wechat"]');
          if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
        await sleep(700);
        const shell2 = await fval("window.__shell || []");
        expect(shell2.some((c) => c[0] === "openWith" && c[1] === "/Applications/WeChat.app" &&
          canon(c[2]) === wantAbs),
          "「用微信打开」给外壳的是(应用, 文件):" + JSON.stringify(shell2));
        // 复制这条路要能区分**复制的是图还是路径**(微信那边粘出来的东西完全不同)
        menu = await openMenu();
        await fval(`(function () { var b = document.querySelector('.om [data-act="copy"]');
          if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
        await sleep(600);
        const shell3 = await fval("window.__shell || []");
        expect(shell3.some((c) => c[0] === "copyFile" && canon(c[1]) === wantAbs),
          "「复制这个文件」给外壳的是绝对路径:" + JSON.stringify(shell3.slice(-2)));
        // Esc 关掉(和别的浮层一样)
        menu = await openMenu();
        await fval("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); 'ok'");
        await sleep(400);
        expect(await fval("!!document.querySelector('.om')") === false, "Esc 能关掉菜单");
        // ⚠️ 反向:把外壳能力撤掉(真的浏览器里就是这样),菜单**只剩**「在这一栏里看它」——
        // 不是灰着、也不是点了没反应,而是根本不出现。与侧栏那个开文件夹的按钮同一条规矩。
        await fval(`(function () {
          window.__saved = { openFile: window.teamAgent.openFile, reveal: window.teamAgent.reveal,
            copyFile: window.teamAgent.copyFile, copyText: window.teamAgent.copyText,
            shareTargets: window.teamAgent.shareTargets, openWith: window.teamAgent.openWith };
          window.teamAgent.openFile = null; window.teamAgent.reveal = null;
          window.teamAgent.copyFile = null; window.teamAgent.copyText = null;
          window.teamAgent.shareTargets = null; window.teamAgent.openWith = null; return 'ok'; })()`);
        menu = await openMenu();
        const actsNoShell = menu.acts || [];
        expect(menu.open === true && actsNoShell.length === 1 && actsNoShell[0] === "view",
          "没有外壳能力时菜单只剩「在这一栏里看它」:" + JSON.stringify(actsNoShell));
        await fval("Object.assign(window.teamAgent, window.__saved); 'ok'");
        await fval("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); 'ok'");
        await sleep(300);
      } finally {
        // 夹具是临时的,但**留着那个文件会让下一次运行的初始条数变掉** —— 这一节自己收拾干净。
        if (autoAdded) fs.rmSync(autoAdded, { force: true });
      }

      // ---- 不用框
      expect(snap.bubbleBg === "rgba(0, 0, 0, 0)", "成员发言没有背景框 (" + snap.bubbleBg + ")");
      expect(snap.bubbleBorder === "0px", "成员发言没有边框 (" + snap.bubbleBorder + ")");
      expect(snap.bubblePad === "0px", "发言直接排在栏里,没有内边距盒子 (" + snap.bubblePad + ")");
      expect(snap.mineBg === null || snap.mineBg === "rgba(0, 0, 0, 0)", "自己那条也不是气泡 (" + snap.mineBg + ")");
      // ---- 随栏宽变宽
      expect(snap.bubbleW >= snap.bodyW - 2, "发言填满自己的那一列 (" + snap.bubbleW + " / " + snap.bodyW + ")");
      expect(snap.bubbleW > 680, "旧的 680px 封顶已经不在了 (实测 " + snap.bubbleW + "px)");
      expect(snap.bodyW >= snap.innerContentW * 0.85,
        "那一列吃掉了可用宽度 (列 " + snap.bodyW + " / 可用 " + snap.innerContentW + ")");
      await send("Emulation.setDeviceMetricsOverride",
        { width: 2200, height: 1050, deviceScaleFactor: 1, mobile: false }, fixture.session);
      await sleep(600);
      const wide = await fval(`(function(){var b=document.querySelector('.msg.theirs .bubble');
        return b ? Math.round(b.getBoundingClientRect().width) : null;})()`);
      await send("Emulation.clearDeviceMetricsOverride", {}, fixture.session);
      await sleep(400);
      expect(wide !== null && wide > snap.bubbleW,
        "窗口变宽,发言跟着变宽 (" + snap.bubbleW + "px → " + wide + "px)");
      if (SHOT) {
        // 现在是「思路展开」的状态(上面点开过),先拍它,再点回收起拍常规状态 —— 名字和状态要对上。
        await shotTo(shotPath(".chat-message-think"), fixture.session);
        await fval("(function(){var b=document.querySelector('.think-head'); if(b) b.click(); return 'ok';})()");
        await sleep(250);
        await shotTo(shotPath(".chat-message"), fixture.session);
        // 被看过的那份东西:图和解出来的音频播放器都在这一条里,滚到它再拍。
        await fval(`(function () {
          var m = [].slice.call(document.querySelectorAll('.msg.theirs'));
          var last = m[m.length - 1];
          if (last) last.scrollIntoView({ block: 'center' });
          return 'ok'; })()`);
        await sleep(400);
        await shotTo(shotPath(".review-shown"), fixture.session);
      }
      fixture.stop();
    }
  }

  await esc();
  await sleep(300);

  // ---------------------------------------------------------------- 任务板上的「没能派单」
  // ⚠️⚠️ 这一段**用合成 DOM**量真实样式表，因为真实数据里没有带 `dropped` 的任务板（库里一度是
  // 0 条），照数据断言会变成**空跑**——而空跑的断言和通过的断言在日志里长得一样。
  // 要守的是那件最容易静默失败的事：**类名有、规则没有 → 用户看不到**（项目里已踩过）。
  {
    const style = await val(`(function () {
      var host = document.createElement('div');
      host.style.cssText = 'position:fixed;left:-4000px;top:0;width:360px';
      // 与 PlanCard 里 dropped 那两块**同一个结构、同一批类名**。
      host.innerHTML =
        '<div class="plan-dropped-n">2 项未派单</div>' +
        '<div class="plan-dropped">' +
          '<div class="plan-dropped-head">这次分工里没能派出去的任务</div>' +
          '<ul><li>t7: task t7 is missing its instruction</li>' +
          '<li>t9: unknown owner</li></ul>' +
        '</div>';
      document.body.appendChild(host);
      var n = host.querySelector('.plan-dropped-n');
      var d = host.querySelector('.plan-dropped');
      var h = host.querySelector('.plan-dropped-head');
      var li = host.querySelector('.plan-dropped li');
      var cs = function (e) { return e ? getComputedStyle(e) : null; };
      var box = function (e) { if (!e) return null; var r = e.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height) }; };
      var out = {
        badge: { display: cs(n).display, color: cs(n).color, bg: cs(n).backgroundColor, box: box(n) },
        block: { display: cs(d).display, border: cs(d).borderTopStyle, bg: cs(d).backgroundColor, box: box(d) },
        head: { color: cs(h).color, h: box(h).h },
        items: host.querySelectorAll('.plan-dropped li').length,
        liColour: cs(li).color, liH: box(li).h,
      };
      host.remove();
      return out;
    })()`);
    // 「有这块东西」的最低标准:真的占了地方、真的看得见颜色。类名在而规则不在时,宽度会是 0,
    // display 会是 inline 而背景全透明 —— 那正是「有代码 ≠ 用户看得到」。
    expect(style.badge.display !== "none" && style.badge.box.w > 0 && style.badge.box.h > 0,
      "任务板头部「N 项未派单」真的占了地方 (display=" + style.badge.display +
      ", " + style.badge.box.w + "×" + style.badge.box.h + ")");
    expect(style.badge.bg !== "rgba(0, 0, 0, 0)" && style.badge.color !== style.badge.bg,
      "「未派单」是带底色的徽标,不是裸字 (" + style.badge.bg + " / " + style.badge.color + ")");
    expect(style.block.display === "block" && style.block.box.h > 20,
      "被丢掉的任务有一整块 (display=" + style.block.display + ", 高 " + style.block.box.h + ")");
    expect(style.block.border === "dashed", "那一块是虚线框,与计划内的行区分开 (" + style.block.border + ")");
    expect(style.items === 2 && style.liH > 8, "两条被丢弃的任务各占一行 (n=" + style.items + ", 行高 " + style.liH + ")");
    expect(style.head.color !== style.liColour,
      "标题比正文更醒目,不是一片同样的灰 (" + style.head.color + " vs " + style.liColour + ")");
    console.log("       合成 DOM 量到:徽标 " + style.badge.box.w + "×" + style.badge.box.h +
                " / 块高 " + style.block.box.h + " / 条目 " + style.items);
  }

  console.log("— 专区:侧栏剩下的是「做东西的地方」,能力中心搬进了用户面板");
  // 用户 2026-09-27:「把技能、插件、钩子、MCP、智能体与本地工具、聊天通道都放到用户面板里面,
  // 这个地方留着给后续的专区用……每个专区有专门的资料库、模版、工作流、不同分工可选择的工具
  // 或者 skills 等」。所以这里验两条意图:
  //   ① 侧栏那一组现在是**专区**,点进去是一个专区页,页面上有那四块;
  //   ② 那六样配置入口**还能到**,只是入口换到了用户面板里。
  // ⚠️ 断言按**结构**读(有几项、有没有图标、四块在不在),不按文案 —— 文案会改,
  // 而这里要问的是「用户还够得着吗」。
  const zoneNav = await val(`(function () {
    var nav = document.querySelector('.sidebar .side-nav[aria-label]');
    if (!nav) return null;
    var items = [].slice.call(nav.querySelectorAll('.nav-item'));
    return { label: nav.getAttribute('aria-label'),
             n: items.length,
             withIcon: items.filter(function (b) { return !!b.querySelector('svg'); }).length,
             href: items[0] ? String(items[0].textContent).replace(/\\s+/g, ' ').trim() : '',
             // 那六个配置页的名字**不该**还留在这一组里
             capability: /Skills|Plugins|Hooks|MCP|Chat channels|Agents and local/.test(nav.textContent) };
  })()`);
  expect(!!zoneNav, "侧栏有「专区」那一组 (aria-label=" + (zoneNav && zoneNav.label) + ")");
  expect(zoneNav.n > 0, "专区那一组里有专区 (" + zoneNav.n + " 项)");
  expect(zoneNav.withIcon === zoneNav.n, "每个专区都有图标 (" + zoneNav.withIcon + "/" + zoneNav.n + ")");
  expect(!zoneNav.capability, "那六个配置入口不再占着侧栏这一组");
  await click(".sidebar .side-nav[aria-label] .nav-item");
  await sleep(700);
  const zonePage = await val(`(function () {
    var z = document.querySelector('.zone');
    if (!z) return { there: false };
    var surfaces = [].slice.call(z.querySelectorAll('.zone-surface'));
    var h2 = z.querySelector('.zone-head h2');
    var roles = z.querySelector('.zone-surface.zs-roles .zone-role');
    var p = z.querySelector('.zone-head p');
    var r = h2 ? h2.getBoundingClientRect() : null;
    return { there: true,
             title: h2 ? String(h2.textContent).replace(/\\s+/g, ' ').trim() : '',
             // 标题要在窗口里、占得到地方 ——「有代码」不等于「用户看得到」
             titleW: r ? Math.round(r.width) : 0,
             titleH: r ? Math.round(r.height) : 0,
             // 判「它是不是个标题」只能拿它和**同一页的正文**比:字号更大、字重更重。
             // ⚠️ 第一版这里是拿标题的颜色和容器 .zone 的颜色比 —— 两处都从 body 继承,永远相等,
             // 那条断言从来不可能是第二个结果。断言本身不成立时,它红或绿都没有意义。
             titlePx: h2 ? parseFloat(getComputedStyle(h2).fontSize) : 0,
             titleWeight: h2 ? getComputedStyle(h2).fontWeight : '',
             blurbPx: p ? parseFloat(getComputedStyle(p).fontSize) : 0,
             inView: r ? (r.left >= -1 && r.right <= innerWidth + 1) : false,
             surfaces: surfaces.length,
             surfaceTitles: surfaces.map(function (s) { return String(s.querySelector('h3') ? s.querySelector('h3').textContent : '').replace(/\\s+/g, ' ').trim(); }),
             blurb: (z.querySelector('.zone-head p') || {}).textContent || '',
             // 视频专区应该有工作台(那四个标签页),别的专区没有
             bench: !!z.querySelector('.zone-bench'),
             benchTabs: z.querySelectorAll('.zone-bench .vz-tab').length,
             roleRows: z.querySelectorAll('.zone-role').length,
             // 第四块(分工与技能)该**不在**这一页上 —— 用两个选择器各查一次,是因为
             // 「块还在只是没内容」和「块整个没了」是两件事,而用户要的是后者。
             rolesBlock: z.querySelectorAll('.zone-surface.zs-roles').length };
  })()`);
  expect(zonePage.there, "点一个专区进得去");
  expect(zonePage.titleW > 0 && zonePage.titleH > 0 && zonePage.inView,
    "专区标题真的占了地方、而且在窗口里 (" + zonePage.titleW + "×" + zonePage.titleH + ", inView=" + zonePage.inView + ")");
  expect(zonePage.titlePx > zonePage.blurbPx && zonePage.blurbPx > 0,
    "标题比说明文字更像标题 (标题 " + zonePage.titlePx + "px vs 说明 " + zonePage.blurbPx + "px, 字重 " + zonePage.titleWeight + ")");
  expect(String(zonePage.blurb).trim().length > 10, "标题下面有说明:" + String(zonePage.blurb).slice(0, 40));
  // 三块 —— 用户 2026-09-27 明确「在视频专区里面的各个面板里不需要放分工、工具与技能」，
  // 所以这里既验「三块在」，也验「第四块不在」——少一块和**该少的那一块没少**都是问题。
  expect(zonePage.surfaces === 3, "一个专区是三块 (资料库/模板/工作流, 实际 " + zonePage.surfaces + ")");
  expect(zonePage.surfaceTitles.every(function (x) { return x.length > 1; }), "三块都有标题:" + zonePage.surfaceTitles.join(" / "));
  expect(zonePage.rolesBlock === 0 && zonePage.roleRows === 0,
    "分工与技能不再画在专区页上 (zs-roles " + zonePage.rolesBlock + " 块 / 位子 " + zonePage.roleRows + " 个)");
  expect(zonePage.bench && zonePage.benchTabs > 0, "视频专区带工作台 (" + zonePage.benchTabs + " 个标签页)");
  if (SHOT) await shotTo(shotPath(".zone"));

  console.log("— 作曲面板:一句话就够了");
  // 用户 2026-09-27:「用户填提示词后自动选择音乐生成方式……情绪也没必要放到面板上，这些都在算法里面，
  // 不用在面板中展示出来」。所以这一节主要验**没有**什么 —— 五台推子（流派/情绪/乐器/制作/BPM）
  // 一个都不该还在面板上，而描述写完要能看到「读成了什么」。
  const mixPanel = await val(`(function () {
    var p = document.querySelector('.zone-bench .vz-panel');
    if (!p) return null;
    // 推子在页面上长得就是一堆可点的小标签（自带那个 patch 补齐时用的也是 .vz-chip）
    var selects = [].slice.call(p.querySelectorAll('select'));
    return { textareas: p.querySelectorAll('textarea').length,
             chips: p.querySelectorAll('.vz-chip').length,
             vocals: selects.length ? selects[0].value : '',
             labels: [].slice.call(p.querySelectorAll('label > span')).map(function (s) {
               return String(s.textContent).replace(/\\s+/g, ' ').trim(); }) };
  })()`);
  expect(!!mixPanel, "作曲面板在");
  expect(mixPanel.textareas === 2, "只有两个输入框:描述 + 歌词 (实际 " + (mixPanel && mixPanel.textareas) + ")");
  expect(mixPanel.chips === 0, "流派/情绪/乐器/制作那些可点标签已经不在面板上 (" + (mixPanel && mixPanel.chips) + " 个)");
  expect(mixPanel.vocals === "auto", "人声留着,而且默认是「自动」(" + (mixPanel && mixPanel.vocals) + ")");
  console.log("       面板上的字段:" + (mixPanel.labels || []).join(" / "));

  // 写一句话 → 面板上要出现「读成 …」。这是「自动」必须付的账：读成什么得说出来。
  await val(`(function () {
    var ta = document.querySelector('.zone-bench .vz-panel textarea');
    var set = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    set.call(ta, '安静一点的钢琴，不要鼓，慢慢铺开，垫在旁白下面');
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    return 'typed';
  })()`);
  await sleep(1500);   // 预览是 300ms 防抖 + 一次往返
  const mixRead = await val(`(function () {
    var p = document.querySelector('.zone-bench .vz-panel');
    if (!p) return null;
    var r = p.querySelector('.vz-receipt');
    var d = p.querySelector('.vz-tags');
    return { receipt: r ? String(r.textContent).replace(/\\s+/g, ' ').trim() : '',
             hasDetails: !!d, open: d ? d.open : null,
             tagChars: d && d.querySelector('code') ? String(d.querySelector('code').textContent).length : 0,
             goDisabled: (function () { var b = p.querySelector('.vz-go'); return b ? !!b.disabled : null; })() };
  })()`);
  expect(!!mixRead && /BPM/.test(mixRead.receipt) && mixRead.receipt.length > 8,
    "写完描述,面板上出现「读成 …」的回执:" + (mixRead && mixRead.receipt));
  expect(!!mixRead && mixRead.hasDetails && mixRead.open === false && mixRead.tagChars > 10,
    "那串原始标签收在折叠里、默认不展开 (字符 " + (mixRead && mixRead.tagChars) + ")");
  expect(!!mixRead && mixRead.goDisabled === false, "有描述之后「作曲」按钮才可点");
  await val(`(function () {
    var ta = document.querySelector('.zone-bench .vz-panel textarea');
    var set = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    set.call(ta, ''); ta.dispatchEvent(new Event('input', { bubbles: true }));
    return 'cleared';
  })()`);

  console.log("— 那六样:搬到了设置面板的工具栏(提示词、资料库那一栏)");
  // 用户 2026-09-27:「技术、插件、钩子、MCP、智能体与本地工具、聊天通道都移到用户设置面板
  // 提示词、资料库那个工具栏里」。所以验三件事:
  //   ① 用户面板里**不再**有它们(否则是两处入口,改一处就有一处是旧的);
  //   ② 设置面板的导航里有,和 Prompts/Library 同一个工具栏;
  //   ③ 点一下**不关设置** —— 它们是设置里的一页了,不再是主区域的整页。
  const capPanel = () => val(`(function () {
    var m = document.querySelector('.sidebar .user-menu');
    if (!m) return null;
    var items = [].slice.call(m.querySelectorAll('button[role="menuitem"]'));
    return { texts: items.map(function (b) { return String(b.textContent).replace(/\\s+/g, ' ').trim(); }),
             hasSwitch: !!m.querySelector('.menu-row') };
  })()`);
  const capBefore = await capPanel();
  if (!capBefore) { await click(".sidebar .user-row"); await sleep(400); }
  const capMenu = await capPanel();
  expect(!!capMenu, "用户面板打开了");
  const capWanted = ["Skills", "Plugins", "MCP", "Hooks", "Agents and local tools", "Chat channels"];
  const capLeft = capWanted.filter(function (w) {
    return (capMenu.texts || []).some(function (x) { return x.indexOf(w) >= 0; });
  });
  expect(capLeft.length === 0, "那六样已经从用户面板里搬走 (还留着:" + (capLeft.join("、") || "无") + ")");
  expect(capMenu.hasSwitch, "离线开关还在用户面板里 (没有连带搬走)");
  await clickByText('.sidebar .user-menu button[role="menuitem"]', "/Settings|\\u8bbe\\u7f6e/");
  await sleep(900);
  const sNav = await val(`(function () {
    var s = document.querySelector('.settings');
    if (!s) return null;
    var nav = s.querySelector('.settings-nav');
    var items = [].slice.call(nav.querySelectorAll('.nav-item'));
    var titles = [].slice.call(nav.querySelectorAll('.nav-group-title')).map(function (x) {
      return String(x.textContent).replace(/\\s+/g, ' ').trim(); });
    var b = [].filter.call(items, function (x) { return /Skills/.test(x.textContent); })[0];
    return { opened: true, groups: titles,
             n: items.length,
             texts: items.map(function (x) { return String(x.textContent).replace(/\\s+/g, ' ').trim(); }),
             skillsX: b ? Math.round(b.getBoundingClientRect().left) : null };
  })()`);
  expect(!!sNav, "设置面板打得开");
  const sMissing = capWanted.filter(function (w) {
    return !(sNav.texts || []).some(function (x) { return x.indexOf(w) >= 0; });
  });
  expect(sMissing.length === 0, "六样都在设置面板的工具栏里 (缺:" + (sMissing.join("、") || "无") + ")");
  expect(sNav.texts.some(function (x) { return x.indexOf("Prompts") >= 0 || x.indexOf("Library") >= 0; }),
    "和提示词/资料库在同一个工具栏里 (" + sNav.groups.join(" / ") + ")");
  console.log("       设置导航 " + sNav.n + " 项,分组:" + sNav.groups.join(" / "));
  await clickByText(".settings .settings-nav .nav-item", "/^\\s*Skills\\s*$/");
  await sleep(900);
  const sAfter = await val(`(function () { return {
    settingsOpen: !!document.querySelector('.settings'),
    active: (function () { var b = document.querySelector('.settings .settings-nav .nav-item.on');
      return b ? String(b.textContent).replace(/\\s+/g, ' ').trim() : ''; })(),
    bar: !!document.querySelector('.settings-content .cap-workspace'),
    // 主区域是**被盖住**、不是被换掉：设置是浮层，底下那一页还在 DOM 里。
    mainTitle: (function () { var h = document.querySelector('main .zone-head h2');
      return h ? String(h.textContent).replace(/\\s+/g, ' ').trim() : ''; })(),
  }; })()`);
  expect(sAfter.settingsOpen, "点它之后设置面板还开着 (不再跳主区域)");
  expect(/Skills/.test(sAfter.active), "而且当前项就是它:" + sAfter.active);
  // 「接到哪个群」那条栏必须跟着过来 —— 那六个页面上的「接入本群」按钮读的就是它,
  // 丢了它这些按钮就没有群可指。
  expect(sAfter.bar, "能力页仍然带着「接到哪个群」那条栏");
  // ⚠️ 不是「专区页消失了」—— 设置是浮层，底下那一页本来就还在（第一版这句写成
  // `!document.querySelector('.zone')`，**它永远不可能为真**，等于一条恒红的断言）。
  // 要问的是**主区域换没换**：还是刚才那个专区，没有被这次导航换掉。
  expect(sAfter.mainTitle === zonePage.title && sAfter.mainTitle !== "",
    "主区域还是刚才那个专区，没有被换掉 (" + sAfter.mainTitle + ")");
  await esc();
  await sleep(400);

  if (SHOT) {
    const shot = await send("Page.captureScreenshot", {}, sessionId);
    if (shot.result && shot.result.data) { fs.writeFileSync(SHOT, Buffer.from(shot.result.data, "base64")); console.log("screenshot → " + SHOT); }
  }
  ws.close();
  stop();
  if (problems.length) fail(problems.length + " check(s) failed");
  console.log("\nall checks passed");
}

main().catch((e) => {
  // ⚠️ 把**位置**打出来,不只打 message。只打 message 时,一个 ReferenceError 会变成光秃秃一句
  // 「FAIL: title is not defined」,而它到底是从哪一行冒出来的没有线索 —— 而冒烟有一千多行。
  console.error("FAIL:", (e && e.message) || e);
  if (e && e.stack) console.error(String(e.stack).split("\n").slice(0, 6).join("\n"));
  process.exit(1);
});
