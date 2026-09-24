import { useEffect, useRef, useState } from "react";
import { ChevronDown, FolderOpen, FolderTree, Users, X } from "lucide-react";
import type { Agent } from "../api";
import { useI18n } from "../i18n";
import "../styles/files.css";

/** The last segment of a path — what to call a folder in a chip with no room for the whole thing. */
function leaf(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

/**
 * The two things a new group needs — where it works and who is in it — as chips in the row under the
 * message box, instead of a block of rows above it.
 *
 * That block was the taller half of the home screen: 23 member chips wrapping across six rows inside
 * a 276px card, which pushed the box down the window and left the description the user came to write
 * with the least room on the page. The picker itself was fine; its *place* was wrong — the same
 * complaint, and the same answer, as the workspace before it (a chip down here, the list opens
 * upward). Nothing is lost: the panel carries every chip, the host rule, and the scene's suggested
 * lineup; only the space it takes when closed changed, from 227px to one row.
 *
 * The two chips share one `open` value rather than each owning its own, because they sit 6px apart
 * and both hang over the same part of the screen — two panels open at once would overlap.
 */
export default function HomeSetup({ workspace, onWorkspace, members, picked, onToggle, onPicked, suggested, sceneName }: {
  /** The folder the new group was told to use, or "" for the app-managed one */
  workspace: string;
  onWorkspace: (path: string) => void;
  /** Everybody who could join */
  members: Agent[];
  /** The ids ticked so far, in the order they were ticked (the first one becomes the host) */
  picked: string[];
  onToggle: (id: string) => void;
  /** Tick a whole lineup at once */
  onPicked: (ids: string[]) => void;
  /** The scene's own lineup, offered as a suggestion rather than applied */
  suggested: Agent[];
  /** What that lineup is called, for the button's label */
  sceneName: string;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState<"ws" | "members" | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(null);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(null); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  // The folder chooser is a native dialog, so it only exists inside the desktop shell. Without it the
  // button is not offered at all — the established rule in this app — rather than sitting there doing
  // nothing when the page is opened in a browser.
  const canPickFolder = !!window.teamAgent?.pickFolder;
  const choose = async () => {
    const dir = await window.teamAgent?.pickFolder?.();
    if (dir) onWorkspace(dir);
  };

  const wsLabel = workspace ? leaf(workspace) : t("Managed by the app");
  const memLabel = picked.length ? `${t("Members")} · ${picked.length}` : t("Choose members");

  return (
    <div className="hsetup" ref={wrap}>
      <button className={"wspick-btn" + (open === "ws" ? " on" : "")} aria-expanded={open === "ws"}
        aria-haspopup="dialog" aria-label={t("Workspace")}
        title={workspace || t("Managed by the app, under its own data folder")}
        onClick={() => setOpen((o) => (o === "ws" ? null : "ws"))}>
        <FolderOpen size={13} />
        <span className="wspick-name">{wsLabel}</span>
        <ChevronDown size={12} className="wspick-chev" />
      </button>

      <button className={"wspick-btn" + (open === "members" ? " on" : "")} aria-expanded={open === "members"}
        aria-haspopup="dialog" aria-label={t("Members")}
        title={picked.length ? members.filter((m) => picked.includes(m.id)).map((m) => m.name).join(", ") : t("Choose members")}
        onClick={() => setOpen((o) => (o === "members" ? null : "members"))}>
        <Users size={13} />
        <span className="wspick-name">{memLabel}</span>
        <ChevronDown size={12} className="wspick-chev" />
      </button>

      {open === "ws" && (
        // Absolute inside the composer (the bar itself is not positioned), so it clears the whole box
        // and opens upward — same anchoring, and same bound, as the chip in a group chat.
        <div className="wspick-pop" role="dialog" aria-label={t("Workspace")}>
          <div className="ws-head">
            <FolderOpen size={15} />
            <b>{t("Workspace")}</b>
            <span className="muted ws-path" title={workspace || t("Managed by the app, under its own data folder")}>
              {workspace || t("Managed by the app, under its own data folder")}
            </span>
            <button className="icon-btn" onClick={() => setOpen(null)} title={t("Close")} aria-label={t("Close")}>
              <X size={14} />
            </button>
          </div>
          <div className="wspick-body">
            <div className="ws-where">
              <span className="muted small">
                {workspace
                  ? t("A folder you picked — the members work in it directly.")
                  : t("Managed by the app. Pick a folder of your own to have the members work in it instead.")}
              </span>
              {canPickFolder && (
                <button className="btn small" onClick={() => void choose()}>
                  <FolderTree size={13} /> {t("Choose folder")}
                </button>
              )}
              {workspace !== "" && (
                <button className="btn small" onClick={() => onWorkspace("")} title={t("Go back to the folder the app manages")}>
                  {t("Use the default")}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {open === "members" && (
        <div className="wspick-pop hsetup-wide" role="dialog" aria-label={t("Members")}>
          <div className="ws-head">
            <Users size={15} />
            <b>{t("Members")}</b>
            <span className="grow" />
            <button className="icon-btn" onClick={() => setOpen(null)} title={t("Close")} aria-label={t("Close")}>
              <X size={14} />
            </button>
          </div>
          <div className="wspick-body">
            <div className="ng-members">
              {members.map((a) => (
                <button
                  key={a.id}
                  className={"ng-mem" + (picked.includes(a.id) ? " on" : "")}
                  aria-pressed={picked.includes(a.id)}
                  onClick={() => onToggle(a.id)}
                >
                  <span className="ng-ava">{a.avatar}</span>{a.name}
                  {picked[0] === a.id && <span className="chip host">{t("Host")}</span>}
                </button>
              ))}
            </div>
            {members.length === 0 && (
              <div className="muted ws-empty">{t("No members yet — open the member column beside the sidebar and create one there.")}</div>
            )}
            {/* The suggestion shows while nothing is ticked — it is a starting point, and offering it
                over a selection already made would be offering to replace it. */}
            <div className="hsetup-note">
              <span className="ng-hint">{t("Pick who is in this group chat. The first one becomes the host.")}</span>
              {picked.length === 0 && suggested.length > 0 && (
                <button className="ng-suggest" onClick={() => onPicked(suggested.map((a) => a.id))}>
                  {t("Use the {scene} lineup: {names}", { scene: sceneName, names: suggested.map((a) => a.name).join(", ") })}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
