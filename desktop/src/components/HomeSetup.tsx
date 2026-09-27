import { useEffect, useRef, useState } from "react";
import { ChevronDown, FolderOpen, FolderTree, Users, Wand2, X } from "lucide-react";
import { mayHost, type Agent, type TeamAdvice } from "../api";
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
/** 建议阵容：按任务文本算出来的该进这个群的人。
 *
 *  它**不写库** —— 群还没建，提前把专家建成成员会留下一堆没人要的成员行。所以这里列出的专家
 *  只是"该请的人"，点「用这个阵容」时才真的创建（`api.agentFromPreset`）。
 *
 *  三个部分缺一不可：`reads` 说明它从任务里读到了什么（否则一套阵容没法反驳，也就没人信）、
 *  成员 chip 的 `title` 是每个人的理由、专家 chip 带一个「新」标记（它们现在还不是成员）。
 */
function AdviceBlock({ advice, picked, skipped, onSkip, onAdopt }: {
  advice: TeamAdvice;
  picked: string[];
  /** Chips the user took out of the suggestion (member id or expert key). Kept by the caller so the
   *  removal survives this component re-rendering, and cleared when a *new* suggestion arrives —
   *  a skip that outlived the lineup it was made against would silently drop somebody from the next
   *  suggestion too. */
  skipped: string[];
  onSkip: (key: string) => void;
  onAdopt: (a: TeamAdvice) => void;
}) {
  const { t } = useI18n();
  const reads = [...advice.reads.concepts, ...advice.reads.needs].filter(Boolean);
  const left = advice.members.filter((m) => !skipped.includes(m.id)).length
    + advice.experts.filter((e) => !skipped.includes(e.key)).length;
  if (advice.members.length === 0 && advice.experts.length === 0) return null;
  return (
    <div className="ta-advice">
      <div className="ta-head">
        <Wand2 size={13} aria-hidden />
        <b>{t("Suggested for this task")}</b>
        {reads.length > 0 && <span className="ta-reads" title={t("What it read out of your description")}>{reads.join(" · ")}</span>}
        {/* Nothing left after the removals: there is no lineup to use, and the button has to say so
            rather than create a group with nobody in it. */}
        <button className="btn small ta-use" disabled={left === 0} onClick={() => onAdopt(advice)}>
          {t("Use this lineup")}
        </button>
      </div>
      <div className="ta-chips">
        {advice.members.map((m) => (
          <span key={m.id} className={"ta-chip" + (picked.includes(m.id) ? " on" : "") + (skipped.includes(m.id) ? " off" : "")}
            title={m.why}>
            <span className="ta-ava">{m.avatar}</span>{m.name}
            <RemoveChip label={t("Take {name} out of the suggestion", { name: m.name })} onRemove={() => onSkip(m.id)} />
          </span>
        ))}
        {advice.experts.map((e) => (
          <span key={e.key} className={"ta-chip ta-new" + (skipped.includes(e.key) ? " off" : "")} title={e.why}>
            <span className="ta-ava">{e.avatar}</span>{e.name_zh}
            <span className="ta-flag">{t("not a member yet")}</span>
            <RemoveChip label={t("Take {name} out of the suggestion", { name: e.name_zh })} onRemove={() => onSkip(e.key)} />
          </span>
        ))}
        {/* 只给名字的理由在 title 里，而 touch 与键盘都拿不到 title —— 所以建议照进 aria-label，
            读屏用户至少能听到"为什么是这个人"。 */}
        {advice.members.some((m) => m.why) && (
          <span className="ta-whys">
            {advice.members.filter((m) => m.why && !skipped.includes(m.id)).map((m) => `${m.name}:${m.why}`).join(" · ")}
          </span>
        )}
      </div>
    </div>
  );
}

/** The ✕ on one suggestion chip.
 *
 *  A chip you can only accept is not a suggestion, it is an order. The lineup is a starting point
 *  somebody has to be able to edit — take the picture model out because this task does not need one,
 *  or the expert because you already know that specialty — and editing it has to be possible without
 *  ticking around it: with several suggestions in one row there is no "untick" gesture on a chip that
 *  is not a member yet at all. */
function RemoveChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <button className="ta-x" title={label} aria-label={label}
      onClick={(e) => { e.stopPropagation(); onRemove(); }}>
      <X size={11} aria-hidden />
    </button>
  );
}

export default function HomeSetup({ workspace, onWorkspace, members, picked, onToggle, onPicked, suggested, sceneName, hostId, advice, adviceBusy, skipped, onSkip, onAdopt }: {
  /** The folder the new group was told to use, or "" for the app-managed one */
  workspace: string;
  onWorkspace: (path: string) => void;
  /** Everybody who could join */
  members: Agent[];
  /** The ids ticked so far, in the order they were ticked (the first one that can hold the chair
   *  becomes the host — see `hostId`) */
  picked: string[];
  onToggle: (id: string) => void;
  /** Tick a whole lineup at once */
  onPicked: (ids: string[]) => void;
  /** The scene's own lineup, offered as a suggestion rather than applied */
  suggested: Agent[];
  /** What that lineup is called, for the button's label */
  sceneName: string;
  /** Who will actually run this group — the first ticked member that can, or null when nobody
   *  ticked can (only generators and external agents were chosen). The caller decides it, because
   *  the same answer is what gets sent to the backend. */
  hostId: string | null;
  /** The lineup suggested for the task as typed, or null before there is enough to go on. */
  advice: TeamAdvice | null;
  /** Chips taken out of the suggestion (member id or expert key) — see `AdviceBlock`. */
  skipped: string[];
  onSkip: (key: string) => void;
  /** A suggestion is being computed. Shown so the panel does not look frozen on a slow machine. */
  adviceBusy: boolean;
  /** Take the suggested lineup: the caller ticks those members and creates the named experts. */
  onAdopt: (a: TeamAdvice) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState<"ws" | "members" | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  // How tall this panel is allowed to be, measured rather than guessed. It hangs off the top of the
  // composer (`bottom:100%`), so a tall panel grows *off the top of the window* — measured at 34
  // members: 368px of chips anchored to a composer whose top sat at y=306, i.e. the first 62px
  // (the panel's own header, and the first rows of names) were above the viewport and unreachable.
  // `60vh` does not help: it bounds the panel against the *viewport*, and the space this panel
  // actually has is the space above the composer. So the room is read off the composer's own top
  // edge, with an 8px gap at the top of the window.
  const [room, setRoom] = useState(0);
  useEffect(() => {
    if (!open) return;
    const bar = wrap.current?.closest(".composer") as HTMLElement | null;
    const top = bar ? bar.getBoundingClientRect().top : window.innerHeight;
    setRoom(Math.max(140, Math.round(top - 24)));
  }, [open]);

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
        <div className="wspick-pop hsetup-wide" role="dialog" aria-label={t("Members")}
          style={room ? { maxHeight: Math.min(room, 520) } : undefined}>
          <div className="ws-head">
            <Users size={15} />
            <b>{t("Members")}</b>
            <span className="grow" />
            <button className="icon-btn" onClick={() => setOpen(null)} title={t("Close")} aria-label={t("Close")}>
              <X size={14} />
            </button>
          </div>
          <div className="wspick-body">
            {adviceBusy && !advice && (
              <div className="ta-busy">{t("Reading your description…")}</div>
            )}
            {advice && <AdviceBlock advice={advice} picked={picked} skipped={skipped} onSkip={onSkip} onAdopt={onAdopt} />}
            <div className="ng-members">
              {members.map((a) => (
                <button
                  key={a.id}
                  className={"ng-mem" + (picked.includes(a.id) ? " on" : "") + (mayHost(a) ? "" : " solo")}
                  aria-pressed={picked.includes(a.id)}
                  title={mayHost(a) ? a.name : t("{name} cannot run the group chat — it can join and be addressed, but it does not hold a conversation. The first member that can will be the host.", { name: a.name })}
                  onClick={() => onToggle(a.id)}
                >
                  <span className="ng-ava">{a.avatar}</span>{a.name}
                  {hostId === a.id && <span className="chip host">{t("Host")}</span>}
                  {/* Ticking is already a toggle, but a tick has to be *findable* to be undone —
                      and the ticked chips are the ones people come back to. The ✕ is the same
                      action as clicking the chip, named. */}
                  {picked.includes(a.id) && (
                    <span className="ng-x" role="button" tabIndex={-1}
                      title={t("Take {name} out of this group", { name: a.name })}
                      aria-label={t("Take {name} out of this group", { name: a.name })}
                      onClick={(ev) => { ev.stopPropagation(); onToggle(a.id); }}>
                      <X size={11} aria-hidden />
                    </span>
                  )}
                  {/* Why this one is marked at all. Without it the chip reads as "lesser", and the
                      reason (a generator makes things; an external agent works on its own) is not
                      something the user can guess from an icon. */}
                  {!mayHost(a) && <span className="ng-solo">{t("no chair")}</span>}
                </button>
              ))}
            </div>
            {members.length === 0 && (
              <div className="muted ws-empty">{t("No members yet — open the member column beside the sidebar and create one there.")}</div>
            )}
            {/* The suggestion shows while nothing is ticked — it is a starting point, and offering it
                over a selection already made would be offering to replace it. */}
            <div className="hsetup-note">
              <span className="ng-hint">{t("Pick who is in this group chat. The first one who can run it becomes the host.")}</span>
              {/* Said before the send, not after it: with nobody able to hold the chair the server
                  refuses the whole group, and the picker has to be the place that explains why. */}
              {picked.length > 0 && !hostId && (
                <span className="ng-nohost">{t("Nobody here can run a group chat yet — a picture or video model makes things, and an external agent works on its own. Add a member that can hold a conversation.")}</span>
              )}
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
