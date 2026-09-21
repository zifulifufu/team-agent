import { useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowUp, AtSign, Cloud, ImagePlus, Lock, Square, X } from "lucide-react";
import { api, type Attachment } from "../api";
import { useI18n } from "../i18n";
import MessageImage from "./MessageImage";

interface Mentionable {
  name: string;
  avatar: string;
  role: string;
  /** Text to insert for this entry. Defaults to "@" + name; the "everyone" entry overrides it because
   *  the backend accepts both "@all" and "@所有人" and we want the one that matches the UI language. */
  insert?: string;
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
  /** Images already uploaded and waiting to be sent with the next message */
  images?: Attachment[];
  onImages?: (next: Attachment[]) => void;
  /** Where uploads go. Without a group there is nothing to attach to, so the button stays hidden. */
  groupId?: string;
}

/** Large rounded composer: @ button on the left, routing status in the middle, round send button on the right. */
export default function Composer(p: Props) {
  const { t, lang } = useI18n();
  const [mention, setMention] = useState<{ q: string; idx: number } | null>(null);
  const [imgErr, setImgErr] = useState("");
  const [uploading, setUploading] = useState(0);
  const [over, setOver] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const images = p.images ?? [];
  const canAttach = !!p.groupId && !!p.onImages;

  /** Upload picks, one file at a time. A rejected file reports and does not block the others. */
  const addFiles = async (files: File[]) => {
    if (!canAttach || !files.length) return;
    setImgErr("");
    setUploading((n) => n + files.length);
    for (const file of files.slice(0, 10)) {
      try {
        const got = await api.uploadImage(p.groupId!, file);
        p.onImages!([...(p.images ?? []), got]);
      } catch (e) {
        setImgErr((e as Error).message);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  };

  const removeImage = (id: string) => {
    p.onImages?.(images.filter((i) => i.id !== id));
    void api.dropImage(id).catch(() => undefined);      // best effort: the sweep gets it later
  };

  const sendable = !p.disabled && !p.busy && (!!p.value.trim() || images.length > 0);

  const candidates = useMemo(() => {
    if (!mention) return [];
    const all: Mentionable = {
      name: t("Everyone"),
      insert: lang === "zh" ? "@所有人" : "@all",
      avatar: "👥",
      role: t("Everyone speaks in turn"),
    };
    return [all, ...p.members].filter((a) => a.name.includes(mention.q) || a.name.toLowerCase().includes(mention.q.toLowerCase()));
  }, [mention, p.members, t, lang]);

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
    const files = [...e.clipboardData.items]
      .filter((i) => i.kind === "file" && i.type.startsWith("image/"))
      .map((i) => i.getAsFile())
      .filter((f): f is File => !!f);
    if (files.length) {
      e.preventDefault();                                // a screenshot pastes as an image, not as a filename
      void addFiles(files);
    }
  };

  const onDrop = (e: React.DragEvent) => {
    const files = [...e.dataTransfer.files].filter((f) => f.type.startsWith("image/"));
    setOver(false);
    if (files.length) {
      e.preventDefault();
      void addFiles(files);
    }
  };

  return (
    <div className="composer">
      {mention && candidates.length > 0 && (
        <div className="mention-pop" role="listbox">
          {candidates.map((c, i) => (
            <button key={c.name} role="option" aria-selected={i === mention.idx} className={i === mention.idx ? "on" : ""} onMouseDown={(e) => { e.preventDefault(); chooseMention(c); }}>
              <span className="mp-ava">{c.avatar}</span>
              <b>{c.name}</b>
              <span className="muted">{c.role}</span>
            </button>
          ))}
        </div>
      )}
      {(p.error || imgErr) && <div className="err composer-err">{p.error || imgErr}</div>}
      <div className={"composer-box" + (over ? " over" : "")}
        onDragOver={(e) => { if (canAttach) { e.preventDefault(); setOver(true); } }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}>
        {(images.length > 0 || uploading > 0) && (
          <div className="composer-imgs">
            {images.map((im) => (
              <span key={im.id} className="ci-thumb">
                <MessageImage id={im.id} name={im.name} alt={im.name} />
                <button className="ci-x" title={t("Remove this image")} aria-label={t("Remove {name}", { name: im.name })} onClick={() => removeImage(im.id)}>
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
          <button className="round-btn" title={t("@-mention a member")} aria-label={t("@-mention a member")} onClick={insertAt}>
            <AtSign size={16} />
          </button>
          {canAttach && (
            <>
              <button className="round-btn" title={t("Attach an image")} aria-label={t("Attach an image")} disabled={p.busy || uploading > 0} onClick={() => fileRef.current?.click()}>
                <ImagePlus size={16} />
              </button>
              <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden
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
