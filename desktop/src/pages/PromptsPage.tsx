import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Braces, Copy, LoaderCircle, Pencil, Plus, Trash2, Users } from "lucide-react";
import { api, type PromptItem, type Skill } from "../api";
import { useData } from "../data";
import { Modal, Switch, useConfirm, useFlash, useOutside } from "../ui";
import "../styles/know.css";
import { tr, useI18n } from "../i18n";

type Vars = { name: string; desc: string }[];

// ------------------------------------------------------------------ helpers
/** Insert text at the textarea caret and leave the caret after it. */
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

/** Debounced (400ms) preview call: returns the text with variables filled in plus a token estimate for the raw text. */
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

/** Group + member picker (first group and its host by default) used to preview variable substitution. */
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

const tokenText = (n: number | null) => (n === null ? "…" : tr("about {n} tokens", { n }));
const excerpt = (s: string, n = 120) => { const f = s.replace(/\s+/g, " ").trim(); return f.length > n ? f.slice(0, n) + "…" : f; };

// --------------------------------------------------------------------- page
export default function PromptsPage() {
  const { t } = useI18n();
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

  // Global system prompt
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
    const extra = p.use_globally ? t("It is marked \"use globally\", so it will no longer be added to members' system prompts.") : "";
    if (!(await confirm(t("Delete the prompt \"{title}\"?", { title: p.title }) + extra + t("Content already applied to a group is not affected."), { okText: t("Delete") }))) return;
    setRowErr("");
    try {
      await api.delPrompt(p.id);
      await load();
    } catch (e) {
      setRowErr(t("Delete failed: {err}", { err: (e as Error).message }));
    }
  };

  return (
    <div className="kn-page">
      <div className="kn-inner">
        <div className="kn-head">
          <div className="kn-head-main">
            <h1>{t("Prompts")}</h1>
            <p className="kn-desc">{t("Manage the global system prompt every member receives, plus reusable prompts you can apply again and again.")}</p>
          </div>
        </div>
        {err && <div className="err kn-block">{err}</div>}
        {loading && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> {t("Loading…")}</div>}

        {!loading && !err && (
          <>
            <SystemPromptSection sys={sys} setSys={setSys} saved={saved} setSaved={setSaved} vars={vars} defaultText={defaultText} />

            <section className="kn-sec" aria-labelledby="kn-lib">
              <div className="kn-sec-head">
                <h2 id="kn-lib">{t("Prompt library")}</h2>
                <button className="btn primary" onClick={() => setEdit("new")}><Plus size={15} /> {t("Add prompt")}</button>
              </div>
              <p className="kn-desc">
                {t("Reusable prompts. Ones marked \"use globally\" are appended to every member's system prompt as additional requirements after their role prompt, and they use up context on every turn, so keep them few. The rest can be applied to a group's own prompt or copied at any time.")}
              </p>
              {globals > 0 && (
                <p className={"small kn-globals" + (globals >= 4 ? " warn" : " muted")}>
                  {t("Currently {n} marked as global", { n: globals })}{globals >= 4 ? t(", which is a lot — keep only the essential ones") : ""}.
                </p>
              )}
              {notice && <div className="ok-text kn-block">{notice}</div>}
              {rowErr && <div className="err kn-block">{rowErr}</div>}
              {items.length === 0 ? (
                <div className="kn-empty-box">
                  <p><b>{t("The prompt library is empty.")}</b>{t("Save the tone, output format and project background you keep repeating as prompts, then apply them in one click.")}</p>
                  <button className="btn small" onClick={() => setEdit("new")}><Plus size={14} /> {t("Add prompt")}</button>
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
              <div className="kn-sec-head"><h2 id="kn-skills">{t("Group-rule skills")}</h2></div>
              <p className="kn-desc">
                {t("Skills attached to a whole group apply to every member (review-meeting rules, brainstorming rules). Tick them under Extensions on the right of a group chat, and create or install them under Settings → Skills.")}
              </p>
              {skillErr && <div className="err kn-block">{skillErr}</div>}
              {skills && groupSkills.length === 0 && !skillErr && <div className="empty">{t("No group-rule skills yet.")}</div>}
              {groupSkills.length > 0 && (
                <div className="kn-skills">
                  {groupSkills.map((s) => (
                    <div key={s.name} className="kn-skill">
                      <div className="kn-skill-name">{s.name}</div>
                      <div className="kn-skill-desc">{s.description || t("(no description)")}</div>
                    </div>
                  ))}
                </div>
              )}
              <p className="kn-note"><Users size={14} /> {t("Roles such as host, reviewer, scribe or librarian can also be pulled into a group at any time from Members → Role presets on the right.")}</p>
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
          onDone={async (name) => { setApplying(null); await reloadGroups(); setNotice(t("Applied \"{title}\" to the prompt of group \"{group}\".", { title: applying.title, group: name })); }}
        />
      )}
    </div>
  );
}

// ------------------------------------------------ A. Global system prompt
function SystemPromptSection({ sys, setSys, saved, setSaved, vars, defaultText }: {
  sys: string; setSys: (v: string) => void; saved: string; setSaved: (v: string) => void; vars: Vars; defaultText: string;
}) {
  const { t } = useI18n();
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

  // Auto-grow the textarea (at least 10 rows)
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
    if (!(await confirm(t("Restore the default global system prompt? What you have written now, including unsaved changes, is overwritten."), { okText: t("Restore default") }))) return;
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
      <div className="kn-sec-head"><h2 id="kn-sys">{t("Global system prompt")}</h2></div>
      <p className="kn-desc">{t("Every member receives this text before its own role prompt, on every turn.")}</p>

      <textarea
        ref={ta}
        className="kn-mono"
        rows={10}
        value={sys}
        onChange={(e) => setSys(e.target.value)}
        aria-label={t("Global system prompt")}
        spellCheck={false}
      />
      <div className="kn-var-bar">
        <span className="small muted kn-var-label"><Braces size={13} /> {t("Variables")}</span>
        {vars.map((v) => (
          <button key={v.name} className="kn-var" title={v.desc} onClick={() => insertAtCursor(ta, sys, setSys, `{{${v.name}}}`)}>
            {`{{${v.name}}}`}
          </button>
        ))}
      </div>
      <div className="kn-sys-bar">
        <span className="small muted" title={t("An estimate; models tokenize differently")}>{tokenText(pv.raw)}</span>
        {dirty && <span className="tag warn">{t("Unsaved changes")}</span>}
        {ok && !dirty && <span className="ok-text">{t("Saved")}</span>}
        {err && <span className="err small">{err}</span>}
        <span className="grow" />
        <button className="btn" onClick={() => void reset()} disabled={busy || (sys === defaultText && saved === defaultText)}>{t("Restore default")}</button>
        <button className={"btn" + (showPv ? " on" : "")} aria-pressed={showPv} onClick={() => setShowPv((v) => !v)}>{t("Preview")}</button>
        <button className="btn primary" disabled={busy || !dirty} onClick={() => void save()}>{busy ? t("Saving…") : t("Save")}</button>
      </div>

      {showPv && (
        <div className="kn-preview" aria-label={t("Preview")}>
          <div className="kn-preview-head">
            <span className="small muted">{t("With variables filled in, using")}</span>
            {target.groups.length === 0 ? (
              <span className="small muted">{t("(no group chat yet, so variables cannot be filled in)")}</span>
            ) : (
              <>
                <select className="kn-select" value={target.groupId} onChange={(e) => target.setGroup(e.target.value)} aria-label={t("Group chat used for the preview")}>
                  {target.groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
                <select className="kn-select" value={target.agentId} onChange={(e) => target.setAgent(e.target.value)} aria-label={t("Member used for the preview")}>
                  {target.members.map((a) => <option key={a.id} value={a.id}>{a.avatar} {a.name}</option>)}
                </select>
                <span className="small muted">{t("as the example")}</span>
              </>
            )}
            {pv.busy && <LoaderCircle size={13} className="kn-spin" />}
          </div>
          {pv.err ? <div className="err small">{pv.err}</div> : <div className="kn-preview-text">{pv.text || <span className="muted">{t("(empty)")}</span>}</div>}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- card
function PromptCard({ p, onGlobal, onEdit, onDelete, onApply, canApply }: {
  p: PromptItem; onGlobal: (v: boolean) => void; onEdit: () => void; onDelete: () => void; onApply: () => void; canApply: boolean;
}) {
  const { t } = useI18n();
  const [copied, flash] = useFlash();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(p.content);
      flash();
    } catch {
      /* Silent when the clipboard is unavailable */
    }
  };
  return (
    <article className={"kn-prompt" + (p.use_globally ? " global" : "")}>
      <div className="kn-prompt-top">
        <span className="kn-prompt-title">{p.title}</span>
        <span className={"tag " + (p.kind === "group" ? "on" : "")}>{p.kind === "group" ? t("Group prompt") : t("General")}</span>
        <span className="grow" />
        <label className="kn-global">
          <span>{t("Use globally")}</span>
          <Switch checked={p.use_globally} onChange={onGlobal} label={t("Use \"{title}\" globally", { title: p.title })} />
        </label>
      </div>
      <p className="kn-prompt-text">{excerpt(p.content)}</p>
      <div className="kn-prompt-ops">
        <button className="btn small" onClick={onApply} disabled={!canApply} title={canApply ? t("Write into a group's own prompt") : t("No group chat yet")}><Users size={13} /> {t("Apply to a group")}</button>
        <button className="btn small ghost" onClick={() => void copy()}><Copy size={13} /> {copied ? t("Copied") : t("Copy")}</button>
        <span className="grow" />
        <button className="icon-btn tiny" aria-label={t("Edit \"{title}\"", { title: p.title })} title={t("Edit")} onClick={onEdit}><Pencil size={14} /></button>
        <button className="icon-btn tiny kn-del" aria-label={t("Delete \"{title}\"", { title: p.title })} title={t("Delete")} onClick={onDelete}><Trash2 size={14} /></button>
      </div>
    </article>
  );
}

// ------------------------------------------------ Add / edit dialog
function PromptDialog({ prompt, vars, onClose, onSaved }: { prompt: PromptItem | null; vars: Vars; onClose: () => void; onSaved: () => void | Promise<void> }) {
  const { t } = useI18n();
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
    if (!title.trim()) return setErr(t("Please enter a title"));
    if (!content.trim()) return setErr(t("Please enter the content"));
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
      title={prompt ? t("Edit prompt") : t("Add prompt")}
      wide
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={() => void save()}>{busy ? t("Saving…") : t("Save")}</button>
        </>
      }
    >
      <label className="field">
        <span>{t("Title")}</span>
        <input value={title} autoFocus onChange={(e) => { setTitle(e.target.value); setErr(""); }} placeholder={t("e.g. Lead with the conclusion")} />
      </label>

      <div className="field">
        <div className="kn-label-row">
          <label htmlFor="kn-pd-content">{t("Content")}</label>
          <span className="grow" />
          <div className="kn-menu-wrap" ref={menuRef}>
            <button className="btn small" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
              <Braces size={13} /> {"{x}"} {t("variables")}
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
        <textarea id="kn-pd-content" ref={ta} className="kn-mono" rows={9} value={content} onChange={(e) => { setContent(e.target.value); setErr(""); }} placeholder={t("Write the prompt members should receive; {{variables}} are allowed")} spellCheck={false} />
        <div className="kn-pd-foot">
          <label className="check-inline"><Switch checked={showPv} onChange={setShowPv} label={t("Preview")} /> {t("Preview")}</label>
          <span className="grow" />
          <span className="small muted">{tokenText(pv.raw)}</span>
        </div>
      </div>

      {showPv && (
        <div className="kn-preview inner">
          <div className="kn-preview-head">
            <span className="small muted">
              {gname
                ? t("With variables filled in (using {member} in {group})", { member: aname ?? t("Member"), group: gname })
                : t("No group chat yet, so variables cannot be filled in")}
            </span>
            {pv.busy && <LoaderCircle size={13} className="kn-spin" />}
          </div>
          {pv.err ? <div className="err small">{pv.err}</div> : <div className="kn-preview-text">{pv.text || <span className="muted">{t("(empty)")}</span>}</div>}
        </div>
      )}

      <div className="field">
        <span>{t("Type")}</span>
        <div className="kn-kinds" role="radiogroup" aria-label={t("Prompt type")}>
          {([["general", t("General")], ["group", t("Group prompt")]] as const).map(([k, label]) => (
            <button key={k} role="radio" aria-checked={kind === k} className={"kn-kind-pick" + (kind === k ? " on" : "")} onClick={() => setKind(k)}>{label}</button>
          ))}
        </div>
        <span className="muted small">{t("A group prompt sets how the whole group works together (project background, discussion rules); a general prompt carries tone and format requirements. Both can be applied to a group.")}</span>
      </div>
      <div className="setting-row">
        <div>
          <div className="sr-title">{t("Use globally")}</div>
          <div className="sr-desc">{t("When on, it is added to every member's system prompt and uses up context on every turn.")}</div>
        </div>
        <Switch checked={glob} onChange={setGlob} label={t("Use globally")} />
      </div>
    </Modal>
  );
}

// ------------------------------------------------ Apply to a group chat
function ApplyModal({ prompt, onClose, onDone }: { prompt: PromptItem; onClose: () => void; onDone: (groupName: string) => void | Promise<void> }) {
  const { t } = useI18n();
  const { groups } = useData();
  const [gid, setGid] = useState(groups[0]?.id ?? "");
  const [mode, setMode] = useState<"replace" | "append">("replace");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const g = groups.find((x) => x.id === gid);
  const has = !!g?.prompt.trim();

  const go = async () => {
    if (!g) return setErr(t("Please pick a group chat"));
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
      title={t("Apply \"{title}\"", { title: prompt.title })}
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || !g} onClick={() => void go()}>{busy ? t("Applying…") : t("Apply")}</button>
        </>
      }
    >
      <label className="field">
        <span>{t("Which group's own prompt to write into")}</span>
        <select value={gid} onChange={(e) => setGid(e.target.value)}>
          {groups.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
      </label>
      <div className="field">
        <span>{t("Mode")}</span>
        <div className="seg" role="radiogroup" aria-label={t("How to apply")}>
          <button role="radio" aria-checked={mode === "replace"} className={mode === "replace" ? "on" : ""} onClick={() => setMode("replace")}>{t("Replace")}</button>
          <button role="radio" aria-checked={mode === "append"} className={mode === "append" ? "on" : ""} onClick={() => setMode("append")}>{t("Append")}</button>
        </div>
        <span className={"small " + (has && mode === "replace" ? "kn-warn" : "muted")}>
          {!has
            ? t("This group has no prompt of its own yet.")
            : mode === "replace"
              ? t("This group already has a {n}-character prompt; replacing overwrites it.", { n: g!.prompt.length })
              : t("It is appended after the existing group prompt, with a blank line between.")}
        </span>
      </div>
    </Modal>
  );
}
