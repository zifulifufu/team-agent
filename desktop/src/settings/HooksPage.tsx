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

  if (hooks === null) return <div className="sp"><p className="sp-desc">{t("Loading…")}</p></div>;

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
            <span className={"chip" + (h.kind === "gate" ? " warn" : "")}
                  title={h.kind === "gate"
                    ? t("Asked before something irreversible happens: it may object or rewrite, never grant")
                    : t("Told what happened; its answer is ignored")}>
              {h.kind === "gate" ? t("Gate") : t("Observer")}
            </span>
            <Switch checked={h.enabled} label={t("Enable {name}", { name: h.name || h.id })}
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

          {/* Which groups a hook may see is part of "what can it do", so it sits next to the switch
              rather than behind a dialog. */}
          <div className="hook-groups">
            <span className="muted small">{h.groups.length === 0 ? t("Applies to every group") : t("Only these groups")}</span>
            {groups.length > 0 && (
              <select multiple className="hook-groups-pick" aria-label={t("Groups this hook applies to")}
                      value={h.groups}
                      onChange={(e) => void act(h.id, () => api.patchHook(h.id, { groups: Array.from(e.target.selectedOptions, (o) => o.value) }))}>
                {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
            )}
          </div>

          {h.error && <div className="err small">{h.error}</div>}

          <div className="hook-foot">
            {h.last?.at ? (
              <span className={"small " + (h.last.ok ? "muted" : "err")}>
                {h.last.ok ? <CircleCheck size={12} aria-hidden /> : <CircleX size={12} aria-hidden />}{" "}
                {h.last.ok ? t("Last run: ok") : t("Last run failed: {note}", { note: h.last.note ?? "" })} · {relTime(h.last.at)}
              </span>
            ) : <span className="muted small">{t("Never run yet")}</span>}
            <span style={{ flex: 1 }} />
            <button className="btn small" disabled={busy === h.id || !!h.error}
                    title={h.error ? t("Fix the hook first") : ""}
                    onClick={() => void act(h.id, async () => {
                      const r = await api.testHook(h.id, { event: h.events[0], group_id: groups[0]?.id });
                      if (!r.ok) throw new Error(r.note || t("It did not answer"));
                    })}>
              {busy === h.id ? <LoaderCircle size={13} className="spin" /> : <Play size={13} />} {t("Run once")}
            </button>
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
