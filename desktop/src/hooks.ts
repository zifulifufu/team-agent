import { useEffect, useState } from "react";
import { api, type RoutePreview } from "./api";
import { useData } from "./data";
import { routeText } from "./lib";

/** 当前生效的路由链(随设置/模型变化刷新),供输入框上的状态胶囊使用。 */
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
