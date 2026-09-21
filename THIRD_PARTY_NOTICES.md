Third-Party Notices
===================

Team Agent itself is released under the **Business Source License 1.1**
(see [LICENSE](LICENSE); Licensor `zifulifufu`, Change Date 2030-09-21, after which it
converts to **MPL 2.0**). That license covers only this project's own content and does
not change the terms of the third-party components listed below — those remain under
their own licenses.

This project bundles no third-party content. Everything shipped with it — including the
team / role / skill / prompt templates in the template gallery — is original text written
for this project, so redistributing the program (including selling packaged builds)
creates no third-party license obligations for that content.

Last verified: 2026-09-21 (checked against the versions actually installed on a local
machine, reading each package's metadata and LICENSE file).

Backend (Python — see `backend/requirements.txt`)
-------------------------------------------------

| Component | License |
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

Frontend (Node — see `desktop/package.json`)
--------------------------------------------

| Component | License |
| --- | --- |
| react / react-dom | MIT |
| react-markdown | MIT |
| remark-gfm | MIT |
| lucide-react | ISC |

Transitive dependencies
-----------------------

There is **no GPL / AGPL** anywhere in the dependency chain, so no dependency requires
this project to open-source its own code. `tqdm` and `certifi` are **MPL-2.0** (weak,
file-based copyleft): as long as you do not modify their source files and then
redistribute them, they do not affect this project's licensing. You can regenerate the
full list in your own environment with `pip-licenses` (backend) and `license-checker`
(frontend).

Obligations when you distribute
-------------------------------

All of the components above use permissive licenses (MIT / BSD / Apache-2.0 / ISC).
**When you distribute the program to others** (for example a PyInstaller backend bundle
plus an electron-builder desktop build), you must:

1. keep the copyright notices and license texts of those components;
2. for the Apache-2.0 components (pytest-asyncio, openai), also keep their NOTICE file
   if present, and mark any files you modified;
3. ideally ship an "Open source licenses" screen in the app, or place this file and the
   full license texts in the installation directory.

Reference commands to generate the complete list:

```bash
# backend
.venv/bin/pip install pip-licenses && .venv/bin/pip-licenses --format=markdown
# frontend
cd desktop && npx license-checker --summary
```

External integrations (not part of this repository)
---------------------------------------------------

The template gallery's team / role / skill / prompt entries ship with the program and are
original text written for this project (see `backend/app/presets.py` and `EXAMPLE_SKILLS`
in `backend/app/tools.py`). They are not extracted from any third-party project and may
be redistributed with the program.

- Templates you place in the `templates/` directory are your own responsibility; if they
  contain third-party content, you are the one who must meet the corresponding license
  obligations (attribution, a copy of the license, marking modifications, and so on).
- The servers listed in the template gallery under **MCP** (for example
  `@modelcontextprotocol/server-filesystem`) are only suggested commands and arguments.
  This program neither contains nor distributes that software. Read its license and
  documentation before using it; imported servers are always left disabled.

The same applies to the external-agent feature: it only invokes a command-line program
that is already installed on your machine. You are responsible for complying with that
program's license and terms of service. This project has no affiliation with, and is not
sponsored or endorsed by, its developers.
