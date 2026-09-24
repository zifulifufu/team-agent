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
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const WebSocket = require("ws");

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

/** The token the Electron preload injects (`--team-agent-token=…` on the renderer process). */
function token() {
  const out = execSync(
    "/usr/bin/pgrep -fl team-agent-token 2>/dev/null | head -1 | sed -n 's/.*--team-agent-token=\\([A-Za-z0-9_-]*\\).*/\\1/p'",
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

async function main() {
  if (!fs.existsSync(path.join(DIST, "index.html"))) fail("dist/ is missing — run the frontend build first");
  const server = await serveDist();
  const ui = server.address().port;
  const dbg = ui + 1000;
  const chrome = spawn(chromePath, [
    "--headless=new", "--remote-debugging-port=" + dbg, "--user-data-dir=/tmp/ta-ui-smoke",
    "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--disable-dev-shm-usage",
    // Chrome's own sandbox cannot start inside a sandboxed shell (it dies with SIGTRAP); it is a
    // throwaway profile reading a local page, so this costs nothing here.
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-crash-reporter", "--noerrdialogs",
    "--window-size=1680,1050", "about:blank",
  ], { stdio: "ignore" });
  const stop = () => { try { chrome.kill(); } catch {} try { server.close(); } catch {} };
  process.on("exit", stop);

  let ver; for (let i = 0; i < 40 && !ver; i++) { try { ver = await get(dbg, "/json/version"); } catch { await sleep(250); } }
  if (!ver) fail("Chrome never opened a debugging port");
  const ws = new WebSocket(ver.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((ok, no) => { ws.on("open", ok); ws.on("error", (e) => no(e)); });
  let id = 0; const waiting = new Map();
  ws.on("message", (raw) => {
    const m = JSON.parse(raw.toString());
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

  const val = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
    if (r.result && r.result.exceptionDetails) return { __exc: JSON.stringify(r.result.exceptionDetails).slice(0, 200) };
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
  /** What is in the sidebar, in the member column, and in the main area — as text, not as classes. */
  const state = () => val(`(function () {
    var side = document.querySelector('.sidebar');
    var rail = document.querySelector('.mrail');
    var page = document.querySelector('.page-cols');
    var sr = side ? side.getBoundingClientRect() : null;
    var rr = rail ? rail.getBoundingClientRect() : null;
    return {
      sidebarMemberLists: document.querySelectorAll('.sidebar .mdock, .sidebar .mc, .sidebar .list-item').length,
      sidebarMemberEntry: ([].filter.call(side ? side.querySelectorAll('.nav-item') : [],
        function (b) { return /\\u6210\\u5458|Members/.test(b.textContent); })[0] || {}).textContent,
      railPresent: !!rail,
      railGapFromSidebar: (sr && rr) ? Math.round(rr.left - sr.right) : null,
      railTitle: rail ? (rail.querySelector('.mrail-title') || {}).textContent : null,
      railSub: rail ? (rail.querySelector('.mrail-sub') || {}).textContent : null,
      railCards: rail ? rail.querySelectorAll('.mc').length : 0,
      railEmptyHint: rail ? !!rail.querySelector('.mrail-empty') : false,
      railFoot: rail ? ((rail.querySelector('.mrail-foot button') || {}).textContent) : null,
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

  const problems = [];
  const expect = (ok, what) => { console.log((ok ? "  ok   " : "  FAIL ") + what); if (!ok) problems.push(what); };

  console.log("— 首页,未打开任何群聊");
  let s = await state();
  expect(s.sidebarMemberLists === 0, "左栏里没有任何成员列表");
  expect(s.railPresent && s.railEmptyHint, "成员栏在,并说明还没有打开群聊");
  expect(s.railFoot === "Every member…" || /全部成员/.test(s.railFoot || ""), "成员栏有「全部成员…」入口");

  console.log("— 打开一个群聊");
  await click(".sidebar .conv-main");
  await sleep(3000);
  s = await state();
  const declared = Number((String(s.sidebarMemberEntry).match(/\((\d+)\)/) || [])[1]);
  expect(s.sidebarMemberLists === 0, "左栏里仍然没有任何成员列表");
  expect(s.railPresent && s.railCards > 0, "成员栏列出了本群成员 (" + s.railCards + " 个)");
  expect(s.railGapFromSidebar === 0, "成员栏紧贴左栏右侧(无缝隙,相对位置 " + s.railGapFromSidebar + ")");
  expect(declared === s.railCards, "左栏写的数字与栏里的条数一致 (" + declared + " vs " + s.railCards + ")");
  expect(/·\s*\d+/.test(String(s.railSub)), "成员栏自己写明是「本群成员 · n」:" + s.railSub);

  console.log("— 用左栏那一项开合");
  if (s.railPresent) {
    await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
    await sleep(900);
    s = await state();
    expect(!s.railPresent, "点一下收起");
    await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
    await sleep(1500);
    s = await state();
    expect(s.railPresent && s.railCards > 0, "再点一下展开");
  }

  console.log("— 用成员栏自己的收起按钮");
  // By aria-label, not by position: the header also holds the add-member button.
  const hidden = await val(`(function () {
    var b = [].filter.call(document.querySelectorAll('.mrail-head button'), function (x) {
      return /column|\\u6210\\u5458\\u6807/.test(x.getAttribute('aria-label') || ''); })[0];
    if (!b) return 'missing'; b.click(); return 'clicked'; })()`);
  await sleep(900);
  s = await state();
  expect(hidden === "clicked" && !s.railPresent, "栏里的收起按钮也能关掉");

  console.log("— 「全部成员…」");
  await clickByText(".sidebar .nav-item", "/\\u6210\\u5458|Members/");
  await sleep(1400);
  await click(".mrail-foot button");
  await sleep(2600);
  s = await state();
  expect(s.memberPage && s.memberPageItems > 0, "打开了全应用成员页 (" + s.memberPageItems + " 个成员)");
  expect(s.sidebarMemberLists === 0, "即使在这一页,左栏也没有第二份成员列表");

  if (SHOT) {
    const shot = await send("Page.captureScreenshot", {}, sessionId);
    if (shot.result && shot.result.data) { fs.writeFileSync(SHOT, Buffer.from(shot.result.data, "base64")); console.log("screenshot → " + SHOT); }
  }
  ws.close();
  stop();
  if (problems.length) fail(problems.length + " check(s) failed");
  console.log("\nall checks passed");
}

main().catch((e) => { console.error("FAIL:", e && e.message); process.exit(1); });
