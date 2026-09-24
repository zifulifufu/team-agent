import { ChevronLeft, Users } from "lucide-react";
import type { Group } from "../../api";
import { useI18n } from "../../i18n";
import AddMemberButton from "./AddMemberButton";
import MemberDock from "./MemberDock";

/**
 * The group you are in, and who is in it — a column of its own, immediately right of the sidebar.
 *
 * It used to be a list nested *inside* the sidebar, under the "Members" entry. That put two member
 * lists on screen at once with nothing to tell them apart: the sidebar's (this group's roster, with
 * the group's count printed next to the entry) and the Members page's (every member in the app,
 * which is where that same entry took you). Worse, the sidebar's one kept showing the last group
 * opened no matter which page you were on, so it disagreed with everything around it. Two lists,
 * both called "Members", different contents — and the sidebar read as one panel with a foreign list
 * growing inside it.
 *
 * So: the sidebar is navigation only, and this is the single place a group's roster is shown. The
 * count here is the same number the sidebar prints, because it is the same value — and it sits
 * beside the list rather than inside the sidebar, which is what keeps the left panel readable.
 * Every member in the app is one link away, in this column's footer.
 */
export default function MemberRail({ group, count, onOpenAll, onClose }: {
  /** The group this column lists. Null until a group chat has been opened, which is said out loud
   *  rather than shown as an empty column. */
  group: Group | null;
  /** How many members that group has — the *same* value the sidebar prints beside "Members", passed
   *  in rather than recomputed here, so the number and the list behind it cannot come from two
   *  different readings of the group. */
  count: number;
  /** Every member in the app, with the form for making and editing them */
  onOpenAll: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  return (
    <aside className="mrail" aria-label={group ? t("Members of \"{group}\"", { group: group.name }) : t("Members")}>
      <div className="mrail-head">
        <Users size={15} aria-hidden />
        <span className="mrail-titles">
          <span className="mrail-title">{group ? group.name : t("Members")}</span>
          <span className="mrail-sub">
            {group ? t("This group's members · {n}", { n: count })
                   : t("No group chat is open")}
          </span>
        </span>
        <span className="grow" />
        {group && <AddMemberButton group={group} />}
        <button className="icon-btn tiny" title={t("Collapse the member column")} aria-label={t("Collapse the member column")} onClick={onClose}>
          <ChevronLeft size={15} />
        </button>
      </div>
      <div className="mrail-body">
        {group ? <MemberDock group={group} /> : (
          <div className="mrail-empty">{t("Open a group chat and its members show up here — you can add more at any time")}</div>
        )}
      </div>
      <div className="mrail-foot">
        <button className="btn small" onClick={onOpenAll}>{t("Every member…")}</button>
      </div>
    </aside>
  );
}
