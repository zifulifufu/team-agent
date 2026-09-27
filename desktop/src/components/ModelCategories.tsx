import { useI18n } from "../i18n";
import type { ModelUse } from "../api";
import "../styles/models.css";

export const MODEL_CATEGORIES = [
  { id: "all", label: "All types" },
  { id: "chat", label: "Chat models" },
  { id: "reasoning", label: "Reasoning models" },
  { id: "coding", label: "Coding" },
  { id: "writing", label: "Writing models" },
  { id: "multimodal", label: "Image understanding" },
  { id: "image", label: "Image generation" },
  { id: "video", label: "Video generation" },
  { id: "responses", label: "Other interfaces" },
] as const;
export type ModelCategory = typeof MODEL_CATEGORIES[number]["id"];
export function matchesCategory(model: { use: ModelUse; strengths: string[] }, category: ModelCategory): boolean {
  if (category === "all") return true;
  if (["chat", "image", "video", "responses"].includes(category)) return model.use === category;
  return model.use === "chat" && model.strengths.includes(category);
}

/** Capability filters deliberately overlap; image understanding is separate from generating images. */
export default function ModelCategories({ value, onChange, models, section }: {
  value: ModelCategory; onChange: (value: ModelCategory) => void;
  models: { use: ModelUse; strengths: string[] }[]; section?: "members" | "tools";
}) {
  const { t } = useI18n();
  const categories = MODEL_CATEGORIES.filter(({ id }) => !section || id === "all"
    || (section === "tools" ? ["image", "video"].includes(id) : !["image", "video"].includes(id)));
  return <div className="model-categories" role="group" aria-label={t("Filter by model type")}>
    {categories.map(({ id, label }) => {
      const count = models.filter((m) => matchesCategory(m, id)).length;
      return <button key={id} type="button" aria-pressed={value === id}
        disabled={!count && value !== id} onClick={() => onChange(id)}>
        {t(label)}<span>{count}</span>
      </button>;
    })}
  </div>;
}
