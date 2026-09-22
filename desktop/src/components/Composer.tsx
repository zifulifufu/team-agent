import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowUp, AtSign, Cloud, FileText, FolderOpen, Lock, Paperclip, Square, X } from "lucide-react";
import { api, type Attachment, type WorkspaceFile } from "../api";
import { useI18n } from "../i18n";
import MessageImage from "./MessageImage";
import "../styles/files.css";

interface Mentionable {
  name: string;
  avatar: string;
  role: string;
  /** Text to insert for this entry. Defaults to "@" + name; the "everyone" entry overrides it because
   *  the backend accepts both "@all" and "@所有人" and we want the one that matches the UI language. */
  insert?: string;
  /** Which band of the picker this belongs to: members, files, folders, documents. */
  group?: "member" | "file" | "folder" | "document";
}

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  busy?: boolean;
  onStop?: () => void;
  members: Mentionable[];
  placeholder: string;
  routeText?: string;
  offline?: boolean;
  onToggleExternal?: () => void;
  rows?: number;
  autoFocus?: boolean;
  disabled?: boolean;
  error?: string;
  /** Extra control at the bottom-left of the box (the home page uses it for "send to which group") */
  extra?: ReactNode;
  /** Files already uploaded and waiting to be sent with the next message */
  files?: Attachment[];
  onFiles?: (next: Attachment[]) => void;
  /** Where uploads go. Without a group there is nothing to attach to, so the button stays hidden. */
  groupId?: string;
}

/** Large rounded composer: @ button on the left, routing status in the middle, round send button on the right. */
export default function Composer(p: Props) {
  const { t, lang } = useI18n();
  const [mention, setMention] = useState<{ q: string; idx: number } | null>(null);
  const [err, setErr] = useState("");
  const [uploading, setUploading] = useState(0);
  const [over, setOver] = useState(false);
  const [tree, setTree] = useState<WorkspaceFile[]>([]);
  const [docs, setDocs] = useState<{ id: string; title: string }[]>([]);
  const ta = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const files = p.files ?? [];
  const canAttach = !!p.groupId && !!p.onFiles;

  /** Upload picks, one file at a time. A rejected file reports and does not block the others. */
  const addFiles = async (picked: File[]) => {
    if (!canAttach || !picked.length) return;
    setErr("");
    setUploading((n) => n + picked.length);
    for (const file of picked.slice(0, 20)) {
      try {
        const got = await api.uploadImage(p.groupId!, file);
        p.onFiles!([...(p.files ?? []), got]);
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  };

  const removeFile = (id: string) => {
    p.onFiles?.(files.filter((i) => i.id !== id));
    void api.dropImage(id).catch(() => undefined);      // best effort: the sweep gets it later
  };

  const sendable = !p.disabled && !p.busy && (!!p.value.trim() || files.length > 0);

  /** What the @ picker can offer: members, then the files and folders of this group's workspace,
   *  then the documents its knowledge bases hold. Loaded when the picker opens (and only then) —
   *  a chat that never references a file pays nothing for the tree. */
  useEffect(() => {
    if (!mention || !p.groupId) return;
    let live = true;
    void api.workspace(p.groupId).then((w) => { if (live) setTree(w.files); }).catch(() => undefined);
    void api.library({ group: p.groupId }).then((r) => {
      if (live) setDocs(r.docs.map((d) => ({ id: d.id, title: d.title })));
    }).catch(() => undefined);
    return () => { live = false; };
  }, [mention !== null, p.groupId]);      // eslint-disable-line react-hooks/exhaustive-deps

  const candidates = useMemo(() => {
    if (!mention) return [];
    const q = mention.q.toLowerCase();
    const hit = (s: string) => !q || s.toLowerCase().includes(q);
    const everyone: Mentionable = {
      name: t("Everyone"),
      insert: lang === "zh" ? "@所有人" : "@all",
      avatar: "👥",
      role: t("Everyone speaks in turn"),
      group: "member",
    };
    const people = [everyone, ...p.members].filter((a) => hit(a.name)).map((m) => ({ ...m, group: "member" as const }));
    const folders = new Set(tree.map((f) => f.folder).filter(Boolean));
    const folderEntries: Mentionable[] = [...folders]
      .filter(hit)
      .slice(0, 6)
      .map((f) => ({ name: `${f}/`, insert: `@dir:${f} `, avatar: "", role: t("folder"), group: "folder" as const }));
    const fileEntries: Mentionable[] = tree
      .filter((f) => hit(f.path))
      .slice(0, 12)
      .map((f) => ({ name: f.path, insert: `@file:${f.path} `, avatar: "", role: f.kind, group: "file" as const }));
    const docEntries: Mentionable[] = docs
      .filter((d) => hit(d.title))
      .slice(0, 8)
      .map((d) => ({ name: d.title, insert: `@doc:${d.id} `, avatar: "", role: t("document"), group: "document" as const }));
    return [...people, ...folderEntries, ...fileEntries, ...docEntries];
  }, [mention, p.members, tree, docs, t, lang]);

  const onInput = (v: string) => {
    p.onChange(v);
    const pos = ta.current?.selectionStart ?? v.length;
    const m = /@([^\s@]*)$/.exec(v.slice(0, pos));
    setMention(m ? { q: m[1], idx: 0 } : null);
  };

  /** Replace the half-typed @mention at the caret with the chosen entry. */
  const chooseMention = (c: Mentionable) => {
    const pos = ta.current?.selectionStart ?? p.value.length;
    const token = c.insert ?? "@" + c.name;
    const before = p.value.slice(0, pos).replace(/@([^\s@]*)$/, token + " ");
    p.onChange(before + p.value.slice(pos));
    setMention(null);
    ta.current?.focus();
  };

  const insertAt = () => {
    const pos = ta.current?.selectionStart ?? p.value.length;
    const pre = p.value.slice(0, pos);
    const sep = pre && !/\s$/.test(pre) ? " " : "";
    p.onChange(pre + sep + "@" + p.value.slice(pos));
    setMention({ q: "", idx: 0 });
    ta.current?.focus();
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention && candidates.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const d = e.key === "ArrowDown" ? 1 : -1;
        setMention({ ...mention, idx: (mention.idx + d + candidates.length) % candidates.length });
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing)) {
        e.preventDefault();
        chooseMention(candidates[mention.idx]);
        return;
      }
      if (e.key === "Escape") return setMention(null);
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (sendable) p.onSend();
    }
  };

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const picked = [...e.clipboardData.items]
      .filter((i) => i.kind === "file")
      .map((i) => i.getAsFile())
      .filter((f): f is File => !!f);
    if (picked.length) {
      e.preventDefault();                                // a screenshot pastes as a file, not as a filename
      void addFiles(picked);
    }
  };

  const onDrop = (e: React.DragEvent) => {
    const picked = [...e.dataTransfer.files];
    setOver(false);
    if (picked.length) {
      e.preventDefault();
      void addFiles(picked);
    }
  };

  return (
    <div className="composer">
      {mention && candidates.length > 0 && (
        <div className="mention-pop" role="listbox">
          {candidates.map((c, i) => (
            <button key={`${c.group}-${c.name}`} role="option" aria-selected={i === mention.idx}
              className={(i === mention.idx ? "on " : "") + "mp-" + (c.group ?? "member")}
              onMouseDown={(e) => { e.preventDefault(); chooseMention(c); }}>
              <span className="mp-ava">{c.avatar || (c.group === "folder" ? <FolderOpen size={13} /> : <FileText size={13} />)}</span>
              <b>{c.name}</b>
              <span className="muted">{c.role}</span>
            </button>
          ))}
        </div>
      )}
      {(p.error || err) && <div className="err composer-err">{p.error || err}</div>}
      <div className={"composer-box" + (over ? " over" : "")}
        onDragOver={(e) => { if (canAttach) { e.preventDefault(); setOver(true); } }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}>
        {(files.length > 0 || uploading > 0) && (
          <div className="composer-imgs">
            {files.map((f) => (
              <span key={f.id} className={"ci-thumb ci-" + (f.kind ?? "image")}>
                {!f.kind || f.kind === "image"
                  ? <MessageImage id={f.id} name={f.name} alt={f.name} />
                  : (
                    <span className="ci-file">
                      <FileText size={15} />
                      <span className="ci-name" title={f.name}>{f.name}</span>
                      <span className="ci-size">{humanSize(f.bytes)}</span>
                    </span>
                  )}
                <button className="ci-x" title={t("Remove this file")} aria-label={t("Remove {name}", { name: f.name })} onClick={() => removeFile(f.id)}>
                  <X size={11} />
                </button>
              </span>
            ))}
            {uploading > 0 && <span className="ci-uploading">{t("Uploading…")}</span>}
          </div>
        )}
        <textarea
          ref={ta}
          value={p.value}
          autoFocus={p.autoFocus}
          onChange={(e) => onInput(e.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          placeholder={p.placeholder}
          rows={p.rows ?? 3}
          aria-label={t("Message input")}
        />
        <div className="composer-bar">
          <button className="round-btn" title={t("Mention a member, a file, a folder or a document")} aria-label={t("@-mention")} onClick={insertAt}>
            <AtSign size={16} />
          </button>
          {canAttach && (
            <>
              <button className="round-btn" title={t("Add a file, image or video")} aria-label={t("Add a file")} disabled={p.busy || uploading > 0} onClick={() => fileRef.current?.click()}>
                <Paperclip size={16} />
              </button>
              <input ref={fileRef} type="file" multiple hidden
                onChange={(e) => { const picked = [...(e.target.files ?? [])]; e.target.value = ""; void addFiles(picked); }} />
            </>
          )}
          {p.extra}
          <div className="grow" />
          {p.routeText !== undefined && (
            <button className={"route-pill" + (p.offline ? " off" : "")} onClick={p.onToggleExternal} title={t("Click to allow / block hosted model calls")}>
              {p.offline ? <Lock size={13} /> : <Cloud size={13} />}
              <span>{p.routeText}</span>
            </button>
          )}
          {p.busy && p.onStop ? (
            <button className="send-btn stop" title={t("Stop")} aria-label={t("Stop")} onClick={p.onStop}>
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button className="send-btn" title={t("Send")} aria-label={t("Send")} disabled={!sendable} onClick={p.onSend}>
              <ArrowUp size={17} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function humanSize(n: number): string {
  for (const [unit, size] of [["GB", 1024 ** 3], ["MB", 1024 ** 2], ["KB", 1024]] as const) {
    if (n >= size) return `${(n / size).toFixed(1)} ${unit}`;
  }
  return `${n} B`;
}
