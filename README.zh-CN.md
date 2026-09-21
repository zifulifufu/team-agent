# Team Agent

[English](README.md) · 中文

把国内外的大模型拉进同一个群聊,让它们按各自强项分工、用 `@成员名` 互相交接。面向办公文档、视频制作、
写作与研究场景的桌面工作台。

- **多模型同群。** 每个成员可以是不同的模型;某个模型失败或被限流,请求自动回退到下一个,最后有本地模型兜底。
- **群主负责分工。** 稍复杂的任务由群主拆成任务(负责人、依赖、交付物),执行完再由群主整合。
- **工具真的能被调用。** 工具调用走纯文本协议,云端与本地模型都能用。
- **本地资料与记忆。** 可检索的资料库,以及跟着群走的记忆。
- **数据留在本机。** 后端只监听回环地址,密钥存在系统钥匙串里。

界面语言:**默认英文**,可在 *设置 → 外观 → 语言* 切换成中文。内置内容(模型目录、本地型号清单、强项标签、模板中心)同样双语并跟随该设置;所有接口都接受 `?lang=zh` 或 `Accept-Language` 请求头。

## 工作原理

```mermaid
flowchart LR
  UI["桌面应用<br/>Electron + React"] <-->|"HTTP + WebSocket<br/>仅 127.0.0.1"| API["后端<br/>FastAPI"]
  API --> ORCH["编排器<br/>谁发言、上下文、@ 交接"]
  API --> ROUTE["路由<br/>挑模型、回退"]
  API --> TOOLS["工具<br/>内置 · 插件 · MCP"]
  ROUTE --> MODELS["云端与本地模型"]
  TOOLS --> EXT["MCP 服务器 · 插件代码"]
  API --> DB[("SQLite<br/>消息 · 记忆 · 资料")]
```

一轮对话:

```mermaid
sequenceDiagram
  participant U as 你
  participant H as 群主模型
  participant M as 成员
  U->>H: 任务(可 @ 某个成员)
  H->>H: 分工 — 目标、统一约定、任务、依赖
  H->>M: 按依赖顺序派活
  M->>H: 交付结果
  H->>U: 整合后的答复
```

## 快速开始

```bash
scripts/dev-setup.sh            # 建 venv、装后端与前端依赖
scripts/setup_local_model.sh    # 可选:安装 Ollama 并拉取本地兜底模型
cd desktop && npm run dev       # 启动后端并打开桌面窗口
```

首次打开后在 *设置 → 模型服务* 填入 API 密钥(不填则全部走本地模型),回到首页选一个场景或群聊模板,
写下任务即可。

只在浏览器里看:`cd backend && python -m app`,再 `cd desktop && npm run dev:web`,打开
<http://localhost:5173>。

## 测试

```bash
cd backend && ../.venv/bin/python -m pytest    # 全量,约一分钟
cd desktop && npx tsc --noEmit                 # 前端类型
python3 scripts/check-i18n.py                  # 在仓库根目录运行:查词典缺口与语言被冻结
```

有 **1 个测试默认跳过**:`test_real_keychain_round_trip`。它会往你真实的登录钥匙串写一条
(service `team-agent`、account `selftest:roundtrip`),读回来再删掉,所以平时跑测试不会碰到你的密钥。
要连它一起跑:

```bash
cd backend
TEAM_AGENT_KEYCHAIN_TEST=1 ../.venv/bin/python -m pytest tests/test_compliance.py::test_real_keychain_round_trip
```

其余测试一律走内存里的假钥匙串——`tests/conftest.py` 用 `TEAM_AGENT_NO_KEYCHAIN=1` 维持这一点,
把这行去掉才会碰到真实钥匙串。

## 功能

| 模块 | 说明 |
| --- | --- |
| 群聊 | 成员、群主、`@` 交接、分工声明、实时任务板 |
| 分工 | 自动 / 总是 / 不分工,可按群设置;计划先校验再执行 |
| 模型 | 内置目录 + 实时清单、按强项挑模型、路由链、自动回退、每个模型一盏连通指示灯 |
| 工具 | 文本协议调用(每条回复最多 3 次)、5 个内置工具、Python 插件、stdio / SSE / HTTP 的 MCP |
| 资料库 | txt、md、csv、json、html、pdf、docx → 分片 → BM25 检索(支持中文);可按群限定范围;`#文档标题` 引用 |
| 记忆 | 全局 / 群 / 成员 × 偏好、事实、决定、教训、做法;自动提炼;与 Obsidian 双向同步 |
| 提示词 | 可编辑的全局系统提示词、提示词库、群提示词、`{{变量}}` |
| 模板中心 | 51 条现成的团队、岗位、技能、提示词与 MCP 用法,一键安装;也支持放自己的 JSON |
| 本地模型 | 推荐目录、硬件适配估算、新版本发现 |
| 外部智能体 | 把命令行智能体当群成员用,只读 / 可改 / 完全三级权限,默认关闭 |
| 数据 | 备份与恢复、聊天导出 Markdown、使用统计 |

## 安全

- 后端只监听 `127.0.0.1`,每次启动生成随机令牌,所有请求都要带上。
- API 密钥与 GitHub 令牌存在**系统钥匙串**里,数据库只留引用。
- 备份、导出与接口返回都不含明文密钥。
- **默认不自动外联**:自动检查更新默认关闭。
- 「禁止调用云端模型」开关会拦住所有云端请求,包括更新检查与远程 MCP。
- 插件与 MCP 服务器以你的权限运行代码,请只启用信任的。

## WhatsApp 通道

群聊也可以从 WhatsApp 那边够到:别人给你的 WhatsApp 号码发消息,这里的群作答,回复再发回去。
默认关闭,并且需要在本应用之外准备三件事。

**1. 一个公网 HTTPS 地址。** Meta 的 Cloud API 会把每个事件推到回调地址,而它拒收 `localhost`、
内网地址和纯 HTTP。常规做法是用隧道:`cloudflared tunnel --url http://127.0.0.1:8765`,或
`ngrok http 8765`,把它打印出的域名填进 *设置 → WhatsApp 通道 → 公网域名*。那个域名是「API 只监听
回环」的唯一例外;走到它上面的请求靠 Meta 的签名鉴权,而不是靠应用令牌。改完需要重启应用。

**2. 一个开通了 WhatsApp 产品的 Meta 应用**:phone number id、访问令牌、App Secret,再加上一个
你自己编的 verify token。把 `<你的域名>/hooks/whatsapp` 和那个 verify token 填进
*WhatsApp → Configuration → Webhook*,并订阅 `messages` 字段。请用永久访问令牌,临时令牌 24 小时就过期。

**3. 在中国大陆还需要一个代理。** 那里访问不到 `graph.facebook.com`,所以发送回复要配代理
(本机 Clash 是 `http://127.0.0.1:7890`)。点「检查连通性」只发一个请求就能同时验证令牌、number id
与代理是否都通,而且不会给任何人发消息。

只有文本消息会进来,而且只有白名单里的号码能说话——其他人一律丢弃,只在设置页里计数,不会得到回复。
由这条通道触发的一轮**只能使用只读工具**:可以检索资料库和记忆,但永远不能运行代码或写文件,
因为这台机器前没有人能替你点确认。回复会按设定长度截断;若 WhatsApp 拒收(最常见的原因是超过
24 小时会话窗口、必须改用预审模板),原因会显示在那一页上,而不是无声消失。

## 许可

**[Apache License 2.0](LICENSE)**,Copyright 2026 **zifulifufu**。

- 可自由使用、修改、再分发与出售,包括商业用途。
- 分发副本时保留版权声明、许可全文与 [NOTICE](NOTICE),并注明你改动了哪些文件。
- 含明确的专利授权:若你以本软件的专利起诉贡献者,该授权自动终止。不授予商标权,亦不提供担保。
- **0.5.0 及更早**的版本以 Business Source License 1.1 发布;**0.5.1 起**本仓库改用 Apache-2.0。
  你此前按 BUSL-1.1 取得的副本,仍适用当时的条款。

第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md):全部是宽松许可(MIT / BSD / Apache-2.0 /
ISC),没有 GPL / AGPL,不会要求你开源自己的代码;但再分发时要保留它们的声明。仓库不附带任何其它项目的内容。

## 已知限制

- 只在 macOS 上验证过。尚未做代码签名、公证与自动更新。
- 模型目录是快照;强项标签是启发式的,不是评测成绩。
- 分工质量取决于群主模型是否遵守计划格式;格式不对会降级为普通接力。
- PDF 抽取不做 OCR,扫描版读不了。
- 资料库文件夹重新导入按文件大小判断变化,同样大小的修改会被漏掉。
- 只有一家服务商时,按强项挑模型的效果有限,接入更多模型才能体现价值。

## 路线图

1. 打包:macOS 与 Windows 的签名安装包,以及自动更新。
2. 群聊支持附件(图片、音视频)。
3. 插件沙箱。
