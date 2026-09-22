import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import { api } from "../api";
import { useI18n } from "../i18n";

/**
 * An image a member drew, shown inside the tool trace.
 *
 * Same reason as `MessageVideo`: the backend wants the app token in a header, so a plain
 * `<img src="/api/...">` would be rejected. The bytes come through the API and become an object
 * URL, revoked on unmount so a long transcript does not leak memory.
 *
 * `alt` is the file name rather than the prompt: the prompt is a model's own text, and a screen
 * reader announcing it as a description of the picture would state something nobody verified.
 */
export default function MessageImageGen({ gid, name, bytes }: { gid: string; name: string; bytes?: number }) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    let made = "";
    setUrl("");
    setFailed(false);
    api.drawnImageBytes(gid, name).then((blob) => {
      if (!live) return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; if (made) URL.revokeObjectURL(made); };
  }, [gid, name]);

  if (failed) {
    return (
      <span className="msg-img-broken" role="img" aria-label={t("The image could not be loaded")}>
        <ImageOff size={14} /> {name}
      </span>
    );
  }
  const size = typeof bytes === "number"
    ? (bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`)
    : "";
  return (
    <figure className="msg-video">
      {url
        ? <img src={url} alt={name} style={{ maxWidth: 320, borderRadius: 10, display: "block" }} />
        : <span className="msg-video-loading">{t("Loading the image…")}</span>}
      <figcaption>{name}{size ? ` · ${size}` : ""}</figcaption>
    </figure>
  );
}
