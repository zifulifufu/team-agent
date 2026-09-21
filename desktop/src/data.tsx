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
  /** 整库被替换(恢复备份)后用:重新读取全部数据,并让主区里的页面重新加载,不留旧内容 */
  refreshAll: () => Promise<void>;
  /** 每次 refreshAll 加一;App 用它给主区换 key */
  epoch: number;
  reloadGroups: () => Promise<void>;
  /** 待处理的更新提醒数(程序 / 模型目录 / 技能 / 插件 / 新模型),用于侧边栏红点 */
  updateCount: number;
  reloadUpdates: () => Promise<void>;
  /** 模型连通指示灯:model_id → 状态 */
  health: Record<string, ModelHealth>;
  reloadHealth: () => Promise<void>;
  /** 主动检测:不传 ids = 全部;cloud=false 只探测本地服务(不花 token) */
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
        api.modelsHealth().catch(() => null), // 改了密钥/启用状态后灯要跟着变
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

  // 指示灯:上线后读一次,并静默探测本地服务(不花 token);之后每 2 分钟读一次库(聊天和检测会顺带更新它)
  useEffect(() => {
    if (!online) return;
    void reloadHealth();
    api.checkModelsHealth(undefined, false).then((r) => setHealth(r.health)).catch(() => undefined);
    const t = window.setInterval(() => void reloadHealth(), 2 * 60 * 1000);
    return () => window.clearInterval(t);
  }, [online, reloadHealth]);

  // 更新提醒:上线后查一次,之后每 10 分钟刷新(后端自己按设置的间隔去 GitHub 检查)
  useEffect(() => {
    if (!online) return;
    void reloadUpdates();
    const t = window.setInterval(() => void reloadUpdates(), 10 * 60 * 1000);
    return () => window.clearInterval(t);
  }, [online, reloadUpdates]);

  // 心跳:后端中途退出了要能发现(连续两次失败才算),界面才会显示「重连中」并自动重试
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

  // 后端还没起来时自动重试
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
