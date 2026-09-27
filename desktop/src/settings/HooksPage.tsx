import { useCallback, useEffect, useState } from "react";
import { Anchor, ChevronDown, ChevronRight, CircleCheck, CircleX, FileCode2, Info, LoaderCircle, Play, RefreshCw, X } from "lucide-react";
import { api, relTime, type HookEntry, type HookLogRow } from "../api";
import { Callout } from "../components/ExtBits";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch, useFlash } from "../ui";
import "../styles/hooks.css";

/** Hooks: the user's own code at six fixed points of a group chat.
 *
 *  Two things are deliberate. The state lives in the **list** — on/off, the last run, the reason
 *  it failed — rather than in a dialog: a hook is code running on this machine, and that has to
 *  be visible at a glance. And every hook can be **run once from the page**, because "it is
 *  installed" and "it works" are different claims and this page should not ask for belief in the
 *  first one.
 */
/** What a hook of each kind may change, in one place. Three kinds, three different powers:
 *  an observer is told and ignored, an injector may only add lines to a prompt, a gate may object
 *  or rewrite the thing it stands in front of. The label says which, because it decides what the
 *  user has to trust that hook with. */
function kindOf(kind: string): { label: string; title: string; warn: boolean } {
  if (kind === "gate") {
    return { label: "Gate",
             title: "Asked before something irreversible happens: it may object or rewrite, never grant",
             warn: true };
  }
  if (kind === "inject") {
    return { label: "Adds to the prompt",
             title: "May add lines to a prompt and nothing else — it cannot replace or remove what the app wrote",
             warn: false };
  }
  return { label: "Observer", title: "Told what happened; its answer is ignored", warn: false };
}


export default function HooksPage() {
  const { t } = useI18n();
  const { groups } = useData();
  const [hooks, setHooks] = useState<HookEntry[] | null>(null);
  const [log, setLog] = useState<HookLogRow[]>([]);
  const [guide, setGuide] = useState("");
  const [dir, setDir] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [openLog, setOpenLog] = useState(false);
  const [openGuide, setOpenGuide] = useState(false);
  const [source, setSource] = useState<{ id: string; content: string } | null>(null);
  const [flash, ok] = useFlash();

  const load = useCallback(async () => {
    try {
      const r = await api.hooks();
      setHooks(r.hooks);
      setGuide(r.guide);
      setDir(r.directory);
      setErr(r.errors.join(" · "));
      setLog((await api.hooksLog(50)).entries);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  /** Every action reloads afterwards: what this page shows is the hook's own file, and the file
   *  is the truth — not whatever this component happened to remember. */
  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setErr("");
    try {
      await fn();
      await load();
      ok();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  if (hooks === null) return <div className="sp">{err ? <div className="ext-errbox"><p className="err" role="alert">{err}</p><button className="btn" onClick={() => void load()}>{t("Retry")}</button></div> : <p className="sp-desc">{t("Loading…")}</p>}</div>;

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("Hooks")}</h2>
        <div className="sp-head-actions">
          <button className="btn" onClick={() => void act("reload", () => api.reloadHooks())}>
            {busy === "reload" ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />} {t("Reload")}
          </button>
          <button className="btn" onClick={() => setOpenGuide((v) => !v)}><Info size={14} /> {t("How to write one")}</button>
        </div>
      </div>
      <p className="sp-desc">
        {t("Your own code at a few fixed points of a group chat. Each hook runs in its own process with a trimmed environment and a timeout; it is off until you switch it on, and a gate can only object or rewrite — never grant what your permission settings forbid.")}
      </p>
      <div className="muted small hook-dir">{t("Folder")}: <code>{dir}</code></div>
      {flash && <div className="ok-text small" style={{ padding: "6px 0" }}>{t("Done")}</div>}
      {err && <div className="err" role="alert">{err}</div>}
      {openGuide && <pre className="hook-guide">{guide}</pre>}

      {hooks.length === 0 && (
        <div className="card muted small">{t("No hooks yet. A hook is one folder with HOOK.json and hook.py — the folder above is watched, so copy one in and press Reload.")}</div>
      )}

      {hooks.map((h) => (
        <div key={h.id} className={"card hook" + (h.error ? " bad" : "")}>
          <div className="hook-head">
            <Anchor size={15} aria-hidden />
            <div className="hook-title">
              <b>{h.name || h.id}</b>
              <div className="muted small">{h.id}</div>
            </div>
            <span className={"chip" + (kindOf(h.kind).warn ? " warn" : "")} title={t(kindOf(h.kind).title)}>
              {t(kindOf(h.kind).label)}
            </span>
            <span className={"tag " + (h.enabled ? "on" : "")}>{h.enabled ? t("Enabled") : t("Disabled")}</span>
            <Switch checked={h.enabled} disabled={!!busy || !!h.error} label={t("Enable {name}", { name: h.name || h.id })}
                    onChange={(v) => void act(h.id, () => api.patchHook(h.id, { enabled: v }))} />
          </div>

          {h.description && <div className="muted small hook-desc">{h.description}</div>}

          <div className="hook-meta">
            <span className="muted small">{h.events.join(" · ")}</span>
            <span className="muted small">{t("{n} ms at most", { n: h.timeout_ms })}</span>
            {h.kind === "gate" && (
              <span className="muted small"
                    title={t("What happens when the hook itself fails: auto lets reads through and holds back writes or anything leaving this machine")}>
                {t("If it fails")}: {h.on_error === "auto" ? t("hold back writes, allow reads") : h.on_error === "closed" ? t("hold back") : t("let through")}
              </span>
            )}
          </div>

          <HookScope hook={h} groups={groups} busy={!!busy} onSave={(selected) => act(h.id, () => api.patchHook(h.id, { groups: selected }))} />
          {h.error && <div className="err small">{h.error}</div>}

          <div className="hook-foot">
            {h.last?.at ? (
              <span className={"small " + (h.last.ok ? "muted" : "err")}>
                {h.last.ok ? <CircleCheck size={12} aria-hidden /> : <CircleX size={12} aria-hidden />}{" "}
                {h.last.ok ? t("Last run: ok") : t("Last run failed: {note}", { note: h.last.note ?? "" })} · {relTime(h.last.at)}
              </span>
            ) : <span className="muted small">{t("Never run yet")}</span>}
            <span style={{ flex: 1 }} />
            <HookTest hook={h} groups={groups} busy={!!busy} run={(event, group_id) => act(h.id, async () => {
              const r = await api.testHook(h.id, { event, group_id });
              if (!r.ok) throw new Error(r.note || t("It did not answer"));
            })} />
            <button className="btn small" onClick={() => void act(h.id, async () => setSource(await api.hookSource(h.id)))}>
              <FileCode2 size={13} /> {t("View code")}
            </button>
          </div>

          {source?.id === h.id && (
            <div className="hook-source">
              <button className="icon-btn hook-source-x" aria-label={t("Close")}
                      onClick={(e) => { e.stopPropagation(); setSource(null); }}><X size={14} /></button>
              <pre>{source.content}</pre>
            </div>
          )}
        </div>
      ))}

      <div className="sec">
        <button className="link hook-log-toggle" onClick={() => setOpenLog((v) => !v)}>
          {openLog ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {t("Recent runs")}
        </button>
      </div>
      {openLog && (log.length === 0
        ? <div className="card muted small">{t("Nothing has run yet. Switch a hook on, or press Run once.")}</div>
        : (
          <div className="card flush">
            {log.map((row, i) => (
              <div key={i} className="hook-logrow">
                <span className={"chip" + (row.ok ? "" : " warn")}>{row.event}</span>
                <b className="small">{row.hook}</b>
                <span className={"small " + (row.ok ? "muted" : "err")}>{row.ok ? t("ok") : (row.note || t("failed"))}</span>
                <span style={{ flex: 1 }} />
                <span className="muted small">{relTime(row.at)}</span>
              </div>
            ))}
          </div>
        ))}

      <Callout tone="info" title={t("What a hook cannot do")}>
        <div>{t("Grant a tool call your permission settings deny — a gate can only object or rewrite.")}</div>
        <div>{t("Read a credential: the process gets no app token and no inherited API keys, and credential-named arguments are replaced before a gate sees them.")}</div>
        <div>{t("Hold the group up: it runs in a separate process, is killed at its timeout, and a failure is recorded instead of raised.")}</div>
        <div>{t("Fail in silence: the reason is on this page, and in the hook log next to the data directory.")}</div>
      </Callout>
    </div>
  );
}

function HookScope({ hook, groups, busy, onSave }: { hook: HookEntry; groups: { id: string; name: string }[]; busy: boolean; onSave: (ids: string[]) => Promise<void> }) {
  const { t } = useI18n();
  const [all, setAll] = useState(hook.groups.length === 0);
  const [selected, setSelected] = useState(hook.groups);
  useEffect(() => { setAll(hook.groups.length === 0); setSelected(hook.groups); }, [hook.groups]);
  const dirty = all ? hook.groups.length !== 0 : hook.groups.length === 0 || [...selected].sort().join() !== [...hook.groups].sort().join();
  return <div className="cap-readiness">
    <b>{t("Scope")}: {hook.groups.length ? t("Only these groups") : t("Applies to every group")}</b>
    <div className="cap-filter">
      <select aria-label={t("Scope for {name}", { name: hook.name })} value={all ? "all" : "selected"} disabled={busy} onChange={(e) => setAll(e.target.value === "all")}>
        <option value="all">{t("All groups")}</option><option value="selected">{t("Selected groups")}</option>
      </select>
      {!all && groups.map((g) => <label className="cap-check" key={g.id}><input type="checkbox" disabled={busy} checked={selected.includes(g.id)} onChange={(e) => setSelected((ids) => e.target.checked ? [...ids, g.id] : ids.filter((id) => id !== g.id))} />{g.name}</label>)}
      {dirty && <button className="btn small" disabled={busy || (!all && !selected.length)} onClick={() => void onSave(all ? [] : selected)}>{t("Save scope")}</button>}
    </div>
    {!all && !selected.length && <span className="err small">{t("Choose at least one group, or explicitly select All groups. No scope change has been saved.")}</span>}
  </div>;
}

function HookTest({ hook, groups, busy, run }: { hook: HookEntry; groups: { id: string; name: string }[]; busy: boolean; run: (event: string, group: string) => Promise<void> }) {
  const { t } = useI18n();
  const allowed = groups.filter((g) => !hook.groups.length || hook.groups.includes(g.id));
  const [groupId, setGroupId] = useState("");
  const [event, setEvent] = useState("");
  const target = allowed.find((g) => g.id === groupId)?.id || allowed[0]?.id || "";
  const trigger = hook.events.includes(event) ? event : hook.events[0] || "";
  return <>
    <select aria-label={t("Test group for {name}", { name: hook.name })} disabled={busy} value={target} onChange={(e) => setGroupId(e.target.value)}>{!allowed.length && <option value="">{t("No available group")}</option>}{allowed.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select>
    <select aria-label={t("Test event for {name}", { name: hook.name })} disabled={busy} value={trigger} onChange={(e) => setEvent(e.target.value)}>{hook.events.map((e) => <option key={e} value={e}>{e}</option>)}</select>
    <button className="btn small" disabled={busy || !!hook.error || !target || !trigger} onClick={() => void run(trigger, target)}><Play size={13} />{t("Run once")}</button>
  </>;
}
