# 专区面板：设计评审与调试（2026-09-27）

用户要求：「几个专区的面板让 codex 好好设计个调试」。**codex 在本机本会话环境里用不了**（三次
实测，见文末），所以这一轮由 **claude**（`/usr/local/bin/claude`，2.1.215，走 DeepSeek）出评审，
**每一条都在代码里核实过**才动手。

两轮审查：

| 轮次 | 问题单 | 产出 |
|---|---|---|
| 设计评审 | `/tmp/review-zones.md` | 页面结构上的真洞 + 「不要有规划中、不能留空」怎么在不撒谎的前提下落地 |
| 调试评审 | `/tmp/review-zones-debug.md`（附未提交改动） | 9 条：3 条缺陷、4 条可疑、2 条风格 |

## 一、已核实并修掉的

### 1. 建群之后用户**落不到新群里**（最重）

- `App.tsx:136-138` 有一条「打开中的群不在列表里就回首页」，而 `groups` 靠 **5 秒**轮询刷新。
- 新增的「从它起一个群」按钮建完群直接 `onOpen(g.id)`，那一刻 `groups` 还是旧的 → 用户被从新群带走。
- 首页那条路一直是这么写的：`HomePage.useTemplate` 里 `await reload()` 之后才 `onOpen`，注释是
  「a template may have created members, so refresh both lists」。新代码没有这一步。
- 修：`ZonePage.startFrom` 改成 **起群 → `await reload()` → 再跳**。
- **断言验证过它会红**：把 `await reload()` 临时撤掉跑冒烟，`按下去之后落在新群的对话里` 立刻失败
  （群确实建出来了 `1 → 2`，但页面 `{home:false, chat:false}` —— 用户什么都看不到）。恢复后绿。

### 2. `zone.template`：声明了、送到了、**全前端没有一处读它**

- 后端注释自己写着「页面会说」——页面从来没有说过。`desktop/src` 全仓 grep `.template` 零命中。
- 修：`zones.detail()` 的组装从「除 name/blurb/四块之外全发」改成**白名单**（`id/icon/state` +
  name/blurb + 三块）。给注册表加键不再会不声不响地多送一份数据。
- ⚠️ **注册表里的 `template` 留着**：两条注册表守卫读它（模板存在、`in_template` 与之对齐），
  将来这个专区起群时那份名单也是它。`api.ts` 的 `ZoneDetail.template` 删掉。
- 守卫：新增 `test_the_response_carries_only_the_keys_the_page_reads`。

### 3. 中文界面下状态徽章显示英文，`Blocked` 还是**错义**

- `STATE_LABEL` 原来是「先拼一张英文表、运行时再查」→ `check-i18n.py`（只看字面量 `t("…")`）
  **完全看不见这四个键**。实测后果：`Ready`/`Partly ready` 在 ZH 表里**根本不存在**；
  `Blocked` 撞上权限页那条 `"Blocked": "禁止"`（「总是禁止」的语境），而专区里它的意思是
  「建好了、本机有一样缺的挡着」（缺节点包、账号没额度、本机没图像模型）。
- 修：① 四条改成**字面量 `t("…")` 调用**（`STATE_LABEL` 里存函数，不再存字符串）——检查器从此看得见；
  ② `ready`/`partial` 补中文；③ `blocked` 换词成 `"Blocked here"`（「本机挡着」），
  与权限页那条**分开**：一个英文词两种意思，英文即 key 的词典装不下。
- 顺带修掉徽标里第二处判断：`zone.state === "ready" ? "Ready" : "Planned"` → 共用同一张表
  （一旦哪天真有 `partial`/`blocked` 的专区，它不会再管它叫「规划中」）。

### 4. 点了没反应的按钮（组件契约）

- `startable` 只看后端的 `action`，与 `onOpen` 无关：不传 `onOpen` 的调用点会得到一排**点了什么都
  不发生**的按钮（`startFrom` 里那个守卫在**点击之后**才生效，治不了）。今天不可达，但契约上没有前提。
- 修：`startable = canStart && it.action === "create-group"`，两个条件都要成立才画按钮。

### 5. 失败提示贴在页头，按钮在下方

- 建群被拒时错误画在 `<header>` 里；用户点完按钮，视口里**没有任何变化** —— 从用户视角就是静默失败。
- 修：错误贴回**按下的那一行**（`failedId`），页头那份撤掉。

## 二、设计评审里已落地的两条（不撒谎地让「规划中」不再是空壳）

- **头部一行摘要**（`由三块数据算出来`，不是手写文案）：「今天能用:研究报告、方案撰写、办公文档、
  写文档 · 还有 5 项没建」。它消解的是**徽标与条目的矛盾**：写作专区徽标写「规划中」，里面却有
  3 条 ready 模板 + 1 条 ready 工作流，用户不知道该信哪个。**没有把 `planned` 改叫 `ready`**。
- **每块的计数从「一共几条」改成「能用几条 / 一共几条」**（写作：`0/2`、`3/4`、`1/3`），
  并且**能用的行排前面**（稳定排序，同状态保持声明顺序 —— 视频专区那 6 条作曲预设的顺序不变）。
- **起点按钮**：`action` 来自 `create_group_from_template` **自己那次查找**，所以按钮不可能指向一个
  端点会拒绝的模板。视频专区那 6 条作曲风格预设被**自动**判成不可建群（同块两种东西被正确区分）。

## 三、剩下的（核实过，未做）

| # | 事实 | 为什么没动 |
|---|---|---|
| 1 | 用户自己写的专区文件里，`templates` 的 `ready` 条目**没有任何守卫** —— 写一个不存在的 id 会得到「绿色 Ready 徽章 + 没有按钮 + 没有解释」 | 内置四份加了守卫测试（`test_every_row_a_builtin_zone_calls_ready_is_reachable`）；**用户文件那条路**要动注册表/加载侧校验，没做 |
| 2 | 模板找得到但成员建不出来时，`create_group_from_template` 里 `ensure_agent` 返回 `None` 被静默过滤，极端情况**建出一个零成员的群并返回 200** | 改的是建群语义（`templates.py`），会牵动已有测试与用户存的模板；**留给单独一轮** |
| 3 | `planned` 条目的 note 现在写的是「将来会是什么」，不是「缺什么」（只有 `blocked` 有这条规矩，见 `test_zones.py:114-120`） | 是**内容规则**（要改 4 个专区约 14 条 note 的中英两版）+ 一条新断言；单独一轮做，别混在渲染改动里 |

（另一条原属「没覆盖」的：故事/创作专区的 `action` 没有断言 —— 本轮已补，见
`test_the_three_zones_without_a_workbench_each_offer_a_real_starting_point`。）

## 四、验证

- 后端全量（隔离环境）：**1739 passed / 1 skipped / 0 failed**（本轮 +3 条专区测试）。
- 界面冒烟：**all checks passed（461 项，本轮 +8）**，其中新增：
  - 起点按钮存在/可见/可点/不是每条都有；
  - **按下去真的建出群、并且落在新群里**（这条实测过会红，见第一节第 1 条）；
  - 头部摘要真的画出来了、两块都说了、计数是「能用/总数」、能用的行排在前；
  - 钉死「看的是写作专区」——story/creation 各有 1 个可点行，否则会在**错误的页面上通过**。
- `tsc` 0 错；i18n `missing 0 · frozen 0`。

## 五、方法坑（这一轮踩到的）

1. ⚠️⚠️ **BSD `grep` 不认 `\|`，静默返回空** —— 本轮**又踩了三次**（查 i18n 状态词、查
   `STATE_LABEL`、查页面根类），每次"没有结果"都长得像"真的没有"。**断言「没有 X」之前用 Grep 工具。**
2. ⚠️⚠️ **冒烟脚本按 `head -1` 取应用令牌**：上一轮 `kill` 留下一个**托孤窗口**（它的后端已死），
   两个令牌里它排在前面 → 401 被答成 `(api_groups || []).map is not a function`，
   **看起来像页面坏了**。已改成「逐个试到后端接受的那个」+ 找不到时报清楚有几个候选。
3. ⚠️ **重启这个开发包要连托孤后端一起停**：`open -a` 只是把已有窗口调到前台；杀掉 Electron 主进程
   之后，`-m app --port 8765` 那个 Python 子进程会活下来（父没了），必须先停它、确认端口放掉，再
   `open`。三个指标一起看：`/api/health` 200、**令牌是新的**、响应里是新代码（例如 `template` 已不在）。
4. ⚠️ **codex 在本机本会话环境不可用**（第三次确认）：裸跑崩在 x64 node（
   `Missing optional dependency @openai/codex-darwin-x64`），换 arm64 node 能打版本号但
   `echo PONG | codex exec` **10 分钟无输出**；再加 `-c 'mcp_servers={}' -c 'model_reasoning_effort="low"'`
   也一样。所以"联合 codex 和 claude"这件事**只有 claude 一路**。
