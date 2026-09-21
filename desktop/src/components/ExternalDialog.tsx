import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FolderOpen, Loader2, ShieldAlert } from "lucide-react";
import { api, type Agent, type ExternalCfg, type ExternalLevel, type ExternalOverview, type ExternalProbe, type Group } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Modal, useBusy } from "../ui";
import "../styles/external.css";

type Props =
  | { mode: "create"; group?: Group; onClose: () => void; onDone: () => void }
  | { mode: "edit"; agent: Agent; onClose: () => void; onDone: () => void };

const LEVEL_ORDER: ExternalLevel[] = ["read", "edit", "full"];

/** Add / configure an external agent (WorkBuddy): permission level, working directory, hand-off. Read-only by default. */
export default function ExternalDialog(props: Props) {
  const { t } = useI18n();
  const { reload, reloadGroups } = useData();
  const edit = props.mode === "edit" ? props.agent : null;
  const [ov, setOv] = useState<ExternalOverview | null>(null);
  const [cfg, setCfg] = useState<ExternalCfg | null>(null);
  const [name, setName] = useState("WorkBuddy");
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

  const eng = ov?.engines[0];
  const set = (p: Partial<ExternalCfg>) => setCfg((c) => (c ? { ...c, ...p } : c));
  const wasFull = edit?.engine_cfg?.level === "full";
  const needAck = !!cfg && cfg.level === "full" && !wasFull;
  const blocked = !ov?.enabled;

  const enableSwitch = () => run(async () => {
    setErr("");
    try { await api.putSettings({ external_agents_enabled: true }); await reload(); await load(); } catch (e) { setErr((e as Error).message); }
  });

  const test = async (live: boolean) => {
    setTesting(live ? "live" : "quick");
    setErr("");
    try {
      setProbe(await api.externalTest({ live, agent_id: edit?.id, cli_path: cfg?.cli_path || undefined }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setTesting("");
    }
  };

  const payload = (): Partial<ExternalCfg> => {
    const c = cfg!;
    return { level: c.level, risk_ack: needAck ? ack : c.risk_ack, web: c.level === "full" ? false : c.web, cwd: c.cwd.trim(), handoff: c.handoff, model: c.model.trim(), timeout: c.timeout, cli_path: c.cli_path.trim() };
  };

  const submit = () => run(async () => {
    if (!cfg) return;
    setErr("");
    try {
      if (props.mode === "create") {
        await api.externalCreate({ engine: "workbuddy", name: name.trim() || undefined, group_id: props.group?.id, cfg: payload() });
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

  const title = props.mode === "create" ? t("Add external agent · WorkBuddy") : t("External agent settings · {name}", { name: props.agent.name });
  return (
    <Modal
      title={title}
      onClose={props.onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={props.onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || blocked || !cfg || (needAck && !ack)} onClick={() => void submit()}>
            {props.mode === "create" ? (props.group ? t("Create and add to this group") : t("Create")) : t("Save")}
          </button>
        </>
      }
    >
      <div className="ext-dlg">
        <p className="ext-intro">
          {t("Let WorkBuddy take part in the discussion as a group member. This app calls the")} <b>{t("command-line engine bundled with WorkBuddy")}</b>{t(" (headless mode), hands it the group chat on every turn, and takes back its reply.")}
          {t("It does not drive the WorkBuddy window, and never reads its account, sessions, or keys. It brings its own tools (reading files, retrieval, and so on), which is why the permissions are yours to set here.")}
        </p>

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
          <div className="ext-box warn" role="status"><AlertTriangle size={15} /><div>{t("Outbound calls are blocked right now: WorkBuddy needs a cloud model, so it will not run in this mode. Allow it under Settings → Routing first.")}</div></div>
        )}

        <div className="ext-status">
          {!eng ? <span className="muted small">{t("Checking…")}</span> : eng.found ? (
            <span className="ext-ok"><CheckCircle2 size={14} /> {t("Command-line engine found")}{probe?.version ? t(" · version {v}", { v: String(probe.version) }) : ""}<small title={eng.path}>{eng.path}</small></span>
          ) : (
            <span className="ext-bad"><AlertTriangle size={14} /> {eng.hint}</span>
          )}
          <span className="ext-status-btns">
            <button className="btn small" disabled={blocked || !!testing} onClick={() => void test(false)}>{testing === "quick" ? <Loader2 size={12} className="spin" /> : null} {t("Check")}</button>
            <button className="btn small" disabled={blocked || !!testing || !ov?.external_calls_enabled} onClick={() => void test(true)} title={t("Actually send one sentence — this calls the cloud model and uses a very small amount of quota")}>
              {testing === "live" ? <Loader2 size={12} className="spin" /> : null} {t("Test the connection")}
            </button>
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
                <input value={name} onChange={(e) => setName(e.target.value)} maxLength={30} />
              </label>
            )}

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

            <label className="field">
              <span>{t("Working directory (the scope it may read and write within; empty = an empty folder created for it under this app's data directory)")}</span>
              <span className="ext-dir">
                <input value={cfg.cwd} onChange={(e) => set({ cwd: e.target.value })} placeholder={t("empty = its own empty folder (safest)")} />
                {pick && <button type="button" className="btn small" onClick={async () => { const p = await pick(); if (p) set({ cwd: p }); }}><FolderOpen size={13} /> {t("Choose…")}</button>}
              </span>
              {cfg.level !== "read" && !cfg.cwd.trim() && <span className="muted small">{t("It is set to {level} right now: it can only touch things inside its own empty folder. To let it work on your project, pick a specific project folder (not the root, and not your whole home directory).", { level: cfg.level === "edit" ? t("Edit files") : t("Full") })}</span>}
            </label>

            <label className="check">
              <input type="checkbox" checked={cfg.handoff} onChange={(e) => set({ handoff: e.target.checked })} />
              {t("When its reply @mentions another member, that member speaks next")}
            </label>

            <details className="ext-adv">
              <summary>{t("Advanced")}</summary>
              <div className="form-row">
                <label className="field grow"><span>{t("Model (empty = the engine default)")}</span><input value={cfg.model} onChange={(e) => set({ model: e.target.value })} /></label>
                <label className="field" style={{ width: 130 }}><span>{t("Timeout per turn (seconds)")}</span><input type="number" min={30} max={3600} value={cfg.timeout} onChange={(e) => set({ timeout: Number(e.target.value) || 600 })} /></label>
              </div>
              <label className="field"><span>{t("Command-line path (empty = find the one bundled with WorkBuddy automatically; the file name must start with codebuddy)")}</span><input value={cfg.cli_path} onChange={(e) => set({ cli_path: e.target.value })} /></label>
            </details>
          </>
        )}

        <p className="ext-foot muted small">
          {t("Note: every WorkBuddy turn is a separate process with no memory across turns (the context comes from the group chat), and a reply usually takes tens of seconds. Its reply is treated purely as chat text — never executed as a plan or a tool call. Nothing in the WorkBuddy app itself is changed, including its own all-access setting.")}
        </p>
        {err && <div className="err" role="alert">{err}</div>}
      </div>
    </Modal>
  );
}
