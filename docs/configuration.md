# Configuration reference

Every setting in one place. Knowledge Index reads two kinds of settings:

- **the project file** `kb.projects.toml`: which projects to index and how they connect;
- **environment variables** `KB_*`: where files live, how the servers behave, what is switched on.
- **`<config>/settings.json`**: the few settings an admin changes on the running server, on the Settings
  page of `/admin` (see below). The env variable of such a setting wins over the file.

`kb --version` prints where the project file and the data folder are. A setting that is not set takes the
default shown here.

## The project file: `kb.projects.toml`

One `[[project]]` block per project. Only `name` is required; a project with `docs` and no `code` is a
docs-only project. The file is TOML (read with Python's `tomllib`): lists can span lines, strings can
use single quotes, and `#` inside a quoted path is part of the path. A path must not contain `|` or
`,`. A file that is not valid TOML stops the build with the line of the error.

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
| `name` | string | The project's name in answers and in access rules. Letters, digits, `-` and `_`, starting with a letter or a digit (it is a folder name under `repos/` and `.docs/`). Required, unique, and not `db` or `domain` (the build uses those for its own nodes). When the KB has its own `docs/` folder, not the name of that folder either (`KB_SELF_DOCS_NAME`, default `kb`). A key the build does not know (for example `serve`) gives a warning. |
| `code` | list of paths | Folders whose code goes into the graph. `vendor`, `node_modules`, `storage`, `public`, `tests`, build output, images, fonts, source maps, minified files, `*.json` and `*.lock` are left out. Secrets are never copied: `.env` and `.env.*` (except `.env.example`) and `bootstrap/cache/` (it holds the DB password and `APP_KEY` after `config:cache`). This copy is the only filter, so the graph is the same in a checkout, a pip install and Docker. Each folder is copied to `repos/<name>/<folder name>/`, so two folders of one project must have different folder names. A link that points out of its folder, device files and pipes are not copied. A missing or empty folder, or a copy that fails (an unreadable file), is an error: the project keeps its previous copy and graph, and the build exits non-zero. |
| `docs` | path | The docs folder: Markdown is searched; PDF, DOCX, ODT, RTF, EPUB and scans are extracted to text first (`docs/guide.pdf` is searched as `docs/guide.pdf.md`). A symlink that points out of the folder is not indexed. |
| `routes` | path | A Laravel app folder: `php artisan route:list --json` there gives the route table, and its routes become graph edges. Its `database/migrations` give the foreign-key edges between tables. Eloquent relations and `$table` are resolved inside each project; two projects that declare the same table name share one `db::<table>` node. A path that ends in `.json` is a ready route table instead ([Docker](docker.md#route-tables-from-the-app-s-container)). An output that is not a JSON list keeps the previous table. |
| `calls` | list of paths | Folders with frontend or backend code whose HTTP calls are linked to the routes of `serves`. |
| `serves` | string or list | The project(s) whose routes these calls go to, e.g. `"api"` or `["api", "billing-api"]`. Each one must be a project of this file with `routes`. |

## Where files live

| Variable | Default | Description |
|---|---|---|
| `KB_HOME` | — | One folder for data and config together, e.g. `/srv/kb`. Overrides the two below. |
| `KB_DATA` | `~/.local/share/knowledge-index` (a checkout: the checkout) | Built graphs, staged code and docs, logs, counters, add-on state. |
| `KB_CONFIG` | `~/.config/knowledge-index` (a checkout: the checkout) | `kb.projects.toml`, `kb.hubs.txt`, `admins.txt`, `settings.json`. |
| `KB_COLLECTION` | `kb` | Prefix of the docs search collections (`kb-<project>`). |
| `KB_SELF_DOCS_NAME` | `kb` | Project name for a checkout's own `docs/` folder. |
| `KB_ALIASES` | the shipped table | Alias table for search (`term = synonym, synonym`). |

## Building the index

| Variable | Default | Description |
|---|---|---|
| `KB_FORCE` | `0` | `1` rebuilds every project and every step, as `kb build` does. `kb update` otherwise skips what did not change. |
| `KB_EXTRACT` | `1` | `0` skips text extraction from PDF, DOCX and scans, and removes the extracted files. With `1`, a file whose size and mtime did not change is not extracted again. A PDF without a text layer goes on to OCR when `tesseract` is installed. A failed file is tried again when it changes or a new parser for it is installed; a timeout is tried again on the next build. |
| `KB_EXTRACT_MAX_MB` | `200` | A document larger than this is not extracted. |
| `KB_EXTRACT_MAX_OUTPUT_MB` | `20` | The text that one run of an extraction tool (`pandoc`, `pdftotext`, `tesseract`) may write, and the size of every file it writes. Longer output is cut, and the extracted file says so. |
| `KB_EXTRACT_MAX_MEM_MB` | `2048` | The memory (data segment) of one run of an extraction tool. A document that needs more (a zip bomb in a `.docx`, a PNG of huge size in pixels) fails and is reported, and the build goes on. |
| `KB_OCR_LANGS` | `eng` | Tesseract languages for scans, e.g. `eng+lav+rus`. |
| `KB_OCR_MAX_PAGES` | `200` | OCR reads at most this many pages of a scanned PDF, one page at a time, and says when it stopped early. |
| `KB_QMD_BACKEND` | asked once, then remembered | Embeddings: `llama` (on device), `none` (keywords only) or `other` (your own command). |
| `KB_QMD_EMBED_CMD` | — | With `other`: the command that computes embeddings. |
| `KB_REFRESH_WAIT_LOCK` | — | `kb-refresh` waits while your deploy holds this lock file. `kb-refresh` builds when a git HEAD, the config, `kb.hubs.txt`, the KB `VERSION` or a route table (`KB_ROUTES_DIR/*.json`, or a `routes` that names a `.json` file) changed, or when it never built; `kb-refresh --force` runs `KB_FORCE=1 kb update`. While a build runs (or the wait lock is held), `kb-refresh --force` starts nothing: it exits with code 75, writes `busy: ...` on stderr and one line in `logs/refresh.log`. A route table outside `KB_ROUTES_DIR` counts by its content hash (over 64 MB: size, inode and the mtime in nanoseconds). |
| `KB_REFRESH_ACK` | — | Set by the admin "Rebuild" button, not by you: the file into which `kb-refresh` writes its pid once it holds the build lock and starts the build. The button says "started" only after that line. |
| — | `<data>/logs/update.lock` | Not a setting: the build lock. `kb build`, `kb update`, `kb graph`, `kb label`, `kb kb` and `kb-refresh` hold a `flock` on this file while they build, so only one build runs at a time. A second build stops at once with exit code 75 and `busy: ...` on stderr. The file stays after the build; test the lock (`flock -n <file> true` fails while a build runs), not the file. |
| `KB_ROUTES_DIR` | — | A folder with ready route tables, `<project>.json`. A project with `routes` reads its table there instead of running `php artisan route:list`. |

## Answers

| Variable | Default | Description |
|---|---|---|
| `KB_DOCS_MODE` | `light` | Docs search: `light` (no reranker, ~650 MB RAM), `full` (LLM reranker, slow on a CPU), `bm25` (keywords, ~70 MB). |
| `KB_PAGE_CHARS` | `6000` | Characters per page of a long answer; a call can ask for 1,000-50,000 with `page_length`. |
| `KB_CURSOR_TTL` | `600` | Seconds a long answer stays behind its cursor. |
| `KB_CURSOR_MAX` | `500` | Answers kept behind cursors at once. |
| `KB_CONTRACT_EDGES` | `1` | `0` hides other projects' ends of HTTP edges from a caller limited to some projects. Under `contracts: own` they are hidden anyway. |
| `KB_CONTRACTS` | `all` (or `settings.json`) | Who sees the contracts between services: `all` = every signed-in user, `own` = only the contracts that touch an allowed project ([the shared server](server.md#who-sees-the-contracts)). Set, it overrides `settings.json` and the Settings page shows it locked. A value that is neither reads as `own`. |
| `KB_SESSION_DIR` | the current folder | The folder that decides a session's default projects. |

## Speed

| Variable | Default | Description |
|---|---|---|
| `KB_WARM` | `1` | `0` runs graphify and qmd as one process per call instead of keeping them loaded. |
| `KB_WARM_KEEPALIVE` | `240` | Seconds between the queries that keep the model of the qmd worker loaded in `kb-http` and `kb-api`: qmd unloads an idle model after 5 minutes, and the next search then waits for the load (13.5 s against 1.2 s on a 4-core server). `0` switches it off and frees about 650 MB while nobody searches. |
| `KB_GRAPH_CACHE_FILES` | `2` | Graph files the graph worker keeps parsed. |
| `KB_GRAPH_VIEWS` | `8` | Caller views (one per project set) kept cut in memory; the recent ones are re-cut after each build. |
| `KB_FAST_ROUTES` | set by the `fast-routes` add-on | `1` computes `graph_routes` on rustworkx. |
| `KB_MAX_GRAPH_JOBS` | `3` | Graph calls at once on the shared server. |
| `KB_MAX_DOCS_JOBS` | `1` | Docs searches at once on the shared server. |

## Add-ons

| Variable | Default | Description |
|---|---|---|
| `KB_ADDONS` | `<data>/addons.json` | Comma-separated add-ons that are on, overriding the file; `none` switches all off. Names: `code-mode`, `tool-multicall`, `fast-routes`, `metrics`, `audit`, `questions`, `scip`, `datadict`, and installed plugins. The admin pages are core, not an add-on; an old `admin` in the file or the variable is ignored. Set, the switches of the Settings page are read-only. |
| `KB_BATCH_MAX` | `10` | Calls in one `batch`. |
| `KB_RUN_SECONDS` | `20` | Seconds a `run_code` program may run. The time of its `kb()` calls counts; a call that is already running finishes first. |
| `KB_RUN_CALLS` | `30` | Tool calls a `run_code` program may make. |
| `KB_SANDBOX_SOCKET` | — | A Unix socket of the sandbox service (`sandbox/socket-server.mjs`). Set: `run_code` runs there, e.g. in a container without network ([Docker](docker.md#the-sandbox-container-code-mode)). |
| `KB_SANDBOX_MAX` | `4` | Sandbox service only: programs that run at once. |
| `KB_METRICS` | `off` | `on` serves `/metrics` without the add-on. |
| `KB_METRICS_TOKEN` | — | Bearer token or basic-auth password for `/metrics` from outside loopback. |
| `KB_DATADICT_DIR` | `<config>/datadict` | Folder of the data dictionary files, one `<project>.toml` each ([data dictionary](data-dictionary.md)). |
| `KB_ADMINS` | `<data>/admins.txt` | Comma-separated user names allowed into `/admin` on the shared server, matched against the `uname`, `username`, `id` or `uid` of the permission webhook (never the display `name`). |
| `KB_SCIP_PHP` | `scip-php` on the `PATH` | Command of the PHP SCIP indexer for the `scip` add-on. It runs in `repos/<project>` and writes `index.scip` there. |
| `KB_SCIP_TIMEOUT` | `600` | Seconds one SCIP indexer run may take. |

## Settings an admin changes at run time: `settings.json`

The Settings page of `/admin` (and `python3 bin/settings.py set <key> <value>`) writes
`<config>/settings.json`, a JSON object such as `{"contracts": "own"}`. The server reads the file again
when it changes: no restart. A key that an env variable sets is locked on the page. `bin/settings.py` is
the registry: each setting with its choices, default, description and env variable.

| Key | Env variable | Default | Choices |
|---|---|---|---|
| `contracts` | `KB_CONTRACTS` | `all` | `all`, `own` ([who sees the contracts](server.md#who-sees-the-contracts)) |

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
| `KB_AUTH_MAX_CHECKS` / `KB_AUTH_CHECK_WAIT` | `16` / `2` | Permission webhook calls at once; one more waits this many seconds for a slot, then gets `503`. |
| `KB_AUTH_HINT` | a sign-in hint | Text a caller without a valid token gets. |
| `KB_RESET_PER_DAY` | `3` | Users plugin: password mails ("Forgot the password?", "Reset password") one user gets in 24 hours, on top of one per 5 minutes. Over it: no mail, the same answer. |
| `KB_OAUTH` | `on` | `off` turns the browser sign-in of MCP clients off: ready tokens only. The admin pages stay. |
| `KB_OAUTH_TOKEN_TTL` | `28800` | Lifetime of an access token of the browser sign-in, seconds; `expires_in` says the same. A refresh gives a new one while the login webhook's token behind it lives. The tokens are kept in `<data>/logs/oauth-tokens.json`: a restart asks nobody to sign in again. |
| `KB_OAUTH_REFRESH_DAYS` | `0` | Days after which a refresh token of the browser sign-in ends; `0` = no end. |
| `KB_SIGNIN_IDLE_DAYS` | `0` | Users plugin: days without a request after which a sign-in token of an agent ends; `0` = no end. |
| `KB_ADMIN_SESSION_IDLE` | `900` | Seconds without a request after which a session of the admin pages ends (8 hours at most in any case). |
| `KB_LOGIN_ENV` | `<data>/kb-login.env`, then `<data>/deploy/kb-login.env` | A KEY=VALUE file with `KB_LOGIN_URL` and `KB_HOOK_HEADERS`. |
| `KB_USAGE_FILE` | `<data>/logs/usage.json` | Base name of the usage database (`usage.sqlite` next to it). |
| `KB_SOURCE_URL` | the public repository | Where users find the source of what they use (AGPL); set it for a changed copy. |

## Clients

| Variable | Default | Description |
|---|---|---|
| `KB_URL` | — | The shared server's `/mcp` address for `kb-login`. |
| `KB_API_TOKEN` | — | Token for the local HTTP API (`kb-api --token`). With a token, every request needs it, the dashboard files too, so the browser dashboard does not open: use it without a token on loopback, or through an SSH tunnel. |
