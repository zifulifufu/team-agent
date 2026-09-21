import { useEffect, useMemo, useRef, useState } from "react";
import { Eye, Save } from "lucide-react";
import { api, type Group, type PromptsInfo } from "../../api";
import { useData } from "../../data";
import { Modal, useConfirm } from "../../ui";

/** 常用变量;完整清单以后端 /api/prompts 的 variables 为准。 */
const COMMON_VARS = ["agent_name", "agent_role", "group_name", "members", "model_name", "date", "time", "datetime", "weekday", "os", "username"];

export default function PromptTab({ group, active }: { group: Group; active: boolean }) {
  const { agents, reloadGroups } = useData();
  const confirm = useConfirm();
  const gid = group.id;
  const [draft, setDraft] = useState(group.prompt);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState<PromptsInfo | null>(null);
  const [infoErr, setInfoErr] = useState("");
  const [showAllVars, setShowAllVars] = useState(false);
  const [applying, setApplying] = useState("");
  const [viewer, setViewer] = useState("");
  const [preview, setPreview] = useState<{ name: string; text: string; tokens: number } | null>(null);
  const [pvErr, setPvErr] = useState("");
  const [pvBusy, setPvBusy] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const sel = useRef({ s: group.prompt.length, e: group.prompt.length });

  const members = useMemo(
    () => group.member_ids.map((id) => agents.find((a) => a.id === id)).filter((a): a is NonNullable<typeof a> => !!a),
    [group.member_ids, agents],
  );
  useEffect(() => {
    if (!viewer || !members.some((m) => m.id === viewer)) setViewer(members[0]?.id ?? "");
  }, [members, viewer]);

  // 后端里的群提示词变了(套用提示词、切换群)→ 同步到输入框
  useEffect(() => {
    setDraft(group.prompt);
  }, [group.prompt, gid]);

  useEffect(() => {
    if (!active || info) return;
    api.prompts().then((p) => { setInfo(p); setInfoErr(""); }).catch((e) => setInfoErr((e as Error).message));
  }, [active, info]);

  const dirty = draft !== group.prompt;

  const save = async (): Promise<boolean> => {
    if (draft === group.prompt) return true;
    setSaving(true);
    setErr("");
    try {
      await api.patchGroup(gid, { prompt: draft });
      await reloadGroups();
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1600);
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const remember = () => {
    const el = ta.current;
    if (el) sel.current = { s: el.selectionStart, e: el.selectionEnd };
  };
  const insertVar = (name: string) => {
    const tag = `{{${name}}}`;
    const { s, e } = sel.current;
    const a = Math.min(s, draft.length);
    const b = Math.min(e, draft.length);
    const next = draft.slice(0, a) + tag + draft.slice(b);
    setDraft(next);
    const pos = a + tag.length;
    sel.current = { s: pos, e: pos };
    requestAnimationFrame(() => {
      ta.current?.focus();
      ta.current?.setSelectionRange(pos, pos);
    });
  };

  const apply = async (id: string, title: string, mode: "replace" | "append") => {
    if (mode === "replace" && (dirty || group.prompt.trim()) && !(await confirm(`用「${title}」替换本群现有的提示词?`, { okText: "替换" }))) return;
    setApplying(id + mode);
    setErr("");
    try {
      const g = await api.applyPrompt(gid, id, mode);
      setDraft(g.prompt);
      await reloadGroups();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setApplying("");
    }
  };

  const openPreview = async () => {
    if (!viewer) return;
    setPvBusy(true);
    setPvErr("");
    try {
      if (!(await save())) return;
      const r = await api.systemPromptPreview(gid, viewer);
      setPreview({ name: members.find((m) => m.id === viewer)?.name ?? "", text: r.text, tokens: r.tokens });
    } catch (e) {
      setPvErr((e as Error).message);
    } finally {
      setPvBusy(false);
    }
  };

  const all = info?.variables ?? [];
  const descOf = new Map(all.map((v) => [v.name, v.desc]));
  const common = COMMON_VARS.filter((n) => !info || descOf.has(n));
  const rest = all.filter((v) => !COMMON_VARS.includes(v.name));
  const lib = useMemo(() => {
    const list = info?.prompts ?? [];
    return [...list.filter((p) => p.kind === "group"), ...list.filter((p) => p.kind !== "group")];
  }, [info]);

  const varChip = (name: string) => (
    <button
      key={name}
      type="button"
      className="gp-var"
      title={descOf.get(name) ?? ""}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => insertVar(name)}
    >
      {`{{${name}}}`}
    </button>
  );

  return (
    <div className="gp-scroll">
      <section className="gp-block">
        <div className="gp-sec">本群提示词</div>
        <label className="sr-only" htmlFor="gp-prompt">本群提示词</label>
        <textarea
          id="gp-prompt"
          ref={ta}
          className="gp-textarea"
          rows={7}
          value={draft}
          placeholder="例如:本群是产品发布小组,统一用「Team Agent」称呼产品,口语化、不超过 3 句。"
          onChange={(e) => { setDraft(e.target.value); remember(); }}
          onSelect={remember}
          onBlur={() => void save()}
        />
        <div className="gp-prompt-bar">
          <span className="gp-note">
            {saving ? "保存中…" : saved ? "已保存" : dirty ? "有未保存的修改(失焦时自动保存)" : "支持 {{变量}},发给成员前会代入真实内容。"}
          </span>
          <span className="grow" />
          <button className="btn small primary" disabled={!dirty || saving} onClick={() => void save()}>
            <Save size={12} /> 保存
          </button>
        </div>
        {err && <div className="err gp-err" role="alert">{err}</div>}
        <div className="gp-sub">点击插入变量</div>
        {infoErr && <div className="err gp-err">{infoErr}</div>}
        <div className="gp-vars">
          {common.map(varChip)}
          {showAllVars && rest.map((v) => varChip(v.name))}
        </div>
        {rest.length > 0 && (
          <button className="link-btn" onClick={() => setShowAllVars((v) => !v)}>
            {showAllVars ? "收起" : `更多变量(${rest.length})`}
          </button>
        )}
      </section>

      <section className="gp-block">
        <div className="gp-sec">从提示词库套用</div>
        {info === null && !infoErr && <div className="gp-none">加载中…</div>}
        {info !== null && lib.length === 0 && <div className="gp-none">提示词库里还没有内容。到左侧「提示词」页添加。</div>}
        <div className="gp-plist">
          {lib.map((p) => (
            <div key={p.id} className="gp-pitem">
              <div className="gp-pitem-head">
                <b title={p.title}>{p.title}</b>
                {p.kind === "group" && <span className="tag on">群聊</span>}
              </div>
              <div className="gp-pitem-body">{p.content}</div>
              <div className="gp-pitem-act">
                <button className="btn small" disabled={!!applying} onClick={() => void apply(p.id, p.title, "replace")} aria-label={`用「${p.title}」替换本群提示词`}>
                  替换
                </button>
                <button className="btn small" disabled={!!applying} onClick={() => void apply(p.id, p.title, "append")} aria-label={`把「${p.title}」追加到本群提示词`}>
                  追加
                </button>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="gp-block">
        <div className="gp-sec">成员实际收到的系统提示词</div>
        <div className="gp-note">全局提示词、岗位设定、本群提示词、技能等拼好之后的最终文本。</div>
        <div className="gp-viewer">
          <select value={viewer} onChange={(e) => setViewer(e.target.value)} aria-label="选择成员" disabled={members.length === 0}>
            {members.map((m) => (
              <option key={m.id} value={m.id}>{m.avatar} {m.name}</option>
            ))}
          </select>
          <button className="btn small" disabled={!viewer || pvBusy} onClick={() => void openPreview()}>
            <Eye size={12} /> {pvBusy ? "读取中…" : "查看"}
          </button>
        </div>
        {pvErr && <div className="err gp-err" role="alert">{pvErr}</div>}
      </section>

      {preview && (
        <Modal title={`「${preview.name}」实际收到的系统提示词`} wide onClose={() => setPreview(null)}>
          <div className="gp-pv-meta">约 {preview.tokens} tokens(估算)</div>
          <pre className="gp-pv" tabIndex={0}>{preview.text}</pre>
        </Modal>
      )}
    </div>
  );
}
