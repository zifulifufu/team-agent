import { useEffect, useRef } from "react";

declare global {
  interface Window {
    teamAgent?: { token?: string; api?: string; pickFolder?: () => Promise<string | null> };
  }
}
// 后端地址:构建时的 VITE_API 优先;桌面版由 Electron 预加载脚本给出(和主进程用的是同一个端口);都没有则默认 8765
const API: string = ((import.meta.env.VITE_API as string | undefined) || window.teamAgent?.api || "http://127.0.0.1:8765").replace(/\/+$/, "");
/** Electron 预加载脚本注入的后端访问令牌;浏览器开发模式下为空。 */
const TOKEN: string = window.teamAgent?.token ?? "";
const authHeaders = (json = false): Record<string, string> => ({
  ...(json ? { "Content-Type": "application/json" } : {}),
  ...(TOKEN ? { "X-Team-Agent-Token": TOKEN } : {}),
});

// ------------------------------------------------------------------ types
/** 强项标签(后端 /api/strengths 给出完整清单):写作 代码 推理 长文本 多模态 速度 低成本 中文 工具调用 本地 */
export type Tag = string;

/** ok 连通 / limited 限速或熔断 / bad 连不通 / unknown 没检测过 / off 现在不会被调用 */
export type HealthState = "ok" | "limited" | "bad" | "unknown" | "off";
export interface ModelHealth {
  state: HealthState;
  detail: string;
  latency_ms: number;
  checked_at: number;
  source: "" | "test" | "chat" | "probe";
  stale: boolean;
}

export interface Model {
  id: string;                      // provider_id/model_name
  provider_id: string;
  model_name: string;
  display_name: string;
  enabled: boolean;
  provider_name?: string;
  kind?: string;
  is_local?: boolean;
  provider_enabled?: boolean;
  strengths: Tag[];                // 当前生效的强项(自定义优先,否则自动推断)
  strengths_auto: Tag[];
  strengths_custom: boolean;
  summary?: string;
  context?: number | null;
  tier?: "flagship" | "balanced" | "fast" | null;
  legacy?: boolean;
  preview?: boolean;
  retired_reason?: string | null;
  in_catalog?: boolean;
}
export interface Provider {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  enabled: boolean;
  is_local: boolean;
  has_key: boolean;
  key_hint: string;
  models: Model[];
}
export interface Preset {
  preset: string;
  name: string;
  kind: string;
  base_url: string;
  is_local: boolean;
  models: string[];
  hint: string;
}
/** 「选择模型」对话框里的一行:某服务商名下的一个型号(目录 + 实时清单 + 已添加的合并结果)。 */
export interface ModelOption {
  id: string;                      // 型号名(不含服务商前缀)
  name: string;
  summary: string;
  context: number | null;
  tier: "flagship" | "balanced" | "fast" | null;
  size_gb: number | null;          // 本地模型的体积
  params: string | null;
  strengths: Tag[];
  in_catalog: boolean;
  legacy: boolean;
  preview: boolean;
  added: boolean;                  // 已添加到本程序
  enabled: boolean;
  live: boolean | null;            // 服务商实时清单里是否有(null = 还没刷新过)
  is_new: boolean;                 // 上次看过之后新出现的
  retired_reason: string | null;
  gone: boolean;                   // 已添加但服务商实时清单里已经没有(多半下线)
  installed?: boolean | null;      // 仅本地服务商
}
export interface ModelOptions {
  provider_id: string;
  catalog_version: string;
  catalog_source: string;
  catalog_key: string;
  live_fetched_at: number | null;
  new_count: number;
  models: ModelOption[];
}
export interface Agent {
  id: string;
  name: string;
  avatar: string;
  role: string;
  prompt: string;
  model_id: string | null;         // null = 按强项自动选模型
  skills: string[];
  tags: Tag[];                     // 这个岗位需要的强项,用来给成员选模型、也用来做分工
  origin?: "" | "model";           // "model" = 由「我添加的模型」直接拉进群时自动创建的成员(就是这个模型本身)
  engine?: string;                 // 非空 = 外部智能体成员(如 workbuddy):不走模型路由,由它自带的命令行引擎发言
  engine_cfg?: ExternalCfg;
}
export type ExternalLevel = "read" | "edit" | "full";
export interface ExternalCfg {
  level: ExternalLevel;
  risk_ack: boolean;
  cwd: string;                     // 空 = 数据目录下专属工作目录
  add_dirs: string[];
  web: boolean;
  model: string;
  max_turns: number;
  timeout: number;
  handoff: boolean;
  cli_path: string;
}
export interface ExternalOverview {
  enabled: boolean;                // 外部智能体总开关
  external_calls_enabled: boolean;
  engines: { id: string; name: string; avatar: string; role: string; found: boolean; path: string; via: string; hint: string }[];
  levels: { id: ExternalLevel; label: string; desc: string }[];
  defaults: ExternalCfg;
  members: { id: string; name: string; engine: string; cfg: ExternalCfg; workspace: string }[];
}
export interface ExternalProbe {
  found: boolean;
  path: string;
  via: string;
  hint: string;
  version: string;
  live: null | { ok: boolean; reply?: string; error?: string; seconds: number; model?: string; cost_usd?: number | null };
}
export interface AwesomeApp {
  id: string;
  title: string;                   // 中文名(没有中文说明时是英文原名)
  title_en: string;
  desc: string;
  category: string;
  framework: string;
  path: string;
  kind: "team" | "agent";
  members: { name: string; role: string; tools: string[] }[];
  lead: string | null;
  sequential: boolean;
}
export interface AwesomeOverview {
  source: { name: string; url: string; license: string; author: string };
  commit: string;
  generated_at: string;
  origin: "shipped" | "local" | "empty";
  teams: AwesomeApp[];
  agents: AwesomeApp[];
  skills: { id: string; name: string; title: string; desc: string; needs_runtime: boolean; clipped: boolean; compatibility: string; installed: boolean }[];
  mcp: { id: string; name: string; command: string; args: string[]; env_keys: string[]; note: string; installed: boolean }[];
}
export interface AgentPreset {
  key: string;
  name: string;
  avatar: string;
  role: string;
  tags: Tag[];
  prompt: string;
  exists: boolean;                 // 已经有同名成员(添加时会复用)
}
export type LibraryMode = "all" | "selected" | "off";
export type PlanMode = "inherit" | "auto" | "on" | "off";
export interface GroupExt {
  skills: string[];                // 挂到本群的技能(群聊规则类 + 成员类均可)
  plugins: string[];               // 启用的插件 ID
  mcp: string[];                   // 启用的 MCP 服务器 ID
  library: { mode: LibraryMode; ids: string[] };
  plan: PlanMode;                  // inherit = 跟随全局设置
  memory: boolean;
}
export interface Group {
  id: string;
  name: string;
  host_agent_id: string | null;
  member_ids: string[];
  ext: GroupExt;
  prompt: string;                  // 本群提示词(支持 {{变量}})
  last_message?: string;
  last_at?: number;
}
interface Attempt {
  model_id: string;
  status: "ok" | "failed" | "skipped";
  detail: string;
  latency_ms: number;
}
export interface ToolCall {
  name: string;
  args: Record<string, string | number | boolean | null>;
  status: "running" | "waiting" | "ok" | "failed" | "denied";   // waiting = 等你在聊天里确认;denied = 被拒绝/超时/被禁止,没有执行
  ms?: number;
  preview?: string;
}
export type PlanTaskStatus = "pending" | "running" | "done" | "failed" | "stopped" | "skipped";
interface PlanTaskView {
  id: string;
  owner: string;
  owner_id: string;
  title: string;
  instruction: string;
  needs: string[];
  strengths: Tag[];
  tools: string[];
  deliverable: string;
  status: PlanTaskStatus;
  message_id: string;
  error: string;
}
export type PlanStatus = "running" | "integrating" | "done" | "stopped" | "failed";
export interface Message {
  id: string;
  group_id: string;
  sender_type: "user" | "agent" | "system" | "plan";
  sender_id: string | null;
  sender_name: string;
  content: string;
  model_id?: string | null;
  fallback_from?: string | null;
  meta?: {
    attempts?: Attempt[];
    tools?: ToolCall[];
    plan_id?: string;              // 该发言属于哪张任务板
    task_id?: string;              // 任务 ID;"final" = 群主整合
    task_title?: string;
    engine?: string;               // 外部智能体发言:引擎名(如 workbuddy)
    level?: ExternalLevel;         // 外部智能体发言时的权限级别
    denied?: string[];             // 被权限挡下的工具名
    external?: { cost_usd?: number; duration_ms?: number; num_turns?: number; model?: string };
    // sender_type === "plan" 时的任务板内容
    kind?: "plan";
    goal?: string;
    conventions?: string;
    status?: PlanStatus;
    tasks?: PlanTaskView[];
  };
  created_at?: number;
  streaming?: boolean;
}
export interface Settings {
  external_calls_enabled: boolean;
  external_agents_enabled: boolean;   // 外部智能体(如 WorkBuddy)总开关,默认关
  route_chain: string[];
  max_hops: number;
  history_limit: number;
  history_clip: number;            // 过去的单条消息最多带入多少字
  tool_output_limit: number;       // 工具结果回填给模型时最多多少字
  request_timeout: number;
  circuit_threshold: number;
  circuit_cooldown: number;
  system_prompt: string;           // 全局系统提示词(支持 {{变量}})
  plan_mode: Exclude<PlanMode, "inherit">;   // auto = 复杂任务由群主先做分工
  plan_max_tasks: number;
  tool_rounds: number;             // 每条回复最多几轮工具调用(0 = 不用工具)
  tool_timeout: number;
  memory_enabled: boolean;
  memory_auto_extract: boolean;
  memory_top_k: number;
  library_top_k: number;
  app_repo: string;                // 程序本体所在的 GitHub 仓库 owner/repo
  catalog_url: string;
  github_token: string;            // 只写:读出来永远是空串,是否已设置看 github_token_set
  github_token_set: boolean;
  auto_check_updates: boolean;
  update_interval_hours: number;
  auto_update_skills: boolean;
  obsidian_dir: string;            // 只读:通过 /api/obsidian 设置
  obsidian_auto: boolean;
  perm_mode: PermMode;             // 工具调用审批模式
  perm_timeout: number;            // 等你确认多少秒,超时按拒绝
  perm_allow: string[];            // 「总是允许」的工具名
  perm_deny: string[];             // 「永远禁止」的工具名
}
export interface ObsidianReport {
  ok: boolean;
  at: number;
  written: number;
  pulled: number;
  imported: number;
  deleted_memories: number;
  removed_files: number;
  conflicts: number;
  files: number;
  warnings: string[];
  error: string;
}
export interface ObsidianStatus {
  dir: string;
  auto: boolean;
  exists: boolean;
  in_vault: boolean;
  notes: number;
  mapped: number;
  last: ObsidianReport | null;
  vaults?: { path: string; name: string }[];
}
export type PermMode = "ask_risky" | "ask_all" | "allow_all";
type ToolRisk = "read" | "write" | "exec";
/** 一条等你确认的工具调用 */
export interface Approval {
  id: string;
  group_id: string;
  message_id: string;
  agent: string;
  tool: string;
  source: string;
  server: string;
  risk: ToolRisk;
  risk_label: string;
  args: Record<string, string | number | boolean | null>;
  expires_at: number;
}
export interface Permissions {
  mode: PermMode;
  timeout: number;
  allow: string[];
  deny: string[];
  tools: { name: string; group: string; risk: ToolRisk; risk_label: string; policy: "allow" | "ask" | "deny" }[];
  access: {
    external_calls: boolean;
    cloud_providers: string[];
    data_dir: string;
    plugins_dir: string;
    plugins: { id: string; name: string; tools: number; error: string }[];
    mcp: { id: string; name: string; enabled: boolean; kind: string; command: string }[];
    library_docs: number;
    groups: { id: string; name: string; plugins: number; mcp: number }[];
  };
}
export interface RoutePreview {
  external_calls_enabled: boolean;
  chain: string[];
  skipped: Attempt[];
}
export interface LocalStatus {
  running: boolean;
  base_url: string;
  installed: string[];
  error?: string;
}
/** 本地模型推荐目录(后端 /api/local/catalog)。fit/disk_ok/slow 是按本机内存、磁盘、加速方式的粗略估算,不是保证。 */
export type LocalFit = "ok" | "tight" | "no" | "unknown";
interface LocalHardware {
  system: string;
  machine: string;
  /** 当前 Python 是 x86_64 版、被 Rosetta 转译着在苹果芯片上运行 */
  translated?: boolean;
  ram_gb: number | null;
  disk_free_gb: number | null;
  accel: "metal" | "cuda" | "none" | "unknown";
}
export interface LocalModelRow {
  tag: string;
  size_gb: number;
  ctx: string;
  note: string;
  fit: LocalFit;
  disk_ok: boolean;
  slow: boolean;
  installed: boolean;
}
export interface LocalFamily {
  id: string;
  vendor: string;
  name: string;
  desc: string;
  license: string;
  strengths: Tag[];
  extra?: boolean;
  models: LocalModelRow[];
}
interface SelfhostModel {
  id: string;
  params: string;
  size_gb: number;
  note: string;
  fit: LocalFit;
  disk_ok: boolean;
  slow: boolean;
}
interface SelfhostFamily {
  id: string;
  vendor: string;
  name: string;
  desc: string;
  license: string;
  strengths: Tag[];
  models: SelfhostModel[];
}
export interface LocalCatalog {
  version: string;
  source: string;
  note: string;
  hardware: LocalHardware;
  families: LocalFamily[];
  selfhost: SelfhostFamily[];
  cloud_only: { name: string; vendor: string; note: string }[];
  running: boolean;
  installed: string[];
}
export interface LocalCandidate {
  source: "ollama" | "successor" | "hf" | "github" | "ollama-release";
  name: string;
  tag: string | null;
  size_gb: number | null;
  desc?: string;
  url?: string;
  replaces?: string;
  license?: string;
  stars?: number;
  local?: string;
  latest?: string;
}
interface LocalCheckResult {
  found: number;
  candidates: LocalCandidate[];
  errors: string[];
  ollama: { local: string; latest: string; url: string; ok?: boolean } | null;
  catalog: string;
  catalog_update: { applied?: boolean; available?: boolean; latest?: string; error?: string; configured?: boolean } | null;
}
export interface Skill {
  name: string;
  description: string;
  path: string;
  scope: "member" | "group";       // group = 群聊提示词类技能,挂到整个群
  version: string;
  source: { repo: string; path: string } | null;
  body?: string;                   // 只有单个读取/创建/更新时才返回
}
export interface PluginInfo {
  id: string;
  file: string;
  name: string;
  description: string;
  version: string;
  tools: string[];
  error: string;
  source: { repo: string; path: string } | null;
}
type McpStatus = "idle" | "connecting" | "ready" | "error";
export interface McpServer {
  id: string;
  name: string;
  command: string;
  args: string[];
  url: string;
  transport: string;
  transport_effective: "stdio" | "sse" | "http";
  description: string;
  enabled: boolean;
  env: Record<string, string>;     // 值已被掩码成 ••••••;原样回传表示保持不变
  headers: Record<string, string>;
  status: McpStatus;
  error: string;
  tools: { name: string; description: string; read_only: boolean }[];
}
export interface McpTemplate {
  name: string;
  command: string;
  args: string[];
  note: string;
}
export interface McpImportPreview {
  servers: { name: string; command: string; args: string[]; env: Record<string, string>; url: string; transport: string; headers: Record<string, string>; description: string; enabled: boolean; exists: boolean }[];
  warnings: string[];
}
export interface LibraryDoc {
  id: string;
  title: string;
  filename: string;
  kind: string;
  size: number;
  chars: number;
  chunks: number;
  enabled: boolean;
  created_at: number;
}
export interface LibraryHit {
  doc_id: string;
  title: string;
  idx: number;
  text: string;
  score: number;
}
export type MemoryScope = "global" | "group" | "agent";
export type MemoryKind = "preference" | "fact" | "decision" | "lesson" | "action";
export interface Memory {
  id: string;
  scope: MemoryScope;
  scope_id: string;
  scope_name?: string;
  kind: MemoryKind;
  content: string;
  pinned: boolean;
  source: string;                  // manual | auto | action
  hits: number;
  created_at: number;
  updated_at: number;
  last_used: number | null;
}
export interface PromptItem {
  id: string;
  title: string;
  content: string;
  kind: "general" | "group";
  use_globally: boolean;
  created_at: number;
}
export interface PromptsInfo {
  prompts: PromptItem[];
  system_prompt: string;
  default_system_prompt: string;
  variables: { name: string; desc: string }[];
}
export interface Capabilities {
  members: {
    agent_id: string;
    name: string;
    avatar: string;
    role: string;
    tags: Tag[];
    is_host: boolean;
    skills: string[];
    model: { id: string; display_name: string; strengths: Tag[]; is_local: boolean } | null;
    manual_model: boolean;
    strengths: Tag[];              // 成员岗位强项 ∪ 所用模型强项(最多 6 个)
    origin: "" | "model";
    engine?: string;               // 非空 = 外部智能体
    model_problem: string;         // 指定的模型现在用不了时的原因(此时实际会回退到别的模型);空 = 正常
  }[];
  tools: { name: string; description: string; source: string }[];
  problems: string[];
  ext: GroupExt;
  docs: number;
}
export interface GroupTemplate {
  id: string;
  name: string;
  scene: string;
  desc: string;
  members: string[];
  host: string;
  skills: string[];
  prompt: string;
}
type UpdateKind = "app" | "catalog" | "skill" | "plugin" | "model" | "localmodel" | "localcatalog";
export interface UpdateItem {
  id: string;
  kind: UpdateKind;
  ref: string;
  title: string;
  detail: Record<string, unknown>;
  status: "new" | "done" | "dismissed";
  created_at: number;
}
export interface UpdatesInfo {
  items: UpdateItem[];
  last_check: (Record<string, unknown> & { at?: number; errors?: string[] }) | null;
  checking: boolean;
  configured: boolean;             // 是否填了程序仓库
  curated: { kind: "skill" | "mcp" | "plugin"; repo: string; desc: string }[];
  catalog: { version: string; source: string };
  sources: { kind: string; name: string; repo: string; path: string; ref: string; sha: string; installed_at: number }[];
}
export interface RepoHit {
  repo: string;
  description: string;
  stars: number;
  updated_at: string;
  url: string;
  license: string;
  archived: boolean;
  default_branch: string;
}
export interface RepoFile {
  path: string;
  sha: string;
  name?: string;
  size?: number;
}
export interface FilePreview {
  repo: string;
  path: string;
  ref: string;
  content: string;
  sha: string;
  sha256: string;                  // 安装插件时必须带上,服务器会重新下载并比对
  size: number;
}

export interface Stats {
  days: number;
  total_requests: number;
  fallbacks: number;
  local_calls: number;
  avg_latency_ms: number | null;
  groups: number;
  by_model: { model_id: string; label: string; is_local: boolean; count: number; avg_latency_ms: number | null }[];
  by_day: { date: string; count: number; fallbacks: number }[];
}
export interface SystemInfo {
  app_version: string;
  python: string;
  platform: string;
  litellm: string | null;
  fastapi: string | null;
  data_dir: string;
  db_bytes: number;
  external_calls_enabled: boolean;
  auth: boolean;
}

export type ChatEvent =
  | { type: "message"; message: Message }
  | { type: "message_start"; message: Message }
  | { type: "delta"; message_id: string; text: string }
  | { type: "reset"; message_id: string }
  | { type: "message_end"; message: Message }
  | { type: "message_discard"; message_id: string }
  | { type: "plan"; message: Message }                                        // 任务板有更新(整条替换)
  | { type: "tool"; message_id: string; index: number; call: ToolCall }       // 第 index 个工具调用的状态变化
  | { type: "approval"; approval: Approval }                                  // 有工具调用在等你确认
  | { type: "approval_done"; id: string; group_id: string; decision: "allow" | "deny" | "timeout" | "cancelled" }
  | { type: "stopped" }
  | { type: "idle" };

// ------------------------------------------------------------------- http
/** 把后端的错误体变成一句人话:FastAPI 的校验错误是数组,不能直接 JSON.stringify 给用户看 */
function errorText(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((d) => {
        const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
        return `${loc ? loc + ":" : ""}${d?.msg ?? JSON.stringify(d)}`;
      })
      .join(";");
  }
  return fallback;
}

/** fetch 的统一入口:带令牌、连不上时给出能看懂的提示、把错误体转成文字 */
async function call(path: string, init: RequestInit): Promise<Response> {
  let r: Response;
  try {
    r = await fetch(API + path, init);
  } catch {
    throw new Error("连不上本地后端(它可能还在启动,或已经退出了)");
  }
  if (!r.ok) {
    let detail: unknown;
    try {
      detail = (await r.json()).detail;
    } catch {
      /* 不是 JSON */
    }
    throw new Error(errorText(detail, `${r.status} ${r.statusText}`.trim()));
  }
  return r;
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await call(path, {
    method,
    headers: authHeaders(body !== undefined),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return (await r.json()) as T;
}
const get = <T,>(p: string) => req<T>("GET", p);
const post = <T,>(p: string, b?: unknown) => req<T>("POST", p, b ?? {});
const patch = <T,>(p: string, b: unknown) => req<T>("PATCH", p, b);
const put = <T,>(p: string, b: unknown) => req<T>("PUT", p, b);
const del = <T,>(p: string) => req<T>("DELETE", p);

const qs = (o: Record<string, string | number | boolean | undefined>) => {
  const p = Object.entries(o).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return p.length ? "?" + p.join("&") : "";
};

export const api = {
  /** 心跳:后端还活着吗(不需要令牌) */
  ping: () => get<{ ok: boolean }>("/api/health"),
  presets: () => get<Preset[]>("/api/presets"),
  // ---- 模型服务
  providers: () => get<Provider[]>("/api/providers"),
  addProvider: (b: Record<string, unknown>) => post<Provider>("/api/providers", b),
  patchProvider: (id: string, b: Record<string, unknown>) => patch<Provider>(`/api/providers/${id}`, b),
  delProvider: (id: string) => del(`/api/providers/${id}`),
  addModel: (pid: string, model_name: string) => post<Model>(`/api/providers/${pid}/models`, { model_name }),
  /** strengths: string[] = 自定义;null = 恢复自动推断 */
  patchModel: (id: string, b: { enabled?: boolean; display_name?: string; strengths?: Tag[] | null }) => patch<Model>(`/api/models/${id}`, b),
  delModel: (id: string) => del(`/api/models/${id}`),
  addModels: (pid: string, model_names: string[]) => post<Model[]>(`/api/providers/${pid}/models/batch`, { model_names }),
  strengthTags: () => get<{ tags: { id: Tag; desc: string }[] }>("/api/strengths"),
  modelOptions: (pid: string) => get<ModelOptions>(`/api/providers/${pid}/model-options`),
  /** 向服务商查询实时清单并返回合并后的选项(联网,外呼关闭时云端服务商返回 403) */
  refreshModelOptions: (pid: string) => post<ModelOptions>(`/api/providers/${pid}/model-options/refresh`),
  /** 把当前所有型号标为「已看过」(清掉「新」标记) */
  markModelsSeen: (pid: string) => post<ModelOptions>(`/api/providers/${pid}/model-options/seen`),
  /** 按强项给「当前可用」的模型排序(score = 命中的强项数);没填 Key 且没有本地模型时返回空数组 */
  recommendModels: (tags: Tag[], limit = 5) =>
    get<{ tags: Tag[]; models: (Model & { score: number })[] }>(`/api/models/recommend${qs({ tags: tags.join(","), limit })}`),
  stats: (days: number) => get<Stats>(`/api/stats?days=${days}`),
  system: () => get<SystemInfo>("/api/system"),
  clearAllMessages: () => del<{ deleted: number }>("/api/data/messages"),
  restoreBackup: (file: File) =>
    postRaw<{ safety_copy: string; groups: number; agents: number; providers: number; memories: number; docs: number }>("/api/data/restore", file),
  exportChatObsidian: (gid: string) => post<{ path: string }>(`/api/groups/${gid}/export-obsidian`),
  obsidian: () => get<ObsidianStatus>("/api/obsidian"),
  setObsidian: (b: { dir?: string; auto?: boolean }) => put<ObsidianStatus>("/api/obsidian", b),
  obsidianSync: (force = false) => post<ObsidianReport>(`/api/obsidian/sync${force ? "?force=true" : ""}`),
  approvals: (group_id: string) => get<Approval[]>(`/api/approvals?group_id=${encodeURIComponent(group_id)}`),
  answerApproval: (id: string, decision: "allow" | "deny", remember = false) =>
    post<{ ok: boolean }>(`/api/approvals/${id}`, { decision, remember }),
  permissions: () => get<Permissions>("/api/permissions"),
  modelsHealth: () => get<{ health: Record<string, ModelHealth> }>("/api/models-health"),
  /** 检测连通性。cloud=false 只探测本地服务(不花 token);云端模型会发一条极短的请求 */
  checkModelsHealth: (model_ids?: string[], cloud = true) =>
    post<{ checked: number; health: Record<string, ModelHealth> }>("/api/models-health/check", { model_ids, cloud }),
  testModel: (model_id: string) =>
    post<{ ok: boolean; latency_ms?: number; reply?: string; error?: string }>("/api/test-model", { model_id }),
  settings: () => get<Settings>("/api/settings"),
  putSettings: (b: Partial<Settings>) => put<Settings>("/api/settings", b),
  routePreview: (preferred?: string | null) =>
    get<RoutePreview>("/api/route/preview" + (preferred ? `?preferred=${encodeURIComponent(preferred)}` : "")),
  localStatus: () => get<LocalStatus>("/api/local/status"),
  localCatalog: () => get<LocalCatalog>("/api/local/catalog"),
  localCheck: () => post<LocalCheckResult>("/api/local/check", {}),
  localAdd: (tag: string, note = "") => post<{ tag: string; size_gb: number }>("/api/local/catalog/add", { tag, note }),
  localRemoveExtra: (tag: string) => del<{ ok: boolean }>(`/api/local/catalog/extra?tag=${encodeURIComponent(tag)}`),
  applyLocalCatalog: () => post<{ applied?: boolean; latest?: string }>("/api/local/catalog/apply", {}),
  // ---- 成员与群聊
  agents: () => get<Agent[]>("/api/agents"),
  createAgent: (b: Partial<Agent>) => post<Agent>("/api/agents", b),
  patchAgent: (id: string, b: Partial<Agent>) => patch<Agent>(`/api/agents/${id}`, b),
  delAgent: (id: string) => del(`/api/agents/${id}`),
  agentPresets: () => get<AgentPreset[]>("/api/agent-presets"),
  groups: () => get<Group[]>("/api/groups"),
  createGroup: (name: string, member_ids: string[], host_agent_id: string | null, extra?: { ext?: Partial<GroupExt>; prompt?: string }) =>
    post<Group>("/api/groups", { name, member_ids, host_agent_id, ...extra }),
  patchGroup: (id: string, b: { name?: string; host_agent_id?: string | null; prompt?: string; ext?: Partial<GroupExt> }) =>
    patch<Group>(`/api/groups/${id}`, b),
  delGroup: (id: string) => del(`/api/groups/${id}`),
  addMember: (gid: string, agent_id: string) => post<Group>(`/api/groups/${gid}/members`, { agent_id }),
  /** 随时把预设岗位(主持/评审/记录/资料员/程序员/翻译/分析师/策划…)拉进群;已有同名成员则复用 */
  addMemberFromPreset: (gid: string, key: string) => post<Group>(`/api/groups/${gid}/members/from-preset`, { key }),
  externalOverview: () => get<ExternalOverview>("/api/external"),
  externalCreate: (b: { engine?: string; name?: string; group_id?: string; cfg: Partial<ExternalCfg> }) => post<Agent>("/api/external/agents", b),
  externalPatch: (id: string, cfg: Partial<ExternalCfg>) => patch<Agent>(`/api/external/agents/${id}`, { cfg }),
  externalTest: (b: { live?: boolean; agent_id?: string; cli_path?: string }) => post<ExternalProbe>("/api/external/test", b),
  awesome: () => get<AwesomeOverview>("/api/awesome"),
  awesomeCreateGroup: (id: string, name?: string) => post<{ group: Group; members: string[]; host: string }>(`/api/awesome/apps/${id}/create-group`, { name }),
  awesomeAddMembers: (id: string, group_id?: string, names?: string[]) => post<{ members: { id: string; name: string }[] }>(`/api/awesome/apps/${id}/members`, { group_id, names }),
  awesomeAddPrompts: (id: string) => post<{ added: string[]; skipped: string[] }>(`/api/awesome/apps/${id}/prompts`, {}),
  awesomeInstallSkill: (id: string) => post<{ name: string }>(`/api/awesome/skills/${id}/install`, {}),
  awesomeAddMcp: (id: string) => post<{ id: string; name: string }>(`/api/awesome/mcp/${id}/add`, {}),
  awesomeRefresh: (path: string) => post<{ commit: string; teams: number; agents: number; skills: number; mcp: number }>("/api/awesome/refresh", { path }),
  awesomeReset: () => post<{ ok: boolean }>("/api/awesome/reset", {}),
  addMemberFromModel: (gid: string, model_id: string) => post<Group>(`/api/groups/${gid}/members/from-model`, { model_id }),
  removeMember: (gid: string, aid: string) => del<Group>(`/api/groups/${gid}/members/${aid}`),
  capabilities: (gid: string) => get<Capabilities>(`/api/groups/${gid}/capabilities`),
  applyPrompt: (gid: string, prompt_id: string, mode: "replace" | "append" = "replace") =>
    post<Group>(`/api/groups/${gid}/apply-prompt`, { prompt_id, mode }),
  systemPromptPreview: (gid: string, agent_id?: string) =>
    get<{ text: string; tokens: number }>(`/api/groups/${gid}/system-prompt-preview${qs({ agent_id })}`),
  templates: () => get<GroupTemplate[]>("/api/templates"),
  createFromTemplate: (tid: string, name?: string) => post<Group>(`/api/templates/${tid}/create-group`, { name }),
  messages: (gid: string) => get<Message[]>(`/api/groups/${gid}/messages`),
  /** 这个群现在有没有一轮协作在跑(刷新页面、WebSocket 重连后用来恢复「发送中」状态) */
  groupStatus: (gid: string) => get<{ busy: boolean }>(`/api/groups/${gid}/status`),
  clearMessages: (gid: string) => del(`/api/groups/${gid}/messages`),
  send: (gid: string, text: string) => post(`/api/groups/${gid}/messages`, { text }),
  stop: (gid: string) => post(`/api/groups/${gid}/stop`),
  // ---- 技能 / 插件 / MCP(三者分开)
  skills: () => get<Skill[]>("/api/skills"),
  skill: (name: string) => get<Skill>(`/api/skills/${encodeURIComponent(name)}`),
  addSkill: (b: { name: string; description: string; body: string; scope: "member" | "group" }) => post<Skill>("/api/skills", b),
  saveSkill: (old: string, b: { name: string; description: string; body: string; scope: "member" | "group" }) =>
    put<Skill>(`/api/skills/${encodeURIComponent(old)}`, b),
  delSkill: (name: string) => del(`/api/skills/${encodeURIComponent(name)}`),
  plugins: () => get<PluginInfo[]>("/api/plugins"),
  reloadPlugins: () => post<PluginInfo[]>("/api/plugins/reload"),
  pluginSource: (id: string) => get<{ id: string; content: string }>(`/api/plugins/${encodeURIComponent(id)}/source`),
  delPlugin: (id: string) => del(`/api/plugins/${encodeURIComponent(id)}`),
  mcp: () => get<McpServer[]>("/api/mcp"),
  mcpTemplates: () => get<McpTemplate[]>("/api/mcp/templates"),
  mcpImportParse: (text: string) => post<McpImportPreview>("/api/mcp/import/parse", { text }),
  mcpImport: (text: string, names: string[]) => post<{ added: McpServer[]; skipped: string[] }>("/api/mcp/import", { text, names }),
  addMcp: (b: Record<string, unknown>) => post<McpServer>("/api/mcp", b),
  patchMcp: (id: string, b: Record<string, unknown>) => patch<McpServer>(`/api/mcp/${id}`, b),
  delMcp: (id: string) => del(`/api/mcp/${id}`),
  connectMcp: (id: string) => post<McpServer>(`/api/mcp/${id}/connect`),
  disconnectMcp: (id: string) => post<McpServer>(`/api/mcp/${id}/disconnect`),
  // ---- 资料库
  library: () => get<{ docs: LibraryDoc[]; total_chars: number; count: number }>("/api/library"),
  addNote: (title: string, content: string) => post<LibraryDoc>("/api/library/note", { title, content }),
  /** 请求体就是文件原始字节(txt/md/csv/json/html/pdf/docx) */
  uploadDoc: (file: File) => postRaw<LibraryDoc>(`/api/library/upload${qs({ filename: file.name })}`, file),
  addDocUrl: (url: string) => post<LibraryDoc>("/api/library/url", { url }),
  addDocDir: (path: string, recursive = true) =>
    post<{ added: LibraryDoc[]; skipped: { name: string; reason: string }[] }>("/api/library/dir", { path, recursive }),
  searchLibrary: (q: string, top_k = 5) => get<LibraryHit[]>(`/api/library/search${qs({ q, top_k })}`),
  readDoc: (id: string, start = 0) =>
    get<{ doc: LibraryDoc; start: number; end: number; total: number; text: string }>(`/api/library/${id}${qs({ start })}`),
  patchDoc: (id: string, b: { title?: string; enabled?: boolean }) => patch<LibraryDoc>(`/api/library/${id}`, b),
  delDoc: (id: string) => del(`/api/library/${id}`),
  // ---- 记忆
  memories: (f: { scope?: MemoryScope; scope_id?: string; kind?: MemoryKind; q?: string } = {}) =>
    get<{ memories: Memory[]; count: number }>(`/api/memories${qs(f)}`),
  addMemory: (b: { content: string; scope?: MemoryScope; scope_id?: string; kind?: MemoryKind; pinned?: boolean }) =>
    post<Memory>("/api/memories", b),
  patchMemory: (id: string, b: { content?: string; kind?: MemoryKind; pinned?: boolean }) => patch<Memory>(`/api/memories/${id}`, b),
  delMemory: (id: string) => del(`/api/memories/${id}`),
  clearMemories: (f: { scope?: MemoryScope; scope_id?: string; source?: string }) =>
    del<{ deleted: number }>(`/api/memories${qs(f)}`),
  // ---- 提示词
  prompts: () => get<PromptsInfo>("/api/prompts"),
  addPrompt: (b: { title: string; content: string; kind?: "general" | "group"; use_globally?: boolean }) => post<PromptItem>("/api/prompts", b),
  patchPrompt: (id: string, b: Partial<Pick<PromptItem, "title" | "content" | "kind" | "use_globally">>) =>
    patch<PromptItem>(`/api/prompts/${id}`, b),
  delPrompt: (id: string) => del(`/api/prompts/${id}`),
  /** 用真实的成员/群信息代入 {{变量}},并估算 token */
  previewPrompt: (content: string, ids: { agent_id?: string; group_id?: string } = {}) =>
    post<{ text: string; tokens: number; raw_tokens: number }>("/api/prompts/preview", { content, ...ids }),
  resetSystemPrompt: () => post<{ system_prompt: string }>("/api/prompts/reset-system"),
  // ---- 更新与发现(GitHub)
  updates: () => get<UpdatesInfo>("/api/updates"),
  checkUpdates: () => post<Record<string, unknown>>("/api/updates/check"),
  dismissUpdate: (id: string) => post(`/api/updates/${id}/dismiss`),
  searchRepos: (kind: "skill" | "plugin" | "mcp", q = "") => get<RepoHit[]>(`/api/updates/search${qs({ kind, q })}`),
  repoSkills: (repo: string, ref = "") => get<RepoFile[]>(`/api/updates/repo/skills${qs({ repo, ref })}`),
  repoPlugins: (repo: string, ref = "") => get<RepoFile[]>(`/api/updates/repo/plugins${qs({ repo, ref })}`),
  repoReadme: (repo: string) => get<{ repo: string; content: string; url: string }>(`/api/updates/repo/readme${qs({ repo })}`),
  previewFile: (repo: string, path: string, ref = "") => post<FilePreview>("/api/updates/preview", { repo, path, ref }),
  installSkill: (repo: string, path: string, ref = "", overwrite = false) =>
    post<Skill>("/api/updates/skill/install", { repo, path, ref, overwrite }),
  updateSkill: (name: string) => post<Skill>(`/api/updates/skill/update/${encodeURIComponent(name)}`),
  /** sha256 必须来自 previewFile 的结果;服务器会重新下载并比对,内容变了就拒绝 */
  installPlugin: (repo: string, path: string, sha256: string, ref = "", overwrite = false) =>
    post<{ id: string; plugins: PluginInfo[] }>("/api/updates/plugin/install", { repo, path, ref, sha256, overwrite }),
  applyCatalog: () => post<Record<string, unknown>>("/api/updates/catalog/apply"),
};

/** 请求体就是文件原始字节的 POST(资料库上传、恢复备份) */
async function postRaw<T>(path: string, body: Blob): Promise<T> {
  const r = await call(path, { method: "POST", headers: { ...authHeaders(), "Content-Type": "application/octet-stream" }, body });
  return (await r.json()) as T;
}

/** 下载后端返回的文件;文件名取自 Content-Disposition(支持 filename*=UTF-8'' 写法) */
async function download(path: string, fallbackName: string, what: string): Promise<void> {
  let r: Response;
  try {
    r = await call(path, { headers: authHeaders() });
  } catch (e) {
    throw new Error(`${what}失败:${(e as Error).message}`);
  }
  const blob = await r.blob();
  const cd = r.headers.get("content-disposition") ?? "";
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)?.[1];
  const name = star ? decodeURIComponent(star) : /filename="?([^";]+)"?/.exec(cd)?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export const downloadBackup = (includeKeys: boolean) => download(`/api/data/export?include_keys=${includeKeys}`, "team-agent-backup.db", "导出");
export const downloadChat = (gid: string) => download(`/api/groups/${gid}/export`, "chat.md", "导出");

/** 拉取 Ollama 模型,逐行回调进度;结束返回是否成功。 */
export async function pullLocalModel(
  model: string,
  onProgress: (p: { status?: string; completed?: number; total?: number; error?: string }) => void,
): Promise<boolean> {
  const r = await fetch(API + "/api/local/pull", {
    method: "POST",
    headers: authHeaders(true),
    body: JSON.stringify({ model }),
  });
  if (!r.body) return false;
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let ok = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      try {
        const j = JSON.parse(line);
        onProgress(j);
        if (j.status === "success") ok = true;
      } catch {
        /* ignore */
      }
    }
  }
  return ok;
}

// -------------------------------------------------------------- websocket
export function useGroupSocket(gid: string | null, onEvent: (e: ChatEvent) => void, onState?: (up: boolean) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  const st = useRef(onState);
  st.current = onState;
  useEffect(() => {
    if (!gid) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let timer: number | undefined;
    const connect = () => {
      ws = new WebSocket(API.replace(/^http/, "ws") + `/ws/groups/${gid}` + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""));
      ws.onopen = () => st.current?.(true);
      ws.onmessage = (m) => {
        try {
          cb.current(JSON.parse(m.data));
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (closed) return;   // 主动关掉的(切群/卸载)不再通知,免得把新连接的状态覆盖成「已断开」
        st.current?.(false);
        timer = window.setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [gid]);
}

// ---------------------------------------------------------------- helpers
export function relTime(ts?: number): string {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function modelLabel(id: string | null | undefined, models: Model[]): string {
  if (!id) return "默认路由";
  if (id.startsWith("ext:")) return `${id.slice(4).replace(/^./, (c) => c.toUpperCase())}(外部)`;
  const m = models.find((x) => x.id === id);
  return m ? m.display_name : id.split("/").slice(1).join("/") || id;
}
