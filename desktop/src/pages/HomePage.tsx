import { useEffect, useMemo, useState } from "react";
import { LayoutTemplate, LoaderCircle, Sparkles } from "lucide-react";
import { api, type GroupTemplate } from "../api";
import { useData } from "../data";
import { useRoute } from "../hooks";
import { SCENES } from "../lib";
import Composer from "../components/Composer";
import type { SettingsTab } from "../settings/SettingsModal";
import "../styles/chat.css";

export default function HomePage({ onOpen, onSettings }: { onOpen: (gid: string, autoSend: string) => void; onSettings?: (t: SettingsTab) => void }) {
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
    // 模板里的成员可能还没创建(会由预设岗位新建),用预设的头像先显示
    api.agentPresets().then((ps) => setPresetAva(new Map(ps.map((p) => [p.name, p.avatar])))).catch(() => undefined);
  }, []);

  const useTemplate = async (t: GroupTemplate) => {
    if (tplBusy) return;
    setTplErr("");
    setTplBusy(t.id);
    try {
      const g = await api.createFromTemplate(t.id);
      await reload(); // 模板可能新建了成员,连同群一起刷新
      onOpen(g.id, "");
    } catch (e) {
      setTplErr((e as Error).message);
    } finally {
      setTplBusy("");
    }
  };

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
    const t = text.trim();
    if (!t || busy) return;
    setErr("");
    setBusy(true);
    try {
      if (targetGroup) return onOpen(targetGroup.id, t);
      if (sceneAgents.length === 0) throw new Error("还没有成员,请先到左侧「成员」里创建");
      const title = t.replace(/@\S+/g, "").replace(/\s+/g, " ").trim().slice(0, 14) || scene.label;
      const g = await api.createGroup(title, sceneAgents.map((a) => a.id), sceneAgents[0].id);
      await reloadGroups();
      onOpen(g.id, t);
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
          <p>把国内外大模型拉进同一个群,各展所长,协同完成办公、视频制作与创作。</p>
        </div>

        <div className="scene-tabs" role="tablist">
          {SCENES.map((s) => (
            <button key={s.id} role="tab" aria-selected={s.id === sceneId} className={s.id === sceneId ? "on" : ""} onClick={() => setSceneId(s.id)}>
              {s.label}
            </button>
          ))}
        </div>

        <Composer
          value={text}
          onChange={setText}
          onSend={start}
          busy={busy}
          members={mentionable}
          placeholder="描述你的任务;输入 @ 点名成员分工,不点名则由小助统筹"
          routeText={route.text}
          offline={route.offline}
          onToggleExternal={route.toggleExternal}
          rows={4}
          autoFocus
          error={err}
          extra={
            <label className="target-select" title="发送到哪个群聊">
              <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="发送到">
                <option value="new">新建群聊 · {sceneAgents.map((a) => a.name).join("、")}</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>发送到「{g.name}」</option>
                ))}
              </select>
            </label>
          }
        />

        <div className="chips">
          {scene.chips.map((c) => (
            <button key={c.label} className="chip-btn" onClick={() => setText(c.prompt)}>
              {c.label}
            </button>
          ))}
        </div>

        {(templates.length > 0 || tplErr) && (
          <section className="tpl-section" aria-label="群聊模板">
            <div className="tpl-title">
              <LayoutTemplate size={14} aria-hidden /> 群聊模板
              <span className="muted small">一键建好成员、群主、技能和提示词</span>
              {onSettings && <button className="link small" style={{ marginLeft: "auto" }} onClick={() => onSettings("awesome")}>更多团队:示例库 →</button>}
            </div>
            {tplErr && <div className="err tpl-err" role="alert">{tplErr}</div>}
            <div className="tpl-grid">
              {templates.map((t) => (
                <button key={t.id} className="tpl-card" disabled={!!tplBusy} onClick={() => void useTemplate(t)} aria-label={`用模板「${t.name}」新建群聊`}>
                  <div className="tpl-name">
                    {t.name}
                    {tplBusy === t.id && <LoaderCircle size={13} className="spin" aria-hidden />}
                  </div>
                  <div className="tpl-desc">{t.desc}</div>
                  <div className="tpl-members">
                    {t.members.map((n) => (
                      <span key={n} className={"tpl-mem" + (n === t.host ? " host" : "")} title={n === t.host ? `${n}(群主)` : n}>
                        <span className="tpl-ava">{agents.find((a) => a.name === n)?.avatar ?? presetAva.get(n) ?? "🤖"}</span>
                        {n}
                      </span>
                    ))}
                  </div>
                  {t.skills.length > 0 && (
                    <div className="tpl-skills">
                      {t.skills.map((k) => (
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
