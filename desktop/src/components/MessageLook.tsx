import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import { api } from "../api";
import { useI18n } from "../i18n";

/**
 * The file a member just *looked at* or *listened to*, shown in the tool trace.
 *
 * `review_picture` and `review_audio` exist because a member cannot see or hear; the person reading
 * the chat can, and a paragraph describing a picture is a worse answer than the picture. So the
 * result carries the file itself and this renders it: an `<img>` for a still, a player for a clip or
 * a recording — the same thing a member just had described to it, one glance away for the user.
 *
 * Where `MessageVideo` / `MessageImageGen` fetch by bare file name out of the generator's own folder,
 * this one fetches by path out of the whole workspace (`inline=1`, so the response carries the real
 * media type) — a reviewed file is usually a figure, an uploaded still or a shot in a task folder,
 * not something a generator wrote.
 *
 * Same reason as those two for fetching through the API: the backend wants the app token in a
 * header, so a plain `<img src="/api/...">` would be rejected. The bytes become an object URL,
 * revoked on unmount so a long transcript does not leak memory.
 */
export default function MessageLook({ gid, name, kind, bytes }: {
  gid: string; name: string; kind: string; bytes?: number;
}) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState("");

  useEffect(() => {
    let live = true;
    let made = "";
    setUrl("");
    setFailed("");
    api.workspaceMediaBytes(gid, name).then((blob) => {
      if (!live) return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    }).catch(() => { if (live) setFailed(t("That file could not be loaded")); });
    return () => { live = false; if (made) URL.revokeObjectURL(made); };
  }, [gid, name, t]);

  if (failed) {
    return (
      <span className="msg-img-broken" role="img" aria-label={failed}>
        <ImageOff size={14} /> {name}
      </span>
    );
  }
  // Bytes before kilobytes: a small still rounded to "0 KB" reads as a file that failed to save.
  const size = typeof bytes !== "number" ? ""
    : bytes < 1024 ? ` · ${bytes} B`
    : bytes < 1024 * 1024 ? ` · ${Math.round(bytes / 1024)} KB`
    : ` · ${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (!url) return <span className="msg-video-loading">{t("Loading…")}</span>;
  const caption = <figcaption>{name}{size}</figcaption>;
  // One class for all three: what a review shows is the file that was reviewed, and which element
  // plays it is the only difference. `msg-img` (the zoomable one) is deliberately not reused — there
  // is no lightbox behind a tool pill, and a zoom-in cursor that does nothing is worse than none.
  if (kind === "audio") {
    return <figure className="msg-look audio"><audio src={url} controls preload="metadata" aria-label={name} />{caption}</figure>;
  }
  if (kind === "video") {
    return <figure className="msg-look"><video src={url} controls preload="metadata" aria-label={name} />{caption}</figure>;
  }
  return <figure className="msg-look"><img src={url} alt={name} />{caption}</figure>;
}
