import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import { api } from "../api";
import { useI18n } from "../i18n";

/**
 * One stored image, rendered in a bubble.
 *
 * The bytes are fetched through the API rather than pointed at with an <img src>: the backend
 * wants the app token in a header, and a plain image request cannot send one. The object URL is
 * revoked when the component goes away, so browsing a long transcript does not leak memory.
 */
export default function MessageImage({ id, name, alt }: { id: string; name?: string; alt?: string }) {
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

  if (failed) {
    return (
      <span className="msg-img-broken" role="img" aria-label={t("Image unavailable")}>
        <ImageOff size={14} /> {name || t("image")}
      </span>
    );
  }
  return url
    ? <img className="msg-img" src={url} alt={alt || name || t("Attached image")} loading="lazy" />
    : <span className="msg-img-loading" aria-hidden />;
}
