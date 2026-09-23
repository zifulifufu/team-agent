import { useEffect, useState } from "react";
import { Download, FileSpreadsheet, FileText, FileVideo, FolderOpen, FolderTree, RefreshCw, X } from "lucide-react";
import { api, type WorkspaceView } from "../api";
import { useData } from "../data";
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
 *
 * The one thing that *is* editable is where the folder is, because that decides which of the
 * user's own projects the members are let loose in: pick one at the top, or hand it back to the
 * app-managed folder. Nothing is moved between the two — files already written stay where they
 * were written, which is the honest thing for a control that only redirects future work.
 */
export default function WorkspacePanel({ gid, onClose }: { gid: string; onClose: () => void }) {
  const { t } = useI18n();
  const { reloadGroups } = useData();
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

  const setWorkspace = async (path: string) => {
    setBusy(true);
    setErr("");
    try {
      await api.patchGroup(gid, { workspace: path });
      await reloadGroups();     // the group's own view of the path changed too
      load();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const choose = async () => {
    const dir = await window.teamAgent?.pickFolder?.();
    if (dir) await setWorkspace(dir);
  };

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

        <div className="ws-where">
          <span className="muted small">
            {view?.managed === false
              ? t("A folder you picked — the members work in it directly.")
              : t("Managed by the app. Pick a folder of your own to have the members work in it instead.")}
          </span>
          <button className="btn small" disabled={busy} onClick={() => void choose()}>
            <FolderTree size={13} /> {t("Choose folder")}
          </button>
          {view?.managed === false && (
            <button className="btn small" disabled={busy} onClick={() => void setWorkspace("")}>
              {t("Use the default")}
            </button>
          )}
        </div>

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
