# Tools and add-ons

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
| `admin` | `/admin`: status, charts, usage, sign-in clients | on a shared server: `KB_ADMINS` |
| `audit` | an Audit tab: sign-ins, admin actions, every tool call with user and argument | `admin` |

`kb addon list` shows what is on, `kb addon remove <name>` switches one off. An add-on can also live
outside this repository, under its own licence: `kb addon install <folder with plugin.py>` (see
`bin/plugins.py` for the four names a plugin defines). A team server with browser
sign-in and access per project: [the shared server](server.md); it asks your own system who may see
what through two webhooks, [access protocol](access-protocol.md).

