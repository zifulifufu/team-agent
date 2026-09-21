import { useEffect, useState } from "react";
import { Plus, Settings2, Trash2 } from "lucide-react";
import { api, type Agent, type AgentPreset, type Model, type PromptItem, type Skill } from "../api";
import { useData } from "../data";
import { useBusy, useConfirm, useFlash } from "../ui";
import { StrengthChips, StrengthPicker } from "../components/Strengths";
import { ModelSelect } from "../components/Health";
import ExternalDialog from "../components/ExternalDialog";
import type { SettingsTab } from "../settings/SettingsModal";
import "../styles/models.css";

const BLANK: Partial<Agent> = { name: "", avatar: "🤖", role: "", prompt: "", model_id: null, skills: [], tags: [] };

export default function AgentsPage({ onSettings }: { onSettings: (t: SettingsTab) => void }) {
  const { agents, reload } = useData();
  const [sel, setSel] = useState<string | "new" | null>(null);
  const creating = sel === "new";
  const cur = creating ? null : agents.find((a) => a.id === sel) ?? (sel === null ? agents[0] : null) ?? null;

  return (
    <div className="page-cols">
      <aside className="page-list">
        <div className="page-list-head">
          <h2>成员</h2>
          <button className="btn small" onClick={() => setSel("new")}><Plus size={14} /> 新建</button>
        </div>
        <div className="page-list-body">
          {agents.map((a) => (
            <button key={a.id} className={"list-item" + (!creating && cur?.id === a.id ? " on" : "")} onClick={() => setSel(a.id)}>
              <span className="avatar sm">{a.avatar}</span>
              <span className="li-main">
                <span className="li-name">{a.name}</span>
                <span className="li-sub">
                  {a.role || "成员"}
                  {a.tags?.length > 0 && <span className="ag-tags"> · {a.tags.slice(0, 2).join(" ")}</span>}
                </span>
              </span>
            </button>
          ))}
        </div>
      </aside>
      <section className="page-detail">
        {creating || cur ? (
          <AgentForm key={creating ? "new" : cur!.id} agent={creating ? null : cur} onDone={async (id) => { await reload(); setSel(id ?? null); }} onSettings={onSettings} />
        ) : (
          <div className="empty big">还没有成员,先新建一个</div>
        )}
      </section>
    </div>
  );
}

function AgentForm({ agent, onDone, onSettings }: { agent: Agent | null; onDone: (id?: string) => Promise<void>; onSettings: (t: SettingsTab) => void }) {
  const { models } = useData();
  const confirm = useConfirm();
  const [f, setF] = useState<Partial<Agent>>(agent ?? BLANK);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [presets, setPresets] = useState<AgentPreset[]>([]);
  const [presetKey, setPresetKey] = useState("");
  const [prompts, setPrompts] = useState<PromptItem[]>([]);
  const [reco, setReco] = useState<{ models: (Model & { score: number })[] } | null>(null);
  const [recoErr, setRecoErr] = useState("");
  const [err, setErr] = useState("");
  const [extOpen, setExtOpen] = useState(false);
  const [saved, flash] = useFlash();
  useEffect(() => {
    api.skills().then((l) => setSkills(l.filter((s) => s.scope !== "group"))).catch(() => undefined);
    api.prompts().then((r) => setPrompts(r.prompts.filter((x) => x.kind === "general"))).catch(() => undefined);
    if (!agent) api.agentPresets().then(setPresets).catch(() => undefined);
  }, [agent]);

  // 没有固定模型时:按岗位强项预览「当前会优先用哪个模型」(防抖 300ms)
  const tagsKey = (f.tags ?? []).join(",");
  useEffect(() => {
    setRecoErr("");
    if (f.model_id || !tagsKey) { setReco(null); return; }
    let alive = true;
    const t = window.setTimeout(() => {
      api.recommendModels(tagsKey.split(","), 3)
        .then((r) => alive && setReco({ models: r.models }))
        .catch((e) => { if (alive) { setReco(null); setRecoErr((e as Error).message); } });
    }, 300);
    return () => { alive = false; window.clearTimeout(t); };
  }, [tagsKey, f.model_id]);

  const applyPreset = (p: AgentPreset) => {
    setPresetKey(p.key);
    setF((x) => ({ ...x, name: p.name, avatar: p.avatar, role: p.role, tags: [...p.tags], prompt: p.prompt }));
  };
  const insertPrompt = (id: string) => {
    const it = prompts.find((x) => x.id === id);
    if (!it) return;
    setF((x) => ({ ...x, prompt: x.prompt?.trim() ? `${x.prompt.replace(/\s+$/, "")}\n\n${it.content}` : it.content }));
  };

  const [saving, guard] = useBusy();
  const save = () => guard(async () => {
    setErr("");
    try {
      if (agent) {
        await api.patchAgent(agent.id, f);
        flash();
        await onDone(agent.id);
      } else {
        const a = await api.createAgent(f);
        await onDone(a.id);
      }
    } catch (e) {
      setErr((e as Error).message);
    }
  });

  const fixed = f.model_id ? models.find((m) => m.id === f.model_id && m.enabled) : undefined;
  return (
    <div className="detail-inner">
      <div className="detail-head">
        <h2>{agent ? agent.name : "新建成员"}</h2>
        {agent && (
          <button
            className="btn ghost small danger-text"
            onClick={async () => {
              if (await confirm(`删除成员「${agent.name}」?`, { okText: "删除" })) {
                await api.delAgent(agent.id);
                await onDone();
              }
            }}
          >
            <Trash2 size={14} /> 删除
          </button>
        )}
      </div>

      {!agent && presets.length > 0 && (
        <div className="field-block">
          <div className="fb-label">从预设创建</div>
          <div className="ag-presets" role="group" aria-label="预设岗位">
            {presets.map((p) => (
              <button
                key={p.key}
                type="button"
                className="ag-preset"
                style={presetKey === p.key ? { borderColor: "var(--primary)", background: "var(--primary-soft)" } : undefined}
                disabled={p.exists}
                title={p.exists ? "已有同名成员" : `${p.role}:${p.tags.join("、")}`}
                onClick={() => applyPreset(p)}
              >
                <span>{p.avatar}</span>{p.name}{p.exists && <small>已有同名成员</small>}
              </button>
            ))}
          </div>
          <div className="ag-help">点一个预设会把名称、职责、岗位强项和提示词填进下面的表单,确认后点「创建」才会保存。</div>
        </div>
      )}
      <div className="card">
        <div className="form-row">
          <label className="field" style={{ width: 84 }}><span>头像</span><input value={f.avatar ?? ""} onChange={(e) => setF({ ...f, avatar: e.target.value })} maxLength={4} /></label>
          <label className="field grow"><span>名称(群里用 @名称 点名,不能含空格)</span><input value={f.name ?? ""} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          <label className="field grow"><span>职责简介</span><input value={f.role ?? ""} onChange={(e) => setF({ ...f, role: e.target.value })} placeholder="如:视频分镜师" /></label>
        </div>
        <div className="field">
          <span>岗位强项(选得越准,分工越准;建议 4~5 个以内)</span>
          <StrengthPicker value={f.tags ?? []} onChange={(v) => setF({ ...f, tags: v })} />
          {(f.tags?.length ?? 0) > 5 && <div className="ag-help warn">已选 {f.tags!.length} 个,太多会让强项失去区分度,建议留最关键的 4~5 个。</div>}
          <div className="ag-help">群主分工时按这些强项分配任务;没有固定模型时,也按它自动挑选最合适的模型。强项是根据模型系列和名称推断的标签,不是评测成绩。</div>
        </div>
        {agent?.engine ? (
          <div className="field">
            <span>外部智能体</span>
            <div className="ag-reco">
              它由 WorkBuddy 自带的命令行引擎发言,不走本程序的模型路由,这里不用选模型。
              权限级别、工作目录、是否接力等,在下面单独设置。
              <div style={{ marginTop: 8 }}><button type="button" className="btn small" onClick={() => setExtOpen(true)}><Settings2 size={12} /> 外部智能体设置</button></div>
            </div>
          </div>
        ) : (
        <div className="field">
          <span>使用的模型(首选失败时自动回退到路由链)</span>
          <ModelSelect value={f.model_id ?? null} autoLabel="按强项自动选择(推荐)" ariaLabel="使用的模型" disabled={agent?.origin === "model"} onChange={(id) => setF({ ...f, model_id: id })} />
          {!f.model_id ? <RecoHint tags={f.tags ?? []} reco={reco} err={recoErr} /> : fixed ? (
            <div className="ag-reco">
              固定使用 <b>{fixed.display_name}</b>,它的强项(供和岗位强项对照):
              {fixed.strengths.length ? <StrengthChips tags={fixed.strengths} max={10} /> : <span className="muted"> 暂无</span>}
            </div>
          ) : (
            <div className="ag-reco warn">这个模型已被移除或未启用,保存后会退回按路由链选择。</div>
          )}
        </div>
        )}
        <div className="field">
          <div className="ag-prompt-head">
            <span>角色提示词</span>
            {prompts.length > 0 && (
              <select value="" onChange={(e) => insertPrompt(e.target.value)} aria-label="从提示词库插入">
                <option value="">从提示词库插入…</option>
                {prompts.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
              </select>
            )}
          </div>
          <textarea rows={6} value={f.prompt ?? ""} onChange={(e) => setF({ ...f, prompt: e.target.value })} placeholder="描述这个成员擅长什么、输出风格和格式要求" aria-label="角色提示词" />
        </div>
        <div className="field">
          <span>技能(Skills,会注入该成员的提示词,所有模型通用)</span>
          <div className="check-grid">
            {skills.map((s) => (
              <label key={s.name} className={"check" + (f.skills?.includes(s.name) ? " on" : "")} title={s.description}>
                <input type="checkbox" checked={!!f.skills?.includes(s.name)} onChange={(e) => setF({ ...f, skills: e.target.checked ? [...(f.skills ?? []), s.name] : (f.skills ?? []).filter((x) => x !== s.name) })} />
                {s.name}
              </label>
            ))}
            {skills.length === 0 && (
              <span className="muted small">暂无技能,<button className="link" onClick={() => onSettings("skills")}>去添加</button></span>
            )}
          </div>
        </div>
      </div>
      <div className="row">
        <button className="btn primary" disabled={!f.name?.trim() || saving} onClick={() => void save()}>{agent ? "保存" : "创建"}</button>
        {saved && <span className="ok-text">已保存</span>}
        {err && <span className="err">{err}</span>}
      </div>
      {extOpen && agent && <ExternalDialog mode="edit" agent={agent} onClose={() => setExtOpen(false)} onDone={() => setExtOpen(false)} />}
    </div>
  );
}

function RecoHint({ tags, reco, err }: { tags: string[]; reco: { models: (Model & { score: number })[] } | null; err: string }) {
  if (err) return <div className="ag-reco warn">暂时无法预估会用哪个模型:{err}</div>;
  if (!tags.length) return <div className="ag-reco">还没选岗位强项:会按「路由与回退」里的默认顺序选模型。选几个强项后,这里会显示当前会优先用哪个模型。</div>;
  if (!reco) return <div className="ag-reco muted">正在按强项挑选…</div>;
  if (!reco.models.length) return <div className="ag-reco warn">还没有可用模型:先在「模型服务」里填 API Key,或启动本地 Ollama。</div>;
  const [top, ...rest] = reco.models;
  const hit = tags.filter((t) => top.strengths.includes(t));
  return (
    <div className="ag-reco">
      当前会优先用:<b>{top.display_name}</b>(命中 {hit.length || Math.round(top.score)} 项{hit.length ? `:${hit.join("、")}` : ""})
      {rest.length > 0 && <div className="muted small">备选:{rest.map((m) => m.display_name).join("、")}</div>}
    </div>
  );
}
