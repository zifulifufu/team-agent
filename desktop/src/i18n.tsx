import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { prefs } from "./theme";

/**
 * 界面多语言。
 *
 * 约定(很重要,加文案时照这个来):
 *   * **源码里直接写英文**,英文就是 key;中文放到下面的 `ZH` 表里。
 *     这样只有一份"原文",不会出现 key 和文案各写一遍、两边慢慢不一致的问题。
 *   * 需要插值就用 `{name}`,例如 `t("Delete the group chat \"{name}\"?", { name })`。
 *   * 组件里用 `useI18n().t`;非组件代码(如 api.ts / lib.ts)用模块级的 `tr`(它按当前语言返回)。
 *   * 英文缺中文时原样显示英文 —— 不会显示成 key 或空白,便于逐批补翻译。
 *
 * 默认语言是**英文**(面向国际传播);用户在「设置 → 外观 → 语言」改过之后按用户的来。
 */

export type Lang = "en" | "zh";
export const LANGS: { id: Lang; label: string }[] = [
  { id: "en", label: "English" },
  { id: "zh", label: "中文(简体)" },
];
export const DEFAULT_LANG: Lang = "en";
const PREF_KEY = "ta.lang";

/** 中文词典:键 = 源码里的英文原文。 */
const ZH: Record<string, string> = {
  // ---------------------------------------------------------------- 侧边栏
  "Collapse sidebar": "收起侧边栏",
  "Search group chats": "搜索群聊",
  "New group chat": "新建群聊",
  "Search group chats…": "搜索群聊…",
  "Clear": "清除",
  "Members": "成员",
  "Collapse members": "收起群成员",
  "Expand members": "展开群成员",
  "Open a group chat and its members show up here — you can add more at any time":
    "打开一个群聊后,这里显示群成员,可随时添加",
  "Tools": "工具",
  "Group chats": "群聊",
  "Delete group chat": "删除群聊",
  "Delete group chat {name}": "删除群聊 {name}",
  "No matching group chats": "没有匹配的群聊",
  "No group chats yet": "还没有群聊",
  "Settings": "设置",
  "Appearance": "外观",
  "Offline mode": "离线模式",
  "Updates & discovery": "更新与发现",
  "About": "关于",
  "Me": "我",
  "Local user": "本地用户",
  "Backend not connected": "后端未连接",
  "Hosted + local": "云端 + 本地",
  "Updates available": "有可用更新",
  'Delete the group chat "{name}" and all of its messages?': "删除群聊「{name}」及其全部聊天记录?",
  "Delete": "删除",

  // ---------------------------------------------------------------- 工具入口(侧边栏与设置共用)
  "Skills": "技能",
  "Plugins": "插件",
  "Prompts": "提示词",
  "Library": "资料库",
  "Memory": "记忆",

  // ---------------------------------------------------------------- 设置导航
  "Models": "模型",
  "App": "应用",
  "Providers": "模型服务",
  "Routing & fallback": "路由与回退",
  "Local models": "本地模型",
  "External agents": "外部智能体",
  "Template gallery": "模板中心",
  "General": "通用",
  "Permissions & control": "权限与操控",
  "Data": "数据",
  "Usage stats": "使用统计",
  "Dependencies": "依赖",
  "Back": "返回",

  // ---------------------------------------------------------------- 外观页
  "Theme": "主题",
  "Light": "浅色",
  "Dark": "深色",
  "Follow system": "跟随系统",
  "Accent colour": "主题色",
  "Custom accent colour (hex)": "自定义主题色(十六进制)",
  "Reset": "重置",
  "Enter a 6-digit hex colour, e.g. #00B96B": "请输入 6 位十六进制颜色,如 #00B96B",
  "The accent colour is used for switches, selected states and highlights. Stored on this machine.":
    "主题色用于开关、选中状态和强调元素。设置保存在本机。",
  "Language": "语言",
  "Interface language. Built-in templates and role presets follow the same setting.":
    "界面语言。自带的模板与岗位预设也跟随这个设置。",
  // ---------------------------------------------------------------- 通用
  "Offline mode · local models only": "离线模式 · 仅本地模型",
  "No model available": "无可用模型",

  // ---------------------------------------------------------------- 首页
  "Pull hosted and local models into one group and let each do what it is best at — office documents, video production, writing.":
    "把国内外大模型拉进同一个群,各展所长,协同完成办公、视频制作与创作。",
  "Describe your task; type @ to assign members, or leave it and 小助 will coordinate":
    "描述你的任务;输入 @ 点名成员分工,不点名则由小助统筹",
  "Which group chat to send to": "发送到哪个群聊",
  "Send to": "发送到",
  "New group chat · {names}": "新建群聊 · {names}",
  'Send to "{name}"': "发送到「{name}」",
  "Group templates": "群聊模板",
  "Members, host, skills and prompt installed in one click": "一键建好成员、群主、技能和提示词",
  "More teams: template gallery →": "更多团队:模板中心 →",
  'Create a group chat from template "{name}"': "用模板「{name}」新建群聊",
  "{name} (host)": "{name}(群主)",
  "No members yet — create some under Members in the sidebar first": "还没有成员,请先到左侧「成员」里创建",
  "Everyone": "所有人",
  "Everyone speaks in turn": "全员依次发言",
  "Message input": "消息输入框",
  "@-mention a member": "@ 点名成员",
  "Click to allow / block hosted model calls": "点击切换:允许 / 禁止调用云端模型",
  "Stop": "停止",
  "Send": "发送",
  " (an updated version)": "(已更新版本)",
  " · web access": " · 可上网",
  "(a model member, pinned to this model)": "(模型成员,固定用这个模型)",
  "(currently: {value})": "(当前:{value})",
  "(no output)": "(无返回内容)",
  "A dedicated empty folder": "专属空文件夹",
  "A plugin is Python code that runs on this machine. You are shown the full source before installing, and it only affects this group once you tick it.": "插件是本机运行的 Python 代码:安装前会让你读完整源码,安装后也要你自己勾选才会对本群生效。",
  "About {n} tokens (estimated)": "约 {n} tokens(估算)",
  "Add": "添加",
  "Add MCP server": "添加 MCP 服务器",
  "Add a command-line agent as a member": "添加命令行智能体为成员",
  "Add anyway": "仍要添加",
  "Add by hand…": "手动添加…",
  "Add group member": "添加群成员",
  "Add member": "添加成员",
  "Add model {name} to the group": "把模型 {name} 拉进群",
  "Add one in Settings": "去设置里添加",
  "Add plugin": "添加插件",
  "Add selected ({n})": "添加所选({n})",
  "Add skill": "添加技能",
  "Add the {name} role preset": "添加预设岗位 {name}",
  "Add to \"{group}\"": "添加到「{group}」",
  "Add {name}": "拉入 {name}",
  "Added": "已添加",
  "Added \"{name}\" and attached it to this group (it connects the first time it is used).": "已添加「{name}」并挂到本群(第一次用到时才会连接)。",
  "Added to the group from \"Models I added\"": "由「我添加的模型」拉进群",
  "Added {name}": "已添加 {name}",
  "Adding…": "添加中…",
  "All": "全部",
  "All role presets have already been created": "预设岗位都已经创建过了",
  "Already in this group": "已在群里",
  "Always split": "总是分工",
  "Always split: every task starts with a plan board. Never split: the host answers directly or names members with @.": "总是分工:每个任务都先出任务板;不分工:群主直接回答或按 @ 点名。",
  "An MCP server is only connected the first time it is used, so \"not connected yet\" is not an error.": "MCP 在第一次被用到时才会连接,「尚未连接」不代表出错。",
  "An MCP server is only connected the first time it is used.": "MCP 在第一次被用到时才会连接。",
  "Append": "追加",
  "Append \"{title}\" to the group prompt": "把「{title}」追加到本群提示词",
  "Apply from the prompt library": "从提示词库套用",
  "Arguments": "参数",
  "Ask the provider what it offers right now (needs network access)": "向服务商查询它此刻提供的型号(需要联网)",
  "Assignment": "分工声明",
  "Auto (only split complex tasks)": "自动(复杂任务才分工)",
  "Auto (pick by strength)": "自动(按强项挑)",
  "Auto ·": "自动·",
  "Auto · {model}": "自动·{model}",
  "Balanced": "均衡",
  "Blocked by the permission level you set: {tools} was not run. Adjust the level under the member's settings if it should be allowed.": "按你设的权限级别,它想用的 {tools} 被挡下了(没有执行)。需要的话,到成员设置里调整权限级别。",
  "Built-in": "内置",
  "Can edit files": "可改文件",
  "Cancel": "取消",
  "Changes save immediately and apply to the whole group": "修改会立即保存并对全群生效",
  "Choose a member": "选择成员",
  "Clear chat history": "清空聊天记录",
  "Clear every \"new\" badge": "清掉所有「新」标记",
  "Clear this group's chat history?": "清空本群的聊天记录?",
  "Click to insert a variable": "点击插入变量",
  "Click to jump to this message": "点击定位到这条发言",
  "Command-line engine found": "已找到命令行引擎",
  "Connected": "已连接",
  "Connecting": "连接中",
  "Connection failed": "连接出错",
  "Consolidated": "整合",
  "Could not load member capabilities: {err}": "读取成员能力失败:{err}",
  "Could not load this group's capabilities: {err}": "读取本群能力失败:{err}",
  "Deliverable": "交付物",
  "Denied": "已拒绝",
  "Depends on {tasks}": "承接 {tasks}",
  "Deselect all": "取消全选",
  "Disabled in Settings": "已在设置里停用",
  "Disabled in the library": "已在资料库里停用",
  "Discover on GitHub…": "从 GitHub 发现…",
  "Done": "完成",
  "Download as Markdown": "下载为 Markdown",
  "Each reply is a separate process and usually takes a while; it cannot host a group.": "每次发言是独立进程,通常要等几十秒;不能当群主。",
  "Enter a command or a remote address and attach it to this group": "填命令或远程地址,并自动挂到本群",
  "Enter a model by hand (not in the catalog)": "手动输入型号(目录里没有的)",
  "Every member is already in this group": "所有成员都已在本群",
  "Existing members": "已有成员",
  "Export chat history": "导出聊天记录",
  "Export failed: ": "导出失败:",
  "External": "外部",
  "External agent · {level}": "外部智能体 · {level}",
  "External agent: replies come from a command-line engine of its own, not through this app's model routing": "外部智能体:由它自带的命令行引擎发言,不走本程序的模型路由",
  "Failed": "失败",
  "Failed ({detail})": "失败({detail})",
  "Fast": "快速",
  "Fell back from {model}": "已从 {model} 回退",
  "Finished": "已完成",
  "Finished tasks / total tasks": "已完成任务 / 总任务数",
  "Flagship": "旗舰",
  "Folder": "目录",
  "Follow the global setting": "跟随全局",
  "For example: this group works on a product launch; call the product \"Team Agent\", keep it conversational, at most 3 sentences.": "例如:本群是产品发布小组,统一用「Team Agent」称呼产品,口语化、不超过 3 句。",
  "Full access": "完全权限",
  "Goal": "总目标",
  "Gone from the provider list": "服务商清单里已没有",
  "Group": "群聊",
  "Group chat not found": "群聊不存在",
  "Group prompt": "本群提示词",
  "Group rules": "群聊规则",
  "Host": "主持",
  "Host consolidation": "群主整合",
  "Host is consolidating": "群主整合中",
  "In progress": "进行中",
  "In use": "实际",
  "Install from GitHub…": "从 GitHub 安装…",
  "Installed": "已安装",
  "Legacy": "旧版",
  "Let a command-line agent that brings its own tools join the discussion as a member. Read-only and off by default; you have to turn it on.": "让自带工具的命令行智能体作为群成员参与讨论。默认只读、默认关闭,需要你主动打开。",
  "Library scope": "资料库范围",
  "Live connection lost, reconnecting…": "实时连接中断,重连中…",
  "Live list updated at {time}, no new models": "实时清单更新于 {time},没有新模型",
  "Live list updated at {time}, {n} new models": "实时清单更新于 {time},有 {n} 个新模型",
  "Loading…": "加载中…",
  "Local": "本地",
  "MCP servers": "MCP 服务器",
  "Make host": "设为群主",
  "Mark all as seen": "全部标为已看",
  "Markdown file downloaded": "已下载 Markdown 文件",
  "Master switch is off (you can turn it on while adding)": "总开关未打开(点添加时可以打开)",
  "Member": "成员",
  "Member skills": "成员技能",
  "Members are working — you can stop at any time": "成员正在协作…可随时点击停止",
  "Members of \"{group}\"": "「{group}」的成员",
  "Merges every member's deliverable into one final answer.": "汇总各成员交付,统一口径后给出最终结果",
  "Model": "模型",
  "Model catalog {version} ({source})": "模型目录 {version}({source})",
  "Model id": "型号 ID",
  "Model id, e.g. deepseek-chat / qwen-plus / gpt-4o-mini": "型号 ID,如 deepseek-chat / qwen-plus / gpt-4o-mini",
  "Model list": "模型列表",
  "Model used by {name}": "{name} 使用的模型",
  "Models I added": "我添加的模型",
  "More variables ({n})": "更多变量({n})",
  "Needs a path change": "要改路径",
  "Never split": "不分工",
  "New": "新",
  "New only": "只看新模型",
  "New skill…": "新建技能…",
  "No API key": "未填 Key",
  "No arguments": "无参数",
  "No command-line engine found (see the notes in the add dialog)": "没找到命令行引擎(见添加窗口里的说明)",
  "No documents selected yet, so this group cannot search any material.": "还没选文档,本群暂时检索不到任何资料。",
  "No models available yet — add and enable one under Settings → Providers.": "还没有可用的模型,先去设置 → 模型服务添加并启用。",
  "No models match": "没有符合条件的模型",
  "No new models. Use \"Refresh live list\" to see whether the provider released any": "没有新模型。点「刷新实时清单」看看服务商有没有发布新型号",
  "No tools are available in this group yet.": "本群暂时没有可用工具。",
  "No {what} yet.": "还没有{what}。",
  "Not connected": "未连接",
  "Not in the live list": "实时清单里没有",
  "Not in the live list: the catalog may be stale, or this account cannot use it": "实时清单里没有这个型号:目录可能已过时,或该账号无权使用",
  "Not installed": "未安装",
  "Not run": "未执行",
  "Not started": "待开始",
  "Note": "说明",
  "Off": "关闭",
  "Other": "其他",
  "Permission level it had for this reply": "它这次发言时的权限级别",
  "Permissions": "权限",
  "Pick models · {provider}": "选择模型 · {provider}",
  "Plain-text skills; installing one never runs code": "纯文本技能,安装后不会执行代码",
  "Plan board": "任务板",
  "Planned tool: {name}": "计划使用工具:{name}",
  "Plugin": "插件",
  "Plugins and MCP servers run code on this machine — only enable ones you trust.": "插件和 MCP 会在本机执行代码,只启用你信任的。",
  "Preview": "预览",
  "Preview the source before installing": "先预览源码再安装",
  "Pull an enabled model from Providers straight into the group: it becomes a member, and its strengths come from the model itself.": "把「模型服务」里已启用的模型直接拉进群,它就是一个成员,强项取自模型本身。",
  "Read-only": "只读",
  "Reading status…": "读取状态…",
  "Refresh live list": "刷新实时清单",
  "Refreshing the live list failed: {err}": "刷新实时清单失败:{err}",
  "Refreshing…": "刷新中…",
  "Reload": "重新加载",
  "Reloaded: {n} plugins.": "已重新加载,共 {n} 个插件。",
  "Remove": "移出",
  "Remove from group": "移出群聊",
  "Remove {name} from this group? (The member itself is kept — it just stops taking part in this group.)": "把 {name} 移出本群?(不会删除这个成员,只是不再参与本群)",
  "Removing {name} leaves the group without a host (messages with no @mention go to the first member); you can appoint a new one. Remove it?": "移出后本群暂时没有群主(未 @ 任何人的消息会交给排在第一位的成员),你可以再指定新的群主。确定移出?",
  "Replace": "替换",
  "Replace the group prompt with \"{title}\"": "用「{title}」替换本群提示词",
  "Replace this group's prompt with \"{title}\"?": "用「{title}」替换本群现有的提示词?",
  "Result preview": "结果预览",
  "Retired": "已停用",
  "Role presets": "预设岗位",
  "Running": "调用中",
  "Running…": "执行中…",
  "Save": "保存",
  "Saved": "已保存",
  "Saving…": "保存中…",
  "Search by name / id / description": "按名称 / 型号 / 简介搜索",
  "Search models": "搜索模型",
  "Search models…": "搜索模型…",
  "Select all filtered": "全选当前筛选",
  "Select {name}": "选择 {name}",
  "Selected only": "仅选定",
  "Shared conventions": "全组统一约定",
  "Show less": "收起",
  "Skills / plugins / MCP and prompts": "技能 / 插件 / MCP 与提示词",
  "Skills, plugins, MCP and prompts panel": "技能、插件、MCP 与提示词面板",
  "Skipped": "已跳过",
  "Skipped ({detail})": "跳过({detail})",
  "Still showing the bundled catalog{suffix}; the \"new\" and \"gone\" badges need a live list to be determined.": "仍显示内置目录{suffix},其中「新」「已没有」等标记需要实时清单才能判断。",
  "Stopped": "已停止",
  "Strengths (multiple allowed, all required):": "强项(可多选,须同时具备):",
  "Succeeded": "成功",
  "Supports {{variables}}, which are filled in before the prompt reaches a member.": "支持 {{变量}},发给成员前会代入真实内容。",
  "Task progress": "任务进度",
  "Task splitting": "分工模式",
  "Task · {title}": "任务 · {title}",
  "Template · {name}": "模板 · {name}",
  "The _chat-log folder in your vault": "库里的 _聊天记录 文件夹",
  "The catalog has no models for this provider; you can add one by hand below": "目录里没有这个服务商的型号,可在下面手动输入",
  "The catalog is a snapshot taken on one date, so it can lag behind the latest releases. To see what the provider offers right now, use \"Refresh live list\". Strength tags are inferred from the model family and name — they are not benchmark results.": "目录是某个日期的快照,可能落后于各家最新发布;想知道服务商此刻真正提供什么,点「刷新实时清单」。强项是根据模型系列和名称推断的标签,不是评测成绩。",
  "The external-agent master switch is still off": "外部智能体总开关还没打开",
  "The final text after the global prompt, the role setup, the group prompt and skills are combined.": "全局提示词、岗位设定、本群提示词、技能等拼好之后的最终文本。",
  "The global memory switch is off, so turning it on here has no effect.": "全局记忆开关目前是关闭的,这里开启也不会生效。",
  "The library has no documents yet.": "资料库里还没有文档。",
  "The live list has not been refreshed yet; showing the bundled catalog.": "还没有刷新过实时清单,当前显示的是内置目录。",
  "The pinned model is unavailable right now": "指定的模型现在用不了",
  "The pinned model is unavailable right now ({problem}); falling back to another for now": "指定的模型现在用不了({problem}),暂时用别的顶替",
  "The prompt library is empty. Add one on the Prompts page.": "提示词库里还没有内容。到左侧「提示词」页添加。",
  "The provider has retired (or is retiring) this model ({reason}). Calls may fail after adding it. Add anyway?": "服务商已停用/将停用该型号({reason})。添加后调用可能失败,仍要添加吗?",
  "The system prompt a member actually receives": "成员实际收到的系统提示词",
  "The system prompt {name} actually receives": "「{name}」实际收到的系统提示词",
  "This group has no members yet — use the + above to add one": "本群还没有成员,点上面的 + 添加",
  "This model was added, but the provider's live list no longer has it — most likely retired": "这个型号已添加,但服务商的实时清单里已经没有了,多半已下线",
  "Time taken for this reply": "这次发言用时",
  "Tool {name}, {state}": "工具 {name},{state}",
  "Tools available in this group": "本群可用工具",
  "Type a message; @mention a member to assign work. Enter to send, Shift+Enter for a new line": "输入消息,@成员 点名分工;Enter 发送,Shift+Enter 换行",
  "Unsaved changes (saved automatically when the field loses focus)": "有未保存的修改(失焦时自动保存)",
  "Use memory in this group": "本群使用记忆",
  "Use this after dropping a .py file into the plugin folder": "刚把 .py 文件放进插件目录后用",
  "View": "查看",
  "Waiting for you": "等你确认",
  "Waiting for your approval at the bottom of the chat…": "等你在聊天窗口底部确认…",
  "When on, members can search it and note down preferences and decisions as needed.": "开启后成员可检索、并在需要时记下偏好与决定。",
  "With no @mention, the group host answers": "不 @ 任何人时由群主持人响应",
  "Write to Obsidian": "写入 Obsidian",
  "Write your own instructions and attach them to this group": "自己写一段说明,并自动挂到本群",
  "Written to Obsidian: {path}": "已写入 Obsidian:{path}",
  "bundled": "内置",
  "plugin": "插件",
  "plugins": "插件",
  "skills": "技能",
  "tasks": "任务",
  "updated": "已更新",
  "{done}/{total} finished": "{done}/{total} 已完成",
  "{label}: {group}": "{label}:{group}",
  "{name} is the host of this group. Removing it leaves the group without a host (messages with no @mention go to the first member); you can appoint a new one. Remove it?": "{name} 是本群群主。移出后本群暂时没有群主(未 @ 任何人的消息会交给排在第一位的成员),你可以再指定新的群主。确定移出?",
  "{n} chars": "{n} 字",
  "{n} s": "{n} 秒",
  "{n} searchable documents. Members search them when they need to; the whole library is never stuffed into the prompt.": "共 {n} 篇可检索的文档。成员需要时会自己检索,不会把整库塞进提示词。",
  "{n} tools": "{n} 个工具",
  "{total} models": "共 {total} 个型号",
  "{total} models, showing {shown}": "共 {total} 个型号,当前显示 {shown} 个",
  "{v} context": "{v} 上下文",
"Clear all": "清空",
  "Ticking one applies it to the whole group; use \"Add\" to create a new one or find one on GitHub.": "勾选后对全群生效;点「添加」随时新建或从 GitHub 找。",
};

let current: Lang = DEFAULT_LANG;

function interpolate(s: string, vars?: Record<string, string | number>): string {
  if (!vars) return s;
  for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

function translate(lang: Lang, key: string, vars?: Record<string, string | number>): string {
  return interpolate(lang === "zh" ? ZH[key] ?? key : key, vars);
}

/** Current UI language, readable from non-component code (api.ts, callbacks). */
export const currentLang = (): Lang => current;

/** 非组件代码(api.ts / lib.ts / 事件回调)用的翻译函数:按当前语言返回。 */
export const tr = (key: string, vars?: Record<string, string | number>) => translate(current, key, vars);

/**
 * 双语内容取值:源码里同时写着英文和中文的「内容」用这个(界面文案不要用它,用 t())。
 * 中文缺失时回退英文,所以可以先把英文补上、中文随后补。
 */
export function pickLang(en: string, zh: string | undefined, lang: Lang): string {
  return lang === "zh" ? zh || en : en;
}

/** Module-level bilingual content picker, using the current language. Use this from
 * non-component code; components should use `useI18n().pick` so they re-render on a
 * language change. */
export const pick = (en: string, zh?: string): string => pickLang(en, zh, current);

interface I18nCtx {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
  /** 双语内容取值(见 pickLang)。 */
  pick: (en: string, zh?: string) => string;
}
const Ctx = createContext<I18nCtx | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => {
    const v = prefs.read(PREF_KEY);
    return v === "zh" || v === "en" ? v : DEFAULT_LANG;
  });
  current = lang;

  const setLang = useCallback((l: Lang) => {
    current = l;
    setLangState(l);
    prefs.write(PREF_KEY, l);
  }, []);

  // 让 <html lang> 跟着变(CSS、系统字体回退、辅助技术都看它)
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => translate(lang, key, vars),
    [lang],
  );

  const pick = useCallback((en: string, zh?: string) => pickLang(en, zh, lang), [lang]);

  const value = useMemo(() => ({ lang, setLang, t, pick }), [lang, setLang, t, pick]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18nCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useI18n 必须在 I18nProvider 里使用");
  return c;
}
