import type { HealthState, Model, ModelHealth, Provider, Settings } from "../api";
import { useI18n } from "../i18n";

export interface Availability {
  state: HealthState;
  label: string;
  detail?: string;
  blocked: boolean;
}

/** Configuration is checked before cached connectivity. Never promote an unchecked model to OK. */
export function modelAvailability(model: Pick<Model, "enabled" | "use" | "setup_required">,
  provider: Provider, health?: ModelHealth, settings?: Settings | null): Availability {
  if (!provider.enabled || !model.enabled) return { state: "off", label: "Disabled", blocked: true };
  if (model.use === "responses") return { state: "off", label: "Unsupported chat interface", blocked: true };
  if (model.setup_required) return { state: "off", label: "Workflow setup needed", blocked: true };
  if (!provider.is_local && !(provider.credentials_ready ?? provider.has_key)) return { state: "off", label: "No API key", blocked: true };
  if (!provider.is_local && settings?.external_calls_enabled === false) return { state: "off", label: "Outbound calls are off", blocked: true };
  if ((model.use === "image" && settings?.image_enabled === false)
    || (model.use === "video" && settings?.video_enabled === false)) return { state: "off", label: "Generation is switched off", blocked: true };
  if (health?.state === "off") return { state: "off", label: "Not ready", detail: health.detail, blocked: true };
  if (health?.stale) return { state: "unknown", label: "Needs re-check", detail: health.detail, blocked: false };
  if (health?.state === "ok") return { state: "ok", label: "Connected", detail: health.detail, blocked: false };
  if (health?.state === "bad") return { state: "bad", label: "Cannot connect", detail: health.detail, blocked: false };
  if (health?.state === "limited") return { state: "limited", label: "Temporarily unavailable", detail: health.detail, blocked: false };
  return { state: "unknown", label: "Not checked", blocked: false };
}

export default function ModelAvailability({ value }: { value: Availability }) {
  const { t } = useI18n();
  return <span className={"model-state model-state-" + value.state} title={value.detail || t(value.label)}>
    <i aria-hidden="true" />{t(value.label)}
  </span>;
}
