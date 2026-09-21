import { useState } from "react";
import { UserPlus } from "lucide-react";
import type { Group } from "../../api";
import { useOutside } from "../../ui";
import MemberAdder from "./MemberAdder";
import { useI18n } from "../../i18n";

/** The "+" button: opens the add-member panel (drops down; a click outside closes it). */
export default function AddMemberButton({ group, align = "left", label, className = "icon-btn tiny" }: { group: Group; align?: "left" | "right"; label?: string; className?: string }) {
  const { t } = useI18n();
  const text = label ?? t("Add group member");
  const [open, setOpen] = useState(false);
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  return (
    <div className="madd-wrap" ref={ref}>
      <button className={className + (open ? " on" : "")} title={text} aria-label={text} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <UserPlus size={15} />
      </button>
      {open && (
        <div className={"madd-pop " + align} role="dialog" aria-label={t("{label}: {group}", { label: text, group: group.name })}>
          <div className="madd-title">{t("Add to \"{group}\"", { group: group.name })}</div>
          <div className="madd-scroll"><MemberAdder group={group} /></div>
        </div>
      )}
    </div>
  );
}
