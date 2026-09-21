import { useCallback, useEffect, useMemo, useState } from "react";
import { Cpu, Plus, TerminalSquare } from "lucide-react";
import { api, type AgentPreset, type ExternalOverview, type Group, type Model } from "../../api";
import { useData } from "../../data";
import { StrengthChips } from "../Strengths";
import { HealthDot } from "../Health";
import ExternalDialog from "../ExternalDialog";

/**
 * 「添加群成员」面板:三个来源 —— 我添加的模型(直接当成员)/ 已有成员 / 预设岗位。
 * 侧边栏「成员」旁的 + 和聊天标题栏的「添加成员」都打开它。
 */
export default function MemberAdder({ group }: { group: Group }) {
  const { agents, providers, health, reload, reloadGroups } = useData();
  const [presets, setPresets] = useState<AgentPreset[] | null>(null);
  const [presetErr, setPresetErr] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [mq, setMq] = useState("");
  const gid = group.id;
  const [ext, setExt] = useState<ExternalOverview | null>(null);
  const [extOpen, setExtOpen] = useState(false);
  const loadExt = useCallback(() => { api.externalOverview().then(setExt).catch(() => setExt(null)); }, []);
  useEffect(loadExt, [loadExt, group.member_ids.length]);

  const loadPresets = useCallback(() => {
    api.agentPresets().then((p) => { setPresets(p); setPresetErr(""); }).catch((e) => setPresetErr((e as Error).message));
  }, []);
  useEffect(loadPresets, [loadPresets, group.member_ids.length]);

  // 服务商和模型都启用的,才能拉进群
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
  const wb = ext?.engines[0];
  const freshPresets = (presets ?? []).filter((p) => !p.exists);

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

      <div className="madd-sec">我添加的模型 <span className="count-badge-lite">{modelCount}</span></div>
      <div className="madd-note">把「模型服务」里已启用的模型直接拉进群,它就是一个成员,强项取自模型本身。</div>
      {usable.length === 0 ? (
        <div className="madd-none">还没有可用的模型,先去设置 → 模型服务添加并启用。</div>
      ) : (
        <>
          {modelCount > 8 && <input className="madd-q" placeholder="搜索模型…" value={mq} onChange={(e) => setMq(e.target.value)} aria-label="搜索模型" />}
          {usable.map(({ p, models }) => {
            const list = models.filter((m) => !mqs || m.display_name.toLowerCase().includes(mqs) || m.model_name.toLowerCase().includes(mqs));
            if (list.length === 0) return null;
            return (
              <div key={p.id} className="madd-group">
                <div className="madd-prov">{p.name}{p.is_local && <span className="tag">本地</span>}{!p.is_local && !p.has_key && <span className="tag warn">未填 Key</span>}</div>
                {list.map((m) => (
                  <div key={m.id} className="madd-item">
                    <span className="avatar sm" aria-hidden><Cpu size={15} /></span>
                    <div className="madd-main">
                      <div className="madd-name"><HealthDot h={health[m.id]} /> {m.display_name}</div>
                      {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={3} />}
                    </div>
                    {inGroup(m) ? (
                      <span className="muted small madd-here">已在群里</span>
                    ) : (
                      <button className="btn small" disabled={!!busy} onClick={() => run("model:" + m.id, () => api.addMemberFromModel(gid, m.id), true)} aria-label={`把模型 ${m.display_name} 拉进群`}>
                        <Plus size={12} /> 拉入
                      </button>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </>
      )}

      <div className="madd-sec">外部智能体</div>
      <div className="madd-note">让 WorkBuddy 这样自带工具的智能体作为群成员参与讨论。默认只读、默认关闭,需要你主动打开。</div>
      {extOthers.map((a) => (
        <div key={a.id} className="madd-item">
          <span className="avatar sm">{a.avatar}</span>
          <div className="madd-main"><div className="madd-name">{a.name}</div><div className="madd-sub">{a.role}</div></div>
          <button className="btn small" disabled={!!busy || !ext?.enabled} title={ext?.enabled ? "" : "外部智能体总开关还没打开"} onClick={() => run("add:" + a.id, () => api.addMember(gid, a.id))} aria-label={`拉入 ${a.name}`}><Plus size={12} /> 拉入</button>
        </div>
      ))}
      <div className="madd-item">
        <span className="avatar sm" aria-hidden><TerminalSquare size={15} /></span>
        <div className="madd-main">
          <div className="madd-name">WorkBuddy</div>
          <div className="madd-sub wrap">
            {!ext ? "读取状态…" : !ext.enabled ? "总开关未打开(点添加时可以打开)" : wb?.found ? "已找到命令行引擎" : "没找到命令行引擎(见添加窗口里的说明)"}
          </div>
        </div>
        <button className="btn small" disabled={!!busy || !ext} onClick={() => setExtOpen(true)} aria-label="添加 WorkBuddy 为外部智能体成员"><Plus size={12} /> 添加</button>
      </div>
      {extOpen && <ExternalDialog mode="create" group={group} onClose={() => setExtOpen(false)} onDone={async () => { setExtOpen(false); await reloadGroups(); loadExt(); }} />}

      <div className="madd-sec">已有成员</div>
      {others.length === 0 ? (
        <div className="madd-none">所有成员都已在本群</div>
      ) : (
        others.map((a) => (
          <div key={a.id} className="madd-item">
            <span className="avatar sm">{a.avatar}</span>
            <div className="madd-main">
              <div className="madd-name">{a.name}</div>
              <div className="madd-sub">{a.role || "成员"}</div>
              {a.tags.length > 0 && <StrengthChips tags={a.tags} max={3} />}
            </div>
            <button className="btn small" disabled={!!busy} onClick={() => run("add:" + a.id, () => api.addMember(gid, a.id))} aria-label={`拉入 ${a.name}`}>
              <Plus size={12} /> 拉入
            </button>
          </div>
        ))
      )}

      <div className="madd-sec">预设岗位</div>
      {presetErr && <div className="err madd-err">{presetErr}</div>}
      {presets === null && !presetErr && <div className="madd-none">加载中…</div>}
      {presets !== null && freshPresets.length === 0 && <div className="madd-none">预设岗位都已经创建过了</div>}
      {freshPresets.map((p) => (
        <div key={p.key} className="madd-item">
          <span className="avatar sm">{p.avatar}</span>
          <div className="madd-main">
            <div className="madd-name">{p.name}</div>
            <div className="madd-sub wrap">{p.role}</div>
            {p.tags.length > 0 && <StrengthChips tags={p.tags} max={3} />}
          </div>
          <button className="btn small" disabled={!!busy} onClick={() => run("preset:" + p.key, () => api.addMemberFromPreset(gid, p.key), true).then(loadPresets)} aria-label={`添加预设岗位 ${p.name}`}>
            <Plus size={12} /> 添加
          </button>
        </div>
      ))}
    </div>
  );
}
