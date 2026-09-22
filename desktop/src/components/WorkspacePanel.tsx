import { useEffect, useState } from "react";
import { Download, FileSpreadsheet, FileText, FileVideo, FolderOpen, RefreshCw, X } from "lucide-react";
import { api, type WorkspaceView } from "../api";
import { useI18n } from "../i18n";
import "../styles/files.css";

const ICONS: Record<string, typeof FileText> = { document: FileText, image: FileText, video: FileVideo, audio: FileText, other: FileText };
const SHEET = ["xlsx", "csv", "xls"];

function icon(name: string, kind: string) {
  const ext = name.toLowerCase().split(".").pop() ?? "";
  if (SHEET.includes(ext)) return FileSpreadsheet;
  return ICONS[kind] ?? FileText;
}

function human(n: number): string {
  for (const [unit, size] of [["GB", 1024 ** 3], ["MB", 1024 ** 2], ["KB", 1024]] as const) {
    if (n >= size) return `${(n / size).toFixed(1)} ${unit}`;
  }
  return `${n} B`;
}

/**
 * The group's workspace, as a list.
 *
 * Every group has one, and this is the window into it: what the members wrote, which folder each
 * task delivered into, and a download button. Read-only on purpose — the files are the members'
 * working area, and deleting them from a side panel would be a surprising amount of power.
 */
export default function WorkspacePanel({ gid, onClose }: { gid: string; onClose: () => void }) {
  const { t } = useI18n();
  const [view, setView] = useState<WorkspaceView | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    setBusy(true);
    api.workspace(gid).then((v) => { setView(v); setErr(""); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  };
  useEffect(load, [gid]);      // eslint-disable-line react-hooks/exhaustive-deps

  const save = async (path: string, name: string) => {
    try {
      const blob = await api.workspaceBytes(gid, path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="ws-back" role="dialog" aria-modal="true" aria-label={t("Workspace")}>
      <div className="ws-panel">
        <div className="ws-head">
          <FolderOpen size={15} />
          <b>{t("Workspace")}</b>
          <span className="muted ws-path" title={view?.path}>{view?.path ?? ""}</span>
          <button className="icon-btn" onClick={load} disabled={busy} title={t("Refresh")} aria-label={t("Refresh")}>
            <RefreshCw size={14} />
          </button>
          <button className="icon-btn" onClick={onClose} title={t("Close")} aria-label={t("Close")}>
            <X size={14} />
          </button>
        </div>
        {err && <div className="err">{err}</div>}

        {view && view.tasks.length > 0 && (
          <div className="ws-section">
            <div className="ws-title">{t("Task folders")}</div>
            {view.tasks.map((task) => (
              <div key={task.path} className="ws-row">
                <FolderOpen size={14} />
                <span className="ws-name">{task.path}/</span>
                <span className="ws-meta">{t("{n} file(s)", { n: task.files })} · {human(task.bytes)}</span>
              </div>
            ))}
          </div>
        )}

        <div className="ws-section">
          <div className="ws-title">{t("Files")}</div>
          {view && view.files.length === 0 && <div className="muted ws-empty">{t("Nothing has been written here yet. Ask a member to save a file.")}</div>}
          {view?.files.map((f) => {
            const Icon = icon(f.name, f.kind);
            return (
              <div key={f.path} className="ws-row" title={f.path}>
                <Icon size={14} />
                <span className="ws-name">{f.path}</span>
                <span className="ws-meta">{human(f.size)}</span>
                <button className="icon-btn" onClick={() => void save(f.path, f.name)} title={t("Download")} aria-label={t("Download {name}", { name: f.name })}>
                  <Download size={13} />
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
