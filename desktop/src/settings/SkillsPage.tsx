import { useCallback, useEffect, useMemo, useState } from "react";
import { Pencil, Plus, RefreshCw, Trash2, Users, User } from "lucide-react";
import { api, type Skill } from "../api";
import { useData } from "../data";
import { Modal, useConfirm } from "../ui";
import { GithubMark, SourceBadge, Spin } from "../components/ExtBits";
import { RepoDiscoverModal } from "../components/RepoDiscover";
import "../styles/ext.css";

const SCOPE_LABEL = { group: "群聊规则", member: "成员技能" } as const;

import type { SettingsTab } from "./SettingsModal";

export default function SkillsPage({ onTab }: { onTab?: (t: SettingsTab) => void } = {}) {
  const { agents, groups, reload, reloadUpdates } = useData();
  const confirm = useConfirm();
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [newer, setNewer] = useState<Set<string>>(new Set());   // GitHub 上有新版本的技能
  const [editing, setEditing] = useState<Skill | "new" | null>(null);
  const [discover, setDiscover] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkMsg, setCheckMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [rowErr, setRowErr] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const [s, u] = await Promise.all([api.skills(), api.updates().catch(() => null)]);
      setSkills(s);
      setLoadErr("");
      if (u) setNewer(new Set(u.items.filter((i) => i.kind === "skill").map((i) => i.ref)));
    } catch (e) {
      setLoadErr((e as Error).message);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const usage = useMemo(() => {
    const m: Record<string, { agents: string[]; groups: string[] }> = {};
    for (const s of skills ?? []) {
      m[s.name] = {
        agents: agents.filter((a) => a.skills.includes(s.name)).map((a) => a.name),
        groups: groups.filter((g) => g.ext?.skills?.includes(s.name)).map((g) => g.name),
      };
    }
    return m;
  }, [skills, agents, groups]);

  const hasSource = (skills ?? []).some((s) => s.source);

  const check = async () => {
    setChecking(true);
    setCheckMsg(null);
    try {
      const r = await api.checkUpdates();
      const errs = (r.errors as string[] | undefined) ?? [];
      await load();
      await reloadUpdates();
      if (r.busy) setCheckMsg({ ok: false, text: "上一次检查还没结束,请稍后再试。" });
      else if (errs.length) setCheckMsg({ ok: false, text: "检查没有完全成功:" + errs.join(";") });
      else setCheckMsg({ ok: true, text: "检查完成。" });
    } catch (e) {
      setCheckMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const update = async (s: Skill) => {
    if (!(await confirm(`用 GitHub 上的最新内容更新技能「${s.name}」?这会覆盖本地内容,你在本地做过的修改会丢失。`, { okText: "更新并覆盖" }))) return;
    setUpdating(s.name);
    setRowErr((r) => ({ ...r, [s.name]: "" }));
    try {
      await api.updateSkill(s.name);
      await load();
      await reloadUpdates();
    } catch (e) {
      setRowErr((r) => ({ ...r, [s.name]: (e as Error).message }));
    } finally {
      setUpdating(null);
    }
  };

  const remove = async (s: Skill) => {
    const u = usage[s.name];
    const used = u && (u.agents.length || u.groups.length) ? `它目前被${u.agents.length ? ` ${u.agents.length} 个成员` : ""}${u.agents.length && u.groups.length ? "、" : ""}${u.groups.length ? ` ${u.groups.length} 个群` : ""}使用。` : "";
    if (!(await confirm(`删除技能「${s.name}」?${used}同时会从所有成员和群里移除对它的勾选。`, { okText: "删除" }))) return;
    try {
      await api.delSkill(s.name);
      await Promise.all([load(), reload(), reloadUpdates()]);
    } catch (e) {
      setRowErr((r) => ({ ...r, [s.name]: (e as Error).message }));
    }
  };

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">技能</h2>
        <div className="sp-head-actions">
          {hasSource && (
            <button className="btn" onClick={check} disabled={checking}>
              {checking ? <><Spin /> 检查中…</> : <><RefreshCw size={14} /> 检查更新</>}
            </button>
          )}
          <button className="btn" onClick={() => setDiscover(true)}><GithubMark size={14} /> 从 GitHub 发现</button>
          <button className="btn primary" onClick={() => setEditing("new")}><Plus size={15} /> 新建技能</button>
        </div>
      </div>
      <p className="sp-desc">
        技能是一段写给模型看的纯文本说明(怎么写公文、评审会怎么开……),不会执行任何代码。「成员技能」勾给某个成员,「群聊规则」挂在整个群上、全员遵守。{onTab && <> 想要现成的?<button className="link" onClick={() => onTab("gallery")}>去模板中心</button>一键导入。</>}
      </p>
      {checkMsg && <div className={checkMsg.ok ? "ok-text" : "err"} style={{ marginBottom: 10 }}>{checkMsg.text}</div>}
      {loadErr && <div className="ext-errbox"><div className="err">读取技能失败:{loadErr}</div><button className="btn small" onClick={() => void load()}>重试</button></div>}

      {!skills && !loadErr && <div className="empty"><Spin /> 加载中…</div>}
      {skills && (
        <div className="card flush">
          {skills.length === 0 && (
            <div className="empty">
              还没有技能。点右上角「新建技能」自己写一个,或者「从 GitHub 发现」现成的。
            </div>
          )}
          {skills.map((s) => {
            const u = usage[s.name] ?? { agents: [], groups: [] };
            const hasNew = newer.has(s.name);
            return (
              <div key={s.name} className="ext-item">
                <div className="model-row">
                  <div className="mr-main">
                    <div className="mr-name">
                      {s.name}
                      <span className={"tag " + (s.scope === "group" ? "warn" : "on")}>{s.scope === "group" ? <Users size={11} /> : <User size={11} />} {SCOPE_LABEL[s.scope]}</span>
                      {s.version && <span className="tag">v{s.version}</span>}
                      {s.source && <SourceBadge repo={s.source.repo} path={s.source.path} />}
                      {hasNew && <span className="tag new">GitHub 上有新版本</span>}
                    </div>
                    {s.description && <div className="ext-item-desc">{s.description}</div>}
                    <div className="ext-item-sub">
                      {u.agents.length === 0 && u.groups.length === 0
                        ? "还没有被使用"
                        : [u.agents.length ? `成员:${u.agents.join("、")}` : "", u.groups.length ? `群:${u.groups.join("、")}` : ""].filter(Boolean).join(" · ")}
                    </div>
                    {rowErr[s.name] && <div className="ext-errline">{rowErr[s.name]}</div>}
                  </div>
                  <div className="ext-item-actions">
                    {s.source && (
                      <button className={"btn small" + (hasNew ? " primary" : "")} disabled={updating === s.name} onClick={() => void update(s)} title="用 GitHub 上的最新内容覆盖本地技能">
                        {updating === s.name ? <><Spin size={12} /> 更新中</> : "更新"}
                      </button>
                    )}
                    <button className="icon-btn" title="编辑" aria-label={`编辑技能 ${s.name}`} onClick={() => setEditing(s)}><Pencil size={15} /></button>
                    <button className="icon-btn" title="删除" aria-label={`删除技能 ${s.name}`} onClick={() => void remove(s)}><Trash2 size={15} /></button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <p className="muted small" style={{ marginTop: 12, lineHeight: 1.7 }}>
        成员技能:在「成员」页编辑成员时勾选。群聊规则(以及成员技能):在群聊右侧面板的「扩展」里挂到群。
      </p>

      {editing && (
        <SkillDialog
          skill={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await Promise.all([load(), reload()]); }}
        />
      )}
      {discover && <RepoDiscoverModal kind="skill" onClose={() => setDiscover(false)} onInstalled={async () => { setDiscover(false); await Promise.all([load(), reloadUpdates()]); }} />}
    </div>
  );
}

export function SkillDialog({ skill, onClose, onSaved }: { skill: Skill | null; onClose: () => void; onSaved: (name: string) => Promise<void> }) {
  const [name, setName] = useState(skill?.name ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [scope, setScope] = useState<"member" | "group">(skill?.scope ?? "member");
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(!!skill);
  const [loadErr, setLoadErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!skill) return;
    let alive = true;
    api.skill(skill.name)
      .then((s) => alive && setBody(s.body ?? ""))
      .catch((e) => alive && setLoadErr((e as Error).message))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [skill]);

  const save = async () => {
    setBusy(true);
    setErr("");
    const payload = { name: name.trim(), description: description.trim(), body, scope };
    try {
      if (skill) await api.saveSkill(skill.name, payload);
      else await api.addSkill(payload);
      await onSaved(payload.name);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const canSave = name.trim() && body.trim() && !loading && !loadErr && !busy;
  return (
    <Modal
      title={skill ? `编辑技能「${skill.name}」` : "新建技能"}
      onClose={onClose}
      wide
      actions={
        <>
          {err && <span className="err ext-act-err" role="alert">{err}</span>}
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={!canSave} onClick={() => void save()}>{busy ? <><Spin /> 保存中…</> : "保存"}</button>
        </>
      }
    >
      <label className="field">
        <span>名称</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如:评审会规则" maxLength={60} autoFocus />
        {skill && skill.source && <span className="ext-field-note">这个技能来自 GitHub。改名后它和来源的对应关系会断开,「更新」将不再可用。</span>}
        {skill && !skill.source && <span className="ext-field-note">改名后,成员和群里对它的勾选会跟着改。</span>}
      </label>
      <label className="field">
        <span>一句话描述(可选)</span>
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="说明它在什么时候用" />
      </label>
      <div className="field">
        <span>范围</span>
        <div className="ext-radio-row" role="radiogroup" aria-label="技能范围">
          <label className={"check" + (scope === "member" ? " on" : "")}>
            <input type="radio" name="skill-scope" checked={scope === "member"} onChange={() => setScope("member")} />
            <span>成员技能<small>勾选给某个成员,只影响他自己的回答方式,如「公文写作规范」。</small></span>
          </label>
          <label className={"check" + (scope === "group" ? " on" : "")}>
            <input type="radio" name="skill-scope" checked={scope === "group"} onChange={() => setScope("group")} />
            <span>群聊规则<small>挂在整个群上,全员遵守,如「评审会规则」「头脑风暴规则」。</small></span>
          </label>
        </div>
      </div>
      <label className="field">
        <span>正文(写给模型看的说明,纯文本 / Markdown)</span>
        {loading ? <div className="empty"><Spin /> 读取内容…</div> : (
          <textarea className="ext-mono-area" rows={11} value={body} onChange={(e) => setBody(e.target.value)} placeholder={"写清楚规则,例如:\n1. 开头一句话交代结论;\n2. 正文分点……"} spellCheck={false} />
        )}
        <div className="ext-count">{body.length} 字</div>
      </label>
      {loadErr && <div className="err">读取正文失败:{loadErr}</div>}
    </Modal>
  );
}
