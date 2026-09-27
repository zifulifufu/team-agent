import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, FolderOpen, Loader2, Server, ShieldAlert } from "lucide-react";
import { api, type Agent, type ExternalCfg, type ExternalLevel, type ExternalOverview, type ExternalProbe, type Group } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Modal, useBusy } from "../ui";
import "../styles/external.css";

type Props =
  | { mode: "create"; group?: Group; engine?: string; onClose: () => void; onDone: () => void }
  | { mode: "edit"; agent: Agent; onClose: () => void; onDone: () => void };

const LEVEL_ORDER: ExternalLevel[] = ["read", "edit", "full"];

/** Add / configure an external agent member. Two kinds of engine share this dialog:
 * a command-line engine (WorkBuddy — permission level, working directory, hand-off) and a chat
 * gateway (Cherry Studio, MetaChat — an OpenAI-compatible endpoint, a key, a model). */
export default function ExternalDialog(props: Props) {
  const { t } = useI18n();
  const { reload, reloadGroups } = useData();
  const edit = props.mode === "edit" ? props.agent : null;
  const [ov, setOv] = useState<ExternalOverview | null>(null);
  const [cfg, setCfg] = useState<ExternalCfg | null>(null);
  const [engine, setEngine] = useState(edit?.engine || (props.mode === "create" ? props.engine : "") || "workbuddy");
  const [name, setName] = useState("");
  const [ack, setAck] = useState(false);
  const [probe, setProbe] = useState<ExternalProbe | null>(null);
  const [testing, setTesting] = useState<"" | "quick" | "live">("");
  const [err, setErr] = useState("");
  const [busy, run] = useBusy();
  const pick = window.teamAgent?.pickFolder;

  const load = useCallback(async () => {
    try {
      const o = await api.externalOverview();
      setOv(o);
      setCfg((c) => c ?? { ...o.defaults, ...(edit?.engine_cfg ?? {}) });
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [edit]);
  useEffect(() => { void load(); }, [load]);

  const eng = ov?.engines.find((e) => e.id === engine) ?? ov?.engines[0];
  const isHttp = eng?.kind === "http";
  // A local tool (`cmd`) is a *program*, not a conversation partner: `external.clean_cfg` ignores
  // its permission level, its working directory, its model address and its key, and it runs the
  // tool's own command inside the group's workspace. So the dialog must not ask for them — the
  // one field it does need is where that program is, for the tools installed into a clone's
  // virtualenv rather than onto PATH (`localcmd.exe_for`).
  const isCmd = eng?.kind === "cmd";
  // Non-null = this engine talks through a model provider (MetaChat): the address, the key and the
  // model list are that provider's, so the dialog asks for none of them — only the model, picked
  // from what the provider offers.
  const bound = eng?.provider ?? null;
  // An engine that lives on a provider is not something to *add*: its models join a group as
  // ordinary members from "Models I added", so offering it here would be the second way in that
  // this change removes. Members already using it stay listed and editable in Settings.
  const pickable = ov?.engines.filter((e) => !e.provider) ?? [];
  // WorkBuddy is one command line on this machine, so there is one member to add (`single`). Once
  // one exists the engine is greyed out rather than offered and then refused by the backend.
  const taken = (id: string) => {
    const e = ov?.engines.find((x) => x.id === id);
    return !!e?.single && !!ov?.members.some((m) => m.engine === id);
  };
  const set = (p: Partial<ExternalCfg>) => setCfg((c) => (c ? { ...c, ...p } : c));
  const wasFull = edit?.engine_cfg?.level === "full";
  const needAck = !!cfg && cfg.level === "full" && !wasFull;
  const blocked = !ov?.enabled;

  // Adding a member starts on the first engine that is not already spoken for, so the dialog does
  // not open on a choice that would be refused the moment it is submitted.
  useEffect(() => {
    if (!ov || props.mode !== "create" || !taken(engine)) return;
    const next = ov.engines.find((e) => !e.provider && !taken(e.id));
    if (next) setEngine(next.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ov]);

  const enableSwitch = () => run(async () => {
    setErr("");
    try { await api.putSettings({ external_agents_enabled: true }); await reload(); await load(); } catch (e) { setErr((e as Error).message); }
  });

  const test = async (live: boolean) => {
    setTesting(live ? "live" : "quick");
    setErr("");
    try {
      // With a saved member the backend uses its stored settings; before it is saved, whatever is
      // typed here is used. A bound engine has no address or key to pass — the backend reads those
      // from the provider — so only the model goes along.
      const body = { live, engine, agent_id: edit?.id, cli_path: cfg?.cli_path || undefined, model: cfg?.model || undefined };
      setProbe(await api.externalTest(edit || bound ? body
        : { ...body, base_url: cfg?.base_url || undefined, api_key: cfg?.api_key || undefined }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setTesting("");
    }
  };

  const payload = (): Partial<ExternalCfg> => {
    const c = cfg!;
    if (bound) {
      // Nothing of the provider's is copied into the member: only what the provider has no opinion
      // about. The backend clears base_url/api_key here anyway; not sending them keeps that obvious.
      return { model: c.model, timeout: c.timeout, handoff: c.handoff };
    }
    if (isHttp) {
      return { model: c.model.trim(), timeout: c.timeout, handoff: c.handoff,
               base_url: c.base_url.trim(),
               // "***" tells the backend to keep the key it already has
               api_key: c.api_key.trim() || (c.has_key ? "***" : "") };
    }
    return { level: c.level, risk_ack: needAck ? ack : c.risk_ack, web: c.level === "full" ? false : c.web, cwd: c.cwd.trim(), handoff: c.handoff, model: c.model.trim(), timeout: c.timeout, cli_path: c.cli_path.trim(), native: c.native, max_turns: c.max_turns,
             // A command line can be pointed at a model of the user's own: the address and the key
             // go over as CODEBUDDY_BASE_URL and CODEBUDDY_API_KEY, and the model above as --model.
             // That trio is what makes it runnable on a build whose command line has no sign-in
             // screen at all, so the same two fields a gateway uses are saved here too.
             base_url: c.base_url.trim(),
             // Same field as a gateway's key, and the same "***" rule: it is handed to the engine as
             // CODEBUDDY_API_KEY, which is a sign-in route that does not need a terminal at all.
             api_key: c.api_key.trim() || (c.has_key ? "***" : "") };
  };

  const submit = () => run(async () => {
    if (!cfg) return;
    setErr("");
    try {
      if (props.mode === "create") {
        await api.externalCreate({ engine, name: name.trim() || undefined, group_id: props.group?.id, cfg: payload() });
        await reload();
      } else {
        await api.externalPatch(props.agent.id, payload());
        await reload();
        await reloadGroups();
      }
      props.onDone();
    } catch (e) {
      setErr((e as Error).message);
    }
  });

  const title = props.mode === "create" ? t("Add external agent") : t("External agent settings · {name}", { name: props.agent.name });
  return (
    <Modal
      title={title}
      onClose={props.onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={props.onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || blocked || !cfg || (needAck && !ack) || (!!bound && !cfg.model)} onClick={() => void submit()}>
            {props.mode === "create" ? (props.group ? t("Create and add to this group") : t("Create")) : t("Save")}
          </button>
        </>
      }
    >
      <div className="ext-dlg">
        {props.mode === "create" && ov && (
          <div className="field">
            <span>{t("Which one should join?")}</span>
            <div className="ext-engines" role="radiogroup" aria-label={t("Which one should join?")}>
              {pickable.map((e) => (
                <label key={e.id} className={"ext-engine" + (engine === e.id ? " on" : "") + (taken(e.id) ? " off" : "")}>
                  <input type="radio" name="ext-engine" checked={engine === e.id} disabled={taken(e.id)} onChange={() => setEngine(e.id)} />
                  <span className="ext-engine-body">
                    <b>{e.avatar} {e.name}</b>
                    <small>{taken(e.id)
                      ? t("Already added — this engine is one command line on this machine, so there is only one member to make of it. Change that member's settings instead.")
                      : e.kind === "http" ? t("A chat gateway you already run, over its OpenAI-compatible endpoint. It joins the discussion only — no files, no commands.") : t("A command-line engine that brings its own tools (files, retrieval). It can act on this machine, so its permissions are yours to set.")}</small>
                  </span>
                </label>
              ))}
            </div>
          </div>
        )}

        {isHttp ? (
          <p className="ext-intro">
            {t("It talks to {name}'s OpenAI-compatible endpoint: the group chat goes out as messages and the reply comes back.", { name: eng?.name ?? "" })}
            {" "}{t("It has no tools on this machine — it cannot read files, run commands or browse — so it takes part in the discussion, not in the work on your disk.")}
          </p>
        ) : (
          <p className="ext-intro">
            {t("Let WorkBuddy take part in the discussion as a group member. This app calls the")} <b>{t("command-line engine bundled with WorkBuddy")}</b>{t(" (headless mode), hands it the group chat on every turn, and takes back its reply.")}
            {t("It does not drive the WorkBuddy window, and never reads its account, sessions, or keys. It brings its own tools (reading files, retrieval, and so on), which is why the permissions are yours to set here.")}
          </p>
        )}

        {blocked && ov && (
          <div className="ext-box warn" role="alert">
            <ShieldAlert size={15} />
            <div>
              <b>{t("The external agent master switch is still off.")}</b>{t("They are agents with tools that may read and write your files, so they are off by default and you have to turn them on.")}
              <div><button className="btn small" disabled={busy} onClick={() => void enableSwitch()}>{t("Turn on the master switch")}</button></div>
            </div>
          </div>
        )}
        {ov && !ov.external_calls_enabled && (
          <div className="ext-box warn" role="status"><AlertTriangle size={15} /><div>{isHttp
            ? t("Outbound calls are blocked right now: this engine reaches its endpoint over the network, so it will not run in this mode. Allow it under Settings → Routing first.")
            : t("Outbound calls are blocked right now: WorkBuddy needs a cloud model, so it will not run in this mode. Allow it under Settings → Routing first.")}</div></div>
        )}

        <div className="ext-status">
          {!eng ? <span className="muted small">{t("Checking…")}</span> : isHttp ? (
            <span className={(() => { const u = bound ? bound.base_url : cfg?.base_url || eng.base_url; return u ? "ext-ok" : "ext-bad"; })()}>
              <CheckCircle2 size={14} /> {t("Chat gateway · {url}", { url: (bound ? bound.base_url : cfg?.base_url || eng.base_url) || t("no address yet") })}
            </span>
          ) : eng.found ? (
            <span className="ext-ok"><CheckCircle2 size={14} /> {t("Command-line engine found")}{probe?.version ? t(" · version {v}", { v: String(probe.version) }) : ""}<small title={eng.path}>{eng.path}</small></span>
          ) : (
            <span className="ext-bad"><AlertTriangle size={14} /> {eng.hint}</span>
          )}
          <span className="ext-status-btns">
            <button className="btn small" disabled={blocked || !!testing} onClick={() => void test(false)}>{testing === "quick" ? <Loader2 size={12} className="spin" /> : null} {t("Check")}</button>
            {!isCmd && <button className="btn small" disabled={blocked || !!testing || !ov?.external_calls_enabled} onClick={() => void test(true)} title={t("Actually send one sentence — this calls the cloud model and uses a very small amount of quota")}>
              {testing === "live" ? <Loader2 size={12} className="spin" /> : null} {t("Test the connection")}
            </button>}
          </span>
        </div>
        {probe?.live && (
          probe.live.ok
            ? <div className="ext-box ok" role="status"><CheckCircle2 size={15} /><div>{t("Connection is fine: the engine replied {reply} in {secs} seconds{model}.", { reply: String(probe.live.reply), secs: String(probe.live.seconds), model: probe.live.model ? t(", model {m}", { m: String(probe.live.model) }) : "" })}</div></div>
            : <div className="ext-box warn" role="alert"><AlertTriangle size={15} /><div>{t("The test did not pass: {err}", { err: String(probe.live.error) })}</div></div>
        )}
        {probe && !probe.live && probe.hint && <div className="ext-box warn"><AlertTriangle size={15} /><div>{probe.hint}</div></div>}

        {cfg && (
          <>
            {props.mode === "create" && (
              <label className="field">
                <span>{t("Member name (mention it as @name in a group; no spaces)")}</span>
                <input value={name} onChange={(e) => setName(e.target.value)} maxLength={30} placeholder={eng ? eng.name.replace(/\s+/g, "") : ""} />
              </label>
            )}

            {isHttp ? (
              <>
                {bound ? (
                  <>
                    <div className={"ext-bound" + (bound.missing ? " bad" : "")}>
                      <Server size={15} aria-hidden />
                      <div>
                        <b>{t("Model provider: {name}", { name: bound.name })}</b>
                        <small>{bound.missing
                          ? t("There is no provider with that id any more, so this member has nothing to talk to. Add it back under Settings → Providers and it will work again.")
                          : t("{address}{key} — the address, the key and the models all come from that provider, so they are set once, there.", {
                              address: bound.base_url || t("no address set"),
                              key: bound.has_key ? t(" · a key is set") : t(" · no key yet"),
                            })}</small>
                      </div>
                    </div>
                    <label className="field">
                      <span>{t("Model to call (from this provider's models)")}</span>
                      <select value={bound.models.some((m) => m.name === cfg.model) ? cfg.model : ""}
                              onChange={(e) => set({ model: e.target.value })}>
                        <option value="">{t("Choose one…")}</option>
                        {bound.models.map((m) => <option key={m.name} value={m.name}>{m.display_name || m.name}</option>)}
                      </select>
                      {bound.models.length === 0 ? (
                        <span className="muted small">{t("That provider has no enabled models yet — add one under Settings → Providers.")}</span>
                      ) : !cfg.model ? (
                        <span className="muted small">{t("Pick one before saving: the gateway has to be told which model to run.")}</span>
                      ) : null}
                    </label>
                  </>
                ) : (
                  <>
                    {/* Which service, as one choice. A command-line engine has no sign-in of its
                        own, so it needs an address, a key and **the model name the service itself
                        spells** — and the one mistake this list removes is a display name in the
                        model field: WorkBuddy shows this user's DeepSeek as "DeepSeek-V4 Flash",
                        while the API only answers to `deepseek-flash`. Picking a service fills the
                        address and the model; the key is the only thing left to paste. */}
                    {(ov?.model_presets?.length ?? 0) > 0 && (
                      <label className="field">
                        <span>{t("Fill in the address and model for me (measured working on {d})", { d: ov?.presets_verified ?? "" })}</span>
                        <select value={ov?.model_presets.find((p) => p.base_url === cfg.base_url)?.id ?? ""}
                                onChange={(e) => {
                                  const p = ov?.model_presets.find((x) => x.id === e.target.value);
                                  if (p) set({ base_url: p.base_url, model: p.measured[0] ?? p.models[0] ?? "" });
                                }}>
                          <option value="">{t("I will type the address and the model myself")}</option>
                          {(ov?.model_presets ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                        </select>
                        {(() => {
                          const p = ov?.model_presets.find((x) => x.base_url === cfg.base_url);
                          if (!p) return null;
                          return (
                            <span className="muted small">
                              {t("Answered a real call from here: {m}. The service also serves: {all}.",
                                 { m: p.measured.join(", "), all: p.models.join(", ") })}
                              {" · "}{t("Where to get a key:")} {p.where}
                            </span>
                          );
                        })()}
                      </label>
                    )}
                    <label className="field">
                      <span>{t("API address (empty = the default for this engine)")}</span>
                      <input value={cfg.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder={eng?.base_url ?? ""} spellCheck={false} />
                    </label>
                    <label className="field">
                      <span>{t("API key")}</span>
                      <input type="password" value={cfg.api_key} onChange={(e) => set({ api_key: e.target.value })} spellCheck={false}
                             placeholder={cfg.has_key ? t("A key is stored already — leave this empty to keep it") : ""} />
                      {eng?.key_hint && (
                        <span className="muted small">
                          {t("Where to get a key:")} {eng.key_hint}
                          {eng.docs ? <> · <a href={eng.docs} target="_blank" rel="noreferrer">{t("Open the documentation")} <ExternalLink size={11} aria-hidden /></a></> : null}
                        </span>
                      )}
                    </label>
                    <label className="field">
                      <span>{t("Model to call (required: a gateway has to be told which model to run)")}</span>
                      <input value={cfg.model} onChange={(e) => set({ model: e.target.value })} placeholder="gpt-5 / claude-sonnet-4-6 / …" spellCheck={false} list="ext-preset-models" />
                      {/* The names as the services themselves spell them — an API id, which is not
                          the display name any desktop app shows it under. */}
                      <datalist id="ext-preset-models">
                        {[...new Set((ov?.model_presets ?? []).flatMap((p) => p.models))].map((m) => <option key={m} value={m} />)}
                      </datalist>
                    </label>
                  </>
                )}
                <label className="check">
                  <input type="checkbox" checked={cfg.handoff} onChange={(e) => set({ handoff: e.target.checked })} />
                  {t("When its reply @mentions another member, that member speaks next")}
                </label>
                <details className="ext-adv">
                  <summary>{t("Other settings")}</summary>
                  <div className="form-row">
                    <label className="field" style={{ width: 150 }}><span>{t("Timeout per turn (seconds)")}</span><input type="number" min={30} max={3600} value={cfg.timeout} onChange={(e) => set({ timeout: Number(e.target.value) || 600 })} /></label>
                  </div>
                </details>
                <p className="muted small">{bound
                  ? t("This engine only exchanges messages: there is no working directory and no permission level to set, and nothing about the provider is duplicated here.")
                  : t("This engine only exchanges messages: there is no working directory and no permission level to set.")}</p>
                {isCmd && <p className="muted small">{t("This member is a program on this machine: it takes the sentence it was given, runs its own command inside this group's workspace, and hands back the files it produced. There is no permission level, no working directory and no model to set — what it runs is fixed by the tool itself, and the only thing it may need from you is the path above.")}</p>}
              </>
            ) : (
              <>
                {!isCmd && (
                  <>
                <div className="field">
                  <span>{t("Permission level")}</span>
                  <div className="ext-levels" role="radiogroup" aria-label={t("Permission level")}>
                    {LEVEL_ORDER.map((id) => {
                      const lv = ov?.levels.find((l) => l.id === id);
                      return (
                        <label key={id} className={"ext-level" + (cfg.level === id ? " on" : "") + (id === "full" ? " danger" : "")}>
                          <input type="radio" name="ext-level" checked={cfg.level === id} onChange={() => set({ level: id })} />
                          <span><b>{lv?.label ?? id}</b>{id === "read" && <em>{t("Recommended")}</em>}<small>{lv?.desc}</small></span>
                        </label>
                      );
                    })}
                  </div>
                  {needAck && (
                    <label className="check ext-ack">
                      <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                      {t("I understand that Full unlocks running commands, reading and writing files, and network access — and that anything anyone says in the group, or any file or page it reads, could make it take a consequential action")}
                    </label>
                  )}
                  {cfg.level !== "full" && (
                    <label className="check ext-web">
                      <input type="checkbox" checked={cfg.web} onChange={(e) => set({ web: e.target.checked })} />
                      {t("Allow it to search the web / fetch pages (off by default)")}
                    </label>
                  )}
                </div>

                  </>
                )}
                {!isCmd && (
                  <>
                <label className="check ext-native">
                  <input type="checkbox" checked={cfg.native} onChange={(e) => set({ native: e.target.checked })} />
                  {t("Use the application's own configuration (load its MCP connectors, no turn limit, and keep one continuing session so it remembers its earlier turns) — off by default, because this is what makes its answers match running it by hand, and it is also what gives it the reach it has there")}
                </label>

                <label className="field">
                  <span>{t("Working directory (the scope it may read and write within; empty = an empty folder created for it under this app's data directory)")}</span>
                  <span className="ext-dir">
                    <input value={cfg.cwd} onChange={(e) => set({ cwd: e.target.value })} placeholder={t("empty = its own empty folder (safest)")} />
                    {pick && <button type="button" className="btn small" onClick={async () => { const p = await pick(); if (p) set({ cwd: p }); }}><FolderOpen size={13} /> {t("Choose…")}</button>}
                  </span>
                  {cfg.level !== "read" && !cfg.cwd.trim() && <span className="muted small">{t("It is set to {level} right now: it can only touch things inside its own empty folder. To let it work on your project, pick a specific project folder (not the root, and not your whole home directory).", { level: cfg.level === "edit" ? t("Edit files") : t("Full") })}</span>}
                </label>

                  </>
                )}
                <label className="check">
                  <input type="checkbox" checked={cfg.handoff} onChange={(e) => set({ handoff: e.target.checked })} />
                  {t("When its reply @mentions another member, that member speaks next")}
                </label>

                {!isCmd && (
                  <>
                <label className="field">
                  <span>{t("Model address (optional — an OpenAI-compatible endpoint this member should run on)")}</span>
                  <input value={cfg.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder="https://api.deepseek.com/v1" spellCheck={false} />
                  <span className="muted small">{eng?.signin
                    ? t("Fill this in with a key and a model name and the engine runs that model instead of the one tied to its own account — useful when signing it in by hand is inconvenient.")
                    : t("This build of WorkBuddy's command line has no sign-in screen (no /login to type in a terminal, and no codebuddy command on your PATH), so it cannot use the models tied to the WorkBuddy account. Fill in these three fields — address, key, model — and it runs that model instead, with the tools it brings.")}</span>
                </label>

                <label className="field">
                  <span>{t("Model key (optional)")}</span>
                  <input type="password" value={cfg.api_key} onChange={(e) => set({ api_key: e.target.value })} spellCheck={false}
                         placeholder={cfg.has_key ? t("A key is stored already — leave this empty to keep it") : ""} />
                  {eng?.key_hint && (
                    <span className="muted small">
                      {t("Where to get a key:")} {eng.key_hint}
                      {eng.docs ? <> · <a href={eng.docs} target="_blank" rel="noreferrer">{t("Open the documentation")} <ExternalLink size={11} aria-hidden /></a></> : null}
                    </span>
                  )}
                  <span className="muted small">
                    {t("It is kept in the keychain and handed to the engine on every run, so it does not depend on how this app was started.")}
                  </span>
                </label>

                <label className="field">
                  <span>{t("Model name (optional — empty = the engine's own default model)")}</span>
                  <input value={cfg.model} onChange={(e) => set({ model: e.target.value })} placeholder="deepseek-chat / gpt-5 / …" spellCheck={false} />
                  <span className="muted small">{t("The model name is passed to the service as written, so use the name that service documents.")}</span>
                </label>

                  </>
                )}
                <details className="ext-adv" open={isCmd}>
                  <summary>{t("Advanced")}</summary>
                  <div className="form-row">
                    <label className="field" style={{ width: 130 }}><span>{t("Timeout per turn (seconds)")}</span><input type="number" min={30} max={3600} value={cfg.timeout} onChange={(e) => set({ timeout: Number(e.target.value) || 600 })} /></label>
                    {!isCmd && (
                      <label className="field" style={{ width: 130 }}><span>{t("Max turns per reply")}</span><input type="number" min={1} max={100} value={cfg.max_turns} disabled={cfg.native} onChange={(e) => set({ max_turns: Number(e.target.value) || 20 })} /></label>
                    )}
                  </div>
                  {/* The one setting a local tool needs. Its install command is often "clone this and
                      run `uv sync`", which puts the console script inside *that clone's* virtualenv —
                      a place this app cannot guess, so the field has to be here and the label has to
                      say what the path looks like. */}
                  <label className="field"><span>{isCmd
                    ? t("Path to the program (empty = look for it by name; for a tool installed with uv sync it lives inside the clone, e.g. <clone>/.venv/bin/omnivoice-infer)")
                    : t("Command-line path (empty = find the one bundled with WorkBuddy automatically; the file name must start with codebuddy)")}</span><input value={cfg.cli_path} onChange={(e) => set({ cli_path: e.target.value })} spellCheck={false} placeholder={isCmd && eng?.path ? eng.path : ""} /></label>
                </details>
              </>
            )}
          </>
        )}

        <p className="ext-foot muted small">
          {isHttp
            ? t("Note: every turn is a separate request with no memory across turns (the context comes from the group chat), and its reply is treated purely as chat text — never executed as a plan or a tool call.")
            : t("Note: every WorkBuddy turn is a separate process with no memory across turns (the context comes from the group chat), and a reply usually takes tens of seconds. Its reply is treated purely as chat text — never executed as a plan or a tool call. Nothing in the WorkBuddy app itself is changed, including its own all-access setting.")}
        </p>
        {err && <div className="err" role="alert">{err}</div>}
      </div>
    </Modal>
  );
}
