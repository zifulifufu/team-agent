import { useCallback, useEffect, useMemo, useState } from "react";
import { Pencil, Plus, RefreshCw, Trash2, Users, User } from "lucide-react";
import { api, type Skill } from "../api";
import { useData } from "../data";
import { currentLang, pickLang, useI18n } from "../i18n";
import { Modal, useConfirm } from "../ui";
import { GithubMark, SourceBadge, Spin } from "../components/ExtBits";
import { RepoDiscoverModal } from "../components/RepoDiscover";
import "../styles/ext.css";

// The scope wording per language. `scopeLabel` is a plain function, so it resolves through
// `pickLang(..., currentLang())` rather than the hook.
const SCOPE_LABEL: Record<string, { en: string; zh: string }> = {
  group: { en: "Group rule", zh: "群聊规则" },
  member: { en: "Member skill", zh: "成员技能" },
};
const scopeLabel = (scope: string): string =>
  pickLang(SCOPE_LABEL[scope]?.en ?? scope, SCOPE_LABEL[scope]?.zh, currentLang());

import type { SettingsTab } from "./SettingsModal";

export default function SkillsPage({ onTab }: { onTab?: (t: SettingsTab) => void } = {}) {
  const { t } = useI18n();
  const { agents, groups, reload, reloadUpdates } = useData();
  const confirm = useConfirm();
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [newer, setNewer] = useState<Set<string>>(new Set());   // Skills that have a newer version on GitHub
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
      if (r.busy) setCheckMsg({ ok: false, text: t("The previous check has not finished yet — try again shortly.") });
      else if (errs.length) setCheckMsg({ ok: false, text: t("The check did not fully succeed:") + errs.join(";") });
      else setCheckMsg({ ok: true, text: t("Check finished.") });
    } catch (e) {
      setCheckMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const update = async (s: Skill) => {
    if (!(await confirm(t('Update the skill "{name}" with the latest content from GitHub? This overwrites the local copy, and anything you changed locally is lost.', { name: s.name }), { okText: t("Update and overwrite") }))) return;
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
    const counts = [
      u?.agents.length ? t("{n} members", { n: u.agents.length }) : "",
      u?.groups.length ? t("{n} groups", { n: u.groups.length }) : "",
    ].filter(Boolean);
    const used = counts.length ? t("It is in use by {counts} right now.", { counts: counts.join(" · ") }) : "";
    if (!(await confirm(t('Delete the skill "{name}"? {used}It is also unticked for every member and group that has it.', { name: s.name, used }), { okText: t("Delete") }))) return;
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
        <h2 className="sp-title">{t("Skills")}</h2>
        <div className="sp-head-actions">
          {hasSource && (
            <button className="btn" onClick={check} disabled={checking}>
              {checking ? <><Spin /> {t("Checking…")}</> : <><RefreshCw size={14} /> {t("Check for updates")}</>}
            </button>
          )}
          <button className="btn" onClick={() => setDiscover(true)}><GithubMark size={14} /> {t("Find on GitHub")}</button>
          <button className="btn primary" onClick={() => setEditing("new")}><Plus size={15} /> {t("New skill")}</button>
        </div>
      </div>
      <p className="sp-desc">
        {t("A skill is a plain-text description written for a model to read (how to write a formal notice, how to run a review meeting…). It never runs code. A member skill is ticked for one member; a group rule is attached to the whole group and everyone follows it.")}{onTab && <> {t("Want one ready-made?")}<button className="link" onClick={() => onTab("gallery")}>{t("Open the template gallery")}</button>{t("and import it in one click.")}</>}
      </p>
      {checkMsg && <div className={checkMsg.ok ? "ok-text" : "err"} style={{ marginBottom: 10 }}>{checkMsg.text}</div>}
      {loadErr && <div className="ext-errbox"><div className="err">{t("Could not read the skills:")} {loadErr}</div><button className="btn small" onClick={() => void load()}>{t("Retry")}</button></div>}

      {!skills && !loadErr && <div className="empty"><Spin /> {t("Loading…")}</div>}
      {skills && (
        <div className="card flush">
          {skills.length === 0 && (
            <div className="empty">
              {t("No skills yet. Write one yourself with New skill in the top right, or find a ready-made one on GitHub.")}
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
                      <span className={"tag " + (s.scope === "group" ? "warn" : "on")}>{s.scope === "group" ? <Users size={11} /> : <User size={11} />} {scopeLabel(s.scope)}</span>
                      {s.version && <span className="tag">v{s.version}</span>}
                      {s.source && <SourceBadge repo={s.source.repo} path={s.source.path} />}
                      {hasNew && <span className="tag new">{t("A newer version is on GitHub")}</span>}
                    </div>
                    {s.description && <div className="ext-item-desc">{s.description}</div>}
                    <div className="ext-item-sub">
                      {u.agents.length === 0 && u.groups.length === 0
                        ? t("Not used yet")
                        : [u.agents.length ? t("Members: {names}", { names: u.agents.join(", ") }) : "", u.groups.length ? t("Groups: {names}", { names: u.groups.join(", ") }) : ""].filter(Boolean).join(" · ")}
                    </div>
                    {rowErr[s.name] && <div className="ext-errline">{rowErr[s.name]}</div>}
                  </div>
                  <div className="ext-item-actions">
                    {s.source && (
                      <button className={"btn small" + (hasNew ? " primary" : "")} disabled={updating === s.name} onClick={() => void update(s)} title={t("Overwrite the local skill with the latest content from GitHub")}>
                        {updating === s.name ? <><Spin size={12} /> {t("Updating")}</> : t("Update")}
                      </button>
                    )}
                    <button className="icon-btn" title={t("Edit")} aria-label={t("Edit the skill {name}", { name: s.name })} onClick={() => setEditing(s)}><Pencil size={15} /></button>
                    <button className="icon-btn" title={t("Delete")} aria-label={t("Delete the skill {name}", { name: s.name })} onClick={() => void remove(s)}><Trash2 size={15} /></button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <p className="muted small" style={{ marginTop: 12, lineHeight: 1.7 }}>
        {t("A member skill is ticked while editing a member on the Members page. A group rule (and a member skill too) is attached to a group under Extensions, in the panel on the right of a chat.")}
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
  const { t } = useI18n();
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
      title={skill ? t('Edit the skill "{name}"', { name: skill.name }) : t("New skill")}
      onClose={onClose}
      wide
      actions={
        <>
          {err && <span className="err ext-act-err" role="alert">{err}</span>}
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={!canSave} onClick={() => void save()}>{busy ? <><Spin /> {t("Saving…")}</> : t("Save")}</button>
        </>
      }
    >
      <label className="field">
        <span>{t("Name")}</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("e.g. review meeting rules")} maxLength={60} autoFocus />
        {skill && skill.source && <span className="ext-field-note">{t("This skill came from GitHub. Renaming it breaks the link to its source, so Update stops working.")}</span>}
        {skill && !skill.source && <span className="ext-field-note">{t("Renaming it also updates where it is ticked for members and groups.")}</span>}
      </label>
      <label className="field">
        <span>{t("One-line description (optional)")}</span>
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t("Say when it should be used")} />
      </label>
      <div className="field">
        <span>{t("Scope")}</span>
        <div className="ext-radio-row" role="radiogroup" aria-label={t("Skill scope")}>
          <label className={"check" + (scope === "member" ? " on" : "")}>
            <input type="radio" name="skill-scope" checked={scope === "member"} onChange={() => setScope("member")} />
            <span>{t("Member skill")}<small>{t("Ticked for one member; it only shapes that member's own answers, for example office writing conventions.")}</small></span>
          </label>
          <label className={"check" + (scope === "group" ? " on" : "")}>
            <input type="radio" name="skill-scope" checked={scope === "group"} onChange={() => setScope("group")} />
            <span>{t("Group rule")}<small>{t("Attached to the whole group and followed by everyone, for example review meeting rules or brainstorming rules.")}</small></span>
          </label>
        </div>
      </div>
      <label className="field">
        <span>{t("Body (the description written for the model; plain text / Markdown)")}</span>
        {loading ? <div className="empty"><Spin /> {t("Reading the content…")}</div> : (
          <textarea className="ext-mono-area" rows={11} value={body} onChange={(e) => setBody(e.target.value)} placeholder={t("Write the rules clearly, for example:\\n1. Open with the conclusion in one sentence;\\n2. Break the body into points…")} spellCheck={false} />
        )}
        <div className="ext-count">{t("{n} characters", { n: body.length })}</div>
      </label>
      {loadErr && <div className="err">{t("Could not read the body:")} {loadErr}</div>}
    </Modal>
  );
}
