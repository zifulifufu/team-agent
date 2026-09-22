import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Settings2, TerminalSquare } from "lucide-react";
import { api, type ExternalOverview, type ExternalProbe } from "../api";
import { useData } from "../data";
import { levelLabel } from "../lib";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import ExternalDialog from "../components/ExternalDialog";
import { Row, useSettingsSaver } from "./rows";
import type { PageProps } from "./SettingsModal";
import "../styles/external.css";

/** Settings → External agents: the master switch, detection and the connectivity test for WorkBuddy's command-line engine, and the external members that have been added. */
export default function ExternalPage(_: PageProps) {
  const { t } = useI18n();
  const { settings, agents } = useData();
  const { set, err: saveErr, saving } = useSettingsSaver();
  const [ov, setOv] = useState<ExternalOverview | null>(null);
  const [probe, setProbe] = useState<ExternalProbe | null>(null);
  const [testing, setTesting] = useState<"" | "quick" | "live">("");
  const [err, setErr] = useState("");
  const [editId, setEditId] = useState<string | null>(null);

  const load = useCallback(() => { api.externalOverview().then((o) => { setOv(o); setErr(""); }).catch((e) => setErr((e as Error).message)); }, []);
  useEffect(load, [load, settings?.external_agents_enabled, settings?.external_calls_enabled, agents.length]);

  if (!settings) return <div className="empty big">{t("Loading…")}</div>;
  const on = settings.external_agents_enabled;
  const eng = ov?.engines[0];
  const members = agents.filter((a) => !!a.engine);
  const editing = members.find((a) => a.id === editId);

  const test = async (live: boolean) => {
    setTesting(live ? "live" : "quick");
    setErr("");
    try { setProbe(await api.externalTest({ live, engine: eng?.id })); } catch (e) { setErr((e as Error).message); } finally { setTesting(""); }
  };

  return (
    <div className="sp ext-page">
      <h2 className="sp-title"><TerminalSquare size={20} aria-hidden /> {t("External agents")}</h2>
      <p className="sp-desc">{t("Let WorkBuddy take part in a discussion as a member of the group. This app calls the command-line engine bundled with the WorkBuddy application (headless), and never drives its window or reads its account, sessions or keys.")}</p>
      {(saveErr || err) && <div className="err" role="alert">{saveErr || err}</div>}

      <div className="card flush">
        <Row title={t("Allow external agents")} desc={t("The master switch, off by default. They come with their own tools for reading and writing files, so decide what permissions you want before turning it on; with it off, external members already in a group stay silent.")}>
          <Switch checked={on} disabled={saving} onChange={(v) => void set({ external_agents_enabled: v })} label={t("Allow external agents")} />
        </Row>
      </div>
      {on && !settings.external_calls_enabled && (
        <div className="ext-box warn" role="status"><AlertTriangle size={15} /><div>{t("Outbound calls are switched off: an external agent needs a cloud model, so it will not run. Allow outbound calls under Routing & fallback first.")}</div></div>
      )}

      <div className="sec">{t("WorkBuddy's command-line engine")}</div>
      <div className="card ext-engine">
        {!eng ? <span className="muted small">{t("Detecting…")}</span> : eng.found ? (
          <div className="ext-ok"><CheckCircle2 size={14} /> {t("Found")}{probe?.version ? t(" · version {v}", { v: probe.version }) : ""}<small title={eng.path}>{eng.path}</small></div>
        ) : (
          <div className="ext-bad"><AlertTriangle size={14} /> {eng.hint}</div>
        )}
        <div className="ext-status-btns">
          <button className="btn small" disabled={!on || !!testing} onClick={() => void test(false)}>{testing === "quick" ? <Loader2 size={12} className="spin" /> : null} {t("Detect")}</button>
          <button className="btn small" disabled={!on || !!testing || !settings.external_calls_enabled} onClick={() => void test(true)} title={t("Send one real line to try it; this calls a cloud model (a tiny amount of quota)")}>
            {testing === "live" ? <Loader2 size={12} className="spin" /> : null} {t("Test the connection")}
          </button>
        </div>
        {!on && <div className="muted small">{t("Turn the master switch on first; detecting really does start the command line once.")}</div>}
        {probe?.live && (probe.live.ok
          ? <div className="ext-box ok" role="status"><CheckCircle2 size={15} /><div>{t("Connected: the engine answered \"{reply}\", in {seconds}s{model}.", { reply: probe.live.reply ?? "", seconds: probe.live.seconds, model: probe.live.model ? t(", model {m}", { m: probe.live.model }) : "" })}</div></div>
          : <div className="ext-box warn" role="alert"><AlertTriangle size={15} /><div>{t("The test did not pass:")} {probe.live.error}</div></div>)}
        {probe && !probe.live && probe.hint && <div className="ext-box warn"><AlertTriangle size={15} /><div>{probe.hint}</div></div>}
      </div>

      <div className="sec">{t("External members that have been added")}</div>
      {members.length === 0 ? (
        <div className="card muted small ext-none">{t("None yet. In a group chat, click Add member → External agents and pick one: WorkBuddy's command-line engine, or a chat gateway such as Cherry Studio or MetaChat.")}</div>
      ) : (
        <div className="card flush">
          {members.map((a) => {
            // A chat gateway has no permission level and no working directory: show what it does have.
            const gateway = a.engine !== "workbuddy";
            return (
              <div key={a.id} className="setting-row pad">
                <div>
                  <div className="sr-title">{a.avatar} {a.name}</div>
                  <div className="sr-desc">{gateway ? (
                    <>{t("Model")}: {a.engine_cfg?.model || t("Not set")} · {t("Address")}: {a.engine_cfg?.base_url || t("Not set")}</>
                  ) : (
                    <>{t("Permissions:")} {levelLabel(a.engine_cfg?.level ?? "read")}{a.engine_cfg?.web ? t(" · web access") : ""} · {t("Working directory:")} {a.engine_cfg?.cwd || t("a dedicated empty folder")}</>
                  )}</div>
                </div>
                <button className="btn small" onClick={() => setEditId(a.id)}><Settings2 size={12} /> {t("Settings")}</button>
              </div>
            );
          })}
        </div>
      )}

      <div className="sec">{t("Good to know")}</div>
      <ul className="ext-notes">
        <li>{t("By default every reply is a separate command-line process with no memory across turns (the context comes from the chat log). Switch on \"Use the application's own configuration\" to keep one continuing session instead. The first run usually takes tens of seconds; later ones are quicker.")}</li>
        <li>{t("An external agent cannot be the group host, and its reply is only chat text — it is never executed as a plan or a tool call.")}</li>
        <li>{t("A read-only working directory is a starting point, not a fence. To limit what it can read, do not give it an account that can read what it should not.")}</li>
        <li>{t("Files and web pages it reads can contain text trying to tell it what to do (prompt injection). The higher the permission, the bigger the risk.")}</li>
        <li>{t("This app never changes any setting of the WorkBuddy application itself, including its own \"allow full access\".")}</li>
      </ul>
      {editing && <ExternalDialog mode="edit" agent={editing} onClose={() => setEditId(null)} onDone={() => { setEditId(null); load(); }} />}
    </div>
  );
}
