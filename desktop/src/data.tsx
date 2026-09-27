import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type Agent, type Group, type Model, type ModelHealth, type Provider, type Settings, type ZoneRow } from "./api";

interface Data {
  online: boolean;
  agents: Agent[];
  groups: Group[];
  providers: Provider[];
  models: Model[];
  settings: Settings | null;
  /** 侧栏那一条专区的清单。**只有身份** —— 每一块的内容由专区页面自己去读。 */
  zones: ZoneRow[];
  /** 用户自己写的专区文件读不了的，点名带出来 */
  zoneErrors: { file: string; why: string }[];
  reload: () => Promise<void>;
  /** After the whole database is replaced (restoring a backup): re-read everything and let the pages in the main area reload, rather than leaving stale content behind */
  refreshAll: () => Promise<void>;
  /** Incremented by every refreshAll; App uses it as the key of the main area */
  epoch: number;
  reloadGroups: () => Promise<void>;
  /** Number of unhandled update notices (app / model catalog / skills / plugins / new models), for the sidebar badge */
  updateCount: number;
  /** Of those, the ones about the app itself — they are shown on their own settings page (Software update) */
  appUpdateCount: number;
  reloadUpdates: () => Promise<void>;
  /** Model connectivity lights: model_id → status */
  health: Record<string, ModelHealth>;
  reloadHealth: () => Promise<void>;
  /** Run a check now: no ids means all of them; cloud=false probes only local services (no tokens spent) */
  checkHealth: (ids?: string[], cloud?: boolean) => Promise<void>;
}

const Ctx = createContext<Data | null>(null);

export function DataProvider({ children }: { children: ReactNode }) {
  const [online, setOnline] = useState(false);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [zones, setZones] = useState<ZoneRow[]>([]);
  const [zoneErrors, setZoneErrors] = useState<{ file: string; why: string }[]>([]);
  const [updateCount, setUpdateCount] = useState(0);
  const [appUpdateCount, setAppUpdateCount] = useState(0);
  const [health, setHealth] = useState<Record<string, ModelHealth>>({});
  const [epoch, setEpoch] = useState(0);

  const reload = useCallback(async () => {
    try {
      const [a, g, p, s, h, z] = await Promise.all([
        api.agents(), api.groups(), api.providers(), api.settings(),
        api.modelsHealth().catch(() => null), // Changing a key or an enabled flag has to move the lights too
        // ⚠️ `.catch` 而不是让它把 `reload` 一起拖垮：这一个接口挂掉不该让整个应用显示成「后台没连上」
        // —— 那会把「侧栏少了一栏」升级成「整个应用都不可用」，而这两件事的下一步完全不同。
        api.zones().catch(() => null),
      ]);
      if (h) setHealth(h.health);
      if (z) { setZones(z.zones); setZoneErrors(z.errors); }
      setAgents(a);
      setGroups(g);
      setProviders(p);
      setSettings(s);
      setOnline(true);
    } catch {
      setOnline(false);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await reload();
    setEpoch((e) => e + 1);
  }, [reload]);

  const reloadGroups = useCallback(async () => {
    try {
      setGroups(await api.groups());
    } catch {
      /* ignore */
    }
  }, []);

  const reloadUpdates = useCallback(async () => {
    try {
      const items = (await api.updates()).items;
      setUpdateCount(items.length);
      setAppUpdateCount(items.filter((i) => i.kind === "app").length);
    } catch {
      /* ignore */
    }
  }, []);

  const reloadHealth = useCallback(async () => {
    try {
      setHealth((await api.modelsHealth()).health);
    } catch {
      /* ignore */
    }
  }, []);

  const checkHealth = useCallback(async (ids?: string[], cloud = true) => {
    setHealth((await api.checkModelsHealth(ids, cloud)).health);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Connectivity lights: read once on startup and probe local services silently (no tokens spent); after that read the table every 2 minutes (chats and manual checks update it as a side effect)
  useEffect(() => {
    if (!online) return;
    void reloadHealth();
    api.checkModelsHealth(undefined, false).then((r) => setHealth(r.health)).catch(() => undefined);
    const t = window.setInterval(() => void reloadHealth(), 2 * 60 * 1000);
    return () => window.clearInterval(t);
  }, [online, reloadHealth]);

  // Update notices: fetch once on startup, then refresh every 10 minutes (the backend checks GitHub on its own configured interval)
  useEffect(() => {
    if (!online) return;
    void reloadUpdates();
    const t = window.setInterval(() => void reloadUpdates(), 10 * 60 * 1000);
    return () => window.clearInterval(t);
  }, [online, reloadUpdates]);

  // The project list is re-read on a slow clock: "which project is working right now" is a fact about
  // this moment, and a project that started a turn after the last manual refresh would show as idle.
  // Cheap — one query, and only while the window is visible.
  useEffect(() => {
    if (!online) return;
    const t = window.setInterval(() => { if (!document.hidden) void reloadGroups(); }, 5000);
    return () => window.clearInterval(t);
  }, [online, reloadGroups]);

  // Heartbeat: notice when the backend exits mid-session (two failures in a row count), so the UI can show Reconnecting and retry
  useEffect(() => {
    if (!online) return;
    let fails = 0;
    const t = window.setInterval(() => {
      api.ping().then(() => { fails = 0; }).catch(() => {
        fails += 1;
        if (fails >= 2) setOnline(false);
      });
    }, 5000);
    return () => window.clearInterval(t);
  }, [online]);

  // Retry automatically while the backend is still starting up
  useEffect(() => {
    if (online) return;
    const t = window.setInterval(() => void reload(), 2000);
    return () => window.clearInterval(t);
  }, [online, reload]);

  const models = useMemo(
    () =>
      providers.flatMap((p) =>
        p.models.map((m) => ({ ...m, provider_name: p.name, kind: p.kind, is_local: p.is_local })),
      ),
    [providers],
  );

  const value = useMemo(
    () => ({ online, agents, groups, providers, models, settings, zones, zoneErrors, reload, refreshAll, epoch, reloadGroups, updateCount, appUpdateCount, reloadUpdates, health, reloadHealth, checkHealth }),
    [online, agents, groups, providers, models, settings, zones, zoneErrors, reload, refreshAll, epoch, reloadGroups, updateCount, appUpdateCount, reloadUpdates, health, reloadHealth, checkHealth],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useData(): Data {
  const v = useContext(Ctx);
  if (!v) throw new Error("DataProvider missing");
  return v;
}
