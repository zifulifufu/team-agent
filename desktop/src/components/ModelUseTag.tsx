import { useI18n } from "../i18n";
import type { ModelUse } from "../api";
import "./../styles/models.css";

/** The badge that says a model is not a chat model.
 *
 * A gateway lists chat, image and video models on one endpoint, so the model list contains rows a
 * member cannot be pointed at — and pointing one at an image model fails only once a round has
 * already gone wrong. Saying what a row is for, on the row, is the whole difference.
 *
 * Nothing is shown for `chat`: that is the overwhelming majority, and a badge on every line would
 * hide the few that matter.
 */
export default function ModelUseTag({ use }: { use?: ModelUse }) {
  const { t } = useI18n();
  if (!use || use === "chat") return null;

  const label = use === "image" ? t("Image model")
    : use === "video" ? t("Video model")
    : t("Responses API");

  const title = use === "responses"
    ? t("This model answers on OpenAI's Responses API rather than /chat/completions, so a member cannot be pointed at it here")
    : t("Not a chat model: a member cannot be pointed at it. Use it under Permissions & control → {where}.", {
        where: use === "image" ? t("Image generation") : t("Video generation"),
      });

  return <span className="tag mp-use" title={title}>{label}</span>;
}
