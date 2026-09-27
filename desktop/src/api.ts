import { useEffect, useRef } from "react";
import { currentLang, tr } from "./i18n";

declare global {
  interface Window {
    teamAgent?: { token?: string; api?: string; pickFolder?: () => Promise<string | null>;
                  openPath?: ((p: string) => Promise<{ ok: boolean; why?: string }>) | null;
                  // 成果栏右键菜单用的那一组。**浏览器里全是 null** —— 菜单据此不显示做不到的动作,
                  // 而不是给一个点了没反应的条目(和 `openPath` 同一条规矩)。
                  openFile?: ((p: string) => Promise<{ ok: boolean; why?: string }>) | null;
                  reveal?: ((p: string) => Promise<{ ok: boolean; why?: string }>) | null;
                  copyFile?: ((p: string) => Promise<{ ok: boolean; why?: string; how?: "image" | "path" }>) | null;
                  copyText?: ((text: string) => Promise<{ ok: boolean; why?: string }>) | null;
                  shareTargets?: (() => Promise<{ id: string; app: string }[]>) | null;
                  openWith?: ((app: string, p: string) => Promise<{ ok: boolean; why?: string }>) | null };
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

/** What a model is for. A gateway lists several kinds on one endpoint, so "this model exists" and
 *  "a member can talk to it" stopped being the same statement. */
export type ModelUse = "chat" | "image" | "video" | "responses";

export interface Model {
  setup_required?: boolean;       // Declared workflow without an executable graph
  id: string;                      // provider_id/model_name
  provider_id: string;
  model_name: string;
  display_name: string;
  enabled: boolean;
  provider_name?: string;
  kind?: string;
  is_local?: boolean;
  provider_enabled?: boolean;
  use: ModelUse;                   // chat = a member can be pointed at it; the others cannot
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
  credentials_ready?: boolean;     // Includes supported environment-variable credentials
  key_hint: string;
  models: Model[];
  /** The rows that generate instead of chatting. Kept apart from `models` because that list is what
   *  a member can be pointed at as a conversational model; these can only join as a media member. */
  media_models: Model[];
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
  use: ModelUse;                   // What the provider says this model is for (see media.purpose_of)
}
export interface MediaOptions {
  use: "image" | "video";
  providers: {
    id: string;
    name: string;
    is_local: boolean;
    enabled: boolean;
    direct: boolean;               // a real image/video service, rather than a gateway that happens to serve one
    models: { id: string; added: boolean; enabled: boolean | null }[];
  }[];
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
/** Where a member came from. Empty for one made here by hand; `model` for one created by
 *  pulling a model in; `workbuddy:<slug>` for one imported from an expert package, which is
 *  also what makes a second import of the same package a skip. */
export type AgentOrigin = "" | "model" | "media" | `workbuddy:${string}`;
export interface Agent {
  is_tool?: boolean;
  id: string;
  name: string;
  avatar: string;
  role: string;
  prompt: string;
  model_id: string | null;         // null = pick a model automatically from the strength tags
  skills: string[];
  tags: Tag[];                     // Strengths this role needs; used to pick models for the member and to split work
  origin?: AgentOrigin;            // "model" = a member created automatically when a model from My models was pulled into the group (the member is that model itself); "media" = the same, for a model that *generates* — it can be addressed in a group, and the sentence it is addressed with becomes its prompt; "workbuddy:<slug>" = imported from an expert package
  engine?: string;                 // Non-empty = an external agent member (e.g. workbuddy): it bypasses model routing and speaks through its own CLI engine
  engine_cfg?: ExternalCfg;
  /** Can this member be the one in charge of a group chat? False for an external agent (it has its
   *  own tools and does not take part in the plan protocol) and for a generator (`origin="media"`).
   *  Sent by the backend from `media.may_host` rather than worked out here: the first member picked
   *  for a new group becomes its host, so the picker has to know who may take that seat, and a
   *  second copy of the rule in TypeScript is one that can drift away from the API's answer.
   *  Absent means "not told" (an older backend) — treat that as yes, not as no. */
  may_host?: boolean;
}

/** Can this member hold the chair of a group chat?
 *
 *  The rule is the backend's (`media.may_host`, decided by `engine` and `origin`) and arrives per
 *  member. This asks it — the one place in the client that reads the field, so "absent means not
 *  told, which means yes" is written down once instead of at every picker that cares. */
export const mayHost = (a: Agent): boolean => a.may_host !== false;

/** One member the team suggester picked, with the sentence explaining what for. */
export interface TeamAdviceMember {
  id: string;
  name: string;
  avatar: string;
  role: string;
  may_host: boolean;
  why: string;
  score: number;
}

/** An expert preset that fits this task but is not a member yet. `key` is what creates it. */
export interface TeamAdviceExpert {
  key: string;
  name: string;
  name_zh: string;
  avatar: string;
  role: string;
  why: string;
  score: number;
}

/** What the suggester read out of the task, and who it would put in the group.
 *
 *  `reads` is shown as well as the names: a lineup nobody can check is a lineup nobody trusts, and
 *  "I read this as a video + writing job" is the one line that makes the choice arguable. */
export interface TeamAdvice {
  host_ref?: string;
  members: TeamAdviceMember[];
  experts: TeamAdviceExpert[];
  reads: { concepts: string[]; needs: string[] };
  models?: { id: string; name: string; use: ModelUse; why: string }[];
  warnings?: string[];
}
export interface TeamDraftMember { kind: "agent" | "model" | "preset"; id: string }
export type ExternalLevel = "read" | "edit" | "full";export interface ExternalCfg {
  level: ExternalLevel;
  risk_ack: boolean;
  cwd: string;                     // Empty = a dedicated working directory under the data directory
  add_dirs: string[];
  web: boolean;
  model: string;
  max_turns: number;
  // Off = the engine runs inside this program's isolation (no MCP of its own, a turn cap, a fresh
  // conversation each round). On = it is driven the way its own application drives it, which is
  // what makes its answers match a direct run.
  native: boolean;
  timeout: number;
  handoff: boolean;
  cli_path: string;                // command-line engines
  base_url: string;                // the OpenAI-compatible endpoint: a chat gateway talks to it, a
                                   // command-line engine is pointed at it (so it can run a model the
                                   // user already has without the engine's own account sign-in)
  api_key: string;                 // either kind: never sent back to the UI (it shows "***")
  has_key?: boolean;               // whether a key is stored — the UI never sees the key itself
}
export interface ExternalProviderModel { name: string; display_name: string }
/** The model provider an engine is bound to. Non-null means the engine owns no address, no key and
 * no model list of its own: they all live in that provider row (Settings → Providers), and the
 * member's settings only hold what the provider has no opinion about. */
export interface ExternalProvider {
  id: string;
  missing: boolean;                // the provider row is gone: nothing to talk to until it is back
  name: string;
  base_url: string;
  has_key: boolean;                // whether that provider has a key — the key itself never comes back
  models: ExternalProviderModel[];  // the ones that are enabled, in the provider's own order
}
/** What a member actually resolves to, shown so the user can confirm a model they did not pick. */
export interface ExternalBinding {
  provider_id?: string;
  provider_name?: string;
  model?: string;
  model_default?: boolean;         // true = we fell back (empty, or the chosen model is gone)
  problem?: string;                // plain-language reason when it cannot run as saved
}
/** An address + model pair that was measured working, so "which service?" is one choice instead of
 *  two fields whose spelling the user would have to know. `models` is what the service's own
 *  `/models` answered; `measured` is the subset actually called and answered from here. */
export interface ModelPreset {
  id: string; name: string;
  base_url: string;
  models: string[];
  measured: string[];
  where: string;
}
export interface ExternalOverview {
  enabled: boolean;                // Master switch for external agents
  external_calls_enabled: boolean;
  engines: { id: string; name: string; avatar: string; role: string; found: boolean; path: string; via: string; hint: string;
             // `cmd` = a program on this machine that takes its turn in a group and runs the tool's
             // own command (see localcmd.py). The backend has had it for a while; this union did
             // not, so the dialog could not ask "is this a local tool" — and answered every such
             // member with the command-line engine's form (permission level, working directory,
             // model address, and a path field labelled for WorkBuddy).
             kind: "cli" | "http" | "cmd"; base_url: string; docs: string; key_hint: string;
             // single = this machine has one of these engines, so only one such member can exist;
             // signin = its command line can be signed in by hand at all (false = the interactive
             // bundle is not shipped, so the only way to make it run is to point it at a model)
             single: boolean; signin: boolean;
             provider: ExternalProvider | null }[];
  levels: { id: ExternalLevel; label: string; desc: string }[];
  defaults: ExternalCfg;
  model_presets: ModelPreset[];
  presets_verified: string;        // the date those pairs were measured on
  members: { id: string; name: string; engine: string; cfg: ExternalCfg; workspace: string;
             binding: ExternalBinding }[];
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
export type GalleryKind = "team" | "agent" | "skill" | "prompt" | "hook" | "mcp";
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
    // MCP servers reached over the network instead of by starting a program: the URL and the
    // headers it wants (an empty one means the reader has to paste a token in).
    url?: string; transport?: string; headers?: Record<string, string>; needs?: string[];
    // Hook templates: which events it wants and the source itself, so it can be read on the card
    // before anything is written to the hooks directory.
    events?: string[]; code?: string; timeout_ms?: number;
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
export interface HookEntry {
  id: string;
  name: string;
  description: string;
  events: string[];
  kind: "observe" | "inject" | "gate";
  timeout_ms: number;
  on_error: "auto" | "open" | "closed";
  enabled: boolean;
  groups: string[];                  // Empty = every group
  error: string;                     // Why it cannot run, if it cannot
  last: { ok?: boolean; note?: string; ms?: number; at?: number };
}
export interface HookLogRow {
  at: number;
  hook: string;
  event: string;
  group_id: string;
  ok: boolean;
  note: string;
  tool?: string;
}
export interface AgentPreset {
  key: string;
  name: string;
  avatar: string;
  role: string;
  tags: Tag[];
  prompt: string;
  kind: "role" | "expert";         // general roles and domain experts are listed separately
  agent_id?: string | null;        // Existing profile, used for per-group membership status
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
  /** Where this project works, and what it is doing (computed by the backend). */
  folder?: GroupFolder;
  task?: GroupTask | null;
  id: string;
  name: string;
  host_agent_id: string | null;
  member_ids: string[];
  ext: GroupExt;
  prompt: string;                  // Group prompt (supports {{variables}})
  /** A directory the user picked for this group; empty = the app manages one under its data dir */
  workspace: string;
  /** Where the group's files really are, resolved by the backend (the picked one, or the managed one) */
  workspace_path?: string;
  /** A group is a project: its own state, set by hand. "active" is what every project starts as. */
  status: "active" | "done";
  /** Filed away: kept out of the normal list, still searchable, workspace and history intact */
  archived: boolean;
  /** A turn is running in this project *right now* — a fact about the process, not the database, so
   *  it only comes with the list and it goes stale within seconds. */
  busy?: boolean;
  archived_at?: number | null;
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
  /** Output produced *while the call is still running* (the tail only). Present on a running call
   *  that has something to say mid-flight — a program being run — and gone once it finishes, when
   *  `preview` carries the result. */
  live?: string;
  /** Files the call produced (a generated clip); they live in the group's workspace */
  /** What the call produced, and where to fetch it. `where: "workspace"` means `name` is a path
   *  inside the group's workspace (`MessageLook`); without it, `name` is a bare file name in one of
   *  the generator's own folders (`MessageVideo` / `MessageImageGen`). */
  files?: { kind: string; name: string; path?: string; bytes?: number; seconds?: number; where?: string }[];
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
    routing_tags?: string[];
    routing_auto?: boolean;
    tools?: ToolCall[];
    /** The model's working, when it streamed any (a reasoning model, or an engine's "thinking"
     *  block). Shown above the reply: the answer says what, this says how. */
    thinking?: string;
    /** Images the user attached to this message; only a model that can look at images receives them */
    files?: Attachment[];           // Attachments since they became any kind of file
  images?: Attachment[];           // What a message stored before that is called
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
    integration?: { status: PlanTaskStatus; message_id: string; error: string };
    status?: PlanStatus;
    tasks?: PlanTaskView[];
    /** Tasks the host wrote that could not be assigned, each `"<id>: <why>"`. **The board has to
     *  show these**: they are part of what the user asked for, and a board that simply lists fewer
     *  rows is how a request goes missing without anybody noticing. Until this was rendered the
     *  backend wrote them into the message body, which `PlanCard` never displays. */
    dropped?: string[];
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
  route_auto_match: boolean;
  host_auto_recruit: boolean;
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
  // ---- searching the library by meaning (see backend/app/embed.py)
  embed_enabled: boolean;          // Search by meaning as well as by keyword
  embed_base_url: string;          // OpenAI-compatible /embeddings; the bundled local model by default
  embed_model: string;             // Which model id to ask for there
  embed_autostart: boolean;        // Start the local model on demand — never to download weights
  embed_batch: number;             // Texts per request while indexing
  embed_timeout: number;           // How long one indexing batch may take, in seconds
  embed_api_key: string;           // Write-only: only needed for somebody else's server
  embed_api_key_set?: boolean;
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
  comfyui_auto_start: boolean;
  comfyui_dir: string;
  comfyui_python: string;
  video_provider_id: string;       // Which video provider to use; empty = the first enabled one
  video_model: string;             // Which of its models, where it serves more than one; empty for a single-checkpoint H3
  video_short_edge: number;        // Output short edge in pixels (H3 is natively 768; MetaChat's API takes 480p/720p and this is mapped onto them)
  video_max_seconds: number;       // Longest clip a member may ask for (H3 accepts 4-15, MetaChat 1-15)
  video_timeout: number;           // How long one generation may take before giving up, in seconds
  music_timeout: number;           // How long composing one piece may take (slower than a clip)
  video_max_mb: number;
  assemble_timeout: number;        // How long joining the shots into one film may take, in seconds
  image_enabled: boolean;            // Let members draw images through an OpenAI-compatible service
  image_provider_id: string;         // Which image provider; empty = the first enabled one
  image_model: string;               // The model name to ask that service for
  image_size: string;                // One of the sizes imagegen accepts
  image_timeout: number;
  image_max_mb: number;            // Cap on the downloaded clip, checked before it is saved
  code_enabled: boolean;           // Let members write and run code in a workspace; off by default
  code_timeout: number;            // Seconds one run may take before it is killed
  code_workdir: string;            // Empty = <data dir>/workspace
  vision_cloud: boolean;           // May attached images reach a cloud model? Separate from external_calls_enabled
  vision_max_mb: number;           // Biggest picture sent inline; a larger one is shrunk first
  vision_model_id: string;         // Which model looks at pictures; empty = any usable one, local first
  upload_max_mb: number;           // Biggest file a user may attach (any kind, a video included)
  video_frames: number;            // Stills taken from an attached video for a model that cannot watch it
  refs_budget: number;             // Characters of referenced files allowed in one prompt
  integration_budget: number;      // Characters of task output allowed into the consolidation prompt
  transcribe_cmd: string;          // Speech-to-text command; empty = whichever known transcriber is installed
  advisor_cmd: string;             // Command-line model to consult read-only; empty = claude/codex if installed
  advisor_timeout: number;         // How long one such consultation may take, in seconds
  process_autojoin: boolean;       // Keep the (hidden) process engineer in every group
  process_autolog: boolean;        // Record the defects the app can measure by itself, silently
  process_review: boolean;         // Ask a model outside the group for the cause and the fix
  // Chat channels (whatsapp_*, telegram_*, wecom_*, feishu_*, dingtalk_*, slack_*) are NOT
  // listed here on purpose: the backend's channel catalogue declares every field with its
  // label, bounds and value, so /api/channels is the single source and adding a platform
  // needs no change on this side. The backend still accepts them through /api/settings.
  scoring_enabled: boolean;          // Grade each planned round with a separate judge model
  score_judge_model: string;         // Empty = pick one automatically (never a group member)
  score_threshold: number;           // Percent; below this a task counts as needing rework
  score_excerpt_chars: number;       // How much of each deliverable the judge reads (head + tail)
  score_max_lessons: number;         // How many lessons one round may write into memory
}
/** One configurable field of a chat channel, described by the backend catalogue.
 *  `setting` is the full settings key; `secret` fields never carry a value, only `set`. */
export interface ChannelField {
  key: string;
  setting: string;
  kind: "switch" | "text" | "secret" | "number" | "group" | "numbers" | "ids";
  label: string;
  desc: string;
  placeholder?: string;
  unit?: string;
  min?: number | null;
  max?: number | null;
  secret: boolean;
  value: string | number | boolean | string[];
  set: boolean;
}
/** A chat channel: how it is reachable, what it still needs, and what it has done.
 *  A webhook is invisible by nature, so without the counters the only symptom of a
 *  misconfiguration is silence. */
export interface ChannelInfo {
  id: string;
  name: string;
  avatar: string;
  direction: "both" | "out";
  transport: "webhook" | "poll" | "robot";
  needs_public_url: boolean;
  docs: string;
  summary: string;
  setup: string[];
  fields: ChannelField[];
  settings: Record<string, string | number | boolean | string[]>;
  ready: boolean;
  missing: string[];
  webhook_path: string;
  public_url: string;
  counters: {
    accepted: number;
    rejected: number;   // refused before any work: bad signature, unconfigured, body too large
    ignored: number;    // authenticated but dropped: not allowlisted, duplicate, rate limited
    last_inbound: { at?: number; sender?: string; name?: string; text?: string };
    last_reply: { at?: number; ok?: boolean; detail?: string; chars?: number };
    last_error: string;
    poller: string;     // "", "running", "stopped" — only for channels this app fetches
  };
}
/** One file attached to a message — any kind, not only images. `bytes` is the stored size, `mime`
 *  what the server sniffed, and `kind` is what it decided the file *is* (image / video /
 *  document / audio / other): the UI renders by kind rather than by extension. */
export interface Attachment {
  id: string;
  name: string;
  mime: string;
  bytes: number;
  kind?: string;
  rel_path?: string;
  url?: string;
  source?: string;
  has_text?: boolean;
  has_vision?: boolean;
}
/** A file inside a group's workspace: where a member's work actually lands. */
export interface WorkspaceFile {
  path: string;
  name: string;
  size: number;
  modified: number;
  kind: string;
  folder: string;
}
export interface WorkspaceTask {
  name: string;
  path: string;
  files: number;
  bytes: number;
  modified: number;
}
export interface WorkspaceView {
  path: string;
  base: string;
  /** true = the app manages this folder for the group; false = the user picked it */
  managed: boolean;
  files: WorkspaceFile[];
  tasks: WorkspaceTask[];
}
/** Which model looks at pictures, and what to do when none can. */
export interface VisionStatus {
  model_id: string;
  model_name: string;
  is_local: boolean;
  /** What "automatic" would choose if the switch allowed it — the app's own recommendation, so the
   *  page can say which model "leave it on automatic" means instead of leaving it a blind choice. */
  recommended_id: string;
  recommended_name: string;
  recommended_local: boolean;
  configured: string;
  /** The name of the model the setting names, for when it is one that cannot look. */
  configured_name: string;
  /** Whether that model still exists at all. */
  configured_found: boolean;
  /** null = automatic; false = the named model cannot look at images (an image generator). */
  configured_sees: boolean | null;
  vision_cloud: boolean;
  candidates: { id: string; name: string; is_local: boolean }[];
  blocked_cloud: boolean;
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
  /** Usable models the priority chain does not mention — the chain is an allow-list, so these are
   *  callable but never chosen. Only used to explain an empty chain honestly. */
  unchained: string[];
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
  /** Which section it is filed under, as a stable key (see skills.ts for the wording). */
  category: string;
  /** Files it carries beyond its SKILL.md. 0 = a paragraph of rules; >0 = a manual with files. */
  files: number;
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
  args?: string[];                 // absent on a template reached over the network
  note: string;
  // Servers this app does not start: it connects to an address instead. An empty header value is
  // one the reader has to paste in (a token), and `header_keys` lists exactly those.
  url?: string;
  transport?: string;
  headers?: Record<string, string>;
  header_keys?: string[];
}
/** One other AI application this machine has, and how much of it could be imported.
 *  Discovery only reads the fixed paths the backend knows about. */
export interface ImportSource {
  key: string;
  app: string;
  kind: "mcp" | "skill" | "expert";
  format: string;
  notes: string;
  found: boolean;
  files: string[];
  count: number;
  error: string;
}
/** One importable thing. For an MCP server the credential-bearing fields come back masked:
 *  the import re-reads the source file on the server, so a key never has to reach here. */
export interface ImportItem {
  kind: "mcp" | "skill" | "expert";
  source: string;
  app: string;
  name: string;
  path: string;
  exists: boolean;
  risks?: string[];
  // mcp
  command?: string;
  args?: string[];
  url?: string;
  transport?: string;
  env_keys?: string[];
  header_keys?: string[];
  description?: string;
  enabled?: boolean;
  // skill
  folder?: string;
  chars?: number;
  // expert: `name` is the package's stable key, `label` is what the package calls itself —
  // four shipped experts share one display name, so the two are not interchangeable
  label?: string;
  role?: string;
  avatar?: string;
  bundled_skills?: number;
}
export interface ImportScan {
  items: ImportItem[];
  notes: string[];
  truncated: boolean;
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
  /** Where this document came from (`library.ORIGINS`): imported, fetched, uploaded, a link, an
   *  attachment, a workspace file, or typed in here. A fact about the row, not a label. */
  origin: string;
  /** What the material is *for* (`library.CATEGORIES`), read off its own words when it is fetched
   *  skill material; empty is a real answer ("its own words do not say"). */
  category: string;
  created_at: number;
}
/** How the document list is asked for: a page, a filter, or the classification. */
export interface LibraryView {
  /** Title / filename substring */
  q?: string;
  /** One `library.ORIGINS` token, or "" for all */
  origin?: string;
  /** One document kind (md, pdf, note…), or "" for all */
  kind?: string;
  /** One `library.CATEGORIES` token (what the material is for), or "" for all */
  category?: string;
  /** "", or the dimension the classification view groups by */
  groupBy?: "" | "origin" | "kind" | "kb" | "category";
  offset?: number;
  limit?: number;
  /** How many documents each class shows in the classification view */
  perGroup?: number;
}
/** One class in the classification view: its size, and the first few documents in it. */
export interface LibraryGroup {
  id: string;
  count: number;
  docs: LibraryDoc[];
}
export interface LibraryPage {
  docs: LibraryDoc[];
  /** Documents in the filtered view, and how many are on this page. */
  total: number;
  count: number;
  total_chars: number;
  offset?: number;
  limit?: number;
  /** The classification of the whole scope: token → documents. */
  origins: Record<string, number>;
  kinds: Record<string, number>;
  /** Knowledge base id → documents */
  bases: Record<string, number>;
  /** What the material is for → documents. `""` is "its own words do not say". */
  categories: Record<string, number>;
  /** Present only when a `groupBy` was asked for; then `docs` is empty. */
  groups?: LibraryGroup[];
}
/** One message's verdict, as the feedback system stores it. */
export interface MessageFeedback {
  message_id: string;
  group_id: string;
  agent_id: string;
  agent_name: string;
  /** `""` is "taken back" — the row is gone, not a neutral verdict. */
  rating: "" | "up" | "down";
  note: string;
  created_at: number;
}

/** One row of feedback with the message it is about, as the panel reads it. */
export interface FeedbackItem extends MessageFeedback {
  text?: string;
  sender_type?: string;
  group_name?: string;
}

/** The scoreboard: every member with both counts, so the bad news is not the part that is missing. */
export interface FeedbackBoard {
  items?: FeedbackItem[];
  members: { agent_id: string; name: string; up: number; down: number;
             notes: { rating: string; note: string; message_id: string; at: number; group: string; text: string }[] }[];
  totals: { up: number; down: number; rated: number };
  feedback?: MessageFeedback | null;
}

export interface LibraryHit {  doc_id: string;
  title: string;
  idx: number;
  text: string;
  /** The BM25 score, or 0 when only the vector half found this document. */
  score: number;
  /** The cosine, or 0 when only the keywords found it. Neither is the ranking: the order is the
   *  fused one, and these two are its inputs. Optional because older backends do not send them. */
  sim?: number;
  via?: "keyword" | "vector" | "both";
}
/** What vector search is doing right now, from `GET /api/library/vector`. */
export interface VectorState {
  enabled: boolean;
  model: string;
  address: string;
  /** The model that produced the vectors present, and how many passages carry them. */
  chunks: number;
  with_vectors: number;
  missing: number;
  models: Record<string, number>;
  /** Whether this build can hold vectors at all (needs numpy), and whether the local runtime exists. */
  numpy: boolean;
  venv: boolean;
  searchable: boolean;
  in_index: number;
  dim: number;
  /** Set when vectors were found but belong to another model — the reason nothing is searchable. */
  note: string;
  server: { up: boolean; ready: boolean; state: string; device?: string; dim?: number;
            load_seconds?: number; error?: string; hint?: string };
  weights: boolean;
  start_command: string;
  fetch_command: string;
  job: { running?: boolean; indexed?: number; documents?: number; seconds?: number; left?: number;
         error?: string; started_at?: number; finished_at?: number };
}
/** A named bag of documents. `group_id` empty = shared: any group may attach it. */
export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  group_id: string;
  docs: number;
  /** One word for where this base's material came from, or `mixed` — with `origins` as the evidence.
   *  Derived from the documents unless `source_override` holds a value; empty means there is nothing
   *  in the base to describe yet. */
  source: string;
  /** What the user set, or empty for "read it from the documents". The badge shows `source`; the
   *  editor opens on this, so "automatic" and "the user chose the same word" stay distinguishable. */
  source_override: string;
  /** The heading this base is filed under. A suggestion when it matches `LibraryVocabulary.purposes`,
   *  otherwise whatever the user typed; empty means nobody has said yet. */
  purpose: string;
  /** {origin: documents}. Present so "mixed" arrives with the numbers that make it true. */
  origins: Record<string, number>;
  created_at: number;
}
/** The words the two labels are written in, served by the backend so the closed origin vocabulary
 *  has exactly one definition (`library.ORIGINS`). */
export interface LibraryVocabulary {
  origins: string[];
  mixed: string;
  purposes: string[];
  /** What fetched material can be *for* (`library.CATEGORIES`) — closed, unlike `purposes`. */
  categories: string[];
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
    origin: AgentOrigin;
    engine?: string;               // Non-empty = an external agent
    participation?: { mode: "listener" | "discussion"; kind: "local_tool" | "generator" | "chat" | "external"; summary: string; preparation: string; workspace: string; tools: string[] };
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
  /** Pictures of this group nobody has looked at yet: each one costs a vision call to describe, so
   *  the panel offers it as a button rather than doing it on a timer. */
  pictures_pending: number;
}
/** The folder a project works in, as the sidebar shows it: a short name, and whether it is the
 *  user's own (only their own can be renamed — the app-managed one *is* the group's id). */
export interface GroupFolder {
  name: string;
  path: string;
  mine: boolean;
  exists?: boolean;
  files?: number;
  bytes?: number;
  items?: string[];
  note?: string;
}

/** The one task a project is summarised by: what is running, else what is next, else what it just
 *  finished — plus the board's counts. `null` for a project that never ran a split. */
export interface GroupTask {
  id: string;
  title: string;
  owner: string;
  status: string;
  board: string;
  goal: string;
  at: number;
  done: number;
  failed: number;
  /** Tasks that still have work in them (pending or running) — the ones neither count above covers */
  open?: number;
  total: number;
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
  /** A template saved from a group the user actually ran — it comes first, newest first. */
  user?: boolean;
  /** The group it was saved from, when it was. */
  from_group_name?: string;
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
  | { type: "group_updated"; group: Group }
  | { type: "message"; message: Message }
  | { type: "message_start"; message: Message }
  | { type: "delta"; message_id: string; text: string }
  | { type: "reset"; message_id: string }
  | { type: "message_end"; message: Message }
  | { type: "message_discard"; message_id: string }
  | { type: "plan"; message: Message }                                        // Plan board updated (the whole message is replaced)
  | { type: "tool"; message_id: string; index: number; call: ToolCall }       // Status change for the tool call at index
  // The member's working, streamed while it is still working. Appended to `meta.thinking`; an
  // empty text clears it (the attempt it belonged to failed and another model took over).
  | { type: "thinking"; message_id: string; text: string }
  | { type: "approval"; approval: Approval }                                  // A tool call is waiting for your confirmation
  | { type: "approval_done"; id: string; group_id: string; decision: "allow" | "deny" | "timeout" | "cancelled" }
  | { type: "stopped" }
  | { type: "idle" };

// -------------------------------------------------------------- video zone
/** 曲库里的一首。`source`/`licence` 是**记录**，不是承诺：库里既可能有本机作的分，也可能有用户
 *  自己放进去的。 */
export interface MusicTrack {
  name: string; title: string; mood: string; tags: string[]; seconds: number;
  bytes: number; source: string; licence: string;
}
/** 一首曲子的**制作过程**。分钟级，所以它是后台任务：页面拿到 id 之后轮询它。 */
export interface MusicJob {
  id: string;
  state: "running" | "done" | "failed";
  prompt: string; name: string; seconds: number; bytes: number;
  /** What the vocabulary had to say about the tag string, shown before and after composing. */
  warnings?: string[];
  error: string; note: string; started: number; finished: number;
}
export interface MusicShelf {
  tracks: MusicTrack[];
  /** 读不出来的 sidecar 文件，点名。空数组 = 没有坏文件，不是"没查"。 */
  errors: string[];
  /** 这台机器现在能不能作曲。不能就给出 `why` —— 一句话说清缺什么、去哪配。 */
  composer: { ready: boolean; base_url: string; why: string };
  limits: { min_seconds: number; max_seconds: number; default_seconds: number };
}
export interface MusicComposeIn {
  /** Free text, appended after the faders. On its own it still works — that is what the tool sends. */
  prompt?: string;
  /** The faders. `musicprompt.compose_tags` turns these into the tag string the model reads. */
  genre?: string;
  instruments?: string[];
  production?: string[];
  vocals?: string;
  seconds?: number; bpm?: number; language?: string; seed?: number;
  lyrics?: string; name?: string; mood?: string; tags?: string[];
}
/** One row of a vocabulary column: `label` is already in the UI language. */
export interface VocabRow {
  id: string; label: string;
  /** genres only — the range this genre actually lives in, used to warn about a mismatched tempo */
  bpm?: [number, number];
  /** genres: what kind of film it suits. instruments: what it is good for. */
  use?: string; for?: string;
}
export interface MusicPreset {
  id: string; label: string; genre: string; mood: string;
  instruments: string[]; production: string[]; vocals: string; bpm: number; note: string;
}
export interface MusicVocabulary {
  genres: VocabRow[]; instruments: VocabRow[]; production: VocabRow[];
  vocals: VocabRow[]; presets: MusicPreset[]; max_tags: number;
  /** From the backend's `music.MOODS` — the shelf's closed vocabulary. Listed here rather than in
   *  the page so the two cannot drift apart (`triumphant` was in a hand-written copy of this list
   *  and is not a value the shelf accepts). */
  moods: string[];
}
/** What the model would actually receive, and anything wrong with it. */
export interface MusicPreview { tags: string; warnings: string[]; }

// --------------------------------------------------------------------- zones
/** 一个专区里某一件东西的状态。
 *
 *  `blocked`（建了、但本机有个东西挡着）和 `planned`（还没建）**故意分开** —— 它们的下一步
 *  不一样，用一个词概括会把用户送去错的那一步。 */
export type ZoneItemState = "ready" | "partial" | "blocked" | "planned";

export interface ZoneItem {
  id: string; label: string; note: string; state: ZoneItemState;
  /** 资料库那一块才有：这一栏现在有几件。 */
  count?: number;
  /** 分工那一块才有。`member` 是内置成员名，`in_template` = 本专区的群模板已经带了这个位子。 */
  member?: string; in_template?: boolean; skills?: string[]; tools?: string[];
}

/** 四块之一。`items` 为空就是空着 —— 后端不会为了「看起来满」编内容。 */
export interface ZoneSurface { note: string; items: ZoneItem[] }

/** 侧栏画的那一行：只有身份。 */
export interface ZoneRow {
  id: string; name: string; icon: string; state: "ready" | "planned"; blurb: string;
}

/** 一个专区页上画的三块：资料库 / 模板 / 工作流。
 *
 *  ⚠️ 注册表里还有第四块 `roles`（分工与技能），但**这一页不画它**，后端也不再送过来 ——
 *  送过来却不渲染的东西等于不存在，这正是这个项目反复在治的那种病。它将来是「从这个专区
 *  起一个群」时那份名单。 */
export interface ZoneDetail extends ZoneRow {
  /** 从这个群模板起一手；空串 = 这个专区还没有名单。 */
  template: string;
  library: ZoneSurface; templates: ZoneSurface; workflows: ZoneSurface;
}
/** 用户自己写的专区文件读不了时**点名**，不静默跳过。 */
export interface ZoneFileError { file: string; why: string }

// ---------------------------------------------------------- the private studio
/** 用户自己的素材（脸、录像、录音）。⚠️ 与其它库**故意相反**：它不在任何群的工作目录里、
 *  不进知识库、不参与导出 —— 成员只能通过显式引用拿到由它生成的**产出**，拿不到原始素材。 */
export interface StudioAsset {
  id: string; title: string; kind: "photo" | "video" | "audio";
  tags: string[]; note: string; created: number; bytes: number;
  original_name: string; mime: string;
}
/** 一次生成。`reviewed` 与 `chosen` 是**两件事**：看过 ≠ 满意。 */
export interface StudioTake {
  id: string; asset: string; name: string; prompt: string;
  params: Record<string, unknown>;
  state: "running" | "done" | "failed";
  created: number; seconds: number; bytes: number;
  reviewed: boolean; chosen: boolean; error: string;
}
export interface StudioAssets {
  assets: StudioAsset[];
  /** 读不出 sidecar 的素材会被**点名**，不是静默跳过 —— 不能再描述的东西就不能拿来生成。 */
  errors: { name: string; why: string; path: string }[];
  kinds: string[];
}

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

/**
 * 一份二进制内容，交回一个 blob URL。
 *
 * ⚠️ 为什么不能用 `<audio src="…/api/…">`：那个请求**带不上**本应用的访问令牌（它走自定义表头
 * `X-Team-Agent-Token`，不是 cookie），端点会回 401，播放器只会静默地什么都不放。把令牌塞进
 * 查询串能"修好"这一条，代价是令牌开始出现在 URL、日志与历史里 —— 那是拿安全换方便。所以走
 * 一次带表头的 fetch，拿字节换成 blob URL 再交给播放器。
 */
async function blobUrl(path: string): Promise<string> {
  const r = await call(path, { headers: authHeaders() });
  return URL.createObjectURL(await r.blob());
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
  // ---- Zones. Two calls on purpose: the sidebar redraws constantly and needs a name and an icon;
  // a zone page needs live shelf counts and the composer's preset list. One call for both would put
  // a directory scan behind every redraw of the navigation.
  zones: () => get<{ zones: ZoneRow[]; errors: ZoneFileError[] }>("/api/zones"),
  zone: (id: string) => get<{ zone: ZoneDetail; errors: ZoneFileError[] }>(`/api/zones/${encodeURIComponent(id)}`),
  // ---- Video zone. Music first: it is the one block whose engine is already on this machine.
  /** The shelf, plus whether this machine can compose another one right now. */
  videoZoneMusic: () => get<MusicShelf>("/api/video-zone/music"),
  /** Minutes, not seconds — returns a job id immediately; poll `musicJob` for it. */
  composeMusic: (b: MusicComposeIn) => post<{ job: MusicJob }>("/api/video-zone/music/compose", b),
  /** The faders: genres (with their BPM ranges), instruments, production, vocals, scene presets. */
  musicVocabulary: () => get<MusicVocabulary>("/api/video-zone/music/vocabulary"),
  /** The tag string the model would receive, composed by the backend so the rules live in one place. */
  previewMusic: (b: MusicComposeIn) => post<MusicPreview>("/api/video-zone/music/preview", b),
  musicJob: (id: string) => get<{ job: MusicJob }>(`/api/video-zone/music/jobs/${encodeURIComponent(id)}`),
  delMusic: (name: string) => del<{ ok: boolean }>(`/api/video-zone/music/${encodeURIComponent(name)}`),
  /** Bytes of one track, as a blob URL a player can use (see `blobUrl`). */
  musicAudio: (name: string) => blobUrl(`/api/video-zone/music/${encodeURIComponent(name)}/audio`),

  // ---- The private studio: the user's own material, and every take made from it.
  studioAssets: (q: { kind?: string; tag?: string } = {}) =>
    get<StudioAssets>(`/api/video-zone/studio/assets${qs(q)}`),
  /** Raw bytes like every other upload; the **type comes from the bytes**, not the file name. */
  uploadStudioAsset: (file: File, meta: { title?: string; note?: string; tags?: string } = {}) =>
    postRaw<{ asset: StudioAsset }>(
      `/api/video-zone/studio/assets${qs({ filename: file.name, ...meta })}`, file),
  patchStudioAsset: (id: string, b: { title?: string; note?: string; tags?: string[] }) =>
    patch<{ asset: StudioAsset }>(`/api/video-zone/studio/assets/${encodeURIComponent(id)}`, b),
  /** Deleting the material deletes every take made from it — that is the rule, not a side effect. */
  delStudioAsset: (id: string) => del<{ ok: boolean }>(`/api/video-zone/studio/assets/${encodeURIComponent(id)}`),
  studioTakes: (assetId: string) =>
    get<{ takes: StudioTake[] }>(`/api/video-zone/studio/assets/${encodeURIComponent(assetId)}/takes`),
  patchStudioTake: (id: string, b: { reviewed?: boolean; chosen?: boolean; name?: string }) =>
    patch<{ take: StudioTake }>(`/api/video-zone/studio/takes/${encodeURIComponent(id)}`, b),
  delStudioTake: (id: string) => del<{ ok: boolean }>(`/api/video-zone/studio/takes/${encodeURIComponent(id)}`),
  /** Material and takes go through `blobUrl` for the same reason music does: an `<img src>` cannot
   *  carry this app's token, so the request would come back 401 and the picture would be blank. */
  studioAssetBlob: (id: string) => blobUrl(`/api/video-zone/studio/assets/${encodeURIComponent(id)}/file`),
  studioTakeBlob: (id: string) => blobUrl(`/api/video-zone/studio/takes/${encodeURIComponent(id)}/file`),
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
  /** Which providers can generate this kind of media, and which of their models to name. The page
   *  cannot work this out itself: it depends on what each provider said about its own models. */
  mediaOptions: (use: "image" | "video") => get<MediaOptions>(`/api/media/options${qs({ use })}`),
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
  channels: () => get<{ channels: ChannelInfo[] }>("/api/channels"),
  hooks: () => get<{ hooks: HookEntry[]; errors: string[]; guide: string; directory: string; log_file: string }>("/api/hooks"),
  hooksLog: (limit = 50) => get<{ entries: HookLogRow[] }>(`/api/hooks/log?limit=${limit}`),
  reloadHooks: () => post<{ hooks: HookEntry[]; errors: string[] }>("/api/hooks/reload", {}),
  patchHook: (id: string, b: { enabled?: boolean; groups?: string[] }) =>
    patch<{ hook: HookEntry }>(`/api/hooks/${encodeURIComponent(id)}`, b),
  hookSource: (id: string) => get<{ id: string; content: string }>(`/api/hooks/${encodeURIComponent(id)}/source`),
  testHook: (id: string, b: { event?: string; group_id?: string }) =>
    post<{ ok: boolean; note: string; answer: Record<string, unknown> | null }>(`/api/hooks/${encodeURIComponent(id)}/test`, b),
  /** `patch` is keyed by field name (enabled, group_id, …), not by the full setting key. */
  setChannel: (id: string, patch: Record<string, unknown>) =>
    put<{ ok: boolean; ready: boolean; missing: string[]; secrets: Record<string, boolean> }>(`/api/channels/${id}`, patch),
  channelProbe: (id: string) => post<{ ok: boolean; detail: string }>(`/api/channels/${id}/probe`, {}),
  channelTest: (id: string) => post<{ ok: boolean; detail: string }>(`/api/channels/${id}/test`, {}),
  channelReconnect: (id: string) => post<{ ok: boolean; poller: string }>(`/api/channels/${id}/reconnect`, {}),
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
  /** Create (or find) the member a preset describes, without putting it in a group. The team
   *  suggester names experts by preset key while the group does not exist yet, so they have to be
   *  able to exist on their own — `createGroup` then takes the id like any other member's. */
  agentFromPreset: (key: string) => post<Agent>("/api/agents/from-preset", { key }),
  /** Who should be in a group chat for this task, and why each. Read-only: nothing is created and
   *  the group need not exist. Rule-based on the server, so it is cheap enough to call while the
   *  user is still typing. */
  teamSuggest: (text: string) => post<TeamAdvice>("/api/team/suggest", { text }),
  groups: () => get<Group[]>("/api/groups"),
  /** 建一个群。`name` 传空串、`extra.task` 传用户敲的那句话 → **服务端**从里面取关键词命名
   *  (规则只有后端那一份,见 `backend/app/names.py`)。模板与老调用方照旧传自己的名字。 */
  createGroup: (name: string, member_ids: string[], host_agent_id: string | null, extra?: { ext?: Partial<GroupExt>; prompt?: string; workspace?: string; task?: string; lineup?: TeamDraftMember[]; host_ref?: string }) =>
    post<Group>("/api/groups", { name, member_ids, host_agent_id, ...extra }),
  patchGroup: (id: string, b: { name?: string; host_agent_id?: string | null; prompt?: string; ext?: Partial<GroupExt>; workspace?: string; status?: "active" | "done"; archived?: boolean }) =>
    patch<Group>(`/api/groups/${id}`, b),
  delGroup: (id: string) => del(`/api/groups/${id}`),
  bindCapability: (gid: string, b: { kind: "skills" | "plugins" | "mcp"; ref: string; attached: boolean }) => patch<Group>(`/api/groups/${gid}/capability-binding`, b),
  addMember: (gid: string, agent_id: string) => post<Group>(`/api/groups/${gid}/members`, { agent_id }),
  /** Pull preset roles (host/reviewer/scribe/librarian/programmer/translator/analyst/planner…) into a group at any time; a member with the same name is reused */
  addMemberFromPreset: (gid: string, key: string) => post<Group>(`/api/groups/${gid}/members/from-preset`, { key }),
  externalOverview: () => get<ExternalOverview>("/api/external"),
  externalCreate: (b: { engine?: string; name?: string; group_id?: string; cfg: Partial<ExternalCfg> }) => post<Agent>("/api/external/agents", b),
  externalPatch: (id: string, cfg: Partial<ExternalCfg>) => patch<Agent>(`/api/external/agents/${id}`, { cfg }),
  externalTest: (b: { live?: boolean; agent_id?: string; engine?: string; cli_path?: string; base_url?: string; api_key?: string; model?: string }) => post<ExternalProbe>("/api/external/test", b),
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
  /** Look at the pictures of a group nobody has looked at yet (`limit` at most 50 per press). */
  describePictures: (gid: string, limit = 12) => post<{ described: number; pending: number; documents: number; reason: string }>(
    `/api/groups/${gid}/library/describe?limit=${limit}`, {}),
  applyPrompt: (gid: string, prompt_id: string, mode: "replace" | "append" = "replace") =>
    post<Group>(`/api/groups/${gid}/apply-prompt`, { prompt_id, mode }),
  systemPromptPreview: (gid: string, agent_id?: string) =>
    get<{ text: string; tokens: number }>(`/api/groups/${gid}/system-prompt-preview${qs({ agent_id })}`),
  templates: () => get<GroupTemplate[]>("/api/templates"),
  /** Keep a group as a template: the members, host, skills and prompt it ran with. */
  /** What is in a project's folder: the numbers the confirmation dialog decides with. */
  folder: (gid: string) => get<GroupFolder>(`/api/groups/${gid}/folder`),
  /** Rename the folder on disk (only a folder the user picked can be). */
  renameFolder: (gid: string, name: string) =>
    post<{ ok: boolean; path: string; name: string }>(`/api/groups/${gid}/folder/rename`, { name }),
  /** Move the folder to the Trash. `expect` is the file count the dialog showed; a mismatch is
   *  refused by the backend rather than carried out. */
  deleteFolder: (gid: string, expect: number | null = null) =>
    post<{ ok: boolean; trashed: string; files?: number; note?: string }>(
      `/api/groups/${gid}/folder/delete`, { confirm: true, expect_files: expect }),
  saveGroupAsTemplate: (gid: string, name = "") =>
    post<GroupTemplate>(`/api/groups/${gid}/save-as-template`, { name }),
  delTemplate: (tid: string) => del<{ ok: boolean }>(`/api/templates/${tid}`),
  createFromTemplate: (tid: string, name?: string) => post<Group>(`/api/templates/${tid}/create-group`, { name }),
  messages: (gid: string) => get<Message[]>(`/api/groups/${gid}/messages`),
  /** Whether a collaboration round is running in this group (used after a page refresh or WebSocket reconnect to restore the sending state) */
  groupStatus: (gid: string) => get<{ busy: boolean }>(`/api/groups/${gid}/status`),
  clearMessages: (gid: string) => del(`/api/groups/${gid}/messages`),
  // ---- What the user thought of a reply, and what can be done with one afterwards
  /** A verdict on one member's reply. `rating` is `"up"`, `"down"`, or `""` to take it back. */
  rate: (gid: string, mid: string, rating: MessageFeedback["rating"], note = "") =>
    post<FeedbackBoard>(`/api/groups/${gid}/messages/${mid}/feedback`, { rating, note }),
  unrate: (gid: string, mid: string) => del<FeedbackBoard>(`/api/groups/${gid}/messages/${mid}/feedback`),
  /** The scoreboard: per member, with the notes beside both thumbs. Whole app, or one group. */
  feedback: (gid = "", limit = 200) => get<FeedbackBoard>(`/api/feedback${qs({ gid, limit })}`),
  /** Hand a reply to another group, through the same path a typed message takes. */
  forward: (gid: string, mid: string, toGroupId: string) =>
    post<{ ok: boolean; group_id: string; group_name: string }>(`/api/groups/${gid}/messages/${mid}/forward`, { to_group_id: toGroupId }),
  /** The reading of one message, made on this machine. WAV, and the headers say which voice read it
   *  and whether a long reply was cut — the button has to be able to say so rather than imply the
   *  whole thing was heard. */
  speech: async (gid: string, mid: string) => {
    const r = await call(`/api/groups/${gid}/messages/${mid}/speech`, { headers: authHeaders() });
    return {
      blob: await r.blob(),
      voice: decodeURIComponent(r.headers.get("X-Team-Agent-Voice") || ""),
      cut: r.headers.get("X-Team-Agent-Cut") === "1",
    };
  },
  /** `files` are ids from `uploadImage` — any kind of file, not only pictures. */
  send: (gid: string, text: string, files: string[] = []) => post(`/api/groups/${gid}/messages`, { text, attachments: files }),
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
  importSources: () => get<{ sources: ImportSource[] }>("/api/import/sources"),
  importScan: (sources?: string[]) => post<ImportScan>("/api/import/scan", { sources: sources ?? null }),
  /** Imports land disabled; the names are re-read from the source file server-side. */
  importMcpFrom: (source: string, names: string[]) =>
    post<{ added: string[]; skipped: string[] }>("/api/import/mcp", { source, names }),
  /** Imports land disabled; the names are re-read from the source file server-side.
   *  `copied` counts the files each skill brought besides its SKILL.md (a skill can be a folder);
   *  `truncated` names the ones a size ceiling cut short, so a half-copied skill is never shown as
   *  a whole one. */
  importSkillsFrom: (source: string, names: string[]) =>
    post<{ added: string[]; skipped: string[]; copied: Record<string, number>;
           truncated: Record<string, number> }>("/api/import/skills", { source, names }),
  /** Expert packages become members. No model is pinned and no skill is attached — that is
   *  a decision for whoever reads what the package actually says. */
  importExpertsFrom: (source: string, names: string[]) =>
    post<{ added: string[]; skipped: string[]; notes?: string[] }>("/api/import/experts", { source, names }),
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
  patchKb: (id: string, b: { name?: string; description?: string; source?: string; purpose?: string }) => patch<KnowledgeBase>(`/api/knowledge-bases/${id}`, b),
  /** Origin vocabulary + purpose suggestions, so the page never keeps its own copy of them. */
  libraryVocabulary: () => get<LibraryVocabulary>("/api/library/vocabulary"),
  /** Removes the knowledge base and every document in it */
  delKb: (id: string) => del<{ ok: boolean; deleted_docs: number }>(`/api/knowledge-bases/${id}`),
  collections: () => get<Collection[]>("/api/collections"),
  addCollection: (b: { name: string; description?: string; kb_ids?: string[] }) => post<Collection>("/api/collections", b),
  patchCollection: (id: string, b: { name?: string; description?: string; kb_ids?: string[] }) => patch<Collection>(`/api/collections/${id}`, b),
  delCollection: (id: string) => del<{ ok: boolean }>(`/api/collections/${id}`),
  // ---- Documents. Narrow to one knowledge base, or to everything a group can reach; no scope
  // means the whole library, which is what the overview shows.
  //
  // One page at a time, plus the classification: a real library is thousands of documents, and both
  // halves of that used to be broken (the response carried every row, the page rendered every row).
  // The counts in `origins`/`kinds`/`bases` describe the whole scope, never the filtered view —
  // they are how the user picks a class, so they must not shrink with the query.
  library: (scope: { kb?: string; group?: string } = {}, view: LibraryView = {}) =>
    get<LibraryPage>(`/api/library${qs({
      kb_id: scope.kb, group_id: scope.group, q: view.q, origin: view.origin, kind: view.kind,
      category: view.category,
      group_by: view.groupBy, offset: view.offset, limit: view.limit, per_group: view.perGroup,
    })}`),
  addNote: (title: string, content: string, scope: { kb?: string; group?: string } = {}) =>
    post<LibraryDoc>("/api/library/note", { title, content, kb_id: scope.kb ?? "", group_id: scope.group ?? "" }),
  /** The request body is the file's raw bytes (txt/md/csv/json/html/pdf/docx) */
  uploadDoc: (file: File, scope: { kb?: string; group?: string } = {}) =>
    postRaw<LibraryDoc>(`/api/library/upload${qs({ filename: file.name, kb_id: scope.kb, group_id: scope.group })}`, file),
  // ---- Files attached to a message (any kind: a screenshot, a PDF, a spreadsheet, a video)
  /** Raw bytes, like the library upload. The server decides the kind from the bytes, not the name. */
  uploadImage: (gid: string, file: File) => postRaw<Attachment>(`/api/groups/${gid}/attachments${qs({ filename: file.name })}`, file),
  dropImage: (id: string) => del<{ ok: boolean }>(`/api/attachments/${id}`),
  /** The bytes, fetched with the token in a header — see `getBlob` for why an <img src> will not do. */
  imageBytes: (id: string) => getBlob(`/api/attachments/${id}`),
  /** Everything ever added to this group, newest first. */
  groupFiles: (gid: string) => get<{ attachments: Attachment[] }>(`/api/groups/${gid}/attachments`),
  /** Attach a document the group can already reach, without copying it into the workspace. */
  attachDoc: (gid: string, docId: string) => post<Attachment>(`/api/groups/${gid}/attachments/from-library`, { doc_id: docId }),
  /** The group's workspace: what the members have written, and one folder per task. */
  workspace: (gid: string) => get<WorkspaceView>(`/api/groups/${gid}/workspace`),
  workspaceBytes: (gid: string, path: string) => getBlob(`/api/groups/${gid}/workspace/file${qs({ path })}`),
  /** The same file, but served with the type the browser needs to show or play it — what a
   *  `review_picture` / `review_audio` result is displayed through (`MessageLook`). */
  workspaceMediaBytes: (gid: string, path: string) => getBlob(`/api/groups/${gid}/workspace/file${qs({ path, inline: 1 })}`),
  /** One file's text, so the outputs column can show a deliverable in place instead of handing it
   *  to another program. The backend decides what counts as text; a file with none is refused. */
  workspaceText: (gid: string, path: string) =>
    get<{ text: string; chars: number; truncated: boolean; extracted: boolean; name: string }>(
      `/api/groups/${gid}/workspace/text${qs({ path })}`),
  makeFolder: (gid: string, path: string) => post<{ ok: boolean; path: string }>(`/api/groups/${gid}/workspace/folder`, { path }),
  /** Who can look at pictures; the settings page shows this instead of failing quietly. */
  vision: () => get<VisionStatus>("/api/vision"),
  /** What this machine can do with files: which document kinds are read locally, whether
   *  ffmpeg is there for video frames, and who can look at pictures. */
  machineCapabilities: () => get<{ vision: VisionStatus; documents: string[]; video_frames: boolean; audio_transcribe: boolean; transcriber_install: string; upload_max_mb: number; advisor: { ready: boolean; reason: string; label: string; installed: string[]; install: string } }>("/api/capabilities"),
  /** The process engineer: where it is and what it has written. It is invisible in every group, so
   *  this panel is the only place it can be seen at all. */
  process: () => get<{ name: string; hidden: boolean; autojoin: boolean; autolog: boolean; review: boolean; groups: number; in_groups: number; not_in: string[]; entries: Record<string, number>; ledgers: number; recent: { group: string; gid: string; id: string; title: string; status: string; severity: string; stage: string; found: string; by: string; seen: number; sentence: string; cause: string; fix: string; hint: string; verify: string; advised: number; last_advised: string; review_state: string; review_note: string }[] }>("/api/process"),
  /** A clip a member generated, out of that group's own workspace. Same header problem, so the
   *  bytes come through the API and are turned into an object URL (`MessageVideo`). */
  videoBytes: (gid: string, name: string) => getBlob(`/api/groups/${gid}/video/${encodeURIComponent(name)}`),
  /** An image a member *drew*, fetched the same way as a clip. Named apart from `imageBytes`
   *  above, which is an uploaded attachment: the two collide in the same object literal, and a
   *  collision there silently rebinds the older one. */
  drawnImageBytes: (gid: string, name: string) => getBlob(`/api/groups/${gid}/image/${encodeURIComponent(name)}`),
  testImage: () => post<{ ok: boolean; provider?: { id: string; name: string; base_url: string }; detail: string }>("/api/image/test", {}),
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
  // ---- Searching by meaning (vectors), see backend/app/embed.py
  vectorState: () => get<VectorState>("/api/library/vector"),
  vectorStart: () => post<{ started: boolean; ready: boolean; reason: string }>("/api/library/vector/start", {}),
  vectorIndex: (b: { kb_id?: string; limit?: number } = {}) =>
    post<{ started: boolean; reason?: string }>("/api/library/vector/index", { kb_id: b.kb_id ?? "", limit: b.limit ?? 0 }),
  vectorClear: () => post<{ cleared: number }>("/api/library/vector/clear", {}),
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
  // ---- Updates (GitHub)
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
/** Every task of every board this group has run, as one table. */
export const downloadTasks = (gid: string) => download(`/api/groups/${gid}/export-tasks`, "tasks.csv", tr("Export"));

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
