import { useEffect, useRef, useState } from "react";
import { Check, Copy, CornerUpRight, Pause, Play, Quote, ThumbsDown, ThumbsUp } from "lucide-react";
import { api, type Group, type Message, type MessageFeedback } from "../api";
import { useI18n } from "../i18n";

/**
 * What you can do with one member's reply: copy it, judge it, pass it on, hear it, quote it.
 *
 * The keys sit under every member message rather than in a menu, because the whole point of the
 * request was that they be *there*. They are quiet until the message is hovered, except for a reply
 * that has already been judged, which keeps its thumb lit — a rating you cannot see is one you will
 * give twice.
 *
 * What each one is actually doing, since three of them look interchangeable and are not:
 *
 *   * **copy** — the message as Markdown, the same text a model was given. Nothing leaves the app.
 *   * **judge** — stored by the backend as *the user's verdict* (`rating` + an optional note), one
 *     row per message, and shown in the feedback panel. It is evidence, not a hidden prompt edit:
 *     nothing about a member's behaviour changes behind the user's back.
 *   * **pass on** — hands the text to another group through the same path a typed message takes, so
 *     that group's members actually read it. Attachments stay behind: they live in one workspace.
 *   * **hear it** — a local voice reads it (WAV from this machine, cached). If the reply was longer
 *     than the cap the button says so instead of implying the whole thing was read.
 *   * **quote** — the *passage the user has selected inside this message* if there is one (that is
 *     what "quote a piece of what it said" means), otherwise a reference to the whole message.
 */

/** Copy text, falling back for a browser that refuses the clipboard (Electron without focus). */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const el = document.createElement("textarea");
      el.value = text;
      el.style.position = "fixed";
      el.style.opacity = "0";
      document.body.appendChild(el);
      el.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(el);
      return ok;
    } catch {
      return false;
    }
  }
}

/** What the user selected inside this message, if anything. */
function selectedIn(el: HTMLElement | null): string {
  const sel = window.getSelection();
  if (!el || !sel || sel.isCollapsed || sel.rangeCount === 0) return "";
  if (!el.contains(sel.anchorNode) || !el.contains(sel.focusNode)) return "";
  return sel.toString().trim();
}

export default function MessageActions({ m, gid, groups, initial, onQuote }: {
  m: Message;
  gid: string;
  /** For the forward picker: everything the user could hand this to, minus the group it is in. */
  groups: Group[];
  /** The verdict already given, if the message has been judged before. */
  initial?: MessageFeedback;
  onQuote?: (text: string, whole: boolean) => void;
}) {
  const { t } = useI18n();
  const [rating, setRating] = useState<"" | "up" | "down">(initial?.rating ?? "");
  const [note, setNote] = useState(initial?.note ?? "");
  const [asking, setAsking] = useState(false);          // the note popover after a thumb
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [picking, setPicking] = useState(false);        // the forward picker
  const [playing, setPlaying] = useState(false);
  const [audio, setAudio] = useState<HTMLAudioElement | null>(null);
  const [said, setSaid] = useState("");                 // "read up to the cap" notice
  const body = useRef<HTMLDivElement>(null);

  useEffect(() => { setRating(initial?.rating ?? ""); setNote(initial?.note ?? ""); },
    [initial?.rating, initial?.note]);
  useEffect(() => () => { audio?.pause(); }, [audio]);

  const judge = async (want: "" | "up" | "down", withNote = note) => {
    const next = rating === want ? "" : want;           // clicking the lit thumb takes it back
    setErr("");
    setBusy("rate");
    try {
      await api.rate(gid, m.id, next, next ? withNote : "");
      setRating(next);
      if (!next) { setNote(""); setAsking(false); }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const hear = async () => {
    if (audio) { audio.pause(); setAudio(null); setPlaying(false); return; }
    setErr("");
    setBusy("say");
    try {
      const got = await api.speech(gid, m.id);
      const url = URL.createObjectURL(got.blob);
      const el = new Audio(url);
      el.onended = () => { setPlaying(false); setAudio(null); URL.revokeObjectURL(url); };
      setSaid(got.cut ? t("A long reply — read up to the limit, not to the end.") : "");
      setPlaying(true);
      setAudio(el);
      await el.play();
    } catch (e) {
      setErr((e as Error).message);
      setPlaying(false);
    } finally {
      setBusy("");
    }
  };

  const forward = async (toGroupId: string) => {
    setErr("");
    setBusy("forward");
    try {
      const got = await api.forward(gid, m.id, toGroupId);
      setPicking(false);
      setSaid(t("Handed to {name} — its members will read it now.", { name: got.group_name }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const quote = () => {
    const picked = selectedIn(body.current);
    onQuote?.(picked || m.content, !picked);
    window.getSelection()?.removeAllRanges();
  };

  const others = groups.filter((g) => g.id !== gid);

  return (
    <div className="msg-acts" ref={body}
      // Selecting text inside the message is how a *passage* gets quoted; a click on a key must not
      // clear that selection before the handler reads it.
      onMouseDown={(e) => { if ((e.target as HTMLElement).closest("button")) e.preventDefault(); }}>
      <button className="act" title={t("Copy this message")} aria-label={t("Copy this message")}
        onClick={async () => { setCopied(await copyText(m.content)); window.setTimeout(() => setCopied(false), 1400); }}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
        <span>{copied ? t("Copied") : t("Copy")}</span>
      </button>

      <button className={"act" + (rating === "up" ? " on up" : "")} disabled={busy === "rate"}
        title={t("Good answer — this goes into the feedback panel")}
        aria-label={t("Good answer")} aria-pressed={rating === "up"}
        onClick={() => { setAsking(true); void judge("up"); }}>
        <ThumbsUp size={13} /><span>{t("Helpful")}</span>
      </button>
      <button className={"act" + (rating === "down" ? " on down" : "")} disabled={busy === "rate"}
        title={t("Not good enough — this goes into the feedback panel")}
        aria-label={t("Not good enough")} aria-pressed={rating === "down"}
        onClick={() => { setAsking(true); void judge("down"); }}>
        <ThumbsDown size={13} /><span>{t("Not helpful")}</span>
      </button>

      <button className="act" title={t("Hand this reply to another group")}
        aria-label={t("Forward this message")} onClick={() => setPicking((v) => !v)}>
        <CornerUpRight size={13} /><span>{t("Forward")}</span>
      </button>

      <button className={"act" + (playing ? " on" : "")} disabled={busy === "say"}
        title={t("Read this message out loud, on this machine")}
        aria-label={playing ? t("Stop reading") : t("Read this message out loud")}
        aria-pressed={playing} onClick={() => void hear()}>
        {playing ? <Pause size={13} /> : <Play size={13} />}
        <span>{playing ? t("Stop") : busy === "say" ? t("Reading…") : t("Read aloud")}</span>
      </button>

      {onQuote && (
        <button className="act" title={t("Quote this message, or the part of it you selected")}
          aria-label={t("Quote this message")} onClick={quote}>
          <Quote size={13} /><span>{t("Quote")}</span>
        </button>
      )}

      {asking && rating && (
        <div className="act-note">
          <input value={note} autoFocus maxLength={500}
            placeholder={t("Why? (optional)")} aria-label={t("Why? (optional)")}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void judge(rating, note); if (e.key === "Escape") setAsking(false); }} />
          <button className="btn tiny" onClick={() => void judge(rating, note)}>{t("Save")}</button>
          <button className="act" onClick={() => setAsking(false)} aria-label={t("Close")}>{t("Close")}</button>
        </div>
      )}

      {picking && (
        <div className="act-list" role="dialog" aria-label={t("Hand this reply to another group")}>
          <div className="act-list-head">{t("Which group should read this?")}</div>
          {others.length === 0 && <div className="muted small">{t("There is no other group yet.")}</div>}
          {others.map((g) => (
            <button key={g.id} className="act-row" disabled={busy === "forward"} onClick={() => void forward(g.id)}>
              <b>{g.name}</b>
              <span className="muted small">{t("Forwarding starts a round there")}</span>
            </button>
          ))}
          <button className="act" onClick={() => setPicking(false)}>{t("Cancel")}</button>
        </div>
      )}

      {said && <span className="muted small act-said">{said}</span>}
      {err && <span className="err small">{err}</span>}
    </div>
  );
}
