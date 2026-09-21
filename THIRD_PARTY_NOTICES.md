第三方组件声明 / Third-Party Notices
====================================

本文件列出 Team Agent 使用的第三方开源组件及其许可证。**本程序不分发、不附带任何第三方内容**：
模板中心里的团队 / 角色 / 技能 / 提示词都是本程序自己写的原创文本，不含任何第三方项目的数据，
因此随程序分发（包括打包出售）不产生第三方许可义务。

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
「模板中心」的团队 / 角色 / 技能 / 提示词由本程序自带，是本项目的原创文本（见 `backend/app/presets.py`
与 `backend/app/tools.py` 的 `EXAMPLE_SKILLS`），**不是**从第三方项目提取的，可以随程序分发。

- 你自己放进数据目录 `templates/` 的模板由你负责其内容与来源；若其中含第三方内容，
  由你履行对应许可的义务（保留署名与许可证副本、标注「已修改」等）。
- 「模板中心 → MCP」列出的服务器（如 `@modelcontextprotocol/server-filesystem`）只是**命令与参数的
  推荐写法**，本程序不包含也不分发这些软件；使用前请自行阅读其许可与文档，导入后它们一律是停用状态。

「外部智能体」功能同理：它只是调用你本机已安装的某个命令行程序。那些程序的许可与服务条款
由你自行遵守，本程序与其开发者之间不存在任何关联、赞助或背书关系。
