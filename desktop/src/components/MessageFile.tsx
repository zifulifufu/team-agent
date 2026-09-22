import { useEffect, useState } from "react";
import { Download, FileArchive, FileAudio, FileSpreadsheet, FileText, FileVideo, Presentation } from "lucide-react";
import { api, type Attachment } from "../api";
import { useI18n } from "../i18n";
import MessageImage from "./MessageImage";
import "../styles/files.css";

/**
 * One file in a bubble — whatever kind it is.
 *
 * Images and clips come through the API as object URLs (the backend wants the app token in a
 * header, which an `<img src>` cannot send — see `MessageImage`). Documents are shown as a row:
 * the model already read the text at upload, so the reader mostly needs to know *what* was
 * attached and be able to open it, not to see it rendered.
 */
export default function MessageFile({ file }: { file: Attachment }) {
  const kind = file.kind ?? "image";
  if (kind === "image") return <MessageImage id={file.id} name={file.name} alt={file.name} />;
  if (kind === "video") return <Clip id={file.id} name={file.name} bytes={file.bytes} />;
  if (kind === "audio") return <Sound id={file.id} name={file.name} bytes={file.bytes} />;
  return <Row file={file} />;
}

function iconFor(kind: string, name: string) {
  const ext = name.toLowerCase().split(".").pop() ?? "";
  if (["xlsx", "xls", "csv", "ods"].includes(ext)) return FileSpreadsheet;
  if (["pptx", "ppt", "odp"].includes(ext)) return Presentation;
  if (["zip", "7z", "rar", "tar", "gz"].includes(ext)) return FileArchive;
  if (ext === "pdf") return FileText;
  if (["docx", "doc", "odt", "md", "txt"].includes(ext)) return FileText;
  if (kind === "video") return FileVideo;
  if (kind === "audio") return FileAudio;
  return FileText;
}

/** Documents and anything else: a name, a size, and a way to open it. */
function Row({ file }: { file: Attachment }) {
  const { t } = useI18n();
  const Icon = iconFor(file.kind ?? "", file.name);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  /** Fetch with the token, then hand the bytes to the browser as a download. A plain link would
   *  be rejected for the same reason an <img src> is. */
  const save = async () => {
    setBusy(true);
    setErr("");
    let made = "";
    try {
      const blob = await api.imageBytes(file.id);
      made = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = made;
      a.download = file.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch {
      setErr(t("Could not download it"));
    } finally {
      setBusy(false);
      if (made) setTimeout(() => URL.revokeObjectURL(made), 30_000);
    }
  };

  return (
    <span className="msg-file">
      <Icon size={15} aria-hidden />
      <span className="mf-name" title={file.name}>{file.name}</span>
      <span className="mf-size">{human(file.bytes)}</span>
      {file.has_text && <span className="mf-tag" title={t("Its text was read and given to the members")}>{t("read")}</span>}
      <button className="mf-btn" onClick={() => void save()} disabled={busy} title={t("Download")} aria-label={t("Download {name}", { name: file.name })}>
        <Download size={13} />
      </button>
      {err && <span className="mf-err">{err}</span>}
    </span>
  );
}

/** A video the user attached (not one a member generated — that one lives in the workspace). */
function Clip({ id, name, bytes }: { id: string; name: string; bytes: number }) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    let made = "";
    api.imageBytes(id).then((blob) => {
      if (!live) return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; if (made) URL.revokeObjectURL(made); };
  }, [id]);

  if (failed) return <span className="msg-img-broken"><FileVideo size={14} /> {name}</span>;
  return (
    <figure className="msg-video">
      {url ? <video src={url} controls preload="metadata" aria-label={name} /> : <span className="msg-video-loading">{t("Loading the clip…")}</span>}
      <figcaption>{name} · {human(bytes)}</figcaption>
    </figure>
  );
}

function Sound({ id, name, bytes }: { id: string; name: string; bytes: number }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let live = true;
    let made = "";
    api.imageBytes(id).then((blob) => {
      if (!live) return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    }).catch(() => undefined);
    return () => { live = false; if (made) URL.revokeObjectURL(made); };
  }, [id]);
  return (
    <span className="msg-audio">
      <FileAudio size={14} aria-hidden />
      <span className="mf-name">{name}</span>
      {url ? <audio src={url} controls preload="metadata" aria-label={name} /> : <span className="mf-size">{human(bytes)}</span>}
    </span>
  );
}

function human(n: number): string {
  for (const [unit, size] of [["GB", 1024 ** 3], ["MB", 1024 ** 2], ["KB", 1024]] as const) {
    if (n >= size) return `${(n / size).toFixed(1)} ${unit}`;
  }
  return `${n} B`;
}
