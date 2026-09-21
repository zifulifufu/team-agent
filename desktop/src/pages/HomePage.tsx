import { useEffect, useMemo, useState } from "react";
import { LayoutTemplate, LoaderCircle, Sparkles } from "lucide-react";
import { api, type GroupTemplate } from "../api";
import { useData } from "../data";
import { useRoute } from "../hooks";
import { useI18n } from "../i18n";
import { SCENES } from "../lib";
import Composer from "../components/Composer";
import type { SettingsTab } from "../settings/SettingsModal";
import "../styles/chat.css";

export default function HomePage({ onOpen, onSettings }: { onOpen: (gid: string, autoSend: string) => void; onSettings?: (tab: SettingsTab) => void }) {
  const { t, pick } = useI18n();
  const { agents, groups, reload, reloadGroups } = useData();
  const route = useRoute();
  const [sceneId, setSceneId] = useState(SCENES[0].id);
  const [text, setText] = useState("");
  const [target, setTarget] = useState("new");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [templates, setTemplates] = useState<GroupTemplate[]>([]);
  const [tplErr, setTplErr] = useState("");
  const [tplBusy, setTplBusy] = useState("");
  const [presetAva, setPresetAva] = useState<Map<string, string>>(new Map());

  useEffect(() => {
    api.templates().then(setTemplates).catch((e) => setTplErr((e as Error).message));
    // Members referenced by a template may not exist yet (they get created from the role presets),
    // so show the preset avatar until the real member shows up.
    api.agentPresets().then((ps) => setPresetAva(new Map(ps.map((p) => [p.name, p.avatar])))).catch(() => undefined);
  }, []);

  const useTemplate = async (tpl: GroupTemplate) => {
    if (tplBusy) return;
    setTplErr("");
    setTplBusy(tpl.id);
    try {
      const g = await api.createFromTemplate(tpl.id);
      await reload(); // a template may have created members, so refresh both lists
      onOpen(g.id, "");
    } catch (e) {
      setTplErr((e as Error).message);
    } finally {
      setTplBusy("");
    }
  };

  // Only the common templates go on the home page; the rest live in Settings → Template gallery
  const homeTemplates = useMemo(() => templates.filter((tpl) => tpl.home !== false), [templates]);
  const scene = SCENES.find((s) => s.id === sceneId)!;
  const sceneAgents = useMemo(() => {
    const picked = scene.members.map((n) => agents.find((a) => a.name === n)).filter(Boolean) as typeof agents;
    return picked.length ? picked : agents;
  }, [scene, agents]);

  const targetGroup = target === "new" ? null : groups.find((g) => g.id === target) ?? null;
  const mentionable = useMemo(() => {
    if (targetGroup) return targetGroup.member_ids.map((i) => agents.find((a) => a.id === i)).filter(Boolean) as typeof agents;
    return sceneAgents;
  }, [targetGroup, sceneAgents, agents]);

  const start = async () => {
    const task = text.trim();
    if (!task || busy) return;
    setErr("");
    setBusy(true);
    try {
      if (targetGroup) return onOpen(targetGroup.id, task);
      if (sceneAgents.length === 0) throw new Error(t("No members yet — create some under Members in the sidebar first"));
      const title = task.replace(/@\S+/g, "").replace(/\s+/g, " ").trim().slice(0, 14) || pick(scene.label, scene.labelZh);
      const g = await api.createGroup(title, sceneAgents.map((a) => a.id), sceneAgents[0].id);
      await reloadGroups();
      onOpen(g.id, task);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="home">
      <div className="home-inner">
        <div className="hero">
          <div className="hero-logo"><Sparkles size={22} /></div>
          <h1>Team Agent</h1>
          <p>{t("Pull hosted and local models into one group and let each do what it is best at — office documents, video production, writing.")}</p>
        </div>

        <div className="scene-tabs" role="tablist">
          {SCENES.map((s) => (
            <button key={s.id} role="tab" aria-selected={s.id === sceneId} className={s.id === sceneId ? "on" : ""} onClick={() => setSceneId(s.id)}>
              {pick(s.label, s.labelZh)}
            </button>
          ))}
        </div>

        <Composer
          value={text}
          onChange={setText}
          onSend={start}
          busy={busy}
          members={mentionable}
          placeholder={t("Describe your task; type @ to assign members, or leave it and 小助 will coordinate")}
          routeText={route.text}
          offline={route.offline}
          onToggleExternal={route.toggleExternal}
          rows={4}
          autoFocus
          error={err}
          extra={
            <label className="target-select" title={t("Which group chat to send to")}>
              <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label={t("Send to")}>
                <option value="new">{t("New group chat · {names}", { names: sceneAgents.map((a) => a.name).join("、") })}</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>{t('Send to "{name}"', { name: g.name })}</option>
                ))}
              </select>
            </label>
          }
        />

        <div className="chips">
          {scene.chips.map((c) => (
            <button key={c.label} className="chip-btn" onClick={() => setText(pick(c.prompt, c.promptZh))}>
              {pick(c.label, c.labelZh)}
            </button>
          ))}
        </div>

        {(homeTemplates.length > 0 || tplErr) && (
          <section className="tpl-section" aria-label={t("Group templates")}>
            <div className="tpl-title">
              <LayoutTemplate size={14} aria-hidden /> {t("Group templates")}
              <span className="muted small">{t("Members, host, skills and prompt installed in one click")}</span>
              {onSettings && <button className="link small" style={{ marginLeft: "auto" }} onClick={() => onSettings("gallery")}>{t("More teams: template gallery →")}</button>}
            </div>
            {tplErr && <div className="err tpl-err" role="alert">{tplErr}</div>}
            <div className="tpl-grid">
              {homeTemplates.map((tpl) => (
                <button key={tpl.id} className="tpl-card" disabled={!!tplBusy} onClick={() => void useTemplate(tpl)} aria-label={t('Create a group chat from template "{name}"', { name: tpl.name })}>
                  <div className="tpl-name">
                    {tpl.name}
                    {tplBusy === tpl.id && <LoaderCircle size={13} className="spin" aria-hidden />}
                  </div>
                  <div className="tpl-desc">{tpl.desc}</div>
                  <div className="tpl-members">
                    {tpl.members.map((n) => (
                      <span key={n} className={"tpl-mem" + (n === tpl.host ? " host" : "")} title={n === tpl.host ? t("{name} (host)", { name: n }) : n}>
                        <span className="tpl-ava">{agents.find((a) => a.name === n)?.avatar ?? presetAva.get(n) ?? "🤖"}</span>
                        {n}
                      </span>
                    ))}
                  </div>
                  {tpl.skills.length > 0 && (
                    <div className="tpl-skills">
                      {tpl.skills.map((k) => (
                        <span key={k} className="tag on">{k}</span>
                      ))}
                    </div>
                  )}
                </button>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
