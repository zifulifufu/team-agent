import { useState } from "react";
import { Check, FolderOpen, Layers, Plus, Trash2, X } from "lucide-react";
import { api, type Collection, type KnowledgeBase } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { useConfirm } from "../ui";

/**
 * Knowledge bases and the collections that bundle them.
 *
 * A knowledge base is a named bag of documents; a collection is a flat, reusable list of them,
 * so a group can take on a whole set at once instead of ticking six boxes. Both are managed
 * here; *which* ones a group uses is chosen in that group's panel.
 */

/** Rows of knowledge bases, with an inline "new" form. `owner` tells whether a base belongs to a
 *  workspace or is shared with everyone. */
export function KbSection({ kbs, onChanged, groupId, active, onOpen }: {
  kbs: KnowledgeBase[];
  onChanged: () => void;
  /** Set when the page is scoped to a group: a new base is created for that workspace */
  groupId?: string;
  active: string | null;
  onOpen: (id: string | null) => void;
}) {
  const { t } = useI18n();
  const { groups } = useData();
  const confirm = useConfirm();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [text, setText] = useState("");

  const owner = (kb: KnowledgeBase) =>
    kb.group_id ? (groups.find((g) => g.id === kb.group_id)?.name ?? t("Unknown group")) : t("Shared");

  const add = async () => {
    if (!name.trim()) return;
    try {
      const kb = await api.addKb({ name: name.trim(), group_id: groupId ?? "" });
      setName("");
      setAdding(false);
      setErr("");
      onChanged();
      onOpen(kb.id);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const rename = async (kb: KnowledgeBase) => {
    const next = text.trim();
    setRenaming(null);
    if (!next || next === kb.name) return;
    await api.patchKb(kb.id, { name: next });
    onChanged();
  };

  const remove = async (kb: KnowledgeBase) => {
    const ok = await confirm(
      t('Delete the knowledge base "{name}" and all {n} documents in it? Groups that use it will simply stop seeing it.', { name: kb.name, n: kb.docs }),
      { okText: t("Delete") });
    if (!ok) return;
    await api.delKb(kb.id);
    if (active === kb.id) onOpen(null);
    onChanged();
  };

  return (
    <div className="kn-docs" role="table" aria-label={t("Knowledge bases")}>
      <div className="kn-doc-row kn-doc-th" role="row">
        <span />
        <span>{t("Knowledge base")}</span>
        <span className="kn-col-owner">{t("Belongs to")}</span>
        <span className="kn-col-size">{t("Documents")}</span>
        <span />
      </div>
      {kbs.map((kb) => (
        <div key={kb.id} className={"kn-doc-row" + (active === kb.id ? " on" : "")} role="row">
          <span className="kn-doc-ico"><Layers size={16} strokeWidth={1.7} /></span>
          <div className="kn-doc-main">
            {renaming === kb.id ? (
              <input className="kn-rename" autoFocus value={text} aria-label={t("Knowledge base name")}
                onChange={(e) => setText(e.target.value)} onBlur={() => void rename(kb)}
                onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setRenaming(null); }} />
            ) : (
              <button className="kn-doc-title" title={t("Show only this knowledge base")}
                onClick={() => onOpen(active === kb.id ? null : kb.id)}
                onDoubleClick={() => { setText(kb.name); setRenaming(kb.id); }}>
                {kb.name}
              </button>
            )}
            {kb.description && <div className="kn-doc-sub"><span className="kn-doc-file">{kb.description}</span></div>}
          </div>
          <span className="kn-col-owner"><span className={"tag" + (kb.group_id ? "" : " shared")}>{owner(kb)}</span></span>
          <span className="kn-col-size kn-num">{kb.docs}</span>
          <span className="kn-doc-ops">
            {active === kb.id && <Check size={14} />}
            <button className="icon-btn tiny kn-del" aria-label={t('Delete "{name}"', { name: kb.name })} title={t("Delete")} onClick={() => void remove(kb)}><Trash2 size={14} /></button>
          </span>
        </div>
      ))}
      <div className="kn-add-row">
        {adding ? (
          <>
            <input className="kn-rename" autoFocus value={name} placeholder={t("Name of the new knowledge base")}
              aria-label={t("Name of the new knowledge base")} onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void add(); if (e.key === "Escape") { setAdding(false); setName(""); } }} />
            <button className="btn small primary" disabled={!name.trim()} onClick={() => void add()}>{t("Create")}</button>
            <button className="btn small" onClick={() => { setAdding(false); setName(""); }}><X size={13} /></button>
          </>
        ) : (
          <button className="link-btn" onClick={() => setAdding(true)}>
            <Plus size={13} /> {groupId ? t("New knowledge base in this workspace") : t("New shared knowledge base")}
          </button>
        )}
        {err && <span className="err small">{err}</span>}
      </div>
    </div>
  );
}

/** Rows of collections. Membership is edited by ticking knowledge bases, which is the whole point
 *  of a collection: attach one thing to a group instead of six. */
export function CollectionSection({ cols, kbs, onChanged }: {
  cols: Collection[];
  kbs: KnowledgeBase[];
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const { groups } = useData();
  const confirm = useConfirm();
  const [editing, setEditing] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");

  const owner = (gid: string) => (gid ? (groups.find((g) => g.id === gid)?.name ?? t("Unknown group")) : t("Shared"));

  const startEdit = (col: Collection | null) => {
    setEditing(col?.id ?? "new");
    setPicked(col?.kb_ids ?? []);
    setName(col?.name ?? "");
  };

  const save = async () => {
    if (!name.trim()) return;
    try {
      if (editing === "new") await api.addCollection({ name: name.trim(), kb_ids: picked });
      else if (editing) await api.patchCollection(editing, { name: name.trim(), kb_ids: picked });
      setEditing(null);
      setAdding(false);
      setErr("");
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const remove = async (col: Collection) => {
    if (!(await confirm(t('Delete the collection "{name}"? The knowledge bases themselves are kept.', { name: col.name }), { okText: t("Delete") }))) return;
    await api.delCollection(col.id);
    onChanged();
  };

  return (
    <>
      {cols.map((col) => (
        <div key={col.id} className="kn-col-item">
          <div className="kn-col-head">
            <b>{col.name}</b>
            <span className="muted small">{t("{n} knowledge bases", { n: col.kbs.length })}</span>
            <span className="grow" />
            <button className="btn small" onClick={() => startEdit(col)}>{t("Edit")}</button>
            <button className="icon-btn tiny kn-del" aria-label={t('Delete "{name}"', { name: col.name })} title={t("Delete")} onClick={() => void remove(col)}><Trash2 size={14} /></button>
          </div>
          <div className="kn-col-members">
            {col.kbs.length === 0 && <span className="muted small">{t("Empty — edit it to pick knowledge bases.")}</span>}
            {col.kbs.map((k) => (
              <span key={k.id} className={"tag" + (k.group_id ? "" : " shared")} title={owner(k.group_id)}>
                {k.name} · {k.docs}
              </span>
            ))}
          </div>
        </div>
      ))}

      {(editing || adding) && (
        <div className="kn-col-edit">
          <input className="kn-rename" autoFocus value={name} placeholder={t("Name of the collection")}
            aria-label={t("Name of the collection")} onChange={(e) => setName(e.target.value)} />
          <div className="kn-col-pick">
            {kbs.map((kb) => (
              <label key={kb.id} className={"check" + (picked.includes(kb.id) ? " on" : "")}>
                <input type="checkbox" checked={picked.includes(kb.id)}
                  onChange={(e) => setPicked((cur) => (e.target.checked ? [...cur, kb.id] : cur.filter((x) => x !== kb.id)))} />
                {kb.name} <small className="muted">{owner(kb.group_id)}</small>
              </label>
            ))}
          </div>
          <div className="kn-add-row">
            <button className="btn small primary" disabled={!name.trim()} onClick={() => void save()}>{t("Save")}</button>
            <button className="btn small" onClick={() => { setEditing(null); setAdding(false); }}>{t("Cancel")}</button>
            {err && <span className="err small">{err}</span>}
          </div>
        </div>
      )}
      {!editing && !adding && (
        <div className="kn-add-row">
          <button className="link-btn" onClick={() => startEdit(null)}><FolderOpen size={13} /> {t("New collection")}</button>
        </div>
      )}
    </>
  );
}
