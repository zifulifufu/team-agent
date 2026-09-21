import { useState } from "react";
import { UserPlus } from "lucide-react";
import type { Group } from "../../api";
import { useOutside } from "../../ui";
import MemberAdder from "./MemberAdder";

/** 「+」按钮:点开就是添加成员面板(向下展开,点面板外关闭)。 */
export default function AddMemberButton({ group, align = "left", label = "添加群成员", className = "icon-btn tiny" }: { group: Group; align?: "left" | "right"; label?: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  return (
    <div className="madd-wrap" ref={ref}>
      <button className={className + (open ? " on" : "")} title={label} aria-label={label} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <UserPlus size={15} />
      </button>
      {open && (
        <div className={"madd-pop " + align} role="dialog" aria-label={`${label}:${group.name}`}>
          <div className="madd-title">添加到「{group.name}」</div>
          <div className="madd-scroll"><MemberAdder group={group} /></div>
        </div>
      )}
    </div>
  );
}
