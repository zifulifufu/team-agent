import { Fragment, useState } from "react";
import { Check, ChevronDown, ChevronRight, FolderOpen, Layers, Plus, Trash2, X } from "lucide-react";
import { api, type Collection, type KnowledgeBase, type LibraryVocabulary } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { useConfirm } from "../ui";

/**
 * Knowledge bases and the collections that bundle them.
 *
 * A knowledge base is a named bag of documents; a collection is a flat, reusable list of them,
 * so a group can take on a whole set at once instead of ticking six boxes. Both are managed
 * here; *which* ones a group uses is chosen in that group's panel.
 *
 * The two labels are the answer to "I cannot find anything in here": **origin** is where a base's
 * material came from, which is a fact read off its documents, and **purpose** is the heading its
 * user files it under, which is a decision. The list is grouped by the second and shows the first,
 * with the breakdown that makes it true — a base called `mixed` says so *and* says "imported 6029 ·
 * fetched 114", because the word alone is not an answer to anything.
 */

/** `t()` as the page hands it over. */
type TFn = (key: string, vars?: Record<string, string | number>) => string;

/**
 * The word for one origin token.
 *
 * A `switch` and not a lookup table, deliberately: a table would be keyed by a *variable*, so every
 * one of these strings would reach the translator as an expression rather than a literal, and
 * `scripts/check-i18n.py` would never see them — an untranslated word would fall back to English in
 * silence, which is the failure this project spends the most effort avoiding. Written out, each word
 * is a `t("…")` the checker can read. The `default` returns the token itself: a value the backend
 * gained and this file has not learned shows up as its raw name rather than disappearing.
 */
export function originWord(o: string, t: TFn): string {
  switch (o) {
    case "": return t("Not known yet");
    case "import": return t("Imported from a folder");
    case "capture": return t("Fetched by this app");
    case "upload": return t("Uploaded");
    case "link": return t("Web page");
    case "attachment": return t("Group attachment");
    case "workspace": return t("Project workspace");
    case "written": return t("Typed in here");
    default: return o;
  }
}

/** The word for what a piece of fetched material is *for* (`library.CATEGORIES`). Same reasoning as
 *  `originWord`, same `default`; `""` is the honest "its own words do not say", not "other". */
export function categoryWord(c: string, t: TFn): string {
  switch (c) {
    case "": return t("Not classified");
    case "camera": return t("Camera work");
    case "storyboard": return t("Storyboard");
    case "performance": return t("Performance & expressions");
    case "director": return t("Director style");
    case "commercial": return t("Ads & marketing");
    case "trailer": return t("Trailer & opening");
    case "effect": return t("Effects & transitions");
    case "character": return t("Characters & avatars");
    case "story": return t("Story & plot");
    case "edit": return t("Editing & sound");
    case "design": return t("Graphic design");
    case "visual": return t("Look & aesthetics");
    default: return c;
  }
}

/** The word for the heading a base is filed under. Same reasoning as `originWord`, same `default`:
 *  a purpose is free text, so an unrecognised one is almost always the user's own word and is
 *  shown exactly as they typed it. */
export function purposeWord(p: string, t: TFn): string {
  switch (p) {
    case "": return t("Not filed yet");
    case "project": return t("Project material");
    case "reference": return t("Reference");
    case "method": return t("Methods & craft");
    case "data": return t("Data & tables");
    case "writing": return t("Writing material");
    default: return p;
  }
}

interface KbGroup {
  key: string;                       // the purpose, "" for "not filed yet"
  rows: KnowledgeBase[];
  /** Every base in this group belongs to a project: the group opens collapsed, because a project's
   *  own material is the part of a library that grows on its own (nothing files itself any more,
   *  but a group's base is still created when somebody uploads into it). */
  allOwned: boolean;
}

function groupKbs(kbs: KnowledgeBase[], purposes: string[]): KbGroup[] {
  const by = new Map<string, KnowledgeBase[]>();
  for (const kb of kbs) {
    const key = kb.purpose ?? "";
    by.set(key, [...(by.get(key) ?? []), kb]);
  }
  // Suggested headings first and in their own order, then anything the user typed, then the bases
  // nobody has filed — that last one stays last but stays open, since it is the group that needs
  // attention rather than the one that needs hiding.
  const typed = [...by.keys()].filter((k) => k && !purposes.includes(k)).sort();
  const order = [...purposes.filter((p) => by.has(p)), ...typed, ...(by.has("") ? [""] : [])];
  return order.map((key) => {
    const rows = by.get(key) ?? [];
    return { key, rows, allOwned: rows.every((k) => k.group_id) };
  });
}

/** Rows of knowledge bases, with an inline "new" form. `owner` tells whether a base belongs to a
 *  workspace or is shared with everyone. */
export function KbSection({ kbs, onChanged, groupId, active, onOpen, vocab }: {
  kbs: KnowledgeBase[];
  onChanged: () => void;
  /** Set when the page is scoped to a group: a new base is created for that workspace */
  groupId?: string;
  active: string | null;
  onOpen: (id: string | null) => void;
  vocab: LibraryVocabulary | null;
}) {
  const { t } = useI18n();
  const { groups } = useData();
  const confirm = useConfirm();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [filing, setFiling] = useState<string | null>(null);     // the base whose label is being edited
  const [label, setLabel] = useState("");
  const [sourcing, setSourcing] = useState<string | null>(null);  // …and whose origin is
  const [srcDraft, setSrcDraft] = useState("");
  const [shut, setShut] = useState<Record<string, boolean>>({});

  const purposes = vocab?.purposes ?? [];
  const origins = vocab?.origins ?? [];
  const originWordFor = (o: string) => originWord(o, t);
  const purposeWordFor = (p: string) => purposeWord(p, t);

  /** "imported 6029 · fetched 114" — what the source word is summarising. */
  const breakdown = (kb: KnowledgeBase) =>
    Object.entries(kb.origins ?? {})
      .sort((a, b) => b[1] - a[1])
      .map(([o, n]) => `${originWordFor(o)} ${n}`)
      .join(" · ");

  /** The word in force: the user's if they set one, otherwise what the documents say. An empty base
   *  says so rather than claiming its origin is unknown — nothing came in yet, which is a different
   *  statement from "we cannot tell". */
  const sourceWord = (kb: KnowledgeBase) =>
    kb.source === vocab?.mixed ? t("Mixed")
      : kb.source ? originWordFor(kb.source) : t("Nothing in it yet");

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

  /** Write one of the two labels. An empty box is a real answer — "not filed yet", or "work the
   *  origin out from the documents" — so it is saved rather than ignored. */
  const file = async (kb: KnowledgeBase, patch: { purpose?: string; source?: string }) => {
    setFiling(null);
    setSourcing(null);
    await api.patchKb(kb.id, patch);
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

  const on = (g: KbGroup) => !(shut[g.key] ?? g.allOwned);

  const one = (kb: KnowledgeBase) => (
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
        <div className="kn-doc-sub">
          {kb.description && <span className="kn-doc-file">{kb.description}</span>}
          {/* The heading, editable in place: this is the one label the user owns, so it is a control
              rather than a badge. `datalist` gives the suggestions while still allowing a word of
              their own — see `library.PURPOSES`, which is deliberately not a closed vocabulary. */}
          {filing === kb.id ? (
            <input className="kn-label-in" autoFocus value={label} list="kn-purposes" placeholder={t("Heading")}
              aria-label={t("Heading")} onChange={(e) => setLabel(e.target.value)}
              onBlur={() => void file(kb, { purpose: label.trim() })}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                if (e.key === "Escape") setFiling(null);
              }} />
          ) : (
            <button className="tag kn-tag-btn" title={t("Change the heading this base is filed under")}
              onClick={() => { setLabel(kb.purpose ?? ""); setFiling(kb.id); }}>
              {purposeWordFor(kb.purpose ?? "")}
            </button>
          )}
          {/* Where the material came from, with the evidence behind it. Derived from the documents, so
              it is right by construction; opening the editor is how a user disagrees with it, and
              "automatic" is how they change their mind back. */}
          {sourcing === kb.id ? (
            <select className="kn-label-in" autoFocus value={srcDraft} aria-label={t("Where the material came from")}
              onChange={(e) => { setSrcDraft(e.target.value); void file(kb, { source: e.target.value }); }}
              onBlur={() => setSourcing(null)}>
              <option value="">{t("Automatic — read from the documents")}</option>
              {origins.map((o) => <option key={o} value={o}>{originWordFor(o)}</option>)}
              <option value={vocab?.mixed ?? "mixed"}>{t("Mixed")}</option>
            </select>
          ) : (
            <button className="tag kn-tag-btn" title={t("Where the material came from — {parts}", { parts: breakdown(kb) || t("no documents yet") })}
              onClick={() => { setSrcDraft(kb.source_override ?? ""); setSourcing(kb.id); }}>
              {sourceWord(kb)}
            </button>
          )}
          {breakdown(kb) && <span className="kn-doc-file">{breakdown(kb)}</span>}
        </div>
      </div>
      <span className="kn-col-owner"><span className={"tag" + (kb.group_id ? "" : " shared")}>{owner(kb)}</span></span>
      <span className="kn-col-size kn-num">{kb.docs}</span>
      <span className="kn-doc-ops">
        {active === kb.id && <Check size={14} />}
        <button className="icon-btn tiny kn-del" aria-label={t('Delete "{name}"', { name: kb.name })} title={t("Delete")} onClick={() => void remove(kb)}><Trash2 size={14} /></button>
      </span>
    </div>
  );

  return (
    <div className="kn-docs" role="table" aria-label={t("Knowledge bases")}>
      <datalist id="kn-purposes">{purposes.map((p) => <option key={p} value={p}>{purposeWordFor(p)}</option>)}</datalist>
      <div className="kn-doc-row kn-doc-th" role="row">
        <span />
        <span>{t("Knowledge base")}</span>
        <span className="kn-col-owner">{t("Belongs to")}</span>
        <span className="kn-col-size">{t("Documents")}</span>
        <span />
      </div>
      {kbs.length === 0 && <div className="kn-doc-row" role="row"><span /><span className="muted small">{t("No knowledge base yet.")}</span></div>}
      {groupKbs(kbs, purposes).map((g) => (
        <Fragment key={g.key || "unfiled"}>
          <div className="kn-doc-row kn-grp" role="row">
            <button className="kn-grp-toggle" aria-expanded={on(g)} aria-label={t("Show or hide this heading")}
              onClick={() => setShut((s) => ({ ...s, [g.key]: on(g) }))}>
              {on(g) ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            <span className="kn-grp-name">{purposeWordFor(g.key)}</span>
            <span className="kn-grp-note muted small">
              {t("{n} knowledge bases", { n: g.rows.length })}
              {!g.key && " · " + t("give one a heading and it stops being a loose end")}
              {g.key !== "" && g.allOwned && " · " + t("a project's own material, kept out of the way")}
            </span>
          </div>
          {on(g) && g.rows.map(one)}
        </Fragment>
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
