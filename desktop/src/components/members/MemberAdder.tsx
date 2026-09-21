import { useCallback, useEffect, useMemo, useState } from "react";
import { Cpu, Plus, TerminalSquare } from "lucide-react";
import { api, type AgentPreset, type ExternalOverview, type Group, type Model } from "../../api";
import { useData } from "../../data";
import { StrengthChips } from "../Strengths";
import { useI18n } from "../../i18n";
import { HealthDot } from "../Health";
import ExternalDialog from "../ExternalDialog";

/**
 * The "add group member" panel: three sources — the models you added (used directly
 * as members), existing members, and role presets. Opened by the + next to "Members"
 * in the sidebar and by "Add member" in the chat header.
 */
export default function MemberAdder({ group }: { group: Group }) {
  const { t } = useI18n();
  const { agents, providers, health, reload, reloadGroups } = useData();
  const [presets, setPresets] = useState<AgentPreset[] | null>(null);
  const [presetErr, setPresetErr] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [mq, setMq] = useState("");
  const gid = group.id;
  const [ext, setExt] = useState<ExternalOverview | null>(null);
  const [extOpen, setExtOpen] = useState<string | null>(null);   // the engine id whose dialog is open
  const loadExt = useCallback(() => { api.externalOverview().then(setExt).catch(() => setExt(null)); }, []);
  useEffect(loadExt, [loadExt, group.member_ids.length]);

  const loadPresets = useCallback(() => {
    api.agentPresets().then((p) => { setPresets(p); setPresetErr(""); }).catch((e) => setPresetErr((e as Error).message));
  }, []);
  useEffect(loadPresets, [loadPresets, group.member_ids.length]);

  // Both the provider and the model must be enabled for it to join a group
  const usable = useMemo(
    () => providers.filter((p) => p.enabled).map((p) => ({ p, models: p.models.filter((m) => m.enabled) })).filter((x) => x.models.length > 0),
    [providers],
  );
  const modelCount = usable.reduce((n, x) => n + x.models.length, 0);
  const modelAgent = (m: Model) => agents.find((a) => a.origin === "model" && a.model_id === m.id);
  const inGroup = (m: Model) => { const a = modelAgent(m); return !!a && group.member_ids.includes(a.id); };
  const mqs = mq.trim().toLowerCase();
  const others = agents.filter((a) => !group.member_ids.includes(a.id) && a.origin !== "model" && !a.engine);
  const extOthers = agents.filter((a) => !!a.engine && !group.member_ids.includes(a.id));
  const freshPresets = (presets ?? []).filter((p) => !p.exists);
  const freshExperts = freshPresets.filter((p) => p.kind === "expert");
  const freshRoles = freshPresets.filter((p) => p.kind !== "expert");

  // One row shape for both sections: an expert is an ordinary member with a sharper remit.
  const presetRow = (p: AgentPreset) => (
    <div key={p.key} className="madd-item">
      <span className="avatar sm">{p.avatar}</span>
      <div className="madd-main">
        <div className="madd-name">{p.name}</div>
        <div className="madd-sub wrap">{p.role}</div>
        {p.tags.length > 0 && <StrengthChips tags={p.tags} max={3} />}
      </div>
      <button className="btn small" disabled={!!busy} onClick={() => run("preset:" + p.key, () => api.addMemberFromPreset(gid, p.key), true).then(loadPresets)} aria-label={t("Add {name} to the group", { name: p.name })}>
        <Plus size={12} /> {t("Add")}
      </button>
    </div>
  );

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
      {err && <div className="err madd-err" role="alert">{err}</div>}

      <div className="madd-sec">{t("Models I added")} <span className="count-badge-lite">{modelCount}</span></div>
      <div className="madd-note">{t("Pull an enabled model from Providers straight into the group: it becomes a member, and its strengths come from the model itself.")}</div>
      {usable.length === 0 ? (
        <div className="madd-none">{t("No models available yet — add and enable one under Settings → Providers.")}</div>
      ) : (
        <>
          {modelCount > 8 && <input className="madd-q" placeholder={t("Search models…")} value={mq} onChange={(e) => setMq(e.target.value)} aria-label={t("Search models")} />}
          {usable.map(({ p, models }) => {
            const list = models.filter((m) => !mqs || m.display_name.toLowerCase().includes(mqs) || m.model_name.toLowerCase().includes(mqs));
            if (list.length === 0) return null;
            return (
              <div key={p.id} className="madd-group">
                <div className="madd-prov">{p.name}{p.is_local && <span className="tag">{t("Local")}</span>}{!p.is_local && !p.has_key && <span className="tag warn">{t("No API key")}</span>}</div>
                {list.map((m) => (
                  <div key={m.id} className="madd-item">
                    <span className="avatar sm" aria-hidden><Cpu size={15} /></span>
                    <div className="madd-main">
                      <div className="madd-name"><HealthDot h={health[m.id]} /> {m.display_name}</div>
                      {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={3} />}
                    </div>
                    {inGroup(m) ? (
                      <span className="muted small madd-here">{t("Already in this group")}</span>
                    ) : (
                      <button className="btn small" disabled={!!busy} onClick={() => run("model:" + m.id, () => api.addMemberFromModel(gid, m.id), true)} aria-label={t("Add model {name} to the group", { name: m.display_name })}>
                        <Plus size={12} /> {t("Add")}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </>
      )}

      <div className="madd-sec">{t("External agents")}</div>
      <div className="madd-note">{t("Let an agent you already run join the discussion as a member — a command-line agent with its own tools, or a chat gateway such as Cherry Studio or MetaChat. Off by default, and you have to turn it on.")}</div>
      {extOthers.map((a) => (
        <div key={a.id} className="madd-item">
          <span className="avatar sm">{a.avatar}</span>
          <div className="madd-main"><div className="madd-name">{a.name}</div><div className="madd-sub">{a.role}</div></div>
          <button className="btn small" disabled={!!busy || !ext?.enabled} title={ext?.enabled ? "" : t("The external-agent master switch is still off")} onClick={() => run("add:" + a.id, () => api.addMember(gid, a.id))} aria-label={t("Add {name}", { name: a.name })}><Plus size={12} /> {t("Add")}</button>
        </div>
      ))}
      {ext?.engines.map((e) => (
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
          <div className="madd-main"><div className="madd-name">{t("External agents")}</div><div className="madd-sub">{t("Reading status…")}</div></div>
        </div>
      )}
      {extOpen && <ExternalDialog mode="create" group={group} engine={extOpen} onClose={() => setExtOpen(null)} onDone={async () => { setExtOpen(null); await reloadGroups(); loadExt(); }} />}

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
            <button className="btn small" disabled={!!busy} onClick={() => run("add:" + a.id, () => api.addMember(gid, a.id))} aria-label={t("Add {name}", { name: a.name })}>
              <Plus size={12} /> {t("Add")}
            </button>
          </div>
        ))
      )}

      <div className="madd-sec">{t("Experts")}</div>
      <div className="madd-note">{t("Domain specialists: each one states how it works and where its competence stops. Add one and it takes part like any other member.")}</div>
      {presetErr && <div className="err madd-err">{presetErr}</div>}
      {presets === null && !presetErr && <div className="madd-none">{t("Loading…")}</div>}
      {presets !== null && freshExperts.length === 0 && <div className="madd-none">{t("All experts have already been created")}</div>}
      {freshExperts.map(presetRow)}

      <div className="madd-sec">{t("Role presets")}</div>
      {presets !== null && freshRoles.length === 0 && <div className="madd-none">{t("All role presets have already been created")}</div>}
      {freshRoles.map(presetRow)}
    </div>
  );
}
