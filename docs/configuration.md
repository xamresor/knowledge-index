# Configuration reference

Every setting in one place. Knowledge Index reads two kinds of settings:

- **the project file** `kb.projects.toml`: which projects to index and how they connect;
- **environment variables** `KB_*`: where files live, how the servers behave, what is switched on.

`kb --version` prints where the project file and the data folder are. A setting that is not set takes the
default shown here.

## The project file: `kb.projects.toml`

One `[[project]]` block per project. Only `name` is required; a project with `docs` and no `code` is a
docs-only project.

```toml
[[project]]
name   = "api"
code   = ["/srv/shop/api/app"]
docs   = "/srv/shop/api/docs"
routes = "/srv/shop/api"

[[project]]
name   = "web"
code   = ["/srv/shop/web/src"]
calls  = ["/srv/shop/web/src"]
serves = "api"
```

| Key | Type | Description |
|---|---|---|
| `name` | string | The project's name in answers and in access rules. Letters, digits and `-`. |
| `code` | list of paths | Folders whose code goes into the graph. `vendor`, `node_modules`, `tests`, build output and images are left out. |
| `docs` | path | The docs folder: Markdown is searched; PDF, DOCX, ODT, RTF, EPUB and scans are extracted to text first. |
| `routes` | path | A Laravel app folder: `php artisan route:list --json` there gives the route table, and its routes become graph edges. A path that ends in `.json` is a ready route table instead ([Docker](docker.md#route-tables-from-the-app-s-container)). |
| `calls` | list of paths | Folders with frontend or backend code whose HTTP calls are linked to the routes of `serves`. |
| `serves` | string or list | The project(s) whose routes these calls go to, e.g. `"api"` or `["api", "billing-api"]`. |

## Where files live

| Variable | Default | Description |
|---|---|---|
| `KB_HOME` | — | One folder for data and config together, e.g. `/srv/kb`. Overrides the two below. |
| `KB_DATA` | `~/.local/share/knowledge-index` (a checkout: the checkout) | Built graphs, staged code and docs, logs, counters, add-on state. |
| `KB_CONFIG` | `~/.config/knowledge-index` (a checkout: the checkout) | `kb.projects.toml`, `kb.hubs.txt`, `admins.txt`. |
| `KB_COLLECTION` | `kb` | Prefix of the docs search collections (`kb-<project>`). |
| `KB_SELF_DOCS_NAME` | `kb` | Project name for a checkout's own `docs/` folder. |
| `KB_ALIASES` | the shipped table | Alias table for search (`term = synonym, synonym`). |

## Building the index

| Variable | Default | Description |
|---|---|---|
| `KB_FORCE` | `0` | `1` rebuilds every project and every step, as `kb build` does. `kb update` otherwise skips what did not change. |
| `KB_EXTRACT` | `1` | `0` skips text extraction from PDF, DOCX and scans. |
| `KB_OCR_LANGS` | `eng` | Tesseract languages for scans, e.g. `eng+lav+rus`. |
| `KB_QMD_BACKEND` | asked once, then remembered | Embeddings: `llama` (on device), `none` (keywords only) or `other` (your own command). |
| `KB_QMD_EMBED_CMD` | — | With `other`: the command that computes embeddings. |
| `KB_REFRESH_WAIT_LOCK` | — | `kb-refresh` waits while your deploy holds this lock file. |
| `KB_ROUTES_DIR` | — | A folder with ready route tables, `<project>.json`. A project with `routes` reads its table there instead of running `php artisan route:list`. |

## Answers

| Variable | Default | Description |
|---|---|---|
| `KB_DOCS_MODE` | `light` | Docs search: `light` (no reranker, ~650 MB RAM), `full` (LLM reranker, slow on a CPU), `bm25` (keywords, ~70 MB). |
| `KB_PAGE_CHARS` | `6000` | Characters per page of a long answer; a call can ask for 1,000-50,000 with `page_length`. |
| `KB_CURSOR_TTL` | `600` | Seconds a long answer stays behind its cursor. |
| `KB_CURSOR_MAX` | `500` | Answers kept behind cursors at once. |
| `KB_CONTRACT_EDGES` | `1` | `0` hides other projects' ends of HTTP edges from a caller limited to some projects. |
| `KB_SESSION_DIR` | the current folder | The folder that decides a session's default projects. |

## Speed

| Variable | Default | Description |
|---|---|---|
| `KB_WARM` | `1` | `0` runs graphify and qmd as one process per call instead of keeping them loaded. |
| `KB_GRAPH_CACHE_FILES` | `2` | Graph files the graph worker keeps parsed. |
| `KB_GRAPH_VIEWS` | `8` | Caller views (one per project set) kept cut in memory; the recent ones are re-cut after each build. |
| `KB_FAST_ROUTES` | set by the `fast-routes` add-on | `1` computes `graph_routes` on rustworkx. |
| `KB_MAX_GRAPH_JOBS` | `3` | Graph calls at once on the shared server. |
| `KB_MAX_DOCS_JOBS` | `1` | Docs searches at once on the shared server. |

## Add-ons

| Variable | Default | Description |
|---|---|---|
| `KB_ADDONS` | `<data>/addons.json` | Comma-separated add-ons that are on, overriding the file; `none` switches all off. Names: `code-mode`, `tool-multicall`, `fast-routes`, `metrics`, `admin`, `audit`, `questions`, `scip`, `datadict`, and installed plugins. |
| `KB_BATCH_MAX` | `10` | Calls in one `batch`. |
| `KB_RUN_SECONDS` | `20` | CPU seconds a `run_code` program may use. |
| `KB_RUN_CALLS` | `30` | Tool calls a `run_code` program may make. |
| `KB_SANDBOX_SOCKET` | — | A Unix socket of the sandbox service (`sandbox/socket-server.mjs`). Set: `run_code` runs there, e.g. in a container without network ([Docker](docker.md#the-sandbox-container-code-mode)). |
| `KB_SANDBOX_MAX` | `4` | Sandbox service only: programs that run at once. |
| `KB_METRICS` | `off` | `on` serves `/metrics` without the add-on. |
| `KB_METRICS_TOKEN` | — | Bearer token or basic-auth password for `/metrics` from outside loopback. |
| `KB_DATADICT_DIR` | `<config>/datadict` | Folder of the data dictionary files, one `<project>.toml` each ([data dictionary](data-dictionary.md)). |
| `KB_ADMINS` | `<data>/admins.txt` | Comma-separated user names allowed into `/admin` on the shared server. |
| `KB_SCIP_PHP` | `scip-php` on the `PATH` | Command of the PHP SCIP indexer for the `scip` add-on. It runs in `repos/<project>` and writes `index.scip` there. |
| `KB_SCIP_TIMEOUT` | `600` | Seconds one SCIP indexer run may take. |

## The shared server (`kb-http`)

| Variable | Default | Description |
|---|---|---|
| `KB_SERVER` | `off` | Must be `on`, or the server does not start. |
| `KB_HTTP_HOST` | `127.0.0.1` | Address to listen on; put a TLS proxy in front. |
| `KB_HTTP_PORT` | `8790` | Port to listen on. |
| `KB_PERMISSION_URL` | — (required) | The permission webhook: token and project list in, allowed projects out. See [access protocol](access-protocol.md). |
| `KB_LOGIN_URL` | — | The login webhook for browser sign-in, the admin pages and `kb-login`. |
| `KB_HOOK_HEADERS` | — | Extra headers for both webhooks, a JSON object. |
| `KB_AUTH_CACHE_SECONDS` | `60` | How long a permission answer is kept, per token hash. |
| `KB_AUTH_TIMEOUT` | `10` | Timeout of a webhook call, seconds. |
| `KB_AUTH_HINT` | a sign-in hint | Text a caller without a valid token gets. |
| `KB_OAUTH` | `on` | `off` turns browser sign-in off: ready tokens only. |
| `KB_OAUTH_TOKEN_TTL` | `28800` | `expires_in` told to the client; the login webhook decides the real lifetime. |
| `KB_LOGIN_ENV` | `<data>/kb-login.env`, then `<data>/deploy/kb-login.env` | A KEY=VALUE file with `KB_LOGIN_URL` and `KB_HOOK_HEADERS`. |
| `KB_USAGE_FILE` | `<data>/logs/usage.json` | Base name of the usage database (`usage.sqlite` next to it). |
| `KB_SOURCE_URL` | the public repository | Where users find the source of what they use (AGPL); set it for a changed copy. |

## Clients

| Variable | Default | Description |
|---|---|---|
| `KB_URL` | — | The shared server's `/mcp` address for `kb-login`. |
| `KB_API_TOKEN` | — | Token for the local HTTP API (`kb-api --token`). |
