import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type Agent, type Group, type Model, type ModelHealth, type Provider, type Settings } from "./api";

interface Data {
  online: boolean;
  agents: Agent[];
  groups: Group[];
  providers: Provider[];
  models: Model[];
  settings: Settings | null;
  reload: () => Promise<void>;
  /** After the whole database is replaced (restoring a backup): re-read everything and let the pages in the main area reload, rather than leaving stale content behind */
  refreshAll: () => Promise<void>;
  /** Incremented by every refreshAll; App uses it as the key of the main area */
  epoch: number;
  reloadGroups: () => Promise<void>;
  /** Number of unhandled update notices (app / model catalog / skills / plugins / new models), for the sidebar badge */
  updateCount: number;
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
  const [updateCount, setUpdateCount] = useState(0);
  const [health, setHealth] = useState<Record<string, ModelHealth>>({});
  const [epoch, setEpoch] = useState(0);

  const reload = useCallback(async () => {
    try {
      const [a, g, p, s, h] = await Promise.all([
        api.agents(), api.groups(), api.providers(), api.settings(),
        api.modelsHealth().catch(() => null), // Changing a key or an enabled flag has to move the lights too
      ]);
      if (h) setHealth(h.health);
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
      setUpdateCount((await api.updates()).items.length);
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
    () => ({ online, agents, groups, providers, models, settings, reload, refreshAll, epoch, reloadGroups, updateCount, reloadUpdates, health, reloadHealth, checkHealth }),
    [online, agents, groups, providers, models, settings, reload, refreshAll, epoch, reloadGroups, updateCount, reloadUpdates, health, reloadHealth, checkHealth],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useData(): Data {
  const v = useContext(Ctx);
  if (!v) throw new Error("DataProvider missing");
  return v;
}
