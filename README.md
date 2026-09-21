# Team Agent — a multi-model agent group-chat workbench (v0.5.0)

[中文说明见 README.zh-CN.md](README.zh-CN.md) · English

Pull different large language models — hosted or local — into one group chat, let them **divide the work
according to their own strengths** and hand tasks to each other with `@mentions`. Built for office
documents, video production, writing, research and similar workflows.

Model routing and add/remove are built on [LiteLLM](https://github.com/BerriAI/litellm). DeepSeek is tried
first; if it fails, or outbound calls are disabled, the app falls back to a local model (Ollama + Qwen).

The user interface ships **in English by default** — switch to Chinese any time in
**Settings → Appearance → Language**.

## Architecture

```
Electron desktop shell ── React + TS UI
        │ HTTP + WebSocket (loopback only, 127.0.0.1:8765)
        ▼
Python backend (FastAPI)
  ├─ Orchestrator  orchestrator.py   who speaks, context assembly, @hand-off, tool-call loop, running the host's plan
  ├─ Planning      planner.py        host emits <plan> → validate/topologically order → run in order → host consolidates
  ├─ Routing       router.py         pinned model / pick by strength → outbound switch → try in turn → fall back → circuit breaker
  │                    └─ LiteLLM    DeepSeek / Kimi / Qwen / GLM / Volcengine / MiniMax / OpenAI / Claude / Gemini / Ollama …
  ├─ Model catalog catalog.py + strengths.py + modelopts.py   shipped catalog + live listing + strength tags
  ├─ Tools         toolhub.py toolcall.py   text-protocol tool calls; built-ins + plugins + MCP in one dispatcher
  ├─ MCP           mcp_client.py     official mcp package: stdio / SSE / streamable HTTP
  ├─ Library       library.py        txt/md/csv/json/html/pdf/docx → chunks → BM25 (with CJK bigrams)
  ├─ Memory        memory.py         global/group/member × preference/fact/decision/lesson/playbook; auto-extract + manual
  ├─ Prompts       prompting.py      global system prompt, prompt library, group prompt, {{variables}}
  ├─ Updates       updater.py        GitHub: app / model catalog / skills / plugins, plus skill/plugin/MCP search
  └─ Storage       store.py + store_ext.py   SQLite (~/.team-agent/team-agent.db)
```

## Quick start

```bash
scripts/dev-setup.sh            # create the venv, install backend + frontend deps (mcp, pypdf, python-docx)
scripts/setup_local_model.sh    # install Ollama and pull qwen2.5:7b as the local fallback (optional, recommended)
cd desktop && npm run dev       # launches the Python backend and opens the desktop window
```

On first launch: avatar at the bottom-left → **Settings → Providers → DeepSeek** and paste your API key.
(Without a key it still works — everything goes to the local model.) Then go back home, pick a scene or a
**group template**, type your task and press Enter: a group is created and the agents start working.

Browser-only: `cd backend && python -m app`, then `cd desktop && npm run dev:web` and open http://localhost:5173 .

## What the fourth stage added

### 1. Actually using each model's strengths ("no talking past each other")
- **Ten strength tags**: writing, code, reasoning, long-context, multimodal, speed, low-cost, Chinese,
  tool-use, local. A model's strengths are **inferred** from its family, name and catalogued
  capability/context (editable per model, or reset to auto); members have "role strengths".
- **When a member has no pinned model, the best model for its role strengths is chosen** (ties: hosted
  first, local kept for fallback, then routing-chain order). Only if nothing matches does it fall through
  to the routing chain.
- **Host planning (task board)**: in a group of ≥2 with no `@mention`, the host first decides whether to
  split the work (Automatic / Always / Never, globally and per group). If it does, it emits a plan: goal,
  **shared conventions** (naming, definitions, tone, length — the key to avoiding divergence), and tasks
  (owner, dependencies, required strengths, suggested tools, deliverables). The app validates the plan
  (owner must be a member, no dependency cycles, task count cap, unknown tools dropped), runs it in
  dependency order so downstream members receive upstream results in full, and finally the host consolidates.
- **Every member states its role before speaking**: "【Role】I own …; using my … strength; with … (tools/skills);
  building on …", shown as a highlighted "role statement" bar; task-board cards live-update owner,
  strengths, tools and status.
- Every member's system prompt carries a "group members and their roles" table (who is good at what, which
  model they use, which skills they have), with "← this is you" marked.

### 2. Models: pick any variant, see what's new
- **Add model** opens a picker with every variant of that provider (shipped catalog + live listing +
  already added), each with description, context, tier, strengths and status (new / preview / legacy /
  disabled / no longer listed; local models show installed state and size). Filter by strength, show only
  new, refresh the live listing, or type a variant that is not in the catalog.
- "New" means "appeared since you last looked" (on first open everything counts as seen).
- The shipped catalog was compiled on **2026-09-20** (some variant IDs are inferred from naming patterns).
  Strength tags are **heuristic labels, not benchmark results**. To see what a provider really offers right
  now, hit "Refresh live listing".

### 3. Add members and extensions to a group at any time
Three tabs in the group's right-hand panel: **Members** (role overview; pull in existing members or preset
roles — Host, Reviewer, Scribe, Librarian, Coder, Translator, Analyst, Planner, Editor, QA, Researcher,
Risk, Project manager; set the host) / **Extensions** (attach skills, plugins, MCP servers, library scope,
memory toggle and planning mode to this group; lists the tools actually available here) / **Prompts** (this
group's prompt, apply a prompt from the library, inspect the full system prompt a given member actually
receives). The home page shows five common **group templates** (Office documents, Video production,
Creative writing, Brainstorming, Review meeting); the rest live in **Settings → Template gallery**.

### 4. Skills / plugins / MCP kept separate, and genuinely callable
- Members can call tools from their replies through a **text protocol** (`<tool_call>{JSON}</tool_call>`),
  which does not depend on any vendor's function calling, so hosted and local models both work. Max 3 calls
  per reply, max N rounds (configurable), with a timeout; calls render as expandable tool chips under the bubble.
- Built-in tools: `current_time`, `library_search`, `library_read`, `memory_search`, `memory_save`.
- **Skills** are plain-text prompts (`SKILL.md`), split into "member skills" and "group rules" (the latter
  apply to the whole group — review meeting, brainstorming, relay writing, debate).
- **Plugins** live in `~/.team-agent/plugins/*.py` and **run Python inside this process with no sandbox**.
  Their tools become visible to members only once enabled for a group.
- **MCP**: stdio / SSE / streamable HTTP, connected on first use; secrets in env/headers are always returned
  masked. An MCP server is a local process running with your privileges.

### 5. Library
Upload txt / md / csv / json / html / pdf / docx (or write a note) and it is chunked and indexed. Members
retrieve through `library_search` / `library_read`; each group can be set to All / Selected only / Off;
writing `#document title` in a message quotes the whole document. Files stay in your local data directory,
**but retrieved snippets are sent to whichever model is answering (hosted models included)**.

### 6. Memory
- Three scopes (global / group / member) and five kinds (preference / fact / decision / lesson / playbook).
  Recalled by BM25 + pinned + recency before each reply and injected into the prompt.
- After a group finishes, a cheaper model extracts preferences/decisions/lessons; multi-step or tool-using
  tasks record a "playbook", which the host consults when planning similar work later. Auto-extraction can
  be turned off.
- Auto-extraction filters obvious passwords, keys and long digit runs (**best effort, not a guarantee**)
  and may call a hosted model. Every memory can be viewed, edited, pinned and deleted on the Memory page.

### 7. Prompts
The Prompts page holds the **global system prompt** (editable, variable picker, token estimate, preview
rendered with real members/groups, reset to default) and the **prompt library** (title, content,
`{{variables}}`, preview, token count, "use globally"). Any prompt can be applied as a group's own prompt.

### 8. Updates and discovery (GitHub)
**Settings → Updates & discovery** checks the app itself (release version comparison), the model catalog,
upstream changes for installed skills/plugins, and new models per provider; it also searches GitHub for
skills / plugins / MCP servers. Background checks run at the configured interval (can be disabled; no
network when outbound calls are off).

**Security boundaries kept on purpose:**

| Kind | Behaviour |
| --- | --- |
| Text skills, model catalog | Updatable; skill auto-update is **off by default**; the catalog is JSON validated against a schema |
| Plugins (Python code) | **Never installed automatically**: you must read the full source, tick the confirmation, and install with the sha256 computed at preview time — the server re-downloads and compares, and refuses if it changed |
| MCP servers | **Prefilled form only**; the command is saved after you confirm, never installed automatically |
| The app itself | **Only notifies** about a new version and links to the release page; never downloads or replaces itself |

Anything from GitHub is treated as untrusted input. The app repository field is empty by default (fill in
`owner/repo` before the app itself is checked). An optional GitHub token (no scopes needed — it only raises
the API rate limit) is stored in the system keychain like API keys, and excluded from backups by default.

### 9. Local models: curated catalog, hardware assessment, new-version discovery
**Settings → Local models**:
- **The catalog is not just Qwen**: current open-weight models by vendor (Qwen, Gemma, gpt-oss, DeepSeek,
  GLM, Nemotron, Granite, Mistral, Phi, LFM, OLMo, coding agents, …), each with size, context length,
  licence and strength tags. A snapshot ships with the app (`backend/app/data/local_models.json`) and
  "Updates & discovery" can pull a newer one from GitHub (JSON data, schema-validated).
- **Will this machine run it?** Reads local RAM, disk and GPU acceleration (Apple silicon / NVIDIA / none),
  labels each variant "comfortable / tight / won't fit", marks large models without acceleration as "very
  slow", warns about disk space, and double-confirms before pulling something oversized. **This is a rough
  estimate, not a guarantee** (≤50% of RAM = comfortable, ≤80% = tight).
- **Self-hosted DeepSeek**: DeepSeek-V3 (the GitHub repo is code only, no weights) has been superseded by
  **V4**. V3 is 671B, V4-Flash 284B, V4-Pro a 1.6T-parameter MoE — these need multi-GPU servers and **will
  not run on a normal PC**. The catalog lists vLLM / SGLang launch commands and a preset
  "DeepSeek (self-hosted)" (OpenAI-compatible, default `http://127.0.0.1:30000/v1`). To use DeepSeek on a
  personal computer, use the distilled models (`deepseek-r1`, based on Qwen/Llama — not V3) or the hosted API.
- **Discover new models / versions** (automatic + manual "Check"): ① scrape the current `ollama.com/library`
  listing; ② guess "next generation" names for tracked families (qwen3.8 → qwen3.9 / qwen4) and verify
  against the Ollama registry; ③ new repositories from watched organisations on HuggingFace and GitHub
  (informational only, marked "may not be runnable with Ollama"); ④ check whether your local Ollama is old.
  **Boundaries**: discovery only notifies, it **never downloads weights**; "Add to catalog" just adds a row;
  GitHub / HuggingFace content is untrusted; nothing touches the network when "Allow hosted models" is off.

### 10. Rate limits, reasoning models, error messages
- Rate limits (429, e.g. Kimi's free tier at RPM=3): the SDK's silent retries are disabled for non-Ollama
  providers. If the error carries "retry in N seconds" and N ≤ 5, it **retries exactly once**; otherwise it
  immediately falls back to the next model in the chain (with a local model as the final fallback).
  The "Test" button runs serially so it does not trigger rate limits itself.
- A reasoning model that emits only its thinking and no answer shows "connected" in the test, but falls
  back in chat, with a hint that `max_tokens` may have truncated it.
- Error messages shown in the UI are redacted (key fragments, org IDs, Bearer) and explained.

## Added in v0.4.0 (fifth stage)

### A. Entry points rearranged: skills / plugins / MCP / prompts / library / memory
Six flat entries in the sidebar, and the same pages under "Settings → Tools" — both read the same data.

### B. Two-way memory ⇄ Obsidian sync
**Memory page → Obsidian card**: pick a folder inside a vault (an existing absolute path) and memories sync
there as `.md` files, with edits in Obsidian flowing back:
- Three-way comparison (memory store / file / last sync snapshot): one-sided changes are carried over;
  if **both** changed, Obsidian wins and the overwritten copy goes to `_conflict backup/`.
- Notes deleted in Obsidian delete the corresponding memory, but the content is first moved to `_deleted/`;
  **mass deletions** (more than 5 and more than half, or everything) are refused and require "Force sync".
- New `.md` notes are imported as memories; notes over 500 characters or that look like they contain
  passwords/keys are **not** imported and are not truncated or rewritten. Hidden directories, symlinks and
  non-UTF-8 filenames are skipped.
- Sync manually or every 30 seconds; only one sync runs at a time.

### C. Borrowed from Cherry Studio
- **MCP**: built-in server templates (filesystem, web fetch, knowledge-graph memory, sequential thinking,
  Playwright, time, Git) are **prefilled forms only** and are never auto-installed; **JSON import**
  (compatible with `mcpServers`, saved only if every entry validates); the two marketplaces (mcp.so,
  Smithery) are external links only.
- **Tool permission modes** (Settings → Permissions & control): ask on risky (default: plugins and
  non-read-only MCP) / ask always / allow all; per tool you can "always allow" or "never allow"
  (deny wins); approval prompts appear in the chat, and a timeout or cancel counts as denied.
- **Context management**: history length, per-message truncation and tool-output cap are adjustable;
  **the newest message and the most recent user message are never truncated**.
- **Library sources**: upload files / write notes / add a URL / point at a local folder (recursive, skips
  hidden files and symlinks, re-import only updates what changed), plus a recall test.
- **Data**: export a backup (keys excluded by default; with keys included, key-like MCP parameters are kept
  too), restore from a backup (validated in a temporary copy first, with a snapshot of current data taken
  before restoring), export a chat as Markdown.

### D. "Models I added" directly as group members
- The member card in the group panel has a dropdown listing the models you added; one click pulls one in as
  a member. Deleting a model or provider deletes the members it generated.
- The right-hand panel shows this group's skills / plugins / MCP for quick attachment; members can also be
  added from under "Members" in the sidebar.

### E. Model connectivity indicator
Each model has a light: green = reachable, yellow = limited (rate limit / quota / empty reply), red =
unreachable, grey = untested, off = disabled. It reflects both the "Test" results and real conversations.
Changing a key or restoring a backup clears the history. The light is **not a guarantee** — green only means
the most recent call succeeded.

### F. Permissions & control page
Settings → Permissions & control: approval mode and timeout, always-allow / never-allow lists, the global
outbound switch, per-request timeout, circuit-breaker threshold and cooldown. The backend rejects
out-of-range or wrongly typed settings outright.

### G. Full-stack review (v0.4.0 wrap-up)
Went through backend and frontend node by node and link by link, with static tooling for dead code. The
more important fixes:
- **Orchestration**: `@Allen` is no longer treated as `@all` and e-mail addresses are no longer mentions; an
  error while preparing a turn no longer leaves the group permanently "busy"; a failed plan runs off as
  "failed" with a notice; the user's newest message is never truncated by history.
- **Permissions**: "never allow" is re-checked after approval; plugin tools that collide with a built-in are
  ignored; the `current_time` approval exemption applies only to the built-in; with outbound calls off,
  remote MCP and "Test model" no longer reach the network.
- **Data**: restoring validates before touching live data and clears stale Obsidian links and indicator
  history; backup/restore endpoints require `application/octet-stream` (anti cross-site form); settings are
  type-checked; renaming a member rejects empty, duplicate or `@`-containing names.
- **Frontend**: an offline banner when the backend is gone, with messages and approvals backfilled after
  recovery or WebSocket reconnect; the composer keeps its content when sending fails; settings writes are
  queued serially with visible failures; modal overlays close only when press and release both land on the
  overlay; repeat submissions are debounced; uncaught errors surface as a toast instead of silence.
- Removed unused functions, imports, API wrappers and dead styles.

## Routing rules (router.py)

1. The candidate chain is `[the member's pinned model, or the 2 best models for its role strengths]` + the
   priority chain from settings (default `deepseek-flash → local qwen2.5:7b`), de-duplicated.
2. With "Allow hosted models" off (offline mode) every non-local provider is skipped and **no hosted request
   is made at all** (update checks and remote MCP are refused as well).
3. Missing key / disabled / circuit open (2 consecutive failures → 30 s cooldown) → skipped.
4. Try one by one; auth failures, network errors, timeouts, rate limits and empty replies all trigger a
   fallback; a stream that fails midway clears the partial text and restarts on the next model.
5. If no local model is in the chain, an enabled local model is appended as a fallback (Ollama first).
6. Every message records the model actually used and the outcome of each attempt.

## Local security

The backend listens on `127.0.0.1` only. The desktop build has Electron generate a random token at startup,
and every `/api` and WebSocket request must carry it; the Host header is checked as well. **API keys and the
GitHub token live in the system keychain** (macOS login keychain, `service=team-agent`); the database keeps
only a `keychain:` reference, so backups, exports and a casually copied `.db` file contain no plaintext
secrets. If the keychain is unavailable (non-macOS, or locked) it falls back to plaintext — the active mode
is reported by `/api/system` as `key_secret_backend` (`keychain` / `plaintext`). API keys, the GitHub token
and MCP env/headers are always returned masked; database exports strip them by default (you may opt in, to
move machines). External links are limited to http(s) and handed to the system browser.
**Plugins and MCP servers execute code on your machine with your privileges — only enable ones you trust.**

## Usage stats and data

Usage stats (calls / fallbacks / local share / average latency; **token and cost accounting is not included
yet**); the data page exports a database snapshot (keys excluded by default) and clears chat history.

## Added in v0.5.0 (sixth stage)

### Fixes found by real-hardware integration testing (on a Mac running WorkBuddy)
- **`no such column: rowid` on SQLite ≥ 3.51** (which 500s the whole message-history endpoint and makes
  every member report "failed to speak"): the outer query no longer references a subquery's rowid.
  **If you see that error you are still running old code — upgrade and restart.**
- On Apple silicon with an Intel (Rosetta) Python, hardware detection now identifies the real chip as Metal;
  with a proxy such as Clash running, loopback addresses (local Ollama, local MCP) are added to `NO_PROXY`;
  the failure counter resets once the circuit breaker's cooldown expires; 429s retry using the advertised
  wait time; version numbers compare numerically (`1.10.0` is newer than `1.9.0`); the Electron window got a
  `will-navigate` guard and a tightened preload; a plugin tool that collides with another plugin's now
  errors instead of silently replacing it; a failed plan no longer produces two contradictory messages.

### External agents: WorkBuddy as a group member (Settings → External agents)
- **How it connects**: the WorkBuddy app bundle ships the CodeBuddy Code CLI engine
  (`.../app.asar.unpacked/cli/bin/codebuddy`) with a headless mode (`-p --output-format stream-json`). Each
  turn the app hands it the group transcript, reads its streamed output and posts the reply as that member's
  message. **It does not drive WorkBuddy's window, does not read its account, sessions or keys, and does not
  change any of its settings (including "allow full access").**
- **Off by default**: there is a master switch; without it the engine cannot be created and never runs. It
  also does not run while "no outbound calls" is on (it needs a hosted model).
- **Three permission levels, read-only by default**: read-only (read files/search; cannot write files, run
  commands or use the network) / can edit files (no command line) / full (requires ticking a risk
  confirmation). Networking can be enabled separately. The working directory for edit/full levels cannot be
  `/` or the entire home directory. Verified on real hardware: at read-only, Bash and Write are denied; at
  edit, Write succeeds and Bash is denied; with networking off, WebFetch is denied — blocked actions show as
  "denied" in the group.
- **Boundaries**: an external agent cannot be the host; its replies are treated as chat text only and are
  **never** parsed as a plan or tool call; it starts with a whitelisted environment (no app token, no
  provider keys); cancelling or timing out kills the whole process group; the configuration is re-validated
  at run time (against a tampered database or a restored backup).
- **What it is like**: each turn is a fresh process with no cross-turn memory (context comes from the
  transcript); the first turn takes tens of seconds (~40 s measured, ~7 s afterwards). The "read-only"
  working directory is a starting point, **not a fence** — it can read any file your account can read.

### Template gallery: ready-made teams, roles, skills and prompts (Settings → Template gallery)
- **Works out of the box**: templates ship with the app — no repository to clone, no path to enter, no
  network, no scripts. One click on "Create group / Create member / Import skill / Save to prompt library /
  Add MCP", and it only ever writes text into your local database and `skills` directory.
- **The content is written by this project** (no third-party files or data), so shipping it creates no
  third-party licence obligations. **51 entries**: 12 team templates, 13 role presets, 12 skills,
  7 prompts, 7 MCP recipes.
- **One-click group creation** installs members, host, group rules and dependent skills together: members
  are reused by name (never duplicated); a duplicate group name gets a numeric suffix ("Office documents 2")
  instead of overwriting your group; a bundled skill that already exists keeps your local copy — only
  "Re-import" overwrites it with the template version.
- **Idempotent**: applying the same template twice never duplicates skills, prompts or MCP servers. MCP
  servers are imported **disabled and with no secrets**; review the command and enable them yourself on the
  MCP page.
- **The home page shows only the five common team cards**; the other seven live in the template gallery.
- **Bring your own templates (optional)**: drop JSON into the `templates/` subdirectory of your data
  directory and it appears on this page (refresh to pick it up — no restart). The format is documented with
  an example at the bottom of the page: `team` / `agent` / `skill` / `prompt` are supported; invalid entries
  are skipped **and the reason is shown**, never silently ignored. For safety **`mcp` is not accepted**
  (that would let a JSON file decide which command this machine runs) — add those on the MCP page.

### Home-page group templates
- The home page's "group templates" and the template gallery are the same data (so they cannot drift apart);
  one click creates the group, creating any missing members from the role presets.

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest        # 343 cases (1 needs the real keychain and is skipped)
cd desktop && npm run typecheck && npm run build
```

The backend suite covers **data** (in-place upgrade of a v0.3.1 database, backup/restore round-trip, 3000
messages + 300 memories + 60 documents, bulk Obsidian round-trip), **conversation** (WebSocket multi-turn
@hand-off, planning, approvals, stopping mid-run, recovery after a backend crash), an integration test that
does not mock LiteLLM (local fake services standing in for DeepSeek / Ollama), end-to-end tests against a
**real stdio MCP server**, and updater tests that fake GitHub with `httpx.MockTransport`.

## Known limitations (please read)

- **Secret storage**: API keys and the GitHub token now live in the system keychain (macOS only); on other
  platforms, or if the keychain is unavailable, they fall back to plaintext (check `/api/system`'s
  `key_secret_backend`). MCP server `env`/`headers` are still stored as masked plaintext in the local
  database — only enable MCP on a machine you trust.
- **v0.5.0, additionally unverified**: ① **External agents (WorkBuddy) have only been verified on one Mac
  with one CLI version** (codebuddy 2.137.1, WorkBuddy 5.5.6): the CLI is found, `--version` works, one real
  call succeeded, the stream-json event shapes match the parser, and denying works at all three permission
  levels. **Not verified**: the full shape of `tool_use` / `tool_result` events in real runs (tested against
  docs and a fake engine; denied tools are detected from the error text), whether the permission level
  really blocks writes outside the working directory, the CLI location on Windows / Linux (only the macOS
  app-bundle path and `PATH` are recognised today), and event-format changes in future engine updates.
  ② The template gallery now ships first-party templates (the old "examples" clone / refresh / path-entry
  mechanism was removed entirely); the MCP package names and arguments in it (especially Playwright MCP)
  **were not verified online** — defer to each project's own docs; the `templates/` schema validation
  follows this version's `schema_version`, and older files stay readable when fields are added.
  ③ **On real hardware this version's code has not been through the full suite yet** (all 343 cases pass in
  the development sandbox).
- **v0.4.0, still unverified**: ① WorkBuddy's GUI has never been driven (this version uses its CLI engine
  instead); UI end-to-end was run with Playwright + Chromium (17 items including disconnect/recovery, send
  failure, save failure, Esc and modal behaviour) plus a WebSocket conversation script (33 items), **which
  is not the same as testing the real application**. ② The "Choose folder" button relies on Electron's
  folder-picker IPC and could not be tested without a real Electron shell; in a browser you can only type a
  path. ③ The package names and arguments in the built-in MCP templates (especially Playwright MCP) and the
  mcp.so / Smithery links were not verified online — defer to the respective project docs. ④ Obsidian sync
  was only tested against a fake vault (a plain folder), never with the real Obsidian app holding the vault
  open (Obsidian's own sync plugins writing to the same directory at the same time can race; a conflict
  backup is the safety net).
- **Known but unfixed**: when MCP tool names are de-duplicated with a suffix, "always allow / never allow"
  matches by name and can mismatch on collisions (give MCP servers distinct names); an MCP server's own
  "read-only" declaration is trusted; re-importing a library folder detects changes by file size, so **an
  edit that keeps the size is missed** (delete and re-import manually); `#12`-style document references use
  substring matching; some 402s (insufficient balance) are recorded as "unreachable"; an empty reply or a
  timeout also turns the indicator red.
- Verified in the development sandbox: the full backend suite, frontend type-check and build, and the
  complete UI flow driven by Chromium (fake hosted/local model services + real backend + real LiteLLM + real
  stdio MCP). **Not verified**: a real Electron window, real DeepSeek or other hosted accounts, real Ollama
  weights, **real GitHub** (unreachable from the sandbox — update/search/install flows were only exercised
  against the API and error paths), mcp 2.x (pinned to `mcp<2`), and text extraction from real PDFs. **The
  networked part of the local-model catalog is likewise unverified**: the Ollama registry listing API, the
  ollama.com page structure and the HuggingFace / GitHub discovery requests were only tested against fake
  services, so a page or API change can make them return nothing (the reason is shown in the UI; it does not
  crash). On first run, hit "Test" once each for DeepSeek and Ollama under Providers.
- **Planning quality depends on the host model following the `<plan>` and `<tool_call>` formats**. Small
  models can get the format wrong: a non-conforming plan is downgraded to plain relay with a notice, and a
  bad tool call returns the reason to the model for a retry — no guarantee.
- Strength tags are heuristic; with a single provider (say only DeepSeek) model differences are small and
  strength-based selection adds little — plugging in more models with different characters is what makes it
  worthwhile.
- The model catalog was compiled on 2026-09-20; some variant IDs are inferred from naming patterns and may
  not match the official ones or may have changed; local model sizes come from ollama.com at that time.
  "Next generation" names are pattern guesses and are only surfaced when the registry confirms they exist.
- The vLLM / SGLang commands for self-hosted DeepSeek come from HuggingFace model pages and **have not been
  run**; the hardware-fit assessment is a rough estimate.
- The default local fallback is still `qwen2.5:7b` (backwards compatible); after pulling another model you
  can hit "Set as fallback" on the Local models page.
- Library PDF extraction uses pypdf; scanned (image-only) PDFs have no text layer and are reported as
  unextractable — there is no OCR.
- The UI's layout and style were implemented with reference to screenshots of WorkBuddy / Cherry Studio. It
  is not a pixel-perfect copy, and none of their icons or copy were used.

## Licence and compliance

- **Licence**: this project is released under the [Business Source License 1.1](LICENSE) (BUSL-1.1),
  Licensor **zifulifufu**. In short:
  - **Source-available — read, modify, redistribute**: you may read, modify and use it free of charge for
    non-production purposes (development, testing, evaluation, demonstration, non-commercial research).
  - **Production use needs a commercial licence**: BUSL-1.1 grants non-production use only, and the
    `Additional Use Grant` is `None`, so production use requires a commercial licence from the Licensor.
  - **Converts to MPL 2.0**: the Change Date is **2030-09-21** (four years from the first public release),
    after which this version is licensed under the Mozilla Public License 2.0 with no commercial restriction.
  - **No trademark rights**: the licence does not include any right to the "Team Agent" name or logo.
  - ⚠️ Strictly speaking BUSL-1.1 is a **source-available licence, not an OSI-approved open-source licence**.
    It was chosen so that "free for your own use, licensed for commercial production" can both be true. If
    you want a permissive open-source licence, switch to Apache-2.0; if you want "commercial use allowed
    but no competing hosted service", switch to the Elastic License 2.0.
- **Distribution duty**: BUSL-1.1 requires you to **display this licence conspicuously on every original or
  modified copy** (when packaging for others, include `LICENSE` in the package and make it visible in the
  app). Third-party copyright notices and licence texts must be preserved as well (next bullet).
- **Third-party components**: the full list and licences are in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). All dependencies are permissively licensed
  (MIT / BSD / Apache-2.0 / ISC) with **no GPL or AGPL**, so none of them require you to open-source your own
  code; but **when you redistribute a package** you must keep their copyright notices and licence texts
  (an in-app "open-source licences" page is a good idea).
- **No bundled third-party content**: the repository contains no files or data from third-party projects —
  every team/role/skill/prompt in the template gallery is original text written for this project, so shipping
  it (including selling it packaged) creates no third-party licence obligations. Templates you put in your own
  `templates/` directory are your responsibility.
- **No automatic outbound calls by default**: "Check for updates automatically" is **off** by default and the
  backend does not go online on its own after starting (an old database gets switched off once on upgrade);
  hit "Check for updates" when you want it. Hosted model calls are still governed by the "Allow hosted
  models" switch, which is on by default.
- **Relationship to third-party products**: this is an independently implemented tool. Its layout and
  interactions were implemented with reference to WorkBuddy / Cherry Studio, but **none of their code, icons
  or copy were used**, and there is no affiliation, sponsorship or endorsement by their developers. The
  "external agents" feature merely invokes a command-line program already installed on your machine; those
  programs' licences and terms of service are their own, and complying with them is up to you. Product names
  and trademarks mentioned belong to their respective owners and are used only to describe compatibility or
  interoperability.

## Roadmap

1. Packaging: bundle the backend as a single PyInstaller binary under `resources/backend/` (already supported
   by `electron/main.cjs`), then produce installers with electron-builder; in-app "auto-update" currently
   stops at notifying about a new version.
2. Files and attachments (images, audio/video) in the group chat; finer-grained workflow templates for office
   documents and video production.
3. A plugin sandbox (v0.4.0 added per-call confirmation, but plugins still have no sandbox).
4. ~~Move API keys into the system keychain~~ **Done** (including the GitHub token; MCP `env`/`headers` are
   still masked plaintext in the local database — still to do).
