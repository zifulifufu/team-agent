import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, ApiError, type FilePreview } from "../api";
import { useI18n } from "../i18n";
import { Modal, useConfirm } from "../ui";
import { Callout, dupMessage, fmtBytes, githubUrl, Spin } from "./ExtBits";
import "../styles/ext.css";

/**
 * Installing a plugin: this is the only path. The full source is shown to the user first and
 * installation stays disabled until "I have read the source" is ticked. The install request
 * carries the sha256 from the preview; the server downloads again and compares, refusing if
 * the content changed.
 */
export default function PluginInstallModal({
  repo,
  path,
  gitRef = "",
  overwrite = false,
  onClose,
  onInstalled,
}: {
  repo: string;
  path: string;
  gitRef?: string;
  overwrite?: boolean;      // true for "reinstall" (the user explicitly wants to replace the installed plugin of the same name)
  onClose: () => void;
  onInstalled: (id: string) => void;
}) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [pv, setPv] = useState<FilePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadErr, setLoadErr] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [stale, setStale] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadErr("");
    setErr("");
    setStale(false);
    setAck(false);
    try {
      setPv(await api.previewFile(repo, path, gitRef));
    } catch (e) {
      setPv(null);
      setLoadErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [repo, path, gitRef]);
  useEffect(() => { void load(); }, [load]);

  const install = async (over: boolean) => {
    if (!pv) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.installPlugin(repo, path, pv.sha256, gitRef, over);
      onInstalled(r.id);
    } catch (e) {
      const msg = (e as Error).message;
      // Branch on the HTTP status, never on the message: the server words it per request
      // language. 412 = the preview went stale, 409 = a duplicate name.
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 412) {
        setStale(true);
        setErr(t("The file changed after you previewed it. Preview it again — for safety, nothing you have not read is ever installed."));
      } else if (status === 409 && !over) {
        const ok = await confirm(t("{msg}. Overwriting replaces the local plugin of the same name with the code you just previewed. Overwrite it?", { msg: dupMessage(msg) }), { okText: t("Overwrite and install") });
        if (ok) {
          setBusy(false);
          return install(true);
        }
      } else {
        setErr(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  const lines = pv ? pv.content.split("\n").length : 0;
  return (
    <Modal
      title={overwrite ? t("Review and reinstall a plugin") : t("Preview and install a plugin")}
      onClose={onClose}
      wide
      actions={
        <div className="ext-act-col">
          {pv && (
            <label className={"check ext-ack" + (ack ? " on" : "")}>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} disabled={stale} />
              {t("I have read the source above and know it will run on this machine with my permissions")}
            </label>
          )}
          {err && (
            <div className="ext-errbox" role="alert">
              <div className="err">{err}</div>
              {stale && <button className="btn small" onClick={() => void load()}><RefreshCw size={13} /> {t("Preview again")}</button>}
            </div>
          )}
          <div className="ext-act-btns">
            <button className="btn" onClick={onClose}>{t("Cancel")}</button>
            <button className="btn primary" disabled={!pv || !ack || busy || stale || loading} onClick={() => void install(overwrite)}>
              {busy ? <><Spin /> {t("Installing…")}</> : overwrite ? t("Overwrite and install") : t("Install")}
            </button>
          </div>
        </div>
      }
    >
      <div className="ext-xl">
        <Callout tone="warn" title={t("A plugin is Python code")}>
          {t("It runs inside this app's process, with no sandbox, and can reach your files and network. The complete source is below — read it from top to bottom before you decide.")}
        </Callout>
        <div className="ext-meta">
          <a href={githubUrl(repo)} target="_blank" rel="noreferrer" className="link">{repo}</a>
          <span className="mono">/ {path}{gitRef ? ` @ ${gitRef}` : ""}</span>
        </div>
        {loading && <div className="empty"><Spin /> {t("Downloading the source from GitHub…")}</div>}
        {loadErr && (
          <div className="ext-errbox">
            <div className="err">{loadErr}</div>
            <button className="btn small" onClick={() => void load()}><RefreshCw size={13} /> {t("Retry")}</button>
          </div>
        )}
        {pv && (
          <>
            <div className="ext-meta">
              <span className="muted small">{t("Size {size} · {lines} lines", { size: fmtBytes(pv.size), lines })}</span>
              <span className="muted small">SHA-256</span>
              <code className="ext-hash" title={t("The server downloads the file again at install time and checks this value")}>{pv.sha256}</code>
            </div>
            <pre className="ext-src" tabIndex={0} aria-label={t("Complete plugin source")}>{pv.content}</pre>
          </>
        )}
      </div>
    </Modal>
  );
}
