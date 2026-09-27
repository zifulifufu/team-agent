import { useCallback, useEffect, useState } from "react";
import { Plus, TerminalSquare } from "lucide-react";
import { api, type AgentPreset, type ExternalOverview, type Group } from "../../api";
import { useData } from "../../data";
import { StrengthChips } from "../Strengths";
import { useI18n } from "../../i18n";
import ModelRoster from "./ModelRoster";
import ExternalDialog from "../ExternalDialog";

/**
 * The "add group member" panel: the models you added (used directly as members), the ones that
 * *generate* rather than chat (they take part as a member you can address), existing members, role
 * presets, and external agents. Opened by the + next to "Members" in the sidebar and by "Add
 * member" in the chat header.
 */
export default function MemberAdder({ group, initialSection = "members" }: { group: Group; initialSection?: "members" | "tools" }) {
  const [section, setSection] = useState(initialSection);
  const [source, setSource] = useState(initialSection === "tools" ? "all" : "models");
  const { t } = useI18n();
  const { agents, settings, reload, reloadGroups } = useData();
  const [presets, setPresets] = useState<AgentPreset[] | null>(null);
  const [presetQuery, setPresetQuery] = useState("");
  const [presetErr, setPresetErr] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const gid = group.id;
  const [ext, setExt] = useState<ExternalOverview | null>(null);
  const [extOpen, setExtOpen] = useState<string | null>(null);   // the engine id whose dialog is open
  const loadExt = useCallback(() => { api.externalOverview().then(setExt).catch(() => setExt(null)); }, []);
  useEffect(() => { if (source === "external" || (section === "tools" && source === "all")) loadExt(); }, [loadExt, source, section]);

  const loadPresets = useCallback(() => {
    api.agentPresets().then((p) => { setPresets(p); setPresetErr(""); }).catch((e) => setPresetErr((e as Error).message));
  }, []);
  useEffect(() => { if (source === "presets" || source === "roles") loadPresets(); }, [loadPresets, source, group.member_ids.length]);

  const others = agents.filter((a) => a.origin !== "model" && a.origin !== "media" && !a.engine);
  const extOthers = agents.filter((a) => !!a.engine && !!a.is_tool === (section === "tools"));
  const matchingPresets = (presets ?? []).filter((p) => (p.kind === "expert") === (source === "presets")
    && (!presetQuery || `${p.name} ${p.role}`.toLowerCase().includes(presetQuery.toLowerCase())));

  // One row shape for both sections: an expert is an ordinary member with a sharper remit.
  const presetRow = (p: AgentPreset) => {
    const here = !!p.agent_id && group.member_ids.includes(p.agent_id);
    return (
    <div key={p.key} className="madd-item">
      <span className="avatar sm">{p.avatar}</span>
      <div className="madd-main">
        <div className="madd-name">{p.name}</div>
        <div className="madd-sub wrap">{p.role}</div>
        {p.tags.length > 0 && <StrengthChips tags={p.tags} max={3} />}
      </div>
      {here ? <span className="membership-badge">{t("Already in this group")}</span>
        : <button className="btn small" disabled={!!busy} onClick={() => run("preset:" + p.key, () => api.addMemberFromPreset(gid, p.key), true).then(loadPresets)} aria-label={t("Add {name} to the group", { name: p.name })}>
        <Plus size={12} /> {t("Add")}
      </button>}
    </div>
  ); };

  const run = async (key: string, fn: () => Promise<unknown>, full = false) => {
    setBusy(key);
    setErr("");
    try {
      await fn();
      await (full ? reload() : reloadGroups());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="madd">
      <div className="roster-tabs" role="tablist" aria-label={t("Add members or tools")}>
        {(["members", "tools"] as const).map((value) => <button key={value} role="tab" aria-selected={section === value} onClick={() => { setSection(value); setSource(value === "tools" ? "all" : "models"); }}>{t(value === "tools" ? "Tools" : "Members")}</button>)}
      </div>
      {err && <div className="err madd-err" role="alert">{err}</div>}
      <div className="madd-sources" role="group" aria-label={t("Choose member source")}>
        {(section === "members"
          ? [{ id: "models", label: "Model members" }, { id: "existing", label: "Existing members" },
             { id: "presets", label: "Experts" }, { id: "roles", label: "Role presets" }, { id: "external", label: "External agents" }]
          : [{ id: "all", label: "All tools" }, { id: "models", label: "Image and video tools" }, { id: "external", label: "Local execution tools" }]
        ).map(({ id, label }) => <button key={id} aria-pressed={source === id} onClick={() => setSource(id)}>{t(label)}</button>)}
      </div>
      {(source === "external" || (section === "tools" && source === "all")) && <>
      <div className="madd-sec">{t(section === "tools" ? "Local execution tools" : "External agents")}</div>
      <div className="madd-note">{t(section === "tools" ? "Renderers need a prepared project; voice tools need the exact narration. Add them here, then let members assign the work." : "Let an agent you already run join the discussion as a member — a command-line agent with its own tools, or a chat gateway such as Cherry Studio or MetaChat. Off by default, and you have to turn it on.")}</div>
      <div className="local-tool-roster" role="list" aria-label={t(section === "tools" ? "Local execution tools" : "External agents")}>
      {extOthers.map((a) => {
        const here = group.member_ids.includes(a.id);
        const engine = ext?.engines.find((e) => e.id === a.engine);
        return <div key={a.id} className="madd-item" role="listitem">
          <span className="avatar sm">{a.avatar}</span>
          <div className="madd-main"><div className="madd-name">{a.name}</div><div className="madd-sub wrap">{a.role}</div>
            <span className="madd-sub wrap" title={engine?.hint}>{!settings?.external_agents_enabled ? t("External agents are switched off")
              : !engine ? t("Not checked") : engine.found ? t("Runtime detected; task inputs still required") : t("Needs runtime or project setup")}</span>
          </div>
          {here ? <span className="membership-badge">{t("Already in this group")}</span>
            : <button className="btn small" disabled={!!busy || !settings?.external_agents_enabled} title={settings?.external_agents_enabled ? "" : t("The external-agent master switch is still off")}
                onClick={() => run("add:" + a.id, () => api.addMember(gid, a.id))} aria-label={t("Add {name}", { name: a.name })}><Plus size={12} /> {t("Add")}</button>}
        </div>;
      })}
      </div>
      {ext?.engines.filter((e) => !e.provider && !extOthers.some((a) => a.engine === e.id) && (e.kind === "cmd") === (section === "tools")).map((e) => (
        <div key={e.id} className="madd-item">
          <span className="avatar sm" aria-hidden>{e.avatar}</span>
          <div className="madd-main">
            <div className="madd-name">{e.name}</div>
            <div className="madd-sub wrap">
              {!ext.enabled ? t("Master switch is off (you can turn it on while adding)")
                : e.kind === "http" ? e.base_url
                : e.found ? t("Command-line engine found")
                : t("No command-line engine found (see the notes in the add dialog)")}
            </div>
          </div>
          <button className="btn small" disabled={!!busy || !ext} onClick={() => setExtOpen(e.id)} aria-label={t("Add {name} as a member", { name: e.name })}><Plus size={12} /> {t("Add")}</button>
        </div>
      ))}
      {!ext && (
        <div className="madd-item">
          <span className="avatar sm" aria-hidden><TerminalSquare size={15} /></span>
          <div className="madd-main"><div className="madd-name">{t(section === "tools" ? "Local execution tools" : "External agents")}</div><div className="madd-sub">{t("Reading status…")}</div></div>
        </div>
      )}
      {extOpen && <ExternalDialog mode="create" group={group} engine={extOpen} onClose={() => setExtOpen(null)} onDone={async () => { setExtOpen(null); await reloadGroups(); loadExt(); }} />}

      </>}
      {(source === "models" || (section === "tools" && source === "all")) && <ModelRoster key={section} group={group} section={section} busy={busy} run={run} />}
      {section === "members" && source === "existing" && <>
      <div className="madd-sec">{t("Existing members")}</div>
      {others.length === 0 ? (
        <div className="madd-none">{t("Every member is already in this group")}</div>
      ) : (
        others.map((a) => (
          <div key={a.id} className="madd-item">
            <span className="avatar sm">{a.avatar}</span>
            <div className="madd-main">
              <div className="madd-name">{a.name}</div>
              <div className="madd-sub">{a.role || t("Member")}</div>
              {a.tags.length > 0 && <StrengthChips tags={a.tags} max={3} />}
            </div>
            {group.member_ids.includes(a.id) ? <span className="membership-badge">{t("Already in this group")}</span>
              : <button className="btn small" disabled={!!busy} onClick={() => run("add:" + a.id, () => api.addMember(gid, a.id))} aria-label={t("Add {name}", { name: a.name })}>
                <Plus size={12} /> {t("Add")}
              </button>}
          </div>
        ))
      )}

      </>}
      {section === "members" && (source === "presets" || source === "roles") && <>
        <div className="madd-sec">{t(source === "presets" ? "Experts" : "Role presets")}</div>
        <div className="madd-note">{t("Experts are members with domain expertise. They can lead, execute assignments and review results.")}</div>
        <input className="madd-q" aria-label={t("Search members")} placeholder={t("Search members…")} value={presetQuery} onChange={(e) => setPresetQuery(e.target.value)} />
        {presetErr && <div className="err madd-err">{presetErr}</div>}
        {presets === null && !presetErr && <div className="madd-none">{t("Loading…")}</div>}
        <div className="team-existing-list">{matchingPresets.map(presetRow)}</div>
      </>}
    </div>
  );
}
