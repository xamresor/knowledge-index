<img width="1469" height="842" alt="Screenshot from 2026-07-09 22-06-41" src="https://github.com/user-attachments/assets/88c10be3-36cc-4173-b298-07163019cf17" />
# Code Knowledge Base

A read-only knowledge layer over a set of related code projects (for example a Laravel API and its
Vue frontends) for AI agents. It builds **one code graph across the repositories**, with every
frontend API call linked to the controller that serves it, and an **on-device search** over the
projects' docs. Agents ask it over MCP: which route a page calls and what it touches, what breaks if a
class changes, which ways lead from A to B. Everything runs on your machine, no API keys.

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

That is the whole core. Everything else is optional and off until you turn it on.

## What the agent gets

| Tool | Answers |
|---|---|
| `contracts_search` | a route: its controller, middleware and every file that calls it |
| `graph_explain` | one symbol and all its connections |
| `graph_affected` | what depends on a symbol, across repositories |
| `graph_routes` | several ways from A to B, with the routes on the way |
| `search` | docs and code by meaning |

Long answers come in pages with a cursor; `connections` and `problems` show the service map and its gaps.

## Add-ons

Off until you install them; an add-on that is off adds no tool, page or route.

| Add-on | `kb addon install …` gives | Needs |
|---|---|---|
| `tool-multicall` | `batch`: several tool calls in one request | — |
| `code-mode` | `run_code`: a small program that calls the tools, in a sandbox | Node 22+ |
| `fast-routes` | `graph_routes` on a Rust core, 2-3x faster | graphify via uv or pipx |
| `metrics` | `GET /metrics` for Prometheus, Grafana or PMM | — |
| `admin` | `/admin`: status, charts, usage, sign-in clients | on a shared server: `KB_ADMINS` |

`kb addon list` shows what is on, `kb addon remove <name>` switches one off. A team server with browser
sign-in and access per project: [docs/server.md](docs/server.md); it asks your own system who may see
what through two webhooks, [docs/access-protocol.md](docs/access-protocol.md).

## More

- [Install and configure](docs/install.md): requirements, the configuration, the dashboard, wiring the KB
  into each project.
- [The shared server](docs/server.md): sign-in, access per project, speed, admin pages, metrics.
- [Writing docs the KB can use](docs/documentation-guide.md), and the `doc-organization` skill that
  applies it.
- [Design](docs/design.md): where it sits among similar tools, the plugins, the rules, the roadmap.
- [Changelog](CHANGELOG.md).

## License

[GPL-3.0-or-later](LICENSE) — free software: use, study, share and modify it. Copyright (C) 2026 Romans Sereda. Derivative works must also be licensed under the GPL (copyleft).
