第三方组件声明 / Third-Party Notices
====================================

本文件列出 Team Agent 使用的第三方开源组件及其许可证。**本程序不分发、不附带任何第三方内容**
（例如示例库数据）—— 这类内容需要你自己获取，并自行遵守其许可条款。

Last verified: 2026-09-21（按本机实际安装的版本逐个核对元数据与 LICENSE 文件）

后端（Python，见 `backend/requirements.txt`）
--------------------------------------------

| 组件 | 许可证 |
| --- | --- |
| litellm | MIT |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| mcp (Model Context Protocol Python SDK) | MIT |
| pypdf | BSD-3-Clause |
| python-docx | MIT |
| pytest | MIT |
| pytest-asyncio | Apache-2.0 |
| starlette | BSD-3-Clause |
| pydantic / pydantic-core | MIT |
| openai | Apache-2.0 |
| anyio | MIT |
| tiktoken | MIT |

前端（Node，见 `desktop/package.json`）
--------------------------------------

| 组件 | 许可证 |
| --- | --- |
| react / react-dom | MIT |
| react-markdown | MIT |
| remark-gfm | MIT |
| lucide-react | ISC |

传递依赖
--------
以上组件的依赖链中**没有 GPL / AGPL**（即不存在要求本程序开源自有代码的强 copyleft 依赖）。
其中 `tqdm`、`certifi` 采用 **MPL-2.0**（弱 copyleft、基于文件）：只要你不对其源文件做修改再分发，
就不会影响本程序的授权。完整清单可在你的环境中用 `pip-licenses`（后端）与
`license-checker`（前端）重新生成。

分发时的义务
------------
上述组件均为宽松许可（MIT / BSD / Apache-2.0 / ISC）。**当你把本程序打包分发给他人时**
（例如 PyInstaller 打包后端 + electron-builder 打包桌面端），需在分发物中：

1. 保留这些组件的版权声明与许可证文本；
2. 对 Apache-2.0 组件（pytest-asyncio、openai）额外保留其 NOTICE 文件（如有）并标注修改；
3. 建议在应用内提供「开源许可」页面，或在安装目录放置本文件与各许可证全文。

生成完整清单的参考命令：

```bash
# 后端
.venv/bin/pip install pip-licenses && .venv/bin/pip-licenses --format=markdown
# 前端
cd desktop && npx license-checker --summary
```

可选的外部集成（不属于本仓库内容）
----------------------------------
「示例库」功能可选择性地读取 `awesome-llm-apps`
(<https://github.com/Shubhamsaboo/awesome-llm-apps>，Apache-2.0) 的**本地克隆**，
提取团队 / 角色 / 技能 / MCP 的描述文字，写入**你自己的数据目录**。

- 本仓库**不包含**该项目的任何文件或数据；
- 该功能的输入由你自行准备，提取结果也保存在你的机器上；
- 若你将该内容再分发给第三方，**由你**履行 Apache-2.0 的义务
  （保留署名与许可证副本、标注「已修改」）。

「外部智能体」功能同理：它只是调用你本机已安装的某个命令行程序。那些程序的许可与服务条款
由你自行遵守，本程序与其开发者之间不存在任何关联、赞助或背书关系。
