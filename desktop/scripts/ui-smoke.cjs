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

async function main() {
  if (!fs.existsSync(path.join(DIST, "index.html"))) fail("dist/ is missing — run the frontend build first");
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

  const val = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
    if (r.result && r.result.exceptionDetails) return { __exc: JSON.stringify(r.result.exceptionDetails).slice(0, 200) };
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
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
  const shotTo = async (file) => {
    // A plain viewport capture — **not** `captureBeyondViewport`. With a clip, Chrome re-lays the page
    // out for the capture, which resets the inner scroll container these settings pages use, and the
    // picture then shows the top of the page however carefully the caller scrolled first.
    const r = await send("Page.captureScreenshot", {}, sessionId);
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
  const topBar = () => val(`(function () {
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
      return { name: (c.querySelector('.conv-name') || {}).textContent,
               state: (c.querySelector('.conv-state') || {}).textContent,
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
  /** The filter panel, which is where the four states live. */
  const filterPanel = () => val(`(function () {
    var p = document.querySelector('.side-filter');
    if (!p) return { open: false };
    var all = [].slice.call(p.querySelectorAll('.sf-row')).map(function (b) {
      var m = String(b.textContent).match(/(\\d+)\\s*$/);
      return { all: b.classList.contains('sf-all'),
               label: b.textContent.replace(/\\s+/g, ' ').replace(/(\\d+)\\s*$/, '').trim(),
               n: m ? Number(m[1]) : null, on: b.getAttribute('aria-checked') === 'true' }; });
    var r = p.getBoundingClientRect(), top = document.querySelector('.side-top').getBoundingClientRect();
    return { open: true, all: all[0], states: all.slice(1),
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
      var rows = document.querySelectorAll('.side-filter .sf-row');
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
  expect(pr.rows.every((r) => r.state && !r.archived), "默认列表都是未归档的,且每行写着状态");
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
  expect(narrowed.rows.every((r) => String(r.name).includes(needle)), "结果都含关键字「" + needle + "」");
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
  expect(pr.rows.some((r) => r.name === name), "归档后的项目出现在「已归档」里");
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
  expect(s.railFoot === "Every member…" || /全部成员/.test(s.railFoot || ""), "成员栏底部有「全部成员…」入口");

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
  expect(/本群成员|members/.test(String(s.railSub)), "栏头写明是本群成员:" + s.railSub);

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
    // right and contained none of what it was taken for. So: scroll the container itself, and assert
    // the row is actually inside the viewport before shooting.
    const shown = await val(`(function () {
      var box = document.querySelector('.settings-content');
      var row = [].slice.call(document.querySelectorAll('.settings-content .setting-row')).filter(function (x) {
        return /Keep it in every group|\\u6bcf\\u4e2a\\u7fa4\\u90fd\\u653e\\u4e00\\u4e2a/.test(x.textContent); })[0];
      if (!box || !row) return { ok: false };
      var scroller = box, top = 0;
      while (scroller && scroller.scrollHeight <= scroller.clientHeight + 1) scroller = scroller.parentElement;
      if (scroller) { scroller.scrollTop = scroller.scrollHeight; }
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

  await esc();
  await sleep(300);

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
