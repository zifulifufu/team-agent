import { Maximize2, Minimize2, PanelLeftClose } from "lucide-react";
import type { Group } from "../../api";
import { useI18n } from "../../i18n";
import MemberDock from "./MemberDock";

/** Group roster. The member and tool sections each own their invitation control.
 * Keep the 52px header aligned with the adjacent chat and output panels. */
export default function MemberRail({ group, count, maximized, onMaximize, onOpenAll, onClose }: {
  /** The group this column lists. Null until a group chat has been opened, which is said out loud
   * rather than shown as an empty column. */
  group: Group | null;
  /** How many members that group has — the *same* value the sidebar prints beside "Members", passed
   * in rather than recomputed here, so the number and the list behind it cannot come from two
   * different readings of the group. */
  count: number;
  /** 是否已经占满主区域。 */
  maximized: boolean;
  onMaximize: () => void;
  /** Every member in the app, with the form for making and editing them */
  onOpenAll: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  return (
    <aside className={"mrail" + (maximized ? " maximized" : "")}
      aria-label={group ? t("Members of \"{group}\"", { group: group.name }) : t("Members")}>
      <div className="mrail-head">
        {/* ⚠️ 这里**刻意没有前导图标**:它是 `Users` 那一个,而这一栏从头到尾讲的就是一件事,标题
            已经说清了。272px 的栏头放不下「图标 + 两行标题 + 两个控件」,而先被挤掉的是**副标题**
            (它比标题更该读全:「本群成员 · 12」就是那个数的出处)。 */}
        <span className="mrail-titles">
          <span className="mrail-title">{group ? group.name : t("Members")}</span>
          <span className="mrail-sub">
            {group ? t("Members and tools · {n}", { n: count }) : t("No group chat is open")}
          </span>
        </span>
        <span className="grow" />
        <button className="icon-btn tiny rail-max" onClick={onMaximize}
          title={t(maximized ? "Back to the chat" : "Fill the window with this panel")}
          aria-label={t(maximized ? "Back to the chat" : "Fill the window with this panel")}
          aria-pressed={maximized}>
          {maximized ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
        {/* 收起:一个**带分隔线的方框**(不是箭头)。用户给的参考图里这两个控件就是
            「斜向外的双箭头」+「方框里有竖线」,后者是「把一个侧栏收掉」的通用画法 ——
            箭头在这里会被读成「返回」,而它其实只是把这一栏收起来。 */}
        <button className="icon-btn tiny rail-close" title={t("Collapse the member column")}
          aria-label={t("Collapse the member column")} onClick={onClose}>
          <PanelLeftClose size={15} />
        </button>
      </div>
      <div className="mrail-body">
        {group ? <MemberDock group={group} /> : (
          <div className="mrail-empty">{t("Open a group chat and its members show up here — you can add more at any time")}</div>
        )}
      </div>
      <div className="mrail-foot">
        <button className="btn small" onClick={onOpenAll}>{t("All members and tools…")}</button>
      </div>
    </aside>
  );
}
