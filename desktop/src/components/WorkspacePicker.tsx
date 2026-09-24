import { useEffect, useRef, useState } from "react";
import { ChevronDown, Download, FileSpreadsheet, FileText, FileVideo, FolderOpen, FolderTree, RefreshCw, X } from "lucide-react";
import { api, type Group, type WorkspaceView } from "../api";
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

/** The last segment of a path — what to call a folder in a chip that has no room for the whole thing. */
function leaf(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

/**
 * The group's workspace: a chip in the row under the message box, and a list of what is in it.
 *
 * It used to be a modal that covered the middle of the window, opened from an icon in the header.
 * That put a read-only file list in the place where the conversation had just been, for something
 * people look at *while* talking to the group — and the header already had five icons. The chip says
 * which folder the members are writing into, right where the rest of the send controls are, and the
 * list opens upward out of the composer without covering the conversation.
 *
 * The one editable thing is still where the folder is: that decides which of the user's own projects
 * the members are let loose in. Nothing is moved when it changes — files already written stay where
 * they were written, which is the honest behaviour for a control that only redirects future work.
 */
export default function WorkspacePicker({ gid, group }: {
  gid: string;
  /** The group, for the folder it was told to use. Its resolved path comes with the refresh. */
  group: Group;
}) {
  const { t } = useI18n();
  const { reloadGroups } = useData();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<WorkspaceView | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  const load = () => {
    setBusy(true);
    api.workspace(gid).then((v) => { setView(v); setErr(""); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  };
  // Fetched when it is opened, not on mount: the chip's label comes from the group itself, so a chat
  // that never opens this costs one request less than it used to.
  useEffect(() => { if (open) load(); }, [open, gid]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

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
  // Only the desktop shell has a native folder dialog; in a browser the button is not drawn at all
  // rather than left there doing nothing.
  const canPickFolder = !!window.teamAgent?.pickFolder;

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

  // The resolved path is the truth once it has arrived; before that, the group's own answer is what
  // the backend is working from, so the chip never says "app-managed" for a folder the user picked.
  const full = view?.path || group.workspace_path || group.workspace || "";
  const label = group.workspace ? leaf(group.workspace) : t("Managed by the app");

  return (
    <div className="wspick" ref={wrap}>
      <button className={"wspick-btn" + (open ? " on" : "")} aria-expanded={open} aria-haspopup="dialog"
        title={full || t("Workspace")} aria-label={t("Workspace")}
        onClick={() => setOpen((o) => !o)}>
        <FolderOpen size={13} />
        <span className="wspick-name">{label}</span>
        <ChevronDown size={12} className="wspick-chev" />
      </button>

      {open && (
        // Absolute inside the composer (the bar itself is not positioned), so it opens upward out of
        // the whole box exactly like the @ picker does — and is bounded the same way, because a list
        // anchored to the bottom of the window has nowhere to grow but off the top of the screen.
        <div className="wspick-pop" role="dialog" aria-label={t("Workspace")}>
          <div className="ws-head">
            <FolderOpen size={15} />
            <b>{t("Workspace")}</b>
            <span className="muted ws-path" title={full}>{full}</span>
            <button className="icon-btn" onClick={load} disabled={busy} title={t("Refresh")} aria-label={t("Refresh")}>
              <RefreshCw size={14} />
            </button>
            <button className="icon-btn" onClick={() => setOpen(false)} title={t("Close")} aria-label={t("Close")}>
              <X size={14} />
            </button>
          </div>
          <div className="wspick-body">
            {err && <div className="err">{err}</div>}

            <div className="ws-where">
              <span className="muted small">
                {view?.managed === false
                  ? t("A folder you picked — the members work in it directly.")
                  : t("Managed by the app. Pick a folder of your own to have the members work in it instead.")}
              </span>
              {canPickFolder && (
                <button className="btn small" disabled={busy} onClick={() => void choose()}>
                  <FolderTree size={13} /> {t("Choose folder")}
                </button>
              )}
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
              {/* The list arrives a beat after the panel does (the backend walks the folder), and an
                  empty-looking panel in that beat reads as "nothing has been written here yet" —
                  which is a different, and wrong, message. */}
              {!view && !err && <div className="muted ws-empty">{t("Loading…")}</div>}
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
      )}
    </div>
  );
}
