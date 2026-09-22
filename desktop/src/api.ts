import { useEffect, useRef } from "react";
import { currentLang, tr } from "./i18n";

declare global {
  interface Window {
    teamAgent?: { token?: string; api?: string; pickFolder?: () => Promise<string | null> };
  }
}
// Backend address: VITE_API at build time wins; the desktop build gets it from the Electron preload script (same port the main process uses); otherwise 8765
const API: string = ((import.meta.env.VITE_API as string | undefined) || window.teamAgent?.api || "http://127.0.0.1:8765").replace(/\/+$/, "");
/** Backend access token injected by the Electron preload script; empty when running in a plain browser. */
const TOKEN: string = window.teamAgent?.token ?? "";
const authHeaders = (json = false): Record<string, string> => ({
  ...(json ? { "Content-Type": "application/json" } : {}),
  ...(TOKEN ? { "X-Team-Agent-Token": TOKEN } : {}),
  // Built-in content (model catalog, local model list, strength tags) is served in
  // the UI language; English is the backend default, so only zh needs sending.
  "Accept-Language": currentLang() === "zh" ? "zh-CN" : "en",
});

// ------------------------------------------------------------------ types
/**
 * Strength tag id. Ids are stable ASCII keys stored in the database and never
 * translated (writing / coding / reasoning / long-context / multimodal / speed /
 * low-cost / chinese / tool-use / local); /api/strengths also returns the label
 * and description for the current UI language.
 */
export type Tag = string;

/** ok reachable / limited rate-limited or tripped the breaker / bad unreachable / unknown never probed / off not called right now */
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
  strengths: Tag[];                // Strengths in effect (a custom list wins, otherwise the auto-inferred one)
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
/** One row in the Choose model dialog: a model under some provider (catalog + live list + already-added, merged). */
export interface ModelOption {
  id: string;                      // Model name (without the provider prefix)
  name: string;
  summary: string;
  context: number | null;
  tier: "flagship" | "balanced" | "fast" | null;
  size_gb: number | null;          // Size of a local model
  params: string | null;
  strengths: Tag[];
  in_catalog: boolean;
  legacy: boolean;
  preview: boolean;
  added: boolean;                  // Already added to this app
  enabled: boolean;
  live: boolean | null;            // Present in the provider's live list (null = never refreshed)
  is_new: boolean;                 // Appeared after the last time you looked
  retired_reason: string | null;
  gone: boolean;                   // Added, but no longer in the provider's live list (most likely retired)
  installed?: boolean | null;      // Local providers only
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
  model_id: string | null;         // null = pick a model automatically from the strength tags
  skills: string[];
  tags: Tag[];                     // Strengths this role needs; used to pick models for the member and to split work
  origin?: "" | "model";           // "model" = a member created automatically when a model from My models was pulled into the group (the member is that model itself)
  engine?: string;                 // Non-empty = an external agent member (e.g. workbuddy): it bypasses model routing and speaks through its own CLI engine
  engine_cfg?: ExternalCfg;
}
export type ExternalLevel = "read" | "edit" | "full";
export interface ExternalCfg {
  level: ExternalLevel;
  risk_ack: boolean;
  cwd: string;                     // Empty = a dedicated working directory under the data directory
  add_dirs: string[];
  web: boolean;
  model: string;
  max_turns: number;
  timeout: number;
  handoff: boolean;
  cli_path: string;                // command-line engines
  base_url: string;                // chat gateways: the OpenAI-compatible endpoint
  api_key: string;                 // chat gateways: never sent back to the UI (it shows "***")
  has_key?: boolean;               // whether a key is stored — the UI never sees the key itself
}
export interface ExternalOverview {
  enabled: boolean;                // Master switch for external agents
  external_calls_enabled: boolean;
  engines: { id: string; name: string; avatar: string; role: string; found: boolean; path: string; via: string; hint: string;
             kind: "cli" | "http"; base_url: string; docs: string; key_hint: string }[];
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
/** An entry in the template gallery (Settings → Template gallery). kind decides what happens when you click Use. */
export type GalleryKind = "team" | "agent" | "skill" | "prompt" | "mcp";
export interface GalleryMember { name: string; avatar: string; role: string }
export interface GalleryItem {
  id: string;                        // Carries a category prefix such as team:office / agent:reviewer / skill:xxx
  kind: GalleryKind;
  name: string;
  summary: string;
  icon: string;                      // emoji
  tags: string[];
  source: string;                    // builtin = shipped with the app; custom:<filename> = one you dropped into the data directory
  home?: boolean;                    // Team templates: whether it also shows on the home screen (which only lists the common ones)
  installed: boolean;
  state_note: string;                // A status note such as already in the skill library; empty when there is none
  preview: {
    members?: GalleryMember[]; host?: string; skills?: string[]; prompt?: string;
    avatar?: string; role?: string; tags?: string[];
    description?: string; scope?: string; body?: string;
    kind?: string; content?: string;
    command?: string; args?: string[]; env_keys?: string[]; note?: string;
  };
}
export interface GalleryOverview {
  catalog_version: string;
  schema_version: number;
  categories: { id: GalleryKind; label: string; hint: string }[];
  counts: Record<string, number>;
  total: number;
  items: GalleryItem[];
  custom: {
    dir: string;                     // Directory holding custom templates (display only; the UI never asks for a path)
    exists: boolean;
    loaded: number;
    files: { name: string; items: number; version: string; author: string }[];
    errors: { file: string; index?: number; id?: string; reason: string }[];
  };
}
export interface GalleryApplyResult {
  kind: GalleryKind;
  id: string;
  name: string;
  summary: string;                   // A one-liner that can be shown to the user as is
  group: Group | null;
  agents: string[];
  added: string[];                   // What was actually written this time
  skipped: string[];                 // Skipped because it already existed
  notes: string[];                   // Reminders the user should know about
}
export interface AgentPreset {
  key: string;
  name: string;
  avatar: string;
  role: string;
  tags: Tag[];
  prompt: string;
  kind: "role" | "expert";         // general roles and domain experts are listed separately
  exists: boolean;                 // A member with this name already exists (adding reuses it)
}
export type LibraryMode = "all" | "selected" | "off";
export type PlanMode = "inherit" | "auto" | "on" | "off";
export interface GroupExt {
  skills: string[];                // Skills attached to this group (both chat-rule and member kinds)
  plugins: string[];               // Enabled plugin IDs
  mcp: string[];                   // Enabled MCP server IDs
  /** Which knowledge bases this group searches; selection is by base, not by document */
  library: { mode: LibraryMode; kb_ids: string[]; collection_ids: string[] };
  plan: PlanMode;                  // inherit = follow the global setting
  memory: boolean;
}
export interface Group {
  id: string;
  name: string;
  host_agent_id: string | null;
  member_ids: string[];
  ext: GroupExt;
  prompt: string;                  // Group prompt (supports {{variables}})
  last_message?: string;
  last_at?: number;
}
interface Attempt {
  model_id: string;
  status: "ok" | "failed" | "skipped";
  detail: string;
  latency_ms: number;
  /** Stable code for why a model was skipped: "" | "offline" | "tripped". */
  reason?: string;
}
export interface ToolCall {
  name: string;
  args: Record<string, string | number | boolean | null>;
  status: "running" | "waiting" | "ok" | "failed" | "denied";   // waiting = waiting for you to confirm in the chat; denied = refused, timed out, or blocked, so it never ran
  ms?: number;
  preview?: string;
  /** Files the call produced (a generated clip); they live in the group's workspace */
  files?: { kind: string; name: string; bytes?: number; seconds?: number }[];
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
    /** Images the user attached to this message; only a model that can look at images receives them */
    images?: Attachment[];
    plan_id?: string;              // Which plan board this message belongs to
    task_id?: string;              // Task ID; "final" = the host's synthesis
    task_title?: string;
    engine?: string;               // External agent message: engine name (e.g. workbuddy)
    level?: ExternalLevel;         // Permission level for the external agent's message
    denied?: string[];             // Names of tools blocked by permissions
    external?: { cost_usd?: number; duration_ms?: number; num_turns?: number; model?: string };
    // Plan board contents when sender_type === "plan"
    kind?: "plan";
    goal?: string;
    conventions?: string;
    status?: PlanStatus;
    tasks?: PlanTaskView[];
    /** Present once the round has been graded. The mechanical half is always filled in; `judged`
     *  on a task says whether a model actually scored it, so "no score" never reads as "good". */
    score?: PlanScore;
  };
  created_at?: number;
  streaming?: boolean;
}
export type Verdict = "ok" | "weak" | "rework" | "failed";
/** One graded task. `delivered` and `usable` are the two questions asked — "did the member produce
 *  what was asked" and "could the next task build on it" — and either may be null on a task the
 *  judge did not cover. */
export interface TaskScore {
  task_id: string;
  owner: string;
  title: string;
  verdict: Verdict;
  delivered: number | null;
  usable: number | null;
  reason: string;
  judged: boolean;
  mechanical: { status: string; chars: number; empty: boolean; error: boolean;
                fallback: boolean; tools: string[]; delivered: boolean };
}
export interface PlanScore {
  at: number;
  threshold: number;             // Percent; below this a task counts as needing rework
  judge: string;                 // Model id that graded the round; empty = nothing graded it
  judge_error: string;           // Why there is no judge, when there is none
  skipped?: string;              // "disabled" | "no tasks" when scoring did not run at all
  tasks: TaskScore[];
  lessons: { scope: string; scope_id: string; content: string; memory_id: string }[];
  summary: { total: number; judged: number; ok: number; weak: number; rework: number; failed: number };
}
export interface Settings {
  external_calls_enabled: boolean;
  external_agents_enabled: boolean;   // Master switch for external agents (e.g. WorkBuddy); off by default
  route_chain: string[];
  max_hops: number;
  history_limit: number;
  history_clip: number;            // Max characters of past messages carried into the prompt
  tool_output_limit: number;       // Max characters of tool output fed back to the model
  request_timeout: number;
  circuit_threshold: number;
  circuit_cooldown: number;
  system_prompt: string;           // Global system prompt (supports {{variables}})
  plan_mode: Exclude<PlanMode, "inherit">;   // auto = the host splits complex tasks first
  plan_max_tasks: number;
  tool_rounds: number;             // Max tool-call rounds per reply (0 = no tools)
  tool_timeout: number;
  memory_enabled: boolean;
  memory_auto_extract: boolean;
  memory_top_k: number;
  library_top_k: number;
  app_repo: string;                // GitHub repository of the app itself, owner/repo
  catalog_url: string;
  github_token: string;            // Write-only: always reads back empty; use github_token_set to tell whether it is set
  github_token_set: boolean;
  auto_check_updates: boolean;
  update_interval_hours: number;
  auto_update_skills: boolean;
  obsidian_dir: string;            // Read-only: set through /api/obsidian
  obsidian_auto: boolean;
  perm_mode: PermMode;             // Tool call approval mode
  perm_timeout: number;            // Seconds to wait for your confirmation; a timeout counts as a refusal
  perm_allow: string[];            // Tool names set to Always allow
  perm_deny: string[];             // Tool names set to Always block
  video_enabled: boolean;          // Let members generate video; off by default. The endpoint is not a chat model
  video_provider_id: string;       // Which video provider to use; empty = the first enabled one
  video_short_edge: number;        // Output short edge in pixels (H3 is natively 768)
  video_max_seconds: number;       // Longest clip a member may ask for (H3 itself accepts 4-15)
  video_timeout: number;           // How long one generation may take before giving up, in seconds
  video_max_mb: number;            // Cap on the downloaded clip, checked before it is saved
  code_enabled: boolean;           // Let members write and run code in a workspace; off by default
  code_timeout: number;            // Seconds one run may take before it is killed
  code_workdir: string;            // Empty = <data dir>/workspace
  vision_cloud: boolean;           // May attached images reach a cloud model? Separate from external_calls_enabled
  vision_max_mb: number;           // Per-image size cap, checked before anything is written to disk
  whatsapp_enabled: boolean;        // Let WhatsApp drive a group chat; off by default
  whatsapp_group_id: string;        // Which group chat answers; empty = the channel does nothing
  whatsapp_phone_number_id: string; // Cloud API phone number id, used when sending
  whatsapp_allowed: string[];       // Who may talk to it, as phone numbers; empty = nobody
  whatsapp_public_host: string;     // Public hostname of the tunnel/VPS in front of this app
  whatsapp_proxy: string;           // graph.facebook.com is unreachable from mainland China
  whatsapp_max_chars: number;       // Reply length sent back (WhatsApp's own limit is 4096)
  whatsapp_prefix: string;          // Text put in front of every reply
  whatsapp_token: string;           // Write-only: reads back empty, like github_token
  whatsapp_app_secret: string;      // Write-only
  whatsapp_verify_token: string;    // Write-only
  whatsapp_token_set: boolean;
  whatsapp_app_secret_set: boolean;
  whatsapp_verify_token_set: boolean;
  scoring_enabled: boolean;          // Grade each planned round with a separate judge model
  score_judge_model: string;         // Empty = pick one automatically (never a group member)
  score_threshold: number;           // Percent; below this a task counts as needing rework
  score_excerpt_chars: number;       // How much of each deliverable the judge reads (head + tail)
  score_max_lessons: number;         // How many lessons one round may write into memory
}
/** State of the inbound WhatsApp channel, in the shape the settings page shows it.
 *  A webhook is invisible by nature, so without this the only symptom of a
 *  misconfiguration is silence. */
export interface WhatsAppStatus {
  enabled: boolean;
  webhook_path: string;
  public_url: string;
  group_id: string;
  allowed: string[];
  counters: {
    accepted: number;
    rejected: number;   // refused before any work: bad signature, unconfigured, body too large
    ignored: number;    // authenticated but dropped: not allowlisted, duplicate, rate limited
    last_inbound: { at?: number; wa_id?: string; name?: string; text?: string };
    last_reply: { at?: number; ok?: boolean; detail?: string; chars?: number };
    last_error: string;
  };
}
/** One image attached to a message. `bytes` is the stored size, `mime` what the server sniffed. */
export interface Attachment {
  id: string;
  name: string;
  mime: string;
  bytes: number;
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
  /** Almost every mapped file vanished at once, so deletions were held back */
  mass_missing?: boolean;
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
/** A tool call waiting for your confirmation */
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
    /** Where runs go when no custom workspace is set; shown as the placeholder */
    code_default_dir: string;
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
/** Local model recommendation catalog (backend /api/local/catalog). fit/disk_ok/slow are rough estimates from this machine's memory, disk, and acceleration, not guarantees. */
export type LocalFit = "ok" | "tight" | "no" | "unknown";
interface LocalHardware {
  system: string;
  machine: string;
  /** The current Python is the x86_64 build, running translated by Rosetta on Apple silicon */
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
  scope: "member" | "group";       // group = a chat-prompt skill attached to the whole group
  version: string;
  source: { repo: string; path: string } | null;
  body?: string;                   // Returned only for a single read, create, or update
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
  env: Record<string, string>;     // Values are masked as ••••••; sending one back unchanged keeps it
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
  /** The knowledge base this document lives in */
  kb_id: string;
  created_at: number;
}
export interface LibraryHit {
  doc_id: string;
  title: string;
  idx: number;
  text: string;
  score: number;
}
/** A named bag of documents. `group_id` empty = shared: any group may attach it. */
export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  group_id: string;
  docs: number;
  created_at: number;
}
/** A flat, reusable list of knowledge bases. Not nestable on purpose. */
export interface Collection {
  id: string;
  name: string;
  description: string;
  kb_ids: string[];
  kbs: { id: string; name: string; group_id: string; docs: number }[];
  created_at: number;
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
    strengths: Tag[];              // Member role strengths ∪ strengths of the models used (at most 6)
    origin: "" | "model";
    engine?: string;               // Non-empty = an external agent
    model_problem: string;         // Why the assigned model is unusable right now (another model is used instead); empty = fine
  }[];
  tools: { name: string; description: string; source: string }[];
  problems: string[];
  /** An MCP server that has simply never been connected yet (not a real error) */
  mcp_deferred: boolean;
  /** Whether members can generate video here, and if not, why not (the panel shows the reason) */
  video: { enabled: boolean; provider: { id: string; name: string; base_url: string } | null; problem: string };
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
  /** false = only shown in Settings → Template gallery, not on the home screen (which stays lean) */
  home?: boolean;
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
  configured: boolean;             // Whether an app repository is configured
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
  sha256: string;                  // Required to install a plugin; the server re-downloads and compares it
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
  | { type: "plan"; message: Message }                                        // Plan board updated (the whole message is replaced)
  | { type: "tool"; message_id: string; index: number; call: ToolCall }       // Status change for the tool call at index
  | { type: "approval"; approval: Approval }                                  // A tool call is waiting for your confirmation
  | { type: "approval_done"; id: string; group_id: string; decision: "allow" | "deny" | "timeout" | "cancelled" }
  | { type: "stopped" }
  | { type: "idle" };

// ------------------------------------------------------------------- http
/** Turn the backend error body into something readable: FastAPI validation errors are arrays and must not be JSON.stringify'd straight to the user */
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

/**
 * An error the backend raised, carrying its HTTP status.
 *
 * Callers that need to branch on *why* a request failed must use `status` rather than
 * matching on the message: the text follows the interface language, so a check like
 * `/已有同名/` silently stops matching as soon as the UI runs in English.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Single entry point for fetch: attaches the token, explains a failed connection, and turns the error body into text */
async function call(path: string, init: RequestInit): Promise<Response> {
  let r: Response;
  try {
    r = await fetch(API + path, init);
  } catch {
    throw new Error(tr("Cannot reach the local backend (it may still be starting up, or it has exited)"));
  }
  if (!r.ok) {
    let detail: unknown;
    try {
      detail = (await r.json()).detail;
    } catch {
      /* Not JSON */
    }
    throw new ApiError(errorText(detail, `${r.status} ${r.statusText}`.trim()), r.status);
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
  /** Heartbeat: is the backend alive? (no token needed) */
  ping: () => get<{ ok: boolean }>("/api/health"),
  presets: () => get<Preset[]>("/api/presets"),
  // ---- Model services
  providers: () => get<Provider[]>("/api/providers"),
  addProvider: (b: Record<string, unknown>) => post<Provider>("/api/providers", b),
  patchProvider: (id: string, b: Record<string, unknown>) => patch<Provider>(`/api/providers/${id}`, b),
  delProvider: (id: string) => del(`/api/providers/${id}`),
  addModel: (pid: string, model_name: string) => post<Model>(`/api/providers/${pid}/models`, { model_name }),
  /** strengths: string[] = a custom list; null = go back to auto-inference */
  patchModel: (id: string, b: { enabled?: boolean; display_name?: string; strengths?: Tag[] | null }) => patch<Model>(`/api/models/${id}`, b),
  delModel: (id: string) => del(`/api/models/${id}`),
  addModels: (pid: string, model_names: string[]) => post<Model[]>(`/api/providers/${pid}/models/batch`, { model_names }),
  strengthTags: () => get<{ tags: { id: Tag; label?: string; desc: string }[] }>("/api/strengths"),
  modelOptions: (pid: string) => get<ModelOptions>(`/api/providers/${pid}/model-options`),
  /** Ask providers for their live list and return the merged options (needs network; cloud providers return 403 when outbound calls are off) */
  refreshModelOptions: (pid: string) => post<ModelOptions>(`/api/providers/${pid}/model-options/refresh`),
  /** Mark every current model as seen (clears the new flags) */
  markModelsSeen: (pid: string) => post<ModelOptions>(`/api/providers/${pid}/model-options/seen`),
  /** Rank currently available models by strengths (score = number of matching strengths); returns an empty array when no key is set and there are no local models */
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
  /** Probe reachability. cloud=false only tests local services (no tokens spent); cloud models get a very short request */
  checkModelsHealth: (model_ids?: string[], cloud = true) =>
    post<{ checked: number; health: Record<string, ModelHealth> }>("/api/models-health/check", { model_ids, cloud }),
  testModel: (model_id: string) =>
    post<{ ok: boolean; latency_ms?: number; reply?: string; error?: string }>("/api/test-model", { model_id }),
  /** Is the video server awake? Renders nothing, so it costs one request rather than GPU minutes. */
  testVideo: (provider_id = "") =>
    post<{ ok: boolean; provider: { id: string; name: string; base_url: string } | null; detail: string }>("/api/video/test", { provider_id }),
  settings: () => get<Settings>("/api/settings"),
  putSettings: (b: Partial<Settings>) => put<Settings>("/api/settings", b),
  whatsappStatus: () => get<WhatsAppStatus>("/api/whatsapp/status"),
  whatsappProbe: () => post<{ ok: boolean; detail: string }>("/api/whatsapp/probe", {}),
  routePreview: (preferred?: string | null) =>
    get<RoutePreview>("/api/route/preview" + (preferred ? `?preferred=${encodeURIComponent(preferred)}` : "")),
  localStatus: () => get<LocalStatus>("/api/local/status"),
  localCatalog: () => get<LocalCatalog>("/api/local/catalog"),
  localCheck: () => post<LocalCheckResult>("/api/local/check", {}),
  localAdd: (tag: string, note = "") => post<{ tag: string; size_gb: number }>("/api/local/catalog/add", { tag, note }),
  localRemoveExtra: (tag: string) => del<{ ok: boolean }>(`/api/local/catalog/extra?tag=${encodeURIComponent(tag)}`),
  applyLocalCatalog: () => post<{ applied?: boolean; latest?: string }>("/api/local/catalog/apply", {}),
  // ---- Members and group chats
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
  /** Pull preset roles (host/reviewer/scribe/librarian/programmer/translator/analyst/planner…) into a group at any time; a member with the same name is reused */
  addMemberFromPreset: (gid: string, key: string) => post<Group>(`/api/groups/${gid}/members/from-preset`, { key }),
  externalOverview: () => get<ExternalOverview>("/api/external"),
  externalCreate: (b: { engine?: string; name?: string; group_id?: string; cfg: Partial<ExternalCfg> }) => post<Agent>("/api/external/agents", b),
  externalPatch: (id: string, cfg: Partial<ExternalCfg>) => patch<Agent>(`/api/external/agents/${id}`, { cfg }),
  externalTest: (b: { live?: boolean; agent_id?: string; engine?: string; cli_path?: string; base_url?: string; api_key?: string }) => post<ExternalProbe>("/api/external/test", b),
  /** Template gallery: the catalog (first-party templates shipped with the app + custom ones in the data directory) */
  gallery: () => get<GalleryOverview>("/api/gallery"),
  /** Template detail: brings back the full body so you can read it before installing */
  galleryDetail: (id: string) => get<GalleryItem & { def: Record<string, unknown> }>(`/api/gallery/${encodeURIComponent(id)}`),
  /** One-click apply: team creates a group, agent creates a member (optionally joining the group), skill/prompt go into their libraries, mcp is added disabled */
  galleryApply: (id: string, body: { name?: string; group_id?: string; overwrite?: boolean } = {}) =>
    post<GalleryApplyResult>(`/api/gallery/${encodeURIComponent(id)}/apply`, body),
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
  /** Whether a collaboration round is running in this group (used after a page refresh or WebSocket reconnect to restore the sending state) */
  groupStatus: (gid: string) => get<{ busy: boolean }>(`/api/groups/${gid}/status`),
  clearMessages: (gid: string) => del(`/api/groups/${gid}/messages`),
  send: (gid: string, text: string, images: string[] = []) => post(`/api/groups/${gid}/messages`, { text, images }),
  stop: (gid: string) => post(`/api/groups/${gid}/stop`),
  // ---- Skills / plugins / MCP (kept separate)
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
  // ---- Knowledge bases and collections. `group` narrows to what one group chat can reach: its
  // workspace's own knowledge bases plus the shared ones.
  kbs: (group?: string) => get<KnowledgeBase[]>(`/api/knowledge-bases${qs({ group_id: group })}`),
  addKb: (b: { name: string; description?: string; group_id?: string }) => post<KnowledgeBase>("/api/knowledge-bases", b),
  patchKb: (id: string, b: { name?: string; description?: string }) => patch<KnowledgeBase>(`/api/knowledge-bases/${id}`, b),
  /** Removes the knowledge base and every document in it */
  delKb: (id: string) => del<{ ok: boolean; deleted_docs: number }>(`/api/knowledge-bases/${id}`),
  collections: () => get<Collection[]>("/api/collections"),
  addCollection: (b: { name: string; description?: string; kb_ids?: string[] }) => post<Collection>("/api/collections", b),
  patchCollection: (id: string, b: { name?: string; description?: string; kb_ids?: string[] }) => patch<Collection>(`/api/collections/${id}`, b),
  delCollection: (id: string) => del<{ ok: boolean }>(`/api/collections/${id}`),
  // ---- Documents. Narrow to one knowledge base, or to everything a group can reach; no scope
  // means the whole library, which is what the overview shows.
  library: (scope: { kb?: string; group?: string } = {}) =>
    get<{ docs: LibraryDoc[]; total_chars: number; count: number }>(`/api/library${qs({ kb_id: scope.kb, group_id: scope.group })}`),
  addNote: (title: string, content: string, scope: { kb?: string; group?: string } = {}) =>
    post<LibraryDoc>("/api/library/note", { title, content, kb_id: scope.kb ?? "", group_id: scope.group ?? "" }),
  /** The request body is the file's raw bytes (txt/md/csv/json/html/pdf/docx) */
  uploadDoc: (file: File, scope: { kb?: string; group?: string } = {}) =>
    postRaw<LibraryDoc>(`/api/library/upload${qs({ filename: file.name, kb_id: scope.kb, group_id: scope.group })}`, file),
  // ---- Images attached to a message
  /** Raw bytes, like the library upload. The server decides the type from the bytes, not the name. */
  uploadImage: (gid: string, file: File) => postRaw<Attachment>(`/api/groups/${gid}/attachments${qs({ filename: file.name })}`, file),
  dropImage: (id: string) => del<{ ok: boolean }>(`/api/attachments/${id}`),
  /** The bytes, fetched with the token in a header — see `getBlob` for why an <img src> will not do. */
  imageBytes: (id: string) => getBlob(`/api/attachments/${id}`),
  /** A clip a member generated, out of that group's own workspace. Same header problem, so the
   *  bytes come through the API and are turned into an object URL (`MessageVideo`). */
  videoBytes: (gid: string, name: string) => getBlob(`/api/groups/${gid}/video/${encodeURIComponent(name)}`),
  addDocUrl: (url: string, scope: { kb?: string; group?: string } = {}) =>
    post<LibraryDoc>("/api/library/url", { url, kb_id: scope.kb ?? "", group_id: scope.group ?? "" }),
  addDocDir: (path: string, recursive = true, scope: { kb?: string; group?: string } = {}) =>
    post<{ added: LibraryDoc[]; skipped: { name: string; reason: string }[] }>("/api/library/dir", { path, recursive, kb_id: scope.kb ?? "", group_id: scope.group ?? "" }),
  searchLibrary: (q: string, top_k = 5, scope: { kb?: string; group?: string } = {}) =>
    get<LibraryHit[]>(`/api/library/search${qs({ q, top_k, kb_id: scope.kb, group_id: scope.group })}`),
  readDoc: (id: string, start = 0) =>
    get<{ doc: LibraryDoc; start: number; end: number; total: number; text: string }>(`/api/library/${id}${qs({ start })}`),
  patchDoc: (id: string, b: { title?: string; enabled?: boolean; kb_id?: string }) => patch<LibraryDoc>(`/api/library/${id}`, b),
  delDoc: (id: string) => del(`/api/library/${id}`),
  // ---- Memory
  memories: (f: { scope?: MemoryScope; scope_id?: string; kind?: MemoryKind; q?: string } = {}) =>
    get<{ memories: Memory[]; count: number }>(`/api/memories${qs(f)}`),
  addMemory: (b: { content: string; scope?: MemoryScope; scope_id?: string; kind?: MemoryKind; pinned?: boolean }) =>
    post<Memory>("/api/memories", b),
  patchMemory: (id: string, b: { content?: string; kind?: MemoryKind; pinned?: boolean }) => patch<Memory>(`/api/memories/${id}`, b),
  delMemory: (id: string) => del(`/api/memories/${id}`),
  clearMemories: (f: { scope?: MemoryScope; scope_id?: string; source?: string }) =>
    del<{ deleted: number }>(`/api/memories${qs(f)}`),
  // ---- Prompts
  prompts: () => get<PromptsInfo>("/api/prompts"),
  addPrompt: (b: { title: string; content: string; kind?: "general" | "group"; use_globally?: boolean }) => post<PromptItem>("/api/prompts", b),
  patchPrompt: (id: string, b: Partial<Pick<PromptItem, "title" | "content" | "kind" | "use_globally">>) =>
    patch<PromptItem>(`/api/prompts/${id}`, b),
  delPrompt: (id: string) => del(`/api/prompts/${id}`),
  /** Substitute real member/group data into {{variables}} and estimate tokens */
  previewPrompt: (content: string, ids: { agent_id?: string; group_id?: string } = {}) =>
    post<{ text: string; tokens: number; raw_tokens: number }>("/api/prompts/preview", { content, ...ids }),
  resetSystemPrompt: () => post<{ system_prompt: string }>("/api/prompts/reset-system"),
  // ---- Updates & discovery (GitHub)
  updates: () => get<UpdatesInfo>("/api/updates"),
  checkUpdates: () => post<Record<string, unknown>>("/api/updates/check"),
  dismissUpdate: (id: string) => post(`/api/updates/${id}/dismiss`),
  searchRepos: (kind: "skill" | "plugin" | "mcp", q = "") => get<RepoHit[]>(`/api/updates/search${qs({ kind, q })}`),
  repoSkills: (repo: string, ref = "") => get<RepoFile[]>(`/api/updates/repo/skills${qs({ repo, ref })}`),
  repoPlugins: (repo: string, ref = "") => get<RepoFile[]>(`/api/updates/repo/plugins${qs({ repo, ref })}`),
  repoReadme: (repo: string) => get<{ repo: string; content: string; url: string }>(`/api/updates/repo/readme${qs({ repo })}`),
  previewFile: (repo: string, path: string, ref = "") => post<FilePreview>("/api/updates/preview", { repo, path, ref }),
  installSkill: (repo: string, path: string, ref = "", overwrite = false, sha256 = "") =>
    post<Skill>("/api/updates/skill/install", { repo, path, ref, overwrite, sha256 }),
  updateSkill: (name: string) => post<Skill>(`/api/updates/skill/update/${encodeURIComponent(name)}`),
  /** sha256 must come from a previewFile result; the server re-downloads and compares it, and refuses if the content changed */
  installPlugin: (repo: string, path: string, sha256: string, ref = "", overwrite = false) =>
    post<{ id: string; plugins: PluginInfo[] }>("/api/updates/plugin/install", { repo, path, ref, sha256, overwrite }),
  applyCatalog: () => post<Record<string, unknown>>("/api/updates/catalog/apply"),
};

/** POST whose body is the file's raw bytes (library upload, backup restore) */
async function postRaw<T>(path: string, body: Blob): Promise<T> {
  const r = await call(path, { method: "POST", headers: { ...authHeaders(), "Content-Type": "application/octet-stream" }, body });
  return (await r.json()) as T;
}

/**
 * GET returning the raw body. Needed for images: the backend wants the token in a header, and
 * an <img src> cannot send one — its request would come back 401. Callers turn the blob into an
 * object URL and revoke it when they are done with it.
 */
async function getBlob(path: string): Promise<Blob> {
  const r = await call(path, { method: "GET", headers: authHeaders() });
  return await r.blob();
}

/** Download a file returned by the backend; the name comes from Content-Disposition (handles the filename*=UTF-8'' form) */
async function download(path: string, fallbackName: string, what: string): Promise<void> {
  let r: Response;
  try {
    r = await call(path, { headers: authHeaders() });
  } catch (e) {
    throw new Error(tr("{what} failed: {message}", { what, message: (e as Error).message }));
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

export const downloadBackup = (includeKeys: boolean) => download(`/api/data/export?include_keys=${includeKeys}`, "team-agent-backup.db", tr("Export"));
export const downloadChat = (gid: string) => download(`/api/groups/${gid}/export`, "chat.md", tr("Export"));

/** Pull an Ollama model, reporting progress line by line; returns whether it succeeded. */
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
        if (closed) return;   // Deliberately closed (group switch / unmount): stop notifying, so the new connection's state is not overwritten with disconnected
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
  if (d < 60) return tr("Just now");
  if (d < 3600) return tr("{n} min ago", { n: Math.floor(d / 60) });
  if (d < 86400) return tr("{n} h ago", { n: Math.floor(d / 3600) });
  if (d < 86400 * 30) return tr("{n} d ago", { n: Math.floor(d / 86400) });
  return new Date(ts * 1000).toLocaleDateString();
}

export function modelLabel(id: string | null | undefined, models: Model[]): string {
  if (!id) return tr("Default routing");
  if (id.startsWith("ext:")) return tr("{name} (external)", { name: id.slice(4).replace(/^./, (c) => c.toUpperCase()) });
  const m = models.find((x) => x.id === id);
  return m ? m.display_name : id.split("/").slice(1).join("/") || id;
}
