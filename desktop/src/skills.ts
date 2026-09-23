import type { Skill } from "./api";

/** The sections skills are filed under, in the order the backend already sorted them in.
 *
 * A category is a stable key on the wire — the label is interface text, so it lives here as the
 * English string the dictionary is keyed by and is resolved with `t()`. Nothing on this side
 * decides which section a skill is in; the backend does, once, so that the skills page, the member
 * editor and the group panel cannot each file it differently.
 */
export const SKILL_CATEGORY_LABEL: Record<string, string> = {
  writing: "Writing",
  video: "Video",
  research: "Research",
  analysis: "Data & analysis",
  facilitation: "Meetings & discussion",
  code: "Code & tooling",
  translation: "Translation",
  meta: "Making skills & plugins",
  imported: "Imported from elsewhere",
  other: "Other",
};

export const skillCategoryLabel = (key: string): string =>
  SKILL_CATEGORY_LABEL[key] ?? SKILL_CATEGORY_LABEL.other;

/** Group a list that is already in category order, without re-imposing an order of its own. */
export function groupByCategory(skills: Skill[]): { key: string; items: Skill[] }[] {
  const groups: { key: string; items: Skill[] }[] = [];
  for (const s of skills) {
    const key = s.category || "other";
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(s);
    else groups.push({ key, items: [s] });
  }
  return groups;
}

/** The folder a skill is stored in, when it is not the name shown.
 *
 * A skill whose built-in text has been rewritten by hand keeps the spelling it was written under,
 * so two folders can carry the same title. Showing the stored one is how the reader tells which is
 * which instead of wondering whether the list is broken again.
 */
export function storedFolder(skill: Skill): string {
  const folder = (skill.path || "").split("/").filter(Boolean).slice(-2, -1)[0] ?? "";
  return folder === skill.name ? "" : folder;
}

export const matchesSkill = (skill: Skill, query: string): boolean => {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return `${skill.name} ${skill.description} ${skill.category} ${storedFolder(skill)}`
    .toLowerCase().includes(q);
};
