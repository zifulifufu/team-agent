import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Boxes, Download, HardDrive, Package, Puzzle, RefreshCw, Sparkles, type LucideIcon } from "lucide-react";
import { api, relTime, type Settings, type UpdateItem, type UpdatesInfo } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch, useConfirm } from "../ui";
import { agoIso, Callout, fmtBytes, isHttps, SourceBadge, Spin } from "../components/ExtBits";
import PluginInstallModal from "../components/PluginInstall";
import type { PageProps } from "./SettingsModal";
import "../styles/ext.css";

// English labels, translated where they are shown (a module-level tr() would be frozen at import).
const KIND_META: Record<UpdateItem["kind"], { icon: LucideIcon; label: string }> = {
  app: { icon: Package, label: "App" },
  catalog: { icon: BookOpen, label: "Model catalog" },
  skill: { icon: Sparkles, label: "Skill" },
  plugin: { icon: Puzzle, label: "Plugin" },
  model: { icon: Boxes, label: "New model" },
  localmodel: { icon: HardDrive, label: "Local model" },
  localcatalog: { icon: BookOpen, label: "Local model catalog" },
};

const s = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));

export default function UpdatesPage({ onTab }: PageProps) {
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
      if (r.busy) setMsg({ ok: false, text: t("The previous check has not finished yet — please wait a moment.") });
      else if (errs.length === 0) setMsg({ ok: true, text: u && u.items.length ? t("Check finished — {n} reminders to review.", { n: u.items.length }) : t("Check finished — no new updates found.") });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const last = info?.last_check ?? null;
  const errors = (last?.errors as string[] | undefined) ?? [];
  const appRes = (last?.app ?? null) as Record<string, unknown> | null;
  const busyNow = checking || !!info?.checking;

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("Updates")}</h2>
        <div className="sp-head-actions">
          <button className="btn primary" onClick={check} disabled={busyNow}>
            {busyNow ? <><Spin /> {t("Checking…")}</> : <><RefreshCw size={14} /> {t("Check now")}</>}
          </button>
        </div>
      </div>
      <p className="sp-desc">
        {t("Checks GitHub for new versions of the app, the model catalog, and your installed skills and plugins. Checking needs a network connection (Allow outbound calls under Routing & fallback).")}
      </p>

      {err && <div className="ext-errbox"><div className="err">{t("Failed to load: {err}", { err })}</div><button className="btn small" onClick={() => void refresh()}>{t("Retry")}</button></div>}
      {msg && <div className={msg.ok ? "ok-text" : "err"} style={{ marginBottom: 8 }}>{msg.text}</div>}

      {info && (
        <>
          {!info.configured && (
            <Callout title={t("No app repository configured yet")}>
              {t("To check for new app versions, first fill in the GitHub repository (owner/repo) under Update settings below. Skill, plugin, and new-model checks are unaffected.")}
            </Callout>
          )}
          <div className="ext-check-bar muted small" style={{ marginBottom: 6 }}>
            <span>{t("Last checked:")} {last?.at ? <span title={new Date(last.at * 1000).toLocaleString()}>{relTime(last.at)}</span> : t("Never checked")}</span>
            {last && errors.length === 0 && appRes && !!appRes.configured && appRes.available === false && <span>{t("The app is up to date")}{appRes.current ? ` (${s(appRes.current)})` : ""}</span>}
            {last && errors.length === 0 && appRes && typeof appRes.note === "string" && <span>{appRes.note}</span>}
          </div>
          {errors.length > 0 && (
            <div className="ext-errbox" role="alert">
              <div className="err">
                {t("The last check ran into problems:")}
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </div>
            </div>
          )}

          <div className="sec">{t("Reminders to review")}{info.items.length > 0 && <span className="count-badge-plain">{info.items.length}</span>}</div>
          <div className="card flush">
            {info.items.length === 0 && <div className="empty">{t("No updates to review")}</div>}
            {info.items.map((it) => (
              <UpdateRow key={it.id} item={it} onTab={onTab} onDone={async (text) => { if (text) setMsg({ ok: true, text }); await refresh(); if (text) await reload(); }} />
            ))}
          </div>

          <div className="sec">{t("Update settings")}</div>
          {settings && <SettingsCard settings={settings} onSaved={async () => { await reload(); await refresh(); }} />}

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

// ------------------------------------------------------------------ Single reminder row
function UpdateRow({ item, onTab, onDone }: { item: UpdateItem; onTab: PageProps["onTab"]; onDone: (okText?: string) => Promise<void> }) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [reinstall, setReinstall] = useState(false);
  const d = item.detail;
  const meta = KIND_META[item.kind] ?? KIND_META.app;

  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setErr("");
    try {
      const text = await fn();
      await onDone(text || undefined);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  let body: ReactNode = null;
  let actions: ReactNode = null;

  if (item.kind === "app") {
    const assets = Array.isArray(d.assets) ? (d.assets as { name?: unknown; size?: unknown; url?: unknown }[]) : [];
    body = (
      <>
        <div>{t("Current version")} {s(d.current) || "?"} → {t("latest")} <b>{s(d.latest)}</b>{d.published_at ? <span className="muted"> · {t("published {when}", { when: agoIso(s(d.published_at)) || s(d.published_at) })}</span> : null}</div>
        <div><b>{t("The app is never replaced automatically — download the installer to update by hand.")}</b></div>
        {s(d.notes) && (
          <details className="ext-details" style={{ marginTop: 6 }}>
            <summary>{t("Release notes")}</summary>
            <div className="ext-notes">{s(d.notes)}</div>
          </details>
        )}
        {assets.length > 0 && (
          <div className="ext-assets" aria-label={t("Installers")}>
            {assets.map((a, i) => (
              <div key={i}>
                {isHttps(a.url) ? <a className="link" href={a.url} target="_blank" rel="noreferrer">{s(a.name) || a.url}</a> : <span>{s(a.name)}</span>}
                {typeof a.size === "number" && a.size > 0 && <span className="muted"> · {fmtBytes(a.size)}</span>}
              </div>
            ))}
          </div>
        )}
      </>
    );
    actions = isHttps(d.url) ? <a className="btn small" href={d.url} target="_blank" rel="noreferrer"><Download size={13} /> {t("Open the release page")}</a> : <span className="muted small">{t("No release page link available")}</span>;
  } else if (item.kind === "catalog") {
    body = (
      <>
        <div>{t("Current")} {s(d.current)} → {t("latest")} {s(d.latest)}{typeof d.new_models === "number" ? t(", {n} new models", { n: d.new_models }) : ""}</div>
        <div className="muted">{t("Only the model list and strength tags are updated; models and routes you added are left alone.")}</div>
      </>
    );
    actions = (
      <button className="btn small primary" disabled={busy} onClick={() => void run(async () => {
        const r = await api.applyCatalog();
        return r.applied ? t("Applied the new model catalog ({v}).", { v: s(r.latest) }) : t("The catalog did not change.");
      })}>{busy ? <><Spin size={12} /> {t("Applying")}</> : t("Apply the new catalog")}</button>
    );
  } else if (item.kind === "skill") {
    const name = s(d.name) || item.ref;
    body = <div>{t("The skill files on GitHub have changed. Updating overwrites the local skill with the latest content, so any local edits are lost.")}{s(d.repo) && <> {t("Source:")} <SourceBadge repo={s(d.repo)} path={s(d.path)} /></>}</div>;
    actions = (
      <button className="btn small primary" disabled={busy} onClick={async () => {
        if (!(await confirm(t("Update the skill {name} with the latest content from GitHub? This overwrites the local copy and any local edits are lost.", { name }), { okText: t("Update and overwrite") }))) return;
        void run(async () => { await api.updateSkill(name); return t("The skill {name} was updated.", { name }); });
      }}>{busy ? <><Spin size={12} /> {t("Updating")}</> : t("Update")}</button>
    );
  } else if (item.kind === "plugin") {
    const repo = s(d.repo), path = s(d.path);
    body = (
      <>
        <div>{t("The plugin files on GitHub have changed.")}{repo && <> {t("Source:")} <SourceBadge repo={repo} path={path} /></>}</div>
        <div className="muted">{t("A plugin is code that executes, so it cannot be updated in place: read the new source and confirm before it is reinstalled.")}</div>
      </>
    );
    actions = <button className="btn small primary" disabled={!repo || !path} onClick={() => setReinstall(true)}>{t("Review and reinstall")}</button>;
  } else if (item.kind === "localmodel") {
    const src = s(d.source);
    const tag = s(d.tag);
    body = src === "ollama-release" ? (
      <div>{t("Local Ollama {local}; latest on GitHub {latest}. New models often need a newer Ollama.", { local: s(d.local) || "?", latest: s(d.latest) })} <b>{t("The app never upgrades Ollama automatically")}</b>{t(" — install it yourself.")}</div>
    ) : (
      <>
        <div>{s(d.desc) || t("A new open-source model was found")}{d.size_gb ? t(", about {n} GB", { n: s(d.size_gb) }) : ""}{s(d.license) ? ` · ${s(d.license)}` : ""}</div>
        {tag ? <div className="muted">{t("The Ollama registry confirms this model exists. Add to recommendations only puts it on the local models page — nothing is downloaded.")}</div>
              : <div className="muted">{t("This came from {src}. It is only a heads-up: there may be no Ollama build, so check the licence and hardware requirements yourself.", { src: src === "hf" ? "Hugging Face" : "GitHub" })}</div>}
      </>
    );
    actions = (
      <>
        {tag && (
          <button className="btn small primary" disabled={busy} onClick={() => void run(async () => { await api.localAdd(tag); return t("Added {tag} to the local model recommendations.", { tag }); })}>{t("Add to recommendations")}</button>
        )}
        {isHttps(d.url) && <a className="btn small" href={d.url} target="_blank" rel="noreferrer">{t("Open the page")}</a>}
        <button className="btn small" onClick={() => onTab("local")}>{t("Go to local models")}</button>
      </>
    );
  } else if (item.kind === "localcatalog") {
    body = <div>{t("Current")} {s(d.current)} → {t("latest")} {s(d.latest)}. {t("The catalog is just a list of models and sizes — it contains no code.")}</div>;
    actions = (
      <button className="btn small primary" disabled={busy} onClick={() => void run(async () => {
        const r = await api.applyLocalCatalog();
        return r.applied ? t("Applied the new local model catalog ({v}).", { v: s(r.latest) }) : t("The catalog did not change.");
      })}>{busy ? <><Spin size={12} /> {t("Applying")}</> : t("Apply the new catalog")}</button>
    );
  } else if (item.kind === "model") {
    const ids = Array.isArray(d.ids) ? (d.ids as unknown[]).map(s) : [];
    body = (
      <>
        <div>{t("The live provider list contains models you have not seen yet.")}</div>
        {ids.length > 0 && (
          <div className="ext-chips">
            {ids.slice(0, 8).map((id) => <span key={id} className="ext-tool-chip">{id}</span>)}
            {ids.length > 8 && <span className="muted small">{t("…{n} in total", { n: ids.length })}</span>}
          </div>
        )}
      </>
    );
    actions = <button className="btn small primary" onClick={() => onTab("providers")}>{t("Pick some")}</button>;
  }

  return (
    <div className="ext-upd">
      <div className="ext-upd-ico"><meta.icon size={17} /></div>
      <div className="ext-upd-main">
        <div className="ext-upd-title">{item.title}<span className="tag">{t(meta.label)}</span></div>
        <div className="ext-upd-body">{body}</div>
        {err && <div className="ext-errline">{err}</div>}
        <div className="ext-upd-actions">
          {actions}
          <button className="btn small ghost" disabled={busy} onClick={() => void run(async () => { await api.dismissUpdate(item.id); })}>{t("Ignore")}</button>
        </div>
      </div>
      {reinstall && (
        <PluginInstallModal
          repo={s(d.repo)}
          path={s(d.path)}
          gitRef={s(d.ref)}
          overwrite
          onClose={() => setReinstall(false)}
          onInstalled={() => { setReinstall(false); void onDone(t("The plugin {name} was reinstalled.", { name: item.ref })); }}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ Update settings
const INTERVALS = [1, 6, 12, 24, 72];

function SettingsCard({ settings, onSaved }: { settings: Settings; onSaved: () => Promise<void> }) {
  const { t } = useI18n();
  const [repo, setRepo] = useState(settings.app_repo);
  const [catalogUrl, setCatalogUrl] = useState(settings.catalog_url);
  const [auto, setAuto] = useState(settings.auto_check_updates);
  const [hours, setHours] = useState(settings.update_interval_hours);
  const [autoSkills, setAutoSkills] = useState(settings.auto_update_skills);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const intervals = INTERVALS.includes(hours) ? INTERVALS : [...INTERVALS, hours].sort((a, b) => a - b);
  const dirty =
    repo.trim() !== settings.app_repo || catalogUrl.trim() !== settings.catalog_url || auto !== settings.auto_check_updates ||
    hours !== settings.update_interval_hours || autoSkills !== settings.auto_update_skills || token.trim() !== "";

  const put = async (patch: Partial<Settings>, done: string) => {
    setBusy(true);
    setErr("");
    setOk("");
    try {
      await api.putSettings(patch);
      await onSaved();
      setOk(done);
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const r = repo.trim();
    const u = catalogUrl.trim();
    if (r && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r)) return setErr(t("The app repository must look like owner/repo, for example me/team-agent"));
    if (u && !u.startsWith("https://")) return setErr(t("The catalog URL must start with https://"));
    const patch: Partial<Settings> = {
      app_repo: r, catalog_url: u, auto_check_updates: auto, update_interval_hours: hours, auto_update_skills: autoSkills,
    };
    if (token.trim()) patch.github_token = token.trim();   // only send the token when a new value was entered
    if (await put(patch, t("Saved."))) setToken("");
  };

  return (
    <div className="card flush">
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-repo">{t("App repository")}</label>
        <input id="upd-repo" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder={t("owner/repo, for example me/team-agent")} spellCheck={false} />
        <div className="sr-desc">{t("The newest version in this repository's Releases is compared with the running version. Leave it empty to skip app checks.")}</div>
      </div>
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-cat">{t("Catalog URL (optional)")}</label>
        <input id="upd-cat" value={catalogUrl} onChange={(e) => setCatalogUrl(e.target.value)} placeholder="https://…/catalog.json" spellCheck={false} />
        <div className="sr-desc">{t("Must be https. Leave it empty to use backend/app/data/catalog.json from the app repository.")}</div>
      </div>
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-token">{t("GitHub token (optional)")}</label>
        <div className="ext-inline-input">
          <input id="upd-token" type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false}
            placeholder={settings.github_token_set ? t("Already set (hidden)") : t("Leave empty; ghp_…")} />
          {settings.github_token_set && (
            <button className="btn" disabled={busy} onClick={() => void put({ github_token: "" }, t("GitHub token cleared."))}>{t("Clear")}</button>
          )}
        </div>
        <div className="sr-desc">{t("It needs no scopes and only raises the rate limit on GitHub's API. It is stored in plain text in the local database, and backups exclude it by default.")}</div>
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
        {ok && <span className="ok-text">{ok}</span>}
        {err && <span className="err">{err}</span>}
      </div>
    </div>
  );
}
