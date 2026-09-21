import { useEffect, useState } from "react";
import { VideoOff } from "lucide-react";
import { api } from "../api";
import { useI18n } from "../i18n";

/**
 * A clip a member generated, played inside the tool trace.
 *
 * Same reason as `MessageImage`: the backend wants the app token in a header, so a plain
 * `<video src="/api/...">` would be rejected. The bytes are fetched through the API and turned
 * into an object URL, revoked on unmount so a long transcript does not leak memory. Playing from
 * a blob also means the controls work without the server supporting range requests.
 */
export default function MessageVideo({ gid, name, bytes }: { gid: string; name: string; bytes?: number }) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState("");

  useEffect(() => {
    let live = true;
    let made = "";
    setUrl("");
    setFailed("");
    api.videoBytes(gid, name).then((blob) => {
      if (!live) return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    }).catch(() => { if (live) setFailed(t("The clip could not be loaded")); });
    return () => { live = false; if (made) URL.revokeObjectURL(made); };
  }, [gid, name, t]);

  if (failed) {
    return (
      <span className="msg-img-broken" role="img" aria-label={failed}>
        <VideoOff size={14} /> {name}
      </span>
    );
  }
  return (
    <figure className="msg-video">
      {url
        ? <video src={url} controls preload="metadata" aria-label={name} />
        : <span className="msg-video-loading">{t("Loading the clip…")}</span>}
      <figcaption>
        {name}{typeof bytes === "number" ? ` · ${bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`}` : ""}
      </figcaption>
    </figure>
  );
}
