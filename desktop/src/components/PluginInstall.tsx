import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, type FilePreview } from "../api";
import { Modal, useConfirm } from "../ui";
import { Callout, dupMessage, fmtBytes, githubUrl, Spin } from "./ExtBits";
import "../styles/ext.css";

/**
 * 插件安装:只有这一条路径。必须先把完整源码展示给用户,勾选「我已阅读…」后才能安装,
 * 安装请求带上预览时得到的 sha256,服务器重新下载并比对,内容变了就拒绝。
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
  overwrite?: boolean;      // 「重新安装」时为 true(用户明确要替换已装的同名插件)
  onClose: () => void;
  onInstalled: (id: string) => void;
}) {
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
      if (/不一致|已被修改|重新预览/.test(msg)) {
        setStale(true);
        setErr("文件在你预览后变了,请重新预览。为安全起见不会安装没看过的内容。");
      } else if (/已有同名/.test(msg) && !over) {
        const ok = await confirm(`${dupMessage(msg)}。覆盖会用你刚才预览的这份代码替换本机上的同名插件。要覆盖吗?`, { okText: "覆盖安装" });
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
      title={overwrite ? "查看并重新安装插件" : "预览并安装插件"}
      onClose={onClose}
      wide
      actions={
        <div className="ext-act-col">
          {pv && (
            <label className={"check ext-ack" + (ack ? " on" : "")}>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} disabled={stale} />
              我已阅读以上源码,知道它会在本机以我的权限运行
            </label>
          )}
          {err && (
            <div className="ext-errbox" role="alert">
              <div className="err">{err}</div>
              {stale && <button className="btn small" onClick={() => void load()}><RefreshCw size={13} /> 重新预览</button>}
            </div>
          )}
          <div className="ext-act-btns">
            <button className="btn" onClick={onClose}>取消</button>
            <button className="btn primary" disabled={!pv || !ack || busy || stale || loading} onClick={() => void install(overwrite)}>
              {busy ? <><Spin /> 安装中…</> : overwrite ? "覆盖安装" : "安装"}
            </button>
          </div>
        </div>
      }
    >
      <div className="ext-xl">
        <Callout tone="warn" title="插件是 Python 代码">
          它会在本程序的进程里运行,没有沙箱,能访问你的文件和网络。下面是文件的完整源码,请从头读到尾再决定。
        </Callout>
        <div className="ext-meta">
          <a href={githubUrl(repo)} target="_blank" rel="noreferrer" className="link">{repo}</a>
          <span className="mono">/ {path}{gitRef ? ` @ ${gitRef}` : ""}</span>
        </div>
        {loading && <div className="empty"><Spin /> 正在从 GitHub 下载源码…</div>}
        {loadErr && (
          <div className="ext-errbox">
            <div className="err">{loadErr}</div>
            <button className="btn small" onClick={() => void load()}><RefreshCw size={13} /> 重试</button>
          </div>
        )}
        {pv && (
          <>
            <div className="ext-meta">
              <span className="muted small">大小 {fmtBytes(pv.size)} · {lines} 行</span>
              <span className="muted small">SHA-256</span>
              <code className="ext-hash" title="安装时服务器会重新下载并核对这个值">{pv.sha256}</code>
            </div>
            <pre className="ext-src" tabIndex={0} aria-label="插件完整源码">{pv.content}</pre>
          </>
        )}
      </div>
    </Modal>
  );
}
