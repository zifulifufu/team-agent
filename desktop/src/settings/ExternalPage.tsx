import { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw, Settings2 } from "lucide-react";
import { api, type Agent, type ExternalOverview } from "../api";
import { useData } from "../data";
import { levelLabel } from "../lib";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import ExternalDialog from "../components/ExternalDialog";
import { Row, useSettingsSaver } from "./rows";
import { CapabilityBinding, CapabilityFilter, useCapabilityGroup } from "./CapabilityWorkspace";
import type { PageProps } from "./SettingsModal";
import "../styles/external.css";

export default function ExternalPage({ onTab }: PageProps) {
  const { t } = useI18n();
  const { settings, agents, groups } = useData();
  const group = useCapabilityGroup();
  const { set, err: saveErr, saving } = useSettingsSaver();
  const [ov, setOv] = useState<ExternalOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [editId, setEditId] = useState<string | null>(null);
  const [createEngine, setCreateEngine] = useState("");
  const [q, setQ] = useState("");
  const [attachedOnly, setAttachedOnly] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    try { setOv(await api.externalOverview()); setErr(""); }
    catch (e) { setErr((e as Error).message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load, agents.length]);
  if (!settings) return <div className="empty big">{t("Loading…")}</div>;
  const on = settings.external_agents_enabled;
  const members = (ov?.members ?? []).map((m) => ({
    ...agents.find((a) => a.id === m.id), id: m.id, name: m.name, engine: m.engine,
    engine_cfg: m.cfg, binding: m.binding,
    avatar: agents.find((a) => a.id === m.id)?.avatar || ov?.engines.find((e) => e.id === m.engine)?.avatar || "🧰",
  } as Agent & { binding: typeof m.binding }));
  const editing = members.find((m) => m.id === editId);
  const shown = (ov?.engines ?? []).filter((e) => {
    const mine = members.filter((m) => m.engine === e.id);
    return `${e.name} ${e.role} ${mine.map((m) => m.name).join(" ")}`.toLowerCase().includes(q.toLowerCase())
      && (!group || !attachedOnly || mine.some((m) => group.member_ids.includes(m.id)));
  });
  return <div className="sp ext-page">
    <div className="sp-head"><h2 className="sp-title">{t("Agents and local tools")}</h2>
      <button className="btn" disabled={loading} onClick={() => void load()}><RefreshCw size={14} className={loading ? "spin" : ""} />{t("Refresh detection")}</button>
    </div>
    <p className="sp-desc">{t("External agents receive assignments as members. Local tools render video, synthesize speech or edit media. Create a profile, configure its runtime, then attach it to a group.")}</p>
    {(saveErr || err) && <div className="err" role="alert">{saveErr || err}</div>}
    <div className="card flush"><Row title={t("Allow external agents")} desc={t("This switch controls external agents and local execution tools. Existing group attachments are kept when it is off.")}>
      <Switch checked={on} disabled={saving} onChange={(v) => void set({ external_agents_enabled: v })} label={t("Allow external agents")} />
    </Row></div>
    {!settings.external_calls_enabled && <p className="muted small">{t("Outbound calls are off. Local tools can still run; agents that require cloud models need outbound calls enabled.")}</p>}
    {ov && <CapabilityFilter query={q} onQuery={setQ} attachedOnly={attachedOnly} onAttachedOnly={setAttachedOnly} total={ov.engines.length} shown={shown.length} />}
    {!ov && !err && <div className="empty">{t("Detecting…")}</div>}
    {ov && !shown.length && <div className="empty">{t("No matching capabilities")}</div>}
    {[false, true].map((local) => {
      const rows = shown.filter((e) => (e.kind === "cmd") === local);
      if (!rows.length) return null;
      return <section key={String(local)}><div className="sec">{local ? t("Local execution tools") : t("External agents")} <span className="count-badge-plain">{rows.length}</span></div>
        <div className="cap-engine-grid">{rows.map((e) => {
          const profiles = members.filter((m) => m.engine === e.id);
          return <article className="card" key={e.id}>
            <h3><span>{e.avatar} {e.name}</span><span className="tag">{e.kind === "cmd" ? t("Local execution tools") : e.kind === "http" ? t("Chat gateway") : t("Command-line engine")}</span></h3>
            <p className="muted">{e.role}</p>
            <div className={"tag " + (!on ? "warn" : e.kind !== "http" && e.found ? "on" : "")}>
              {!on ? t("Globally disabled") : e.kind === "http" ? t("Connection not verified") : e.found ? t("Runtime detected; task inputs still required") : t("Needs runtime or project setup")}
            </div>
            {!e.found && e.hint && <details className="ext-details"><summary>{t("Configuration help")}</summary><p className="muted">{e.hint}</p></details>}
            {profiles.map((m) => {
              const used = groups.filter((g) => g.member_ids.includes(m.id));
              return <div className="cap-readiness" key={m.id}>
                <b>{m.avatar} {m.name} <span className="tag">{t("Profile added")}</span></b>
                {e.kind === "cmd" ? <span className="muted">{t("Runs inside the assigned group's workspace. Runtime detection does not verify project files or generation models.")}</span>
                  : m.binding?.provider_id ? <span className="muted">{m.binding.provider_name} · {m.binding.model || t("Not set")}</span>
                    : e.kind === "http" ? <span className="muted">{t("Model")}: {m.engine_cfg?.model || t("Not set")} · {t("Address")}: {m.engine_cfg?.base_url || t("Not set")}</span>
                      : <span className="muted">{t("Permissions:")} {levelLabel(m.engine_cfg?.level ?? "read")}</span>}
                {m.binding?.problem && <div className="err">{m.binding.problem}</div>}
                <div className="muted">{used.length ? t("Groups: {names}", { names: used.map((g) => g.name).join(", ") }) : t("Not used yet")}</div>
                <CapabilityBinding key={group?.id} kind="member" id={m.id} name={m.name} problem={!on ? t("Globally disabled") : ""} />
                <div className="ext-item-actions"><button className="btn small" onClick={() => setEditId(m.id)}><Settings2 size={12} />{t("Configure {name}", { name: m.name })}</button></div>
              </div>;
            })}
            {!profiles.length && <p className="muted">{t("No profile added yet")}</p>}
            {e.provider ? <button className="btn small" onClick={() => onTab("providers")}>{t("Manage provider models")}</button>
              : (!e.single || !profiles.length) && <button className="btn small" disabled={!on} onClick={() => setCreateEngine(e.id)}><Plus size={12} />{profiles.length ? t("Add another profile") : t("Add profile")}</button>}
          </article>;
        })}</div>
      </section>;
    })}
    <p className="muted small">{t("ComfyUI and other image/video models are managed under Providers and join through Add group tools. Skills, plugins and MCP services are managed in their own sections.")} <button className="link" onClick={() => onTab("providers")}>{t("Providers")}</button></p>
    {editing && <ExternalDialog mode="edit" agent={editing} onClose={() => setEditId(null)} onDone={() => { setEditId(null); void load(); }} />}
    {createEngine && <ExternalDialog mode="create" engine={createEngine} onClose={() => setCreateEngine("")} onDone={() => { setCreateEngine(""); void load(); }} />}
  </div>;
}
