<img width="1469" height="842" alt="Screenshot from 2026-07-09 22-06-41" src="https://github.com/user-attachments/assets/88c10be3-36cc-4173-b298-07163019cf17" />
# Knowledge Index — GraphRAG for code

**Documentation: [knowledge-index.sereda.lv](https://knowledge-index.sereda.lv)** — getting started, every setting, the shared server.

**One search over several related code projects and their docs, for AI agents.** In a few minutes it builds a
map of the projects (for example a Laravel API and its Vue or React frontends) and answers what depends on
what, which screen calls which route, how to get from A to B across repositories, and what a pull request
touches. It keeps the map current within seconds of each commit. Agents ask it over MCP. Everything runs on
your machine, no API keys.

Built on [**graphify**](https://github.com/Graphify-Labs/graphify) (AST code graph) and
[**qmd**](https://github.com/tobi/qmd) (on-device Markdown search).

> [!TIP]
> **Using it on your own codebase?** It started on one Laravel API with Vue frontends, so other stacks will hit
> rough edges. Fixes, new language plugins and pull requests are very welcome: open an issue or a PR.

## See it first: the demo

A small built graph of a made-up shop ships with the repo:

```bash
make demo         # open the demo dashboard (python3 only, nothing to build)
```

Red edges are routes: a Vue view's `api.get('/orders')` linked to the controller method that serves it.

## Quick start

1. **Two tools** the KB drives: `graphify` (`uv tool install graphifyy`) and `qmd`
   (`npm i -g @tobilu/qmd`), plus Python 3.11+.
2. **Install:**
   ```bash
   uv tool install knowledge-index     # or: pipx install knowledge-index
   kb-install                          # checks the tools, writes a config to fill in
   ```
3. **Name your projects** in `kb.projects.toml` (`kb --version` shows where it is): the code folders, the
   docs folder, and for a Laravel API the app folder (its routes become graph edges).
   ```toml
   [[project]]
   name   = "api"
   code   = ["/srv/shop/api/app"]
   docs   = "/srv/shop/api/docs"
   routes = "/srv/shop/api"

   [[project]]
   name   = "web"
   code   = ["/srv/shop/web/src"]
   calls  = ["/srv/shop/web/src"]      # its API calls are linked to ...
   serves = "api"                      # ... the controllers of this project
   ```
4. **Build:** `kb build` (a few minutes the first time; `kb update` after that).
5. **Connect your agent:** `claude mcp add kb -- kb-mcp` (or the `.mcp.json` entry in
   [docs/install.md](docs/install.md#1-mcp-server-config)), then restart the session.
6. **Make kb the first search tool:** install the `kb` skill, the two hooks and the `CLAUDE.md` rule
   ([Install, step 3](docs/install.md#3-make-kb-the-first-search-tool-required)). Do not stop at step 5: an agent with the tools only
   connected keeps to grep and never asks the knowledge base.

That is the whole core. Everything else is optional and off until you turn it on.

## What the agent gets

| Tool | Answers |
|---|---|
| `contracts_search` | a route: its controller, middleware and every file that calls it |
| `graph_explain` | one symbol and all its connections |
| `graph_affected` | what depends on a symbol, across repositories |
| `graph_routes` | several ways from A to B, with the routes on the way |
| `review_context` | a diff: what changed, what depends on it, the routes and screens it reaches, the docs to check |
| `search` | docs and code by meaning |
| `code_grep` | exact text with a regular expression: config keys, SQL, error messages |
| `repo_map` | a first look at a project: what most of the code leans on; with `unused`, dead-code candidates |

Long answers come in pages with a cursor; `connections` and `problems` show the service map and its gaps.

## Add-ons

Off until you install them; an add-on that is off adds no tool, page or route.

| Add-on | `kb addon install …` gives | Needs |
|---|---|---|
| `tool-multicall` | `batch`: several tool calls in one request | — |
| `code-mode` | `run_code`: a small program that calls the tools, in a sandbox | Node 22+ |
| `fast-routes` | `graph_routes` on a Rust core, 2-3x faster | graphify via uv or pipx |
| `metrics` | `GET /metrics` for Prometheus, Grafana or PMM | — |
| `audit` | an Audit tab: sign-ins, admin actions, every tool call with user and argument | the admin pages |

`kb addon list` shows what is on, `kb addon remove <name>` switches one off. The admin pages (`/admin`:
status, charts, usage, sign-in clients, settings) are part of the core: on a shared server they need
`KB_ADMINS` or an access plugin with roles. An add-on can also live
outside this repository, under its own licence: `kb addon install <folder with plugin.py>` (see
`bin/plugins.py` for the four names a plugin defines). A team server with browser
sign-in and access per project: [docs/server.md](docs/server.md); it asks your own system who may see
what through two webhooks, [docs/access-protocol.md](docs/access-protocol.md).

## More

- [The documentation site](https://knowledge-index.sereda.lv): everything below, with search, and the
  [configuration reference](https://knowledge-index.sereda.lv/configuration) of every setting.
- [Install and configure](docs/install.md): requirements, the configuration, the dashboard, wiring the KB
  into each project.
- [The shared server](docs/server.md): sign-in, access per project, speed, admin pages, metrics.
- [Review context on pull requests](docs/server.md#review-context-on-pull-requests): what a branch touches.
- [Writing docs the KB can use](docs/documentation-guide.md), and the `doc-organization` skill that
  applies it.
- [Design](docs/design.md): where it sits among similar tools, the plugins, the rules, the roadmap.
- [Changelog](CHANGELOG.md).

## License

[AGPL-3.0-or-later](LICENSE): free software, use, study, share and modify it. Copyright (C) 2026 Romans Sereda.
Changed versions must stay under the AGPL, and that includes running a changed KB as a service for other people:
its users can then ask for the source of what they use. Versions up to 0.13.0 were released under GPL-3.0-or-later.
