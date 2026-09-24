import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, relTime, type Settings, type UpdatesInfo } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import { SourceBadge, Spin } from "../components/ExtBits";
import { UpdateRow } from "../components/UpdateRow";
import type { PageProps } from "./SettingsModal";
import "../styles/ext.css";

/** Discover: everything that is not the app itself — new versions of the model catalog, of the
 *  skills and plugins installed from GitHub, and new models worth knowing about (provider lists,
 *  local model catalogs, local models). The app's own version lives on its own page (Version
 *  update), because the two want different things from the person reading them: one is a heads-up
 *  you may act on, the other is a download you do by hand. */
export default function DiscoverPage({ onTab }: PageProps) {
  const { t } = useI18n();
  const { settings, reload, reloadUpdates } = useData();
  const [info, setInfo] = useState<UpdatesInfo | null>(null);
  const [err, setErr] = useState("");
  const [checking, setChecking] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const poll = useRef<number>();

  const refresh = useCallback(async () => {
    try {
      const u = await api.updates();
      setInfo(u);
      setErr("");
      void reloadUpdates();
      return u;
    } catch (e) {
      setErr((e as Error).message);
      return null;
    }
  }, [reloadUpdates]);
  useEffect(() => { void refresh(); }, [refresh]);

  // While a check runs in the background (scheduled or triggered elsewhere), poll until it ends
  useEffect(() => {
    window.clearTimeout(poll.current);
    if (info?.checking && !checking) poll.current = window.setTimeout(() => void refresh(), 2000);
    return () => window.clearTimeout(poll.current);
  }, [info, checking, refresh]);

  const check = async () => {
    setChecking(true);
    setMsg(null);
    try {
      const r = await api.checkUpdates();
      const errs = Array.isArray(r.errors) ? (r.errors as unknown[]) : [];
      const u = await refresh();
      const n = u ? u.items.filter((i) => i.kind !== "app").length : 0;
      if (r.busy) setMsg({ ok: false, text: t("The previous check has not finished yet — please wait a moment.") });
      else if (errs.length === 0) setMsg({ ok: true, text: n ? t("Check finished — {n} reminders to review.", { n }) : t("Check finished — no new updates found.") });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const last = info?.last_check ?? null;
  const errors = (last?.errors as string[] | undefined) ?? [];
  const items = (info?.items ?? []).filter((i) => i.kind !== "app");
  const appCount = (info?.items.length ?? 0) - items.length;
  const busyNow = checking || !!info?.checking;

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("Discover")}</h2>
        <div className="sp-head-actions">
          <button className="btn primary" onClick={check} disabled={busyNow}>
            {busyNow ? <><Spin /> {t("Checking…")}</> : <><RefreshCw size={14} /> {t("Check now")}</>}
          </button>
        </div>
      </div>
      <p className="sp-desc">
        {t("New versions of the things you installed and models worth knowing about: the model catalog, skills, plugins, provider model lists and local models. Checking needs a network connection (Allow outbound calls under Routing & fallback). The app's own version updates have their own page.")}
      </p>

      {err && <div className="ext-errbox"><div className="err">{t("Failed to load: {err}", { err })}</div><button className="btn small" onClick={() => void refresh()}>{t("Retry")}</button></div>}
      {msg && <div className={msg.ok ? "ok-text" : "err"} style={{ marginBottom: 8 }}>{msg.text}</div>}

      {info && (
        <>
          {appCount > 0 && (
            <div className="ext-check-bar small" style={{ marginBottom: 8 }}>
              <span>{t("The app itself also has something to review — see Software update.")}</span>
              <button className="btn small" onClick={() => onTab("version")}>{t("Open software update")}</button>
            </div>
          )}
          <div className="ext-check-bar muted small" style={{ marginBottom: 6 }}>
            <span>{t("Last checked:")} {last?.at ? <span title={new Date(last.at * 1000).toLocaleString()}>{relTime(last.at)}</span> : t("Never checked")}</span>
          </div>
          {errors.length > 0 && (
            <div className="ext-errbox" role="alert">
              <div className="err">
                {t("The last check ran into problems:")}
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </div>
            </div>
          )}

          <div className="sec">{t("Reminders to review")}{items.length > 0 && <span className="count-badge-plain">{items.length}</span>}</div>
          <div className="card flush">
            {items.length === 0 && <div className="empty">{t("No updates to review")}</div>}
            {items.map((it) => (
              <UpdateRow key={it.id} item={it} onTab={onTab} onDone={async (text) => { if (text) setMsg({ ok: true, text }); await refresh(); if (text) await reload(); }} />
            ))}
          </div>

          <div className="sec">{t("Update settings")}</div>
          {settings && <SettingsCard settings={settings} onSaved={async () => { await reload(); await refresh(); }} onTab={onTab} />}

          <div className="sec">{t("Recorded sources")}</div>
          <p className="muted small" style={{ margin: "-4px 0 8px", lineHeight: 1.7 }}>{t("Skills and plugins installed from GitHub have their source recorded; update checks compare these files against the ones on GitHub.")}</p>
          <div className="card flush">
            {info.sources.length === 0 && <div className="empty">{t("No skills or plugins have been installed from GitHub yet")}</div>}
            {info.sources.map((r) => (
              <div key={r.kind + r.name} className="ext-src-row">
                <span className="tag">{r.kind === "skill" ? t("Skill") : r.kind === "plugin" ? t("Plugin") : r.kind}</span>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 500 }}>{r.name}</div>
                  <div className="muted small mono">{r.path}{r.ref ? ` @ ${r.ref}` : ""}</div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <SourceBadge repo={r.repo} path={r.path} />
                  <div className="muted small" style={{ marginTop: 3 }}>{r.installed_at ? t("Installed {date}", { date: new Date(r.installed_at * 1000).toLocaleDateString() }) : ""}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="ext-foot">
            {t("Model catalog version {v} ({src}).", { v: info.catalog.version, src: info.catalog.source === "override" ? t("updated") : t("built in") })}
            <br />
            {t("The catalog is a snapshot taken on one date; model entries and strength tags are compiled and inferred from public information and naming, not benchmark scores. To see what a provider actually offers right now, refresh the live list under Model services → Add model.")}
          </div>
        </>
      )}
      {!info && !err && <div className="empty"><Spin /> {t("Loading…")}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ Check settings
const INTERVALS = [1, 6, 12, 24, 72];

/** How the checks run (they are one job, so the switch and the interval live here rather than being
 *  copied onto the version page). The app repository and the GitHub token are the version page's:
 *  that is where the version comparison comes from, and the token is only ever used by GitHub. */
function SettingsCard({ settings, onSaved, onTab }: { settings: Settings; onSaved: () => Promise<void>; onTab: PageProps["onTab"] }) {
  const { t } = useI18n();
  const [catalogUrl, setCatalogUrl] = useState(settings.catalog_url);
  const [auto, setAuto] = useState(settings.auto_check_updates);
  const [hours, setHours] = useState(settings.update_interval_hours);
  const [autoSkills, setAutoSkills] = useState(settings.auto_update_skills);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const intervals = INTERVALS.includes(hours) ? INTERVALS : [...INTERVALS, hours].sort((a, b) => a - b);
  const dirty =
    catalogUrl.trim() !== settings.catalog_url || auto !== settings.auto_check_updates ||
    hours !== settings.update_interval_hours || autoSkills !== settings.auto_update_skills;

  const save = async () => {
    const u = catalogUrl.trim();
    if (u && !u.startsWith("https://")) return setErr(t("The catalog URL must start with https://"));
    setBusy(true);
    setErr("");
    setOk("");
    try {
      await api.putSettings({ catalog_url: u, auto_check_updates: auto, update_interval_hours: hours, auto_update_skills: autoSkills });
      await onSaved();
      setOk(t("Saved."));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card flush">
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-cat">{t("Catalog URL (optional)")}</label>
        <input id="upd-cat" value={catalogUrl} onChange={(e) => setCatalogUrl(e.target.value)} placeholder="https://…/catalog.json" spellCheck={false} />
        <div className="sr-desc">{t("Must be https. Leave it empty to use backend/app/data/catalog.json from the app repository.")}</div>
      </div>
      <div className="setting-row pad">
        <div>
          <div className="sr-title">{t("Check for updates automatically")}</div>
          <div className="sr-desc">{t("When on, the app checks GitHub in the background at the chosen interval (Allow outbound calls is required).")}</div>
        </div>
        <div className="row">
          <select className="ext-select" value={hours} onChange={(e) => setHours(Number(e.target.value))} disabled={!auto} aria-label={t("Check interval")}>
            {intervals.map((h) => <option key={h} value={h}>{t("Every {n} hours", { n: h })}</option>)}
          </select>
          <Switch checked={auto} onChange={setAuto} label={t("Check for updates automatically")} />
        </div>
      </div>
      <div className="setting-row pad">
        <div>
          <div className="sr-title">{t("Update skills automatically")}</div>
          <div className="sr-desc">{t("When on, skills installed from GitHub are overwritten with new versions automatically. Only text skills are updated; plugins, MCP servers, and the app itself are never installed automatically.")}</div>
        </div>
        <Switch checked={autoSkills} onChange={setAutoSkills} label={t("Update skills automatically")} />
      </div>
      <div className="setting-row pad" style={{ justifyContent: "flex-start" }}>
        <button className="btn primary" onClick={() => void save()} disabled={busy || !dirty}>{busy ? <><Spin /> {t("Saving…")}</> : t("Save")}</button>
        <button className="btn small ghost" onClick={() => onTab("version")}>{t("Software update")}</button>
        {ok && <span className="ok-text">{ok}</span>}
        {err && <span className="err">{err}</span>}
      </div>
    </div>
  );
}
