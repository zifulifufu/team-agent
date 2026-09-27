import { useEffect, useMemo, useState } from "react";
import { LayoutTemplate, LoaderCircle } from "lucide-react";
import { api, mayHost, type GroupTemplate, type TeamAdvice, type TeamDraftMember, type TeamAdviceExpert } from "../api";
import { useData } from "../data";
import { HOME_DRAFT, draftText, setDraftText } from "../drafts";
import { useRoute } from "../hooks";
import { useI18n } from "../i18n";
import { SCENES } from "../lib";
import BrandMark from "../components/BrandMark";
import Composer from "../components/Composer";
import HomeSetup from "../components/HomeSetup";
import TeamReview from "../components/TeamReview";
import type { SettingsTab } from "../settings/SettingsModal";
import "../styles/chat.css";

export default function HomePage({ onOpen, onSettings }: { onOpen: (gid: string, autoSend: string) => void; onSettings?: (tab: SettingsTab) => void }) {
  const { t, pick, lang } = useI18n();
  const { agents, groups, reload } = useData();
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
  const [pickedModels, setPickedModels] = useState<string[]>([]);
  const [pickedExperts, setPickedExperts] = useState<TeamAdviceExpert[]>([]);
  const [review, setReview] = useState<{ task: string; advice: TeamAdvice; initial: TeamDraftMember[] } | null>(null);
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
  // Existing model members and role members can both be staged in a new team.
  const pickable = agents;
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
  // Who runs the group. It used to be "whoever was ticked first", which silently meant that ticking
  // an external agent (WorkBuddy) or a generator (a picture or video model) first made the *server*
  // refuse the whole creation — the picker showed two names ticked, the send failed, and no group
  // appeared. The chair now goes to the first member who can actually hold it, so the order people
  // happen to tick in stops deciding whether the group can exist at all.
  const host = useMemo(() => members.find(mayHost) ?? null, [members]);
  // The lineup suggested for the task as typed. Computed on a pause rather than per keystroke: the
  // server's rule pass is cheap, but it walks the whole member list, and calling it 40 times for
  // 40 characters is work nobody asked for. Nothing is created here — see `adopt`.
  const [advice, setAdvice] = useState<TeamAdvice | null>(null);
  const [adviceBusy, setAdviceBusy] = useState(false);
  // Chips the user took out of the suggestion: a member id, or an expert key. It lives here rather
  // than inside `AdviceBlock` so it survives that component re-rendering, **and it is cleared the
  // moment a new suggestion arrives**: a removal was made against one lineup, and carrying it into
  // the next one would silently drop somebody the user never looked at.
  const [skipped, setSkipped] = useState<string[]>([]);
  useEffect(() => {
    if (!creating) { setAdvice(null); setSkipped([]); return; }
    const q = text.trim();
    // Below this there is nothing to read: "做个" or "hello" would match half the member list and
    // the suggestion would be noise dressed as advice.
    if (q.length < 6) { setAdvice(null); setAdviceBusy(false); return; }
    let live = true;
    const timer = window.setTimeout(() => {
      setAdviceBusy(true);
      api.teamSuggest(q).then((a) => { if (live) { setAdvice(a); setSkipped([]); } })
        .catch(() => { if (live) setAdvice(null); })
        .finally(() => { if (live) setAdviceBusy(false); });
    }, 700);
    return () => { live = false; window.clearTimeout(timer); };
  }, [text, creating]);

  /** Take one chip out of the suggestion.
   *
   *  Two effects, and both are needed for the screen to stay true: the chip is struck out, and if
   *  that member happened to be ticked already it is unticked as well — otherwise the panel would
   *  show it as in-the-group while the suggestion says it was removed, and the user would have to
   *  work out which of the two the send button is going to believe. */
  const skipOne = (key: string) => {
    setSkipped((cur) => (cur.includes(key) ? cur.filter((x) => x !== key) : [...cur, key]));
    setPicked((cur) => cur.filter((x) => x !== key));
    setPickedExperts((cur) => cur.filter((e) => e.key !== key));
    setPickedModels((cur) => cur.filter((id) => id !== key));
  };

  // Choosing a suggested lineup only edits the draft. New expert rows are created on confirmation.
  const adopt = (a: TeamAdvice) => {
    setErr("");
    setPicked(a.members.filter((m) => !skipped.includes(m.id)).map((m) => m.id));
    setPickedExperts(a.experts.filter((e) => !skipped.includes(e.key)));
    setPickedModels((a.models ?? []).filter((m) => !skipped.includes(m.id)).map((m) => m.id));
  };
  // Only generators and external agents were picked: there is no chair to hand out, and the server
  // would refuse. Said here, next to the choice that caused it, instead of as a red line under the
  // box after the fact.
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
      if (!creating) throw new Error(t("The target group is no longer available. Choose a group again."));
      const next = await api.teamSuggest(task);
      const manual = picked.length > 0 || pickedExperts.length > 0 || pickedModels.length > 0;
      const ids = manual ? picked : next.members.length ? next.members.map((m) => m.id) : suggested.map((m) => m.id);
      const experts = manual ? pickedExperts : next.experts;
      const initial: TeamDraftMember[] = [
        ...ids.map((id) => ({ kind: "agent" as const, id })),
        ...experts.map((e) => ({ kind: "preset" as const, id: e.key })),
        ...(manual ? pickedModels : (next.models ?? []).map((m) => m.id)).map((id) => ({ kind: "model" as const, id })),
      ];
      setReview({ task, advice: { ...next, experts: [...new Map([...next.experts, ...experts].map((e) => [e.key, e])).values()] }, initial });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="home">
      {review && <TeamReview task={review.task} workspace={workspace} advice={review.advice} initial={review.initial}
        onClose={() => setReview(null)} onStart={(group, task) => { setReview(null); setText(""); onOpen(group.id, task); }} />}
      <div className="home-inner">
        <div className="hero">
          <BrandMark size={46} className="hero-logo" />
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

        {creating && <p className="home-flow-note">{t("Describe task → Review team → Assign work → Verify delivery")}</p>}
        <Composer
          value={text}
          onChange={setText}
          onSend={start}
          sendLabel={creating ? t("Build and review team") : t("Send")}
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
                  hostId={host ? host.id : null}
                  advice={advice}
                  adviceBusy={adviceBusy}
                  skipped={skipped}
                  onSkip={skipOne}
                  onAdopt={adopt}
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
            {/* A template brings its own members, its own host and its own prompt — so a lineup
                ticked above, and a folder chosen above, are not used. That used to happen in
                silence: the picker said "Members · 2", the template card was pressed, and the group
                that appeared had the template's people in it. Saying it here costs one line and
                removes the only way this ends with "the members I chose never joined". */}
            {(picked.length > 0 || workspace !== "") && (
              <div className="tpl-note">
                {t("A template brings its own members and working folder, so what you picked above is not used.")}
              </div>
            )}
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
