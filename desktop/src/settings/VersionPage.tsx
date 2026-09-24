import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, relTime, type Settings, type UpdatesInfo } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { APP_VERSION } from "../lib";
import { Callout, Spin } from "../components/ExtBits";
import { UpdateRow } from "../components/UpdateRow";
import type { PageProps } from "./SettingsModal";
import "../styles/ext.css";

const s = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));

/** Software update: the app itself, and nothing else.
 *
 *  It was cut out of the old Updates page because the two halves ask different things of the reader:
 *  a skill or a model catalog can be updated from here in one click, while the app is only ever
 *  *reported* — you download the installer and replace it by hand. Mixing the two made the one that
 *  needs a human decision look like the ones that do not. */
export default function VersionPage({ onTab }: PageProps) {
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

  useEffect(() => {
    window.clearTimeout(poll.current);
    if (info?.checking && !checking) poll.current = window.setTimeout(() => void refresh(), 2000);
    return () => window.clearTimeout(poll.current);
  }, [info, checking, refresh]);

  // One check covers both pages, so the button here runs the same job as the one on Discover
  const check = async () => {
    setChecking(true);
    setMsg(null);
    try {
      const r = await api.checkUpdates();
      const errs = Array.isArray(r.errors) ? (r.errors as unknown[]) : [];
      const u = await refresh();
      const app = (u?.last_check?.app ?? null) as Record<string, unknown> | null;
      if (r.busy) setMsg({ ok: false, text: t("The previous check has not finished yet — please wait a moment.") });
      else if (errs.length === 0) setMsg({ ok: true, text: app?.available === true ? t("Check finished — a new version is available.") : t("Check finished — this is the newest version.") });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const last = info?.last_check ?? null;
  const errors = (last?.errors as string[] | undefined) ?? [];
  const items = (info?.items ?? []).filter((i) => i.kind === "app");
  const otherCount = (info?.items.length ?? 0) - items.length;

  const app = (last?.app ?? null) as Record<string, unknown> | null;
  const current = s(app?.current) || APP_VERSION;
  const latest = s(app?.latest);
  const available = app?.available === true;
  const note = s(app?.note);
  const configured = !!info?.configured;
  const busyNow = checking || !!info?.checking;

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("Software update")}</h2>
        <div className="sp-head-actions">
          <button className="btn primary" onClick={check} disabled={busyNow}>
            {busyNow ? <><Spin /> {t("Checking…")}</> : <><RefreshCw size={14} /> {t("Check now")}</>}
          </button>
        </div>
      </div>
      <p className="sp-desc">
        {t("Upgrading this software: which version is running, which one is the newest on GitHub, and where the installer comes from. Nothing here is replaced automatically — you download the installer and upgrade by hand. Reminders for skills, plugins and models are on Discover.")}
      </p>

      {err && <div className="ext-errbox"><div className="err">{t("Failed to load: {err}", { err })}</div><button className="btn small" onClick={() => void refresh()}>{t("Retry")}</button></div>}
      {msg && <div className={msg.ok ? "ok-text" : "err"} style={{ marginBottom: 8 }}>{msg.text}</div>}

      {info && (
        <>
          <div className="sec">{t("Version status")}</div>
          <div className="card flush">
            <div className="setting-row pad">
              <div className="sr-title">{t("Current version")}</div>
              <span className="muted mono">{current}</span>
            </div>
            <div className="setting-row pad">
              <div className="sr-title">{t("Latest version")}</div>
              <span className="muted mono">{configured ? (latest || "—") : "—"}</span>
            </div>
            <div className="setting-row pad">
              <div className="sr-title">{t("Last checked:")}</div>
              <span className="muted">{last?.at ? <span title={new Date(last.at * 1000).toLocaleString()}>{relTime(last.at)}</span> : t("Never checked")}</span>
            </div>
            {configured && last && errors.length === 0 && (
              <div className="setting-row pad">
                <div className="sr-title">{available ? t("A new version is available") : t("The app is up to date")}</div>
                <span className="muted small">{available ? t("Download the installer below and update by hand.") : t("Nothing to do — this is the newest version.")}</span>
              </div>
            )}
            {note && <div className="setting-row pad"><div className="sr-desc">{note}</div></div>}
          </div>

          {!configured && (
            <Callout title={t("No app repository configured yet")}>
              {t("To check for new app versions, fill in the GitHub repository (owner/repo) below. Skill, plugin, and new-model checks are unaffected — those are on Discover.")}
            </Callout>
          )}
          {errors.length > 0 && (
            <div className="ext-errbox" role="alert">
              <div className="err">
                {t("The last check ran into problems:")}
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </div>
            </div>
          )}

          <div className="sec">{t("Software update reminders")}{items.length > 0 && <span className="count-badge-plain">{items.length}</span>}</div>
          <div className="card flush">
            {items.length === 0 && <div className="empty">{t("No new version to install")}</div>}
            {items.map((it) => (
              <UpdateRow key={it.id} item={it} onTab={onTab} onDone={async (text) => { if (text) setMsg({ ok: true, text }); await refresh(); if (text) await reload(); }} />
            ))}
          </div>

          {otherCount > 0 && (
            <div className="ext-check-bar small" style={{ margin: "10px 0 0" }}>
              <span>{t("There are also {n} reminders for skills, plugins and models on Discover.", { n: otherCount })}</span>
              <button className="btn small" onClick={() => onTab("discover")}>{t("Open Discover")}</button>
            </div>
          )}

          <div className="sec">{t("Update source")}</div>
          {settings && <SourceCard settings={settings} onSaved={async () => { await reload(); await refresh(); }} onTab={onTab} />}
        </>
      )}
      {!info && !err && <div className="empty"><Spin /> {t("Loading…")}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ Where the version comes from
function SourceCard({ settings, onSaved, onTab }: { settings: Settings; onSaved: () => Promise<void>; onTab: PageProps["onTab"] }) {
  const { t } = useI18n();
  const [repo, setRepo] = useState(settings.app_repo);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const dirty = repo.trim() !== settings.app_repo || token.trim() !== "";

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
    if (r && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r)) return setErr(t("The app repository must look like owner/repo, for example me/team-agent"));
    const patch: Partial<Settings> = { app_repo: r };
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
      <div className="setting-row pad" style={{ justifyContent: "flex-start" }}>
        <button className="btn primary" onClick={() => void save()} disabled={busy || !dirty}>{busy ? <><Spin /> {t("Saving…")}</> : t("Save")}</button>
        <span className="muted small">{t("Automatic checks are set on Discover.")}</span>
        <button className="btn small ghost" onClick={() => onTab("discover")}>{t("Open Discover")}</button>
        {ok && <span className="ok-text">{ok}</span>}
        {err && <span className="err">{err}</span>}
      </div>
    </div>
  );
}
