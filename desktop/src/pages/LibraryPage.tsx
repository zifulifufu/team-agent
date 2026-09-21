import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { useData } from "../data";
import {
  ChevronLeft, ChevronRight, CircleAlert, CircleCheck, FileCode, FileJson, FileSpreadsheet, FileText, FileType,
  FolderInput, Link2, LoaderCircle, Pencil, Search, StickyNote, Trash2, Upload, X,
} from "lucide-react";
import { api, relTime, type LibraryDoc, type LibraryHit } from "../api";
import { Modal, Switch, useConfirm } from "../ui";
import "../styles/know.css";

const ACCEPT = ".txt,.md,.markdown,.csv,.json,.html,.htm,.pdf,.docx";
const FORMATS = ["txt", "md", "csv", "json", "html", "pdf(需有文字层)", "docx"];

// ------------------------------------------------------------------ helpers
function fmtChars(n: number): string {
  if (n >= 10000) return `约 ${(n / 10000).toFixed(1)} 万字`;
  if (n >= 1000) return `约 ${(n / 1000).toFixed(1)} 千字`;
  return `${n} 字`;
}

/** 和后端 textindex.tokenize 保持一致:英文数字按词,中文按相邻两字,用来给检索结果做关键词高亮。 */
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

/** 文本高亮(纯 React 节点,不用 dangerouslySetInnerHTML)。 */
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

/** 把过长的片段截成以第一个命中为中心的一小段。 */
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
const kindLabel = (k: string) => (k === "note" ? "笔记" : k.toUpperCase());

interface UpItem {
  key: number;
  name: string;
  state: "wait" | "up" | "ok" | "err";
  msg?: string;
}
let upSeq = 1;

// --------------------------------------------------------------------- page
export default function LibraryPage() {
  const confirm = useConfirm();
  const { reloadGroups } = useData();
  const [docs, setDocs] = useState<LibraryDoc[]>([]);
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
      const r = await api.library();
      setDocs(r.docs);
      setTotal(r.total_chars);
      setLoadErr("");
    } catch (e) {
      setLoadErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => window.clearTimeout(openTimer.current), []);

  // ---- 上传:逐个进行,互不影响
  const patchUp = (key: number, p: Partial<UpItem>) => setUps((l) => l.map((u) => (u.key === key ? { ...u, ...p } : u)));
  const upload = async (files: File[]) => {
    if (!files.length) return;
    const items: UpItem[] = files.map((f) => ({ key: upSeq++, name: f.name, state: "wait" }));
    setUps((l) => [...l, ...items]);
    for (let i = 0; i < files.length; i++) {
      const it = items[i];
      patchUp(it.key, { state: "up" });
      try {
        await api.uploadDoc(files[i]);
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

  // ---- 搜索(300ms 防抖)
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
        const r = await api.searchLibrary(key, 8);
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

  // ---- 行内操作
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
      setRowErr(`重命名失败:${(e as Error).message}`);
    }
  };
  const startRename = (d: LibraryDoc) => {
    window.clearTimeout(openTimer.current);
    setRenameText(d.title);
    setRenaming(d.id);
  };
  const remove = async (d: LibraryDoc) => {
    if (!(await confirm(`删除文档「${d.title}」?它会从资料库中移除,同时从各群「扩展 → 资料库」的「仅选定文档」里去掉,无法恢复。`, { okText: "删除" }))) return;
    setRowErr("");
    try {
      await api.delDoc(d.id);
      await load();
      await reloadGroups();   // 后端已把它从各群「仅选定文档」里去掉,这里同步一下,免得之后保存时又写回来
    } catch (e) {
      setRowErr(`删除失败:${(e as Error).message}`);
    }
  };
  const open = (d: { id: string; title: string }, query = "") => setReading({ id: d.id, title: d.title, q: query });

  const empty = !loading && !loadErr && docs.length === 0;
  const dropZone = (
    <div className={"kn-drop" + (drag ? " over" : "") + (empty ? " big" : "")}>
      <Upload size={empty ? 26 : 18} strokeWidth={1.6} />
      <div className="kn-drop-text">
        <b>{drag ? "松开鼠标,开始上传" : empty ? "把文件拖到这里,或点击「上传文件」" : "拖拽文件到这里上传"}</b>
        <span>支持 {FORMATS.join(" / ")};可一次选多个,逐个上传。</span>
      </div>
      {!drag && (
        <button className="btn small" onClick={() => fileInput.current?.click()}>选择文件</button>
      )}
    </div>
  );

  return (
    <div className="kn-page" onDragEnter={onDragEnter} onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
      <div className="kn-inner">
        <div className="kn-head">
          <div className="kn-head-main">
            <h1>资料库</h1>
            <p className="kn-desc">
              上传的文档会被切成片段并建立索引。群聊里成员需要查资料时会自动检索(可在每个群的「扩展」里选择用全部、只用指定文档或不用);也可以在消息里写 <code>#文档标题</code> 直接引用整篇文档。
            </p>
          </div>
          <div className="kn-head-actions">
            <button className="btn" onClick={() => setSource("url")}><Link2 size={15} /> 从链接</button>
            <button className="btn" onClick={() => setSource("dir")}><FolderInput size={15} /> 从文件夹</button>
            <button className="btn" onClick={() => setNoteOpen(true)}><StickyNote size={15} /> 新建笔记</button>
            <button className="btn primary" onClick={() => fileInput.current?.click()}><Upload size={15} /> 上传文件</button>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept={ACCEPT}
              hidden
              aria-label="选择要上传的文件"
              onChange={(e) => {
                const fs = Array.from(e.target.files ?? []);
                e.target.value = "";
                void upload(fs);
              }}
            />
          </div>
        </div>

        {dropZone}

        {ups.length > 0 && (
          <ul className="kn-ups" aria-label="上传进度">
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
                  {u.state === "wait" && "等待上传"}
                  {u.state === "up" && "正在上传并建立索引…"}
                  {u.state === "ok" && "已添加"}
                  {u.state === "err" && u.msg}
                </span>
                {(u.state === "err" || u.state === "ok") && (
                  <button className="icon-btn tiny" aria-label={`清除「${u.name}」的上传记录`} title="清除" onClick={() => setUps((l) => l.filter((x) => x.key !== u.key))}>
                    <X size={13} />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {loadErr && <div className="err kn-block">{loadErr}</div>}
        {loading && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> 加载中…</div>}

        {empty && (
          <div className="kn-empty">
            <p><b>资料库还是空的。</b>把制度、手册、往期方案、产品资料放进来,成员在群聊里需要时会自己去查。</p>
            <p className="muted">
              文件只保存在本机数据目录里(存的是提取出的文字,不保留原文件)。但成员检索到的片段会随问题一起发给所用的模型,如果用的是云端模型,这些片段就会发到云端。扫描件 PDF(没有文字层)暂不支持。
            </p>
          </div>
        )}

        {docs.length > 0 && (
          <>
            <div className="kn-toolbar">
              <div className="kn-stats">共 {docs.length} 篇 · {fmtChars(total)}</div>
              <label className="search-box kn-search">
                <Search size={15} />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="试试检索:输入关键词,看成员会查到什么"
                  aria-label="检索资料库"
                />
                {q && (
                  <button className="icon-btn tiny" aria-label="清空检索" title="清空" onClick={() => setQ("")}><X size={13} /></button>
                )}
              </label>
            </div>

            {q.trim() && (
              <section className="kn-results" aria-label="检索结果">
                <div className="kn-results-head">
                  <b>检索结果</b>
                  <span className="muted small">
                    这就是成员在群聊里检索时看到的内容:按关键词匹配(中文按相邻两字),取最相关的片段;已停用的文档不参与。
                  </span>
                  {searching && <LoaderCircle size={14} className="kn-spin" />}
                </div>
                {searchErr && <div className="err">{searchErr}</div>}
                {!searchErr && hits && hits.length === 0 && !searching && (
                  <div className="empty">没有命中的片段,换个关键词试试。</div>
                )}
                {hits?.map((h) => (
                  <article key={`${h.doc_id}-${h.idx}`} className="kn-hit">
                    <div className="kn-hit-head">
                      <button className="kn-hit-title" onClick={() => open({ id: h.doc_id, title: h.title }, q.trim())} title="打开这篇文档阅读">
                        {h.title}
                      </button>
                      <span className="muted small">第 {h.idx + 1} 段</span>
                      <span className="grow" />
                      <span className="tag" title="BM25 相关度,只用于相对排序">相关度 {h.score}</span>
                    </div>
                    <p className="kn-hit-text"><Highlight text={snippet(h.text, tokens)} tokens={tokens} /></p>
                  </article>
                ))}
              </section>
            )}

            {rowErr && <div className="err kn-block">{rowErr}</div>}
            <div className="kn-docs" role="table" aria-label="文档列表">
              <div className="kn-doc-row kn-doc-th" role="row">
                <span />
                <span>标题</span>
                <span className="kn-col-size">字数 · 片段</span>
                <span className="kn-col-time">上传时间</span>
                <span className="kn-col-sw">启用</span>
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
                        aria-label="文档标题"
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
                        title="点击阅读,双击重命名"
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
                      <span className="kn-doc-file">{d.filename ? d.filename : "手动笔记"}</span>
                      {!d.enabled && <span className="tag warn">已停用,不会被检索</span>}
                    </div>
                  </div>
                  <span className="kn-col-size kn-num">{d.chars.toLocaleString()} 字 · {d.chunks} 段</span>
                  <span className="kn-col-time kn-num muted">{relTime(d.created_at)}</span>
                  <span className="kn-col-sw"><Switch checked={d.enabled} onChange={(v) => void toggle(d, v)} label={`启用「${d.title}」`} /></span>
                  <span className="kn-doc-ops">
                    <button className="icon-btn tiny" aria-label={`重命名「${d.title}」`} title="重命名" onClick={() => startRename(d)}><Pencil size={14} /></button>
                    <button className="icon-btn tiny kn-del" aria-label={`删除「${d.title}」`} title="删除" onClick={() => void remove(d)}><Trash2 size={14} /></button>
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {source && <SourceModal kind={source} onClose={() => setSource(null)} onDone={() => void load()} />}
      {noteOpen && <NoteModal onClose={() => setNoteOpen(false)} onSaved={() => { setNoteOpen(false); void load(); }} />}
      {reading && <Reader doc={reading} onClose={() => setReading(null)} />}
    </div>
  );
}

// --------------------------------------------------------------- new note
function NoteModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!title.trim()) return setErr("请填写标题");
    if (!content.trim()) return setErr("请填写内容");
    setBusy(true);
    setErr("");
    try {
      await api.addNote(title.trim(), content);
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title="新建笔记"
      wide
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={busy} onClick={save}>{busy ? "保存中…" : "保存"}</button>
        </>
      }
    >
      <p className="muted small" style={{ margin: "0 0 12px" }}>笔记和上传的文档一样会被切片、建立索引,适合记一些制度要点、术语、固定口径。</p>
      <label className="field">
        <span>标题</span>
        <input value={title} autoFocus onChange={(e) => setTitle(e.target.value)} placeholder="如:报销制度要点" />
      </label>
      <label className="field">
        <span>内容</span>
        <textarea rows={10} value={content} onChange={(e) => setContent(e.target.value)} placeholder="在这里输入或粘贴文字" />
      </label>
    </Modal>
  );
}

// ------------------------------------------------------------------ reader
const PAGE = 6000;
function Reader({ doc, onClose }: { doc: { id: string; title: string; q: string }; onClose: () => void }) {
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
            {data ? (data.total === 0 ? "内容为空" : `第 ${from.toLocaleString()}–${data.end.toLocaleString()} / 总 ${data.total.toLocaleString()} 字`) : ""}
          </span>
          <button className="btn small" disabled={!data || data.start <= 0 || busy} onClick={() => setStart(Math.max(0, (data?.start ?? 0) - PAGE))}>
            <ChevronLeft size={14} /> 上一段
          </button>
          <button className="btn small" disabled={!data || data.end >= data.total || busy} onClick={() => setStart(data?.end ?? 0)}>
            下一段 <ChevronRight size={14} />
          </button>
        </>
      }
    >
      <div className="kn-reader" ref={box}>
        {err && <div className="err">{err}</div>}
        {!data && !err && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> 加载中…</div>}
        {data && (
          <>
            <div className="kn-reader-meta muted small">
              {kindLabel(data.doc.kind)}{data.doc.filename ? ` · ${data.doc.filename}` : ""} · 共 {data.doc.chunks} 个片段。
              这里显示的是提取出的文字,可能与原文件的排版不同。
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

/** 从链接 / 文件夹导入:链接会联网抓取(受「允许外呼」开关约束);文件夹只读取本机,再次导入只更新变化的文件。 */
function SourceModal({ kind, onClose, onDone }: { kind: "url" | "dir"; onClose: () => void; onDone: () => void }) {
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
        await api.addDocUrl(value.trim());
        setRes({ added: 1, skipped: [] });
      } else {
        const r = await api.addDocDir(value.trim());
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
      title={kind === "url" ? "从链接导入" : "从文件夹导入"}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>{res ? "完成" : "取消"}</button>
          <button className="btn primary" disabled={busy || !value.trim()} onClick={() => void go()}>{busy ? "导入中…" : "导入"}</button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0, lineHeight: 1.7 }}>
        {kind === "url"
          ? "抓取网页(或 PDF / 文本链接)的正文存进资料库。需要联网;抓取到的只是那一刻的内容,之后网页更新不会自动同步。"
          : "把文件夹(含子文件夹)里的文档批量导入,支持的格式同上传。再次导入同一个文件夹时,没变的文件会跳过,变了的会替换成新版。隐藏文件和符号链接不会导入。"}
      </p>
      <div className="input-group">
        <input value={value} autoFocus onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && !busy && value.trim() && void go()} spellCheck={false}
          placeholder={kind === "url" ? "https://…" : "文件夹的完整路径,如 /Users/你/Documents/制度"} aria-label={kind === "url" ? "链接" : "文件夹路径"} />
        {kind === "dir" && pick && <button className="btn" onClick={async () => { const p = await pick(); if (p) setValue(p); }}><FolderInput size={14} /> 选择…</button>}
      </div>
      {err && <div className="err" role="alert" style={{ marginTop: 8 }}>{err}</div>}
      {res && (
        <div className="ok-text" role="status" style={{ marginTop: 8 }}>
          已导入 {res.added} 篇。
          {res.skipped.length > 0 && (
            <details style={{ marginTop: 4 }}>
              <summary>跳过 {res.skipped.length} 个</summary>
              <ul className="kn-obs-warn">{res.skipped.slice(0, 30).map((s, i) => <li key={i}>{s.name}:{s.reason}</li>)}</ul>
            </details>
          )}
        </div>
      )}
    </Modal>
  );
}
