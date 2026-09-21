import { useEffect, useState } from "react";
import { api, type RoutePreview } from "./api";
import { useData } from "./data";
import { routeText } from "./lib";

/** The route chain in effect (refreshed when settings or models change), used by the status pill above the composer. */
export function useRoute() {
  const { settings, models, providers, reload } = useData();
  const [preview, setPreview] = useState<RoutePreview | null>(null);
  useEffect(() => {
    api.routePreview().then(setPreview).catch(() => undefined);
  }, [settings, models, providers]);
  const { text, offline } = routeText(preview, models);
  const toggleExternal = async () => {
    if (!settings) return;
    await api.putSettings({ external_calls_enabled: !settings.external_calls_enabled });
    await reload();
  };
  return { preview, text, offline, toggleExternal };
}
