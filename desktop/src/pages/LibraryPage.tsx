import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { useData } from "../data";
import {
  ChevronLeft, ChevronRight, CircleAlert, CircleCheck, FileCode, FileJson, FileSpreadsheet, FileText, FileType,
  FolderInput, Layers, Link2, LoaderCircle, Pencil, Search, StickyNote, Trash2, Upload, X,
} from "lucide-react";
import { api, relTime, type Collection, type KnowledgeBase, type LibraryDoc, type LibraryHit } from "../api";
import { CollectionSection, KbSection } from "../components/KnowledgeBases";
import { Modal, Switch, useConfirm } from "../ui";
import "../styles/know.css";
import { tr, useI18n } from "../i18n";

const ACCEPT = ".txt,.md,.markdown,.csv,.json,.html,.htm,.pdf,.docx";
const FORMATS = ["txt", "md", "csv", "json", "html", tr("pdf (needs a text layer)"), "docx"];

// ------------------------------------------------------------------ helpers
function fmtChars(n: number): string {
  if (n >= 10000) return tr("about {v}0k chars", { v: (n / 10000).toFixed(1) });
  if (n >= 1000) return tr("about {v}k chars", { v: (n / 1000).toFixed(1) });
  return tr("{n} chars", { n });
}

/** Kept in step with textindex.tokenize on the backend: words for Latin text and
 * digits, adjacent character pairs for Chinese, used to highlight search hits. */
function queryTokens(q: string): string[] {
  const out = new Set<string>();
  for (const w of q.toLowerCase().match(/[a-z0-9_]+|[一-鿿]+/g) ?? []) {
    if (/^[一-鿿]+$/.test(w)) {
      if (w.length === 1) out.add(w);
      else for (let i = 0; i < w.length - 1; i++) out.add(w.slice(i, i + 2));
    } else if (w.length > 1) out.add(w);
  }
  return [...out].slice(0, 40);
}

/** Text highlighting as plain React nodes (no dangerouslySetInnerHTML). */
function Highlight({ text, tokens }: { text: string; tokens: string[] }) {
  const parts = useMemo(() => {
    const low = text.toLowerCase();
    if (!tokens.length || low.length !== text.length) return [{ t: text, m: false }];
    const mask = new Uint8Array(text.length);
    for (const tk of tokens) {
      let i = low.indexOf(tk);
      while (i >= 0) {
        mask.fill(1, i, i + tk.length);
        i = low.indexOf(tk, i + 1);
      }
    }
    const out: { t: string; m: boolean }[] = [];
    let s = 0;
    for (let i = 1; i <= text.length; i++) {
      if (i === text.length || mask[i] !== mask[s]) {
        out.push({ t: text.slice(s, i), m: mask[s] === 1 });
        s = i;
      }
    }
    return out;
  }, [text, tokens]);
  return (
    <>
      {parts.map((p, i) => (p.m ? <mark key={i} className="kn-mark">{p.t}</mark> : <span key={i}>{p.t}</span>))}
    </>
  );
}

/** Trim an over-long snippet to a short window centred on the first hit. */
function snippet(text: string, tokens: string[], max = 240): string {
  const flat = text.replace(/\n{2,}/g, "\n");
  if (flat.length <= max) return flat;
  const low = flat.toLowerCase();
  let first = -1;
  if (low.length === flat.length) for (const tk of tokens) {
    const i = low.indexOf(tk);
    if (i >= 0 && (first < 0 || i < first)) first = i;
  }
  const from = Math.max(0, first < 0 ? 0 : Math.min(first - 50, flat.length - max));
  return (from > 0 ? "…" : "") + flat.slice(from, from + max) + (from + max < flat.length ? "…" : "");
}

function KindIcon({ kind }: { kind: string }) {
  const p = { size: 17, strokeWidth: 1.7 };
  switch (kind) {
    case "note": return <StickyNote {...p} />;
    case "pdf": case "docx": return <FileType {...p} />;
    case "csv": case "tsv": return <FileSpreadsheet {...p} />;
    case "json": return <FileJson {...p} />;
    case "html": case "htm": return <FileCode {...p} />;
    default: return <FileText {...p} />;
  }
}
const kindLabel = (k: string) => (k === "note" ? tr("Note") : k.toUpperCase());

interface UpItem {
  key: number;
  name: string;
  state: "wait" | "up" | "ok" | "err";
  msg?: string;
}
let upSeq = 1;

// --------------------------------------------------------------------- page
/** Where new material goes: the knowledge base on screen, else everything the group can reach. */
export interface LibScope {
  kb?: string;
  group?: string;
}

/**
 * The document library.
 *
 * With `groupId` it is that group chat's own library: the list, the uploads and the search are
 * all scoped to it, and everything the page shows is what its members can actually reach. No
 * group -> the whole library, which is the overview: every document, with the group each one
 * belongs to (or "Shared").
 */
export default function LibraryPage({ groupId, onBack }: { groupId?: string; onBack?: () => void } = {}) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const { reloadGroups, groups } = useData();
  const group = groups.find((g) => g.id === groupId) ?? null;
  const [docs, setDocs] = useState<LibraryDoc[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [cols, setCols] = useState<Collection[]>([]);
  // Which knowledge base the document list is narrowed to (null = everything in scope)
  const [kbFilter, setKbFilter] = useState<string | null>(null);
  const [shelfOpen, setShelfOpen] = useState(true);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadErr, setLoadErr] = useState("");
  const [rowErr, setRowErr] = useState("");

  const [ups, setUps] = useState<UpItem[]>([]);
  const [drag, setDrag] = useState(false);
  const dragDepth = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<LibraryHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchErr, setSearchErr] = useState("");

  const [reading, setReading] = useState<{ id: string; title: string; q: string } | null>(null);
  const [noteOpen, setNoteOpen] = useState(false);
  const [source, setSource] = useState<"url" | "dir" | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const openTimer = useRef<number>();

  const load = useCallback(async () => {
    try {
      const r = await api.library(scope);
      setDocs(r.docs);
      setTotal(r.total_chars);
      setLoadErr("");
    } catch (e) {
      setLoadErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [groupId, kbFilter]);

  // Knowledge bases and collections are managed here too; `load` refreshes the documents, this
  // refreshes the shelves. Together they keep the counts and the list in step.
  const loadShelf = useCallback(() => {
    api.kbs(groupId).then(setKbs).catch((e) => setLoadErr((e as Error).message));
    api.collections().then(setCols).catch(() => undefined);
  }, [groupId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(loadShelf, [loadShelf]);
  const reloadAll = useCallback(() => { void load(); loadShelf(); }, [load, loadShelf]);
  useEffect(() => () => window.clearTimeout(openTimer.current), []);

  // ---- Uploads: one at a time, independent of each other
  const patchUp = (key: number, p: Partial<UpItem>) => setUps((l) => l.map((u) => (u.key === key ? { ...u, ...p } : u)));
  const upload = async (files: File[]) => {
    if (!files.length) return;
    const items: UpItem[] = files.map((f) => ({ key: upSeq++, name: f.name, state: "wait" }));
    setUps((l) => [...l, ...items]);
    for (let i = 0; i < files.length; i++) {
      const it = items[i];
      patchUp(it.key, { state: "up" });
      try {
        await api.uploadDoc(files[i], scope);
        patchUp(it.key, { state: "ok" });
        window.setTimeout(() => setUps((l) => l.filter((u) => u.key !== it.key)), 6000);
        await load();
      } catch (e) {
        patchUp(it.key, { state: "err", msg: (e as Error).message });
      }
    }
  };

  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
  const onDragEnter = (e: DragEvent) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth.current++; setDrag(true); };
  const onDragOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
  const onDragLeave = (e: DragEvent) => { if (!hasFiles(e)) return; dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDrag(false); };
  const onDrop = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDrag(false);
    void upload(Array.from(e.dataTransfer.files));
  };

  // ---- Search (300ms debounce)
  const tokens = useMemo(() => queryTokens(q), [q]);
  useEffect(() => {
    const key = q.trim();
    if (!key) {
      setHits(null);
      setSearching(false);
      setSearchErr("");
      return;
    }
    setSearching(true);
    let alive = true;
    const t = window.setTimeout(async () => {
      try {
        const r = await api.searchLibrary(key, 8, scope);
        if (!alive) return;
        setHits(r);
        setSearchErr("");
      } catch (e) {
        if (alive) setSearchErr((e as Error).message);
      } finally {
        if (alive) setSearching(false);
      }
    }, 300);
    return () => { alive = false; window.clearTimeout(t); };
  }, [q, docs]);

  // ---- Row actions
  const toggle = async (d: LibraryDoc, enabled: boolean) => {
    setRowErr("");
    setDocs((l) => l.map((x) => (x.id === d.id ? { ...x, enabled } : x)));
    try {
      await api.patchDoc(d.id, { enabled });
    } catch (e) {
      setRowErr(`「${d.title}」:${(e as Error).message}`);
      await load();
    }
  };
  const commitRename = async (d: LibraryDoc) => {
    const title = renameText.trim();
    setRenaming(null);
    if (!title || title === d.title) return;
    setRowErr("");
    try {
      await api.patchDoc(d.id, { title });
      await load();
    } catch (e) {
      setRowErr(t("Rename failed: {err}", { err: (e as Error).message }));
    }
  };
  const startRename = (d: LibraryDoc) => {
    window.clearTimeout(openTimer.current);
    setRenameText(d.title);
    setRenaming(d.id);
  };
  const remove = async (d: LibraryDoc) => {
    if (!(await confirm(t("Delete the document \"{title}\"? It is removed from the library and from every group's selected-documents list. This cannot be undone.", { title: d.title }), { okText: t("Delete") }))) return;
    setRowErr("");
    try {
      await api.delDoc(d.id);
      await load();
      await reloadGroups();   // the backend already dropped it from every group's selected
                              // documents; sync here so a later save cannot write it back
    } catch (e) {
      setRowErr(t("Delete failed: {err}", { err: (e as Error).message }));
    }
  };
  const open = (d: { id: string; title: string }, query = "") => setReading({ id: d.id, title: d.title, q: query });

  const scope: LibScope = { group: groupId, kb: kbFilter ?? undefined };
  const empty = !loading && !loadErr && docs.length === 0 && kbs.length === 0;
  const kbById = new Map(kbs.map((k) => [k.id, k]));
  const dropZone = (
    <div className={"kn-drop" + (drag ? " over" : "") + (empty ? " big" : "")}>
      <Upload size={empty ? 26 : 18} strokeWidth={1.6} />
      <div className="kn-drop-text">
        <b>{drag ? t("Release to start uploading") : empty ? t("Drop files here, or use Upload file") : t("Drag files here to upload")}</b>
        <span>{t("Supports {formats}; select several at once and they upload one by one.", { formats: FORMATS.join(" / ") })}</span>
      </div>
      {!drag && (
        <button className="btn small" onClick={() => fileInput.current?.click()}>{t("Choose files")}</button>
      )}
    </div>
  );

  return (
    <div className="kn-page" onDragEnter={onDragEnter} onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
      <div className="kn-inner">
        <div className="kn-head">
          <div className="kn-head-main">
            {group ? (
              <h1 className="kn-title-row">
                <button className="link" onClick={onBack}>{t("Library")}</button>
                <ChevronRight size={15} aria-hidden />
                <span>{group.name}</span>
              </h1>
            ) : (
              <h1>{t("Library")}</h1>
            )}
            <p className="kn-desc">
              {group
                ? t("This is {name}'s own library. Its members search these documents plus the shared ones; no other group can see them. You can also write", { name: group.name })
                : t("Every document, with the group it belongs to. A document marked Shared is visible to every group. Open a group chat and use its library to add material scoped to that project.")}{" "}
              <code>#document title</code> {t("in a message to cite a whole document.")}
            </p>
          </div>
          <div className="kn-head-actions">
            <button className="btn" onClick={() => setSource("url")}><Link2 size={15} /> {t("From a link")}</button>
            <button className="btn" onClick={() => setSource("dir")}><FolderInput size={15} /> {t("From a folder")}</button>
            <button className="btn" onClick={() => setNoteOpen(true)}><StickyNote size={15} /> {t("New note")}</button>
            <button className="btn primary" onClick={() => fileInput.current?.click()}><Upload size={15} /> {t("Upload file")}</button>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept={ACCEPT}
              hidden
              aria-label={t("Files to upload")}
              onChange={(e) => {
                const fs = Array.from(e.target.files ?? []);
                e.target.value = "";
                void upload(fs);
              }}
            />
          </div>
        </div>

        <div className="kn-shelf">
          <button className="kn-shelf-head" aria-expanded={shelfOpen} onClick={() => setShelfOpen((v) => !v)}>
            <Layers size={15} />
            <b>{t("Knowledge bases")}</b>
            <span className="muted small">{t("{n} in reach here", { n: kbs.length })}</span>
            <span className="grow" />
            <span className="muted small">{shelfOpen ? t("Hide") : t("Show")}</span>
          </button>
          {shelfOpen && (
            <>
              <KbSection kbs={kbs} groupId={groupId} active={kbFilter} onOpen={setKbFilter} onChanged={reloadAll} />
              <div className="kn-shelf-sub">{t("Collections")}</div>
              <CollectionSection cols={cols} kbs={kbs} onChanged={reloadAll} />
            </>
          )}
        </div>

        {kbFilter && !empty && (
          <div className="kn-toolbar">
            <div className="kn-stats">
              {t("Showing only {name}", { name: kbById.get(kbFilter)?.name ?? "" })}
              <button className="link" onClick={() => setKbFilter(null)}>{t("Show everything")}</button>
            </div>
          </div>
        )}

        {dropZone}

        {ups.length > 0 && (
          <ul className="kn-ups" aria-label={t("Upload progress")}>
            {ups.map((u) => (
              <li key={u.key} className={"kn-up " + u.state}>
                <span className="kn-up-ico">
                  {u.state === "up" && <LoaderCircle size={15} className="kn-spin" />}
                  {u.state === "wait" && <LoaderCircle size={15} />}
                  {u.state === "ok" && <CircleCheck size={15} />}
                  {u.state === "err" && <CircleAlert size={15} />}
                </span>
                <span className="kn-up-name">{u.name}</span>
                <span className="kn-up-msg">
                  {u.state === "wait" && t("Waiting")}
                  {u.state === "up" && t("Uploading and indexing…")}
                  {u.state === "ok" && t("Added")}
                  {u.state === "err" && u.msg}
                </span>
                {(u.state === "err" || u.state === "ok") && (
                  <button className="icon-btn tiny" aria-label={t("Clear the upload record for \"{name}\"", { name: u.name })} title={t("Clear")} onClick={() => setUps((l) => l.filter((x) => x.key !== u.key))}>
                    <X size={13} />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {loadErr && <div className="err kn-block">{loadErr}</div>}
        {loading && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> {t("Loading…")}</div>}

        {empty && (
          <div className="kn-empty">
            <p><b>{t("The library is empty.")}</b>{t("Add policies, manuals, past proposals or product material, and members will look them up when they need to.")}</p>
            <p className="muted">
              {t("Files are kept in the local data directory as extracted text; the original file is not retained. Snippets a member retrieves are sent to the model along with the question, so with a hosted model they leave your machine. Scanned PDFs (no text layer) are not supported yet.")}
            </p>
          </div>
        )}

        {docs.length > 0 && (
          <>
            <div className="kn-toolbar">
              <div className="kn-stats">{t("{n} documents", { n: docs.length })} · {fmtChars(total)}</div>
              <label className="search-box kn-search">
                <Search size={15} />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder={t("Try it: type a keyword and see what members would find")}
                  aria-label={t("Search the library")}
                />
                {q && (
                  <button className="icon-btn tiny" aria-label={t("Clear the search")} title={t("Clear all")} onClick={() => setQ("")}><X size={13} /></button>
                )}
              </label>
            </div>

            {q.trim() && (
              <section className="kn-results" aria-label={t("Search results")}>
                <div className="kn-results-head">
                  <b>{t("Search results")}</b>
                  <span className="muted small">
                    {t("This is exactly what a member sees when searching in a group chat: keyword matches (adjacent character pairs for Chinese), most relevant snippets first; disabled documents are excluded.")}
                  </span>
                  {searching && <LoaderCircle size={14} className="kn-spin" />}
                </div>
                {searchErr && <div className="err">{searchErr}</div>}
                {!searchErr && hits && hits.length === 0 && !searching && (
                  <div className="empty">{t("No matching snippets — try another keyword.")}</div>
                )}
                {hits?.map((h) => (
                  <article key={`${h.doc_id}-${h.idx}`} className="kn-hit">
                    <div className="kn-hit-head">
                      <button className="kn-hit-title" onClick={() => open({ id: h.doc_id, title: h.title }, q.trim())} title={t("Open this document to read it")}>
                        {h.title}
                      </button>
                      <span className="muted small">{t("section {n}")}</span>
                      <span className="grow" />
                      <span className="tag" title={t("BM25 relevance, only meaningful for relative ordering")}>{t("relevance {score}")}</span>
                    </div>
                    <p className="kn-hit-text"><Highlight text={snippet(h.text, tokens)} tokens={tokens} /></p>
                  </article>
                ))}
              </section>
            )}

            {rowErr && <div className="err kn-block">{rowErr}</div>}
            <div className="kn-docs" role="table" aria-label={t("Document list")}>
              <div className="kn-doc-row kn-doc-th" role="row">
                <span />
                <span>{t("Title")}</span>
                <span className="kn-col-size">{t("Chars · chunks")}</span>
                <span className="kn-col-time">{t("Added")}</span>
                <span className="kn-col-sw">{t("On")}</span>
                <span />
              </div>
              {docs.map((d) => (
                <div key={d.id} className={"kn-doc-row" + (d.enabled ? "" : " off")} role="row">
                  <span className="kn-doc-ico" title={kindLabel(d.kind)}><KindIcon kind={d.kind} /></span>
                  <div className="kn-doc-main">
                    {renaming === d.id ? (
                      <input
                        className="kn-rename"
                        autoFocus
                        value={renameText}
                        aria-label={t("Document title")}
                        onChange={(e) => setRenameText(e.target.value)}
                        onBlur={() => void commitRename(d)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                          if (e.key === "Escape") { setRenameText(d.title); setRenaming(null); }
                        }}
                      />
                    ) : (
                      <button
                        className="kn-doc-title"
                        title={t("Click to read, double-click to rename")}
                        onClick={() => {
                          window.clearTimeout(openTimer.current);
                          openTimer.current = window.setTimeout(() => open(d), 230);
                        }}
                        onDoubleClick={() => startRename(d)}
                      >
                        {d.title}
                      </button>
                    )}
                    <div className="kn-doc-sub">
                      <span className="tag">{kindLabel(d.kind)}</span>
                      <span className={"tag" + (kbById.get(d.kb_id)?.group_id ? "" : " shared")} title={t("Knowledge base")}>
                        {kbById.get(d.kb_id)?.name ?? t("Unknown knowledge base")}
                      </span>
                      <span className="kn-doc-file">{d.filename ? d.filename : t("Manual note")}</span>
                      {!d.enabled && <span className="tag warn">{t("Disabled — not searched")}</span>}
                    </div>
                  </div>
                  <span className="kn-col-size kn-num">{t("{chars} chars · {chunks} chunks", { chars: d.chars.toLocaleString(), chunks: d.chunks })}</span>
                  <span className="kn-col-time kn-num muted">{relTime(d.created_at)}</span>
                  <span className="kn-col-sw"><Switch checked={d.enabled} onChange={(v) => void toggle(d, v)} label={t("Enable \"{title}\"", { title: d.title })} /></span>
                  <span className="kn-doc-ops">
                    <button className="icon-btn tiny" aria-label={t("Rename \"{title}\"", { title: d.title })} title={t("Rename")} onClick={() => startRename(d)}><Pencil size={14} /></button>
                    <button className="icon-btn tiny kn-del" aria-label={t("Delete \"{title}\"", { title: d.title })} title={t("Delete")} onClick={() => void remove(d)}><Trash2 size={14} /></button>
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {source && <SourceModal kind={source} scope={scope} onClose={() => setSource(null)} onDone={reloadAll} />}
      {noteOpen && <NoteModal scope={scope} onClose={() => setNoteOpen(false)} onSaved={() => { setNoteOpen(false); reloadAll(); }} />}
      {reading && <Reader doc={reading} onClose={() => setReading(null)} />}
    </div>
  );
}

// --------------------------------------------------------------- new note
function NoteModal({ scope, onClose, onSaved }: { scope: LibScope; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!title.trim()) return setErr(t("Please enter a title"));
    if (!content.trim()) return setErr(t("Please enter the content"));
    setBusy(true);
    setErr("");
    try {
      await api.addNote(title.trim(), content, scope);
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title={t("New note")}
      wide
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={save}>{busy ? t("Saving…") : t("Save")}</button>
        </>
      }
    >
      <p className="muted small" style={{ margin: "0 0 12px" }}>{t("A note is chunked and indexed just like an uploaded document — handy for policy highlights, terminology or standard wording.")}</p>
      <label className="field">
        <span>{t("Title")}</span>
        <input value={title} autoFocus onChange={(e) => setTitle(e.target.value)} placeholder={t("e.g. key points of the expenses policy")} />
      </label>
      <label className="field">
        <span>{t("Content")}</span>
        <textarea rows={10} value={content} onChange={(e) => setContent(e.target.value)} placeholder={t("Type or paste text here")} />
      </label>
    </Modal>
  );
}

// ------------------------------------------------------------------ reader
const PAGE = 6000;
function Reader({ doc, onClose }: { doc: { id: string; title: string; q: string }; onClose: () => void }) {
  const { t } = useI18n();
  const [start, setStart] = useState(0);
  const [data, setData] = useState<Awaited<ReturnType<typeof api.readDoc>> | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const tokens = useMemo(() => queryTokens(doc.q), [doc.q]);

  useEffect(() => {
    let alive = true;
    setBusy(true);
    api.readDoc(doc.id, start)
      .then((d) => { if (alive) { setData(d); setErr(""); box.current?.scrollTo({ top: 0 }); } })
      .catch((e) => alive && setErr((e as Error).message))
      .finally(() => alive && setBusy(false));
    return () => { alive = false; };
  }, [doc.id, start]);

  const from = data ? Math.min(data.start + 1, data.total) : 0;
  return (
    <Modal
      title={doc.title}
      wide
      onClose={onClose}
      actions={
        <>
          <span className="kn-range">
            {data ? (data.total === 0 ? t("No content")
              : t("chars {from}-{to} of {total}", { from: from.toLocaleString(), to: data.end.toLocaleString(), total: data.total.toLocaleString() })) : ""}
          </span>
          <button className="btn small" disabled={!data || data.start <= 0 || busy} onClick={() => setStart(Math.max(0, (data?.start ?? 0) - PAGE))}>
            <ChevronLeft size={14} /> {t("Previous")}
          </button>
          <button className="btn small" disabled={!data || data.end >= data.total || busy} onClick={() => setStart(data?.end ?? 0)}>
            {t("Next")} <ChevronRight size={14} />
          </button>
        </>
      }
    >
      <div className="kn-reader" ref={box}>
        {err && <div className="err">{err}</div>}
        {!data && !err && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> {t("Loading…")}</div>}
        {data && (
          <>
            <div className="kn-reader-meta muted small">
              {kindLabel(data.doc.kind)}{data.doc.filename ? ` · ${data.doc.filename}` : ""}{t(" · {n} chunks in total.", { n: data.doc.chunks })}
              {t("This is the extracted text, so the layout may differ from the original file.")}
            </div>
            <div className={"kn-reader-text" + (busy ? " busy" : "")}>
              <Highlight text={data.text} tokens={tokens} />
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

/** Import from a link or a folder: links are fetched over the network (subject to the hosted-calls switch), folders are read locally and a second import only updates what changed. */
function SourceModal({ kind, scope, onClose, onDone }: { kind: "url" | "dir"; scope: LibScope; onClose: () => void; onDone: () => void }) {
  const { t } = useI18n();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [res, setRes] = useState<{ added: number; skipped: { name: string; reason: string }[] } | null>(null);
  const pick = window.teamAgent?.pickFolder;

  const go = async () => {
    setBusy(true);
    setErr("");
    setRes(null);
    try {
      if (kind === "url") {
        await api.addDocUrl(value.trim(), scope);
        setRes({ added: 1, skipped: [] });
      } else {
        const r = await api.addDocDir(value.trim(), true, scope);
        setRes({ added: r.added.length, skipped: r.skipped });
      }
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={kind === "url" ? t("Import from a link") : t("Import from a folder")}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>{res ? t("Done") : t("Cancel")}</button>
          <button className="btn primary" disabled={busy || !value.trim()} onClick={() => void go()}>{busy ? t("Importing…") : t("Import")}</button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0, lineHeight: 1.7 }}>
        {kind === "url"
          ? t("Fetch the body of a web page (or a PDF / text link) into the library. Needs network access, and it captures that moment only — later changes to the page are not synced.")
          : t("Import every document in a folder (including subfolders); the supported formats match uploads. Importing the same folder again skips unchanged files and replaces changed ones. Hidden files and symlinks are skipped.")}
      </p>
      <div className="input-group">
        <input value={value} autoFocus onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && !busy && value.trim() && void go()} spellCheck={false}
          placeholder={kind === "url" ? "https://…" : t("Full path of the folder, e.g. /Users/you/Documents/policies")} aria-label={kind === "url" ? t("Link") : t("Folder path")} />
        {kind === "dir" && pick && <button className="btn" onClick={async () => { const p = await pick(); if (p) setValue(p); }}><FolderInput size={14} /> {t("Choose…")}</button>}
      </div>
      {err && <div className="err" role="alert" style={{ marginTop: 8 }}>{err}</div>}
      {res && (
        <div className="ok-text" role="status" style={{ marginTop: 8 }}>
          {t("Imported {n} documents.", { n: res.added })}
          {res.skipped.length > 0 && (
            <details style={{ marginTop: 4 }}>
              <summary>{t("Skipped {n}")}</summary>
              <ul className="kn-obs-warn">{res.skipped.slice(0, 30).map((s, i) => <li key={i}>{s.name}:{s.reason}</li>)}</ul>
            </details>
          )}
        </div>
      )}
    </Modal>
  );
}
