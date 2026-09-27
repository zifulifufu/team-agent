import { useState } from "react";
import { createPortal } from "react-dom";
import { UserPlus } from "lucide-react";
import type { Group } from "../../api";
import { Modal } from "../../ui";
import MemberAdder from "./MemberAdder";
import { useI18n } from "../../i18n";

/** The section "+" opens a shared invitation dialog outside the scrolling roster. */
export default function AddMemberButton({ group, label, section = "members", className = "icon-btn tiny" }: { group: Group; align?: "left" | "right"; label?: string; section?: "members" | "tools"; className?: string }) {
  const { t } = useI18n();
  const text = label ?? t(section === "tools" ? "Add group tool" : "Add group member");
  const [open, setOpen] = useState(false);
  return (
    <div className="madd-wrap">
      <button className={className + (open ? " on" : "")} title={text} aria-label={text} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <UserPlus size={15} />
      </button>
      {open && createPortal(<Modal title={t("Add to \"{group}\"", { group: group.name })} onClose={() => setOpen(false)} wide>
        <MemberAdder group={group} initialSection={section} />
      </Modal>, document.body)}
    </div>
  );
}
