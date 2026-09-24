import { useEffect, useMemo, useState } from "react";
import { LayoutTemplate, LoaderCircle, Sparkles } from "lucide-react";
import { api, type GroupTemplate } from "../api";
import { useData } from "../data";
import { HOME_DRAFT, draftText, setDraftText } from "../drafts";
import { useRoute } from "../hooks";
import { useI18n } from "../i18n";
import { SCENES } from "../lib";
import Composer from "../components/Composer";
import HomeSetup from "../components/HomeSetup";
import type { SettingsTab } from "../settings/SettingsModal";
import "../styles/chat.css";

export default function HomePage({ onOpen, onSettings }: { onOpen: (gid: string, autoSend: string) => void; onSettings?: (tab: SettingsTab) => void }) {
  const { t, pick, lang } = useI18n();
  const { agents, groups, reload, reloadGroups } = useData();
  const route = useRoute();
  const [sceneId, setSceneId] = useState(SCENES[0].id);
  // The task being described is kept outside this component: opening a group unmounts the home
  // screen, and the same draft loss that hit the chat box would hit this one — with the extra cost
  // that what is lost here is the whole description of the job.
  const [text, setText] = useState(() => draftText(HOME_DRAFT));
  const [target, setTarget] = useState("new");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // What the new group is made of: where it works, and who is in it. Both are the user's choice.
  // A group used to arrive with the scene's three stock members, so every group started with
  // people nobody had picked — and with nowhere of its own to put their files.
  const [workspace, setWorkspace] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [templates, setTemplates] = useState<GroupTemplate[]>([]);
  const [tplErr, setTplErr] = useState("");
  const [tplBusy, setTplBusy] = useState("");
  const [presetAva, setPresetAva] = useState<Map<string, string>>(new Map());

  useEffect(() => {
    setDraftText(HOME_DRAFT, text);
  }, [text]);

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
  const creating = target === "new";
  // Who a new group can be built from. The same rule the member adder uses: a "model member" is a
  // handle the app created for a model, not somebody the user made, so it is not offered here —
  // models join a group through the member adder's model list.
  const pickable = useMemo(() => agents.filter((a) => a.origin !== "model"), [agents]);
  // The scene's own lineup, offered as a *suggestion* rather than applied: the scene tabs used to
  // fill a new group with these three names whether or not anybody wanted them. Now nothing is
  // added until the button is pressed. `/api/agents` answers with the name in the request
  // language, so the lookup uses that language's list.
  const suggested = useMemo(() => {
    const names = lang === "zh" ? scene.membersZh : scene.members;
    return names.map((n) => pickable.find((a) => a.name === n)).filter(Boolean) as typeof agents;
  }, [scene, pickable, lang, agents]);
  const members = useMemo(
    () => picked.map((id) => agents.find((a) => a.id === id)).filter(Boolean) as typeof agents,
    [picked, agents],
  );
  const toggle = (id: string) => setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const targetGroup = creating ? null : groups.find((g) => g.id === target) ?? null;
  const mentionable = useMemo(() => {
    if (targetGroup) return targetGroup.member_ids.map((i) => agents.find((a) => a.id === i)).filter(Boolean) as typeof agents;
    return members;
  }, [targetGroup, members, agents]);

  const start = async () => {
    const task = text.trim();
    if (!task || busy) return;
    setErr("");
    if (creating && members.length === 0) {
      setErr(t("Choose who is in this group chat first — a group needs somebody to do the work."));
      return;
    }
    setBusy(true);
    try {
      // The task is handed over and the draft goes with it. It used to disappear on its own,
      // because opening a group unmounted this page; now that the draft is kept, clearing it has
      // to be deliberate — otherwise walking back to the home screen shows the last task again as
      // if it had never been sent. On failure the text stays, which is the point of keeping it.
      if (targetGroup) {
        setText("");
        return onOpen(targetGroup.id, task);
      }
      const title = task.replace(/@\S+/g, "").replace(/\s+/g, " ").trim().slice(0, 14) || pick(scene.label, scene.labelZh);
      const g = await api.createGroup(title, members.map((a) => a.id), members[0].id, { workspace });
      await reloadGroups();
      setText("");
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
          placeholder={t("Describe the task; type @ to hand it to a member")}
          routeText={route.text}
          offline={route.offline}
          onToggleExternal={route.toggleExternal}
          rows={4}
          autoFocus
          error={err}
          extra={
            <>
              <label className="target-select" title={t("Which group chat to send to")}>
                <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label={t("Send to")}>
                  <option value="new">{t("New group chat")}</option>
                  {groups.map((g) => (
                    <option key={g.id} value={g.id}>{t('Send to "{name}"', { name: g.name })}</option>
                  ))}
                </select>
              </label>
              {/* Where it works and who is in it: the same row as the other send controls, instead of
                  a card above the box that pushed the box down the window. Only while a *new* group is
                  being made — sending to an existing group has no draft to configure. */}
              {creating && (
                <HomeSetup
                  workspace={workspace}
                  onWorkspace={setWorkspace}
                  members={pickable}
                  picked={picked}
                  onToggle={toggle}
                  onPicked={setPicked}
                  suggested={suggested}
                  sceneName={pick(scene.label, scene.labelZh)}
                />
              )}
            </>
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
