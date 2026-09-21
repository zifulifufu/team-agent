import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Braces, Copy, LoaderCircle, Pencil, Plus, Trash2, Users } from "lucide-react";
import { api, type PromptItem, type Skill } from "../api";
import { useData } from "../data";
import { Modal, Switch, useConfirm, useFlash, useOutside } from "../ui";
import "../styles/know.css";

type Vars = { name: string; desc: string }[];

// ------------------------------------------------------------------ helpers
/** 在 textarea 光标处插入文本,并把光标放到插入内容之后。 */
function insertAtCursor(ref: RefObject<HTMLTextAreaElement>, value: string, set: (v: string) => void, text: string) {
  const el = ref.current;
  const s = el?.selectionStart ?? value.length;
  const e = el?.selectionEnd ?? value.length;
  set(value.slice(0, s) + text + value.slice(e));
  window.requestAnimationFrame(() => {
    el?.focus();
    el?.setSelectionRange(s + text.length, s + text.length);
  });
}

/** 防抖(400ms)调用预览接口:返回变量代入后的文本和原文 token 估算。 */
function usePreview(content: string, groupId: string, agentId: string) {
  const [state, setState] = useState<{ text: string; raw: number | null; err: string; busy: boolean }>({ text: "", raw: null, err: "", busy: false });
  useEffect(() => {
    if (!content.trim()) {
      setState({ text: "", raw: 0, err: "", busy: false });
      return;
    }
    setState((s) => ({ ...s, busy: true }));
    let alive = true;
    const t = window.setTimeout(() => {
      api
        .previewPrompt(content, { group_id: groupId || undefined, agent_id: agentId || undefined })
        .then((r) => alive && setState({ text: r.text, raw: r.raw_tokens, err: "", busy: false }))
        .catch((e) => alive && setState((s) => ({ ...s, err: (e as Error).message, busy: false })));
    }, 400);
    return () => { alive = false; window.clearTimeout(t); };
  }, [content, groupId, agentId]);
  return state;
}

/** 群 + 成员选择(默认第一个群与它的群主),用于预览变量代入。 */
function usePreviewTarget() {
  const { groups, agents } = useData();
  const [gid, setGid] = useState("");
  const [aid, setAid] = useState("");
  const group = groups.find((g) => g.id === gid) ?? groups[0] ?? null;
  const members = useMemo(() => (group ? group.member_ids.map((i) => agents.find((a) => a.id === i)).filter(Boolean) as typeof agents : []), [group, agents]);
  const agent = members.find((a) => a.id === aid) ?? members.find((a) => a.id === group?.host_agent_id) ?? members[0] ?? null;
  return {
    groups, members,
    groupId: group?.id ?? "", agentId: agent?.id ?? "",
    setGroup: (id: string) => { setGid(id); setAid(""); },
    setAgent: setAid,
  };
}

const tokenText = (n: number | null) => (n === null ? "…" : `约 ${n} tokens`);
const excerpt = (s: string, n = 120) => { const f = s.replace(/\s+/g, " ").trim(); return f.length > n ? f.slice(0, n) + "…" : f; };

// --------------------------------------------------------------------- page
export default function PromptsPage() {
  const { groups, reloadGroups } = useData();
  const confirm = useConfirm();
  const [items, setItems] = useState<PromptItem[]>([]);
  const [vars, setVars] = useState<Vars>([]);
  const [defaultText, setDefaultText] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [rowErr, setRowErr] = useState("");
  const [notice, setNotice] = useState("");
  const [edit, setEdit] = useState<PromptItem | "new" | null>(null);
  const [applying, setApplying] = useState<PromptItem | null>(null);
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [skillErr, setSkillErr] = useState("");

  // 全局系统提示词
  const [sys, setSys] = useState("");
  const [saved, setSaved] = useState("");

  const load = useCallback(async (first = false) => {
    try {
      const r = await api.prompts();
      setItems(r.prompts);
      setVars(r.variables);
      setDefaultText(r.default_system_prompt);
      if (first) {
        setSys(r.system_prompt);
        setSaved(r.system_prompt);
      }
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(true); }, [load]);
  useEffect(() => { api.skills().then(setSkills).catch((e) => setSkillErr((e as Error).message)); }, []);
  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(""), 3500);
    return () => window.clearTimeout(t);
  }, [notice]);

  const groupSkills = (skills ?? []).filter((s) => s.scope === "group");
  const globals = items.filter((p) => p.use_globally).length;

  const toggleGlobal = async (p: PromptItem, v: boolean) => {
    setRowErr("");
    setItems((l) => l.map((x) => (x.id === p.id ? { ...x, use_globally: v } : x)));
    try {
      await api.patchPrompt(p.id, { use_globally: v });
    } catch (e) {
      setRowErr(`「${p.title}」:${(e as Error).message}`);
      await load();
    }
  };
  const remove = async (p: PromptItem) => {
    if (!(await confirm(`删除提示词「${p.title}」?${p.use_globally ? "它正在「全局使用」,删除后不会再加到成员的系统提示词里。" : ""}已套用到群聊的内容不受影响。`, { okText: "删除" }))) return;
    setRowErr("");
    try {
      await api.delPrompt(p.id);
      await load();
    } catch (e) {
      setRowErr(`删除失败:${(e as Error).message}`);
    }
  };

  return (
    <div className="kn-page">
      <div className="kn-inner">
        <div className="kn-head">
          <div className="kn-head-main">
            <h1>提示词</h1>
            <p className="kn-desc">管理发给每位成员的全局系统提示词,以及可以反复套用的提示词。</p>
          </div>
        </div>
        {err && <div className="err kn-block">{err}</div>}
        {loading && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> 加载中…</div>}

        {!loading && !err && (
          <>
            <SystemPromptSection sys={sys} setSys={setSys} saved={saved} setSaved={setSaved} vars={vars} defaultText={defaultText} />

            <section className="kn-sec" aria-labelledby="kn-lib">
              <div className="kn-sec-head">
                <h2 id="kn-lib">提示词库</h2>
                <button className="btn primary" onClick={() => setEdit("new")}><Plus size={15} /> 添加提示词</button>
              </div>
              <p className="kn-desc">
                可复用的提示词。标记为「全局使用」的会加到所有成员的系统提示词里(以「补充要求」的形式排在角色提示词之后,会占用每次对话的上下文,不宜太多);其余的可以随时套用到某个群的「本群提示词」或复制使用。
              </p>
              {globals > 0 && (
                <p className={"small kn-globals" + (globals >= 4 ? " warn" : " muted")}>
                  目前有 {globals} 条「全局使用」{globals >= 4 ? ",偏多,建议只留最关键的几条" : ""}。
                </p>
              )}
              {notice && <div className="ok-text kn-block">{notice}</div>}
              {rowErr && <div className="err kn-block">{rowErr}</div>}
              {items.length === 0 ? (
                <div className="kn-empty-box">
                  <p><b>提示词库还是空的。</b>把常用的口吻要求、输出格式、项目背景存成提示词,之后一键套用。</p>
                  <button className="btn small" onClick={() => setEdit("new")}><Plus size={14} /> 添加提示词</button>
                </div>
              ) : (
                <div className="kn-prompts">
                  {items.map((p) => (
                    <PromptCard
                      key={p.id}
                      p={p}
                      onGlobal={(v) => void toggleGlobal(p, v)}
                      onEdit={() => setEdit(p)}
                      onDelete={() => void remove(p)}
                      onApply={() => setApplying(p)}
                      canApply={groups.length > 0}
                    />
                  ))}
                </div>
              )}
            </section>

            <section className="kn-sec" aria-labelledby="kn-skills">
              <div className="kn-sec-head"><h2 id="kn-skills">群聊规则技能</h2></div>
              <p className="kn-desc">
                这类技能挂到整个群后全员遵守(如评审会规则、头脑风暴规则)。在群聊右侧「扩展」里勾选;在「设置 → 技能」里新建或从 GitHub 安装。
              </p>
              {skillErr && <div className="err kn-block">{skillErr}</div>}
              {skills && groupSkills.length === 0 && !skillErr && <div className="empty">还没有群聊规则类的技能。</div>}
              {groupSkills.length > 0 && (
                <div className="kn-skills">
                  {groupSkills.map((s) => (
                    <div key={s.name} className="kn-skill">
                      <div className="kn-skill-name">{s.name}</div>
                      <div className="kn-skill-desc">{s.description || "(没有描述)"}</div>
                    </div>
                  ))}
                </div>
              )}
              <p className="kn-note"><Users size={14} /> 主持、评审、记录、资料员等岗位也可以随时从群聊右侧「成员 → 预设岗位」拉进群。</p>
            </section>
          </>
        )}
      </div>

      {edit && (
        <PromptDialog
          prompt={edit === "new" ? null : edit}
          vars={vars}
          onClose={() => setEdit(null)}
          onSaved={async () => { setEdit(null); await load(); }}
        />
      )}
      {applying && (
        <ApplyModal
          prompt={applying}
          onClose={() => setApplying(null)}
          onDone={async (name) => { setApplying(null); await reloadGroups(); setNotice(`已把「${applying.title}」套用到群聊「${name}」的本群提示词。`); }}
        />
      )}
    </div>
  );
}

// --------------------------------------------------- A. 全局系统提示词
function SystemPromptSection({ sys, setSys, saved, setSaved, vars, defaultText }: {
  sys: string; setSys: (v: string) => void; saved: string; setSaved: (v: string) => void; vars: Vars; defaultText: string;
}) {
  const { reload } = useData();
  const confirm = useConfirm();
  const ta = useRef<HTMLTextAreaElement>(null);
  const target = usePreviewTarget();
  const pv = usePreview(sys, target.groupId, target.agentId);
  const [showPv, setShowPv] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, flashOk] = useFlash();
  const dirty = sys !== saved;

  // 自适应高度(至少 10 行)
  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + 2 + "px";
  }, [sys]);

  const save = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.putSettings({ system_prompt: sys });
      setSaved(sys);
      await reload();
      flashOk();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    if (!(await confirm("恢复为默认的全局系统提示词?你现在写的内容(包括未保存的修改)会被覆盖。", { okText: "恢复默认" }))) return;
    setErr("");
    try {
      const r = await api.resetSystemPrompt();
      setSys(r.system_prompt);
      setSaved(r.system_prompt);
      await reload();
      flashOk();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <section className="kn-sec first" aria-labelledby="kn-sys">
      <div className="kn-sec-head"><h2 id="kn-sys">全局系统提示词</h2></div>
      <p className="kn-desc">每个成员每次发言前都会先收到这段话,然后才是他们各自的角色提示词。</p>

      <textarea
        ref={ta}
        className="kn-mono"
        rows={10}
        value={sys}
        onChange={(e) => setSys(e.target.value)}
        aria-label="全局系统提示词"
        spellCheck={false}
      />
      <div className="kn-var-bar">
        <span className="small muted kn-var-label"><Braces size={13} /> 变量</span>
        {vars.map((v) => (
          <button key={v.name} className="kn-var" title={v.desc} onClick={() => insertAtCursor(ta, sys, setSys, `{{${v.name}}}`)}>
            {`{{${v.name}}}`}
          </button>
        ))}
      </div>
      <div className="kn-sys-bar">
        <span className="small muted" title="估算值,不同模型的分词方式不同">{tokenText(pv.raw)}</span>
        {dirty && <span className="tag warn">有未保存的修改</span>}
        {ok && !dirty && <span className="ok-text">已保存</span>}
        {err && <span className="err small">{err}</span>}
        <span className="grow" />
        <button className="btn" onClick={() => void reset()} disabled={busy || (sys === defaultText && saved === defaultText)}>恢复默认</button>
        <button className={"btn" + (showPv ? " on" : "")} aria-pressed={showPv} onClick={() => setShowPv((v) => !v)}>预览</button>
        <button className="btn primary" disabled={busy || !dirty} onClick={() => void save()}>{busy ? "保存中…" : "保存"}</button>
      </div>

      {showPv && (
        <div className="kn-preview" aria-label="预览">
          <div className="kn-preview-head">
            <span className="small muted">变量代入后的样子,以</span>
            {target.groups.length === 0 ? (
              <span className="small muted">(还没有群聊,变量无法代入)</span>
            ) : (
              <>
                <select className="kn-select" value={target.groupId} onChange={(e) => target.setGroup(e.target.value)} aria-label="预览用的群聊">
                  {target.groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
                <select className="kn-select" value={target.agentId} onChange={(e) => target.setAgent(e.target.value)} aria-label="预览用的成员">
                  {target.members.map((a) => <option key={a.id} value={a.id}>{a.avatar} {a.name}</option>)}
                </select>
                <span className="small muted">为例</span>
              </>
            )}
            {pv.busy && <LoaderCircle size={13} className="kn-spin" />}
          </div>
          {pv.err ? <div className="err small">{pv.err}</div> : <div className="kn-preview-text">{pv.text || <span className="muted">(空)</span>}</div>}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- card
function PromptCard({ p, onGlobal, onEdit, onDelete, onApply, canApply }: {
  p: PromptItem; onGlobal: (v: boolean) => void; onEdit: () => void; onDelete: () => void; onApply: () => void; canApply: boolean;
}) {
  const [copied, flash] = useFlash();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(p.content);
      flash();
    } catch {
      /* 剪贴板不可用时静默 */
    }
  };
  return (
    <article className={"kn-prompt" + (p.use_globally ? " global" : "")}>
      <div className="kn-prompt-top">
        <span className="kn-prompt-title">{p.title}</span>
        <span className={"tag " + (p.kind === "group" ? "on" : "")}>{p.kind === "group" ? "群聊提示词" : "通用"}</span>
        <span className="grow" />
        <label className="kn-global">
          <span>全局使用</span>
          <Switch checked={p.use_globally} onChange={onGlobal} label={`全局使用「${p.title}」`} />
        </label>
      </div>
      <p className="kn-prompt-text">{excerpt(p.content)}</p>
      <div className="kn-prompt-ops">
        <button className="btn small" onClick={onApply} disabled={!canApply} title={canApply ? "写入某个群的「本群提示词」" : "还没有群聊"}><Users size={13} /> 套用到群聊</button>
        <button className="btn small ghost" onClick={() => void copy()}><Copy size={13} /> {copied ? "已复制" : "复制"}</button>
        <span className="grow" />
        <button className="icon-btn tiny" aria-label={`编辑「${p.title}」`} title="编辑" onClick={onEdit}><Pencil size={14} /></button>
        <button className="icon-btn tiny kn-del" aria-label={`删除「${p.title}」`} title="删除" onClick={onDelete}><Trash2 size={14} /></button>
      </div>
    </article>
  );
}

// -------------------------------------------------- 添加 / 编辑 对话框
function PromptDialog({ prompt, vars, onClose, onSaved }: { prompt: PromptItem | null; vars: Vars; onClose: () => void; onSaved: () => void | Promise<void> }) {
  const [title, setTitle] = useState(prompt?.title ?? "");
  const [content, setContent] = useState(prompt?.content ?? "");
  const [kind, setKind] = useState<"general" | "group">(prompt?.kind ?? "general");
  const [glob, setGlob] = useState(prompt?.use_globally ?? false);
  const [showPv, setShowPv] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const menuRef = useOutside<HTMLDivElement>(menu, () => setMenu(false));
  const ta = useRef<HTMLTextAreaElement>(null);
  const target = usePreviewTarget();
  const pv = usePreview(content, target.groupId, target.agentId);
  const gname = target.groups.find((g) => g.id === target.groupId)?.name;
  const aname = target.members.find((a) => a.id === target.agentId)?.name;

  const save = async () => {
    if (!title.trim()) return setErr("请填写标题");
    if (!content.trim()) return setErr("请填写内容");
    setBusy(true);
    setErr("");
    try {
      if (prompt) await api.patchPrompt(prompt.id, { title: title.trim(), content, kind, use_globally: glob });
      else await api.addPrompt({ title: title.trim(), content, kind, use_globally: glob });
      await onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <Modal
      title={prompt ? "编辑提示词" : "添加提示词"}
      wide
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "保存"}</button>
        </>
      }
    >
      <label className="field">
        <span>标题</span>
        <input value={title} autoFocus onChange={(e) => { setTitle(e.target.value); setErr(""); }} placeholder="如:先给结论" />
      </label>

      <div className="field">
        <div className="kn-label-row">
          <label htmlFor="kn-pd-content">内容</label>
          <span className="grow" />
          <div className="kn-menu-wrap" ref={menuRef}>
            <button className="btn small" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
              <Braces size={13} /> {"{x}"} 变量
            </button>
            {menu && (
              <div className="kn-menu kn-var-menu" role="menu">
                {vars.map((v) => (
                  <button
                    key={v.name}
                    role="menuitem"
                    className="kn-menu-item kn-var-item"
                    onClick={() => { setMenu(false); insertAtCursor(ta, content, setContent, `{{${v.name}}}`); }}
                  >
                    <code>{`{{${v.name}}}`}</code>
                    <span className="muted small">{v.desc}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        <textarea id="kn-pd-content" ref={ta} className="kn-mono" rows={9} value={content} onChange={(e) => { setContent(e.target.value); setErr(""); }} placeholder="写下要发给成员的提示词,可用 {{变量}}" spellCheck={false} />
        <div className="kn-pd-foot">
          <label className="check-inline"><Switch checked={showPv} onChange={setShowPv} label="预览" /> 预览</label>
          <span className="grow" />
          <span className="small muted">{tokenText(pv.raw)}</span>
        </div>
      </div>

      {showPv && (
        <div className="kn-preview inner">
          <div className="kn-preview-head">
            <span className="small muted">
              {gname ? `变量代入后的样子(以「${gname}」的「${aname ?? "成员"}」为例)` : "还没有群聊,变量无法代入"}
            </span>
            {pv.busy && <LoaderCircle size={13} className="kn-spin" />}
          </div>
          {pv.err ? <div className="err small">{pv.err}</div> : <div className="kn-preview-text">{pv.text || <span className="muted">(空)</span>}</div>}
        </div>
      )}

      <div className="field">
        <span>类型</span>
        <div className="kn-kinds" role="radiogroup" aria-label="提示词类型">
          {([["general", "通用"], ["group", "群聊提示词"]] as const).map(([k, label]) => (
            <button key={k} role="radio" aria-checked={kind === k} className={"kn-kind-pick" + (kind === k ? " on" : "")} onClick={() => setKind(k)}>{label}</button>
          ))}
        </div>
        <span className="muted small">「群聊提示词」用来约定整个群怎么协作(项目背景、讨论规则),通用的是口吻、格式之类的要求;两者都可以套用到群聊。</span>
      </div>
      <div className="setting-row">
        <div>
          <div className="sr-title">全局使用</div>
          <div className="sr-desc">开启后会加到所有成员的系统提示词里,占用每次对话的上下文。</div>
        </div>
        <Switch checked={glob} onChange={setGlob} label="全局使用" />
      </div>
    </Modal>
  );
}

// ------------------------------------------------------- 套用到群聊
function ApplyModal({ prompt, onClose, onDone }: { prompt: PromptItem; onClose: () => void; onDone: (groupName: string) => void | Promise<void> }) {
  const { groups } = useData();
  const [gid, setGid] = useState(groups[0]?.id ?? "");
  const [mode, setMode] = useState<"replace" | "append">("replace");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const g = groups.find((x) => x.id === gid);
  const has = !!g?.prompt.trim();

  const go = async () => {
    if (!g) return setErr("请选择一个群聊");
    setBusy(true);
    setErr("");
    try {
      await api.applyPrompt(g.id, prompt.id, mode);
      await onDone(g.name);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`套用「${prompt.title}」`}
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={busy || !g} onClick={() => void go()}>{busy ? "套用中…" : "套用"}</button>
        </>
      }
    >
      <label className="field">
        <span>套用到哪个群聊的「本群提示词」</span>
        <select value={gid} onChange={(e) => setGid(e.target.value)}>
          {groups.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
      </label>
      <div className="field">
        <span>方式</span>
        <div className="seg" role="radiogroup" aria-label="套用方式">
          <button role="radio" aria-checked={mode === "replace"} className={mode === "replace" ? "on" : ""} onClick={() => setMode("replace")}>替换</button>
          <button role="radio" aria-checked={mode === "append"} className={mode === "append" ? "on" : ""} onClick={() => setMode("append")}>追加</button>
        </div>
        <span className={"small " + (has && mode === "replace" ? "kn-warn" : "muted")}>
          {!has
            ? "这个群目前没有本群提示词。"
            : mode === "replace"
              ? `这个群已有 ${g!.prompt.length} 字的本群提示词,替换会覆盖它。`
              : "会接在现有的本群提示词后面(空一行)。"}
        </span>
      </div>
    </Modal>
  );
}
