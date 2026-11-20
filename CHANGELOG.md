# Changelog

All notable changes to **GGRAG** (graph-grounded RAG for code; called knowledge-index before 0.15) are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/) with `0.x` semantics: **breaking changes bump the minor**
until the MCP tool contract stabilises at `1.0.0`.

The single source of truth for the current version is the `VERSION` file. It is reported by
`bin/kb --version` and by the MCP server in `initialize` → `serverInfo.version`.

> **Not the same axis:** a consumer may carry its own component-level compatibility field (e.g. a
> plugin/cartridge manifest version). Those numbers and this repo's version are independent and will
> diverge — don't sync them.

## [Unreleased]

## [0.15.0] — 2026-11-20

- **Renamed: Knowledge Index is now GGRAG** (graph-grounded RAG for code). The package name, the Python
  module, the data folders and the `kb` commands stay as they are, so nothing breaks for an installed copy.

- **Added: the `audit` add-on** (`kb addon install audit`, free core; off by default): the Audit tab of the
  admin pages (`/admin/audit`) with sign-ins and admin actions (browser
  sign-in, refused sign-in with the reason, admin sign-in and sign-out, rebuild, revoked client) and every
  tool call with its user, projects, argument, time and result, filtered by user, tool or errors. Kept in
  the usage database for 90 days.

- **Added: external add-ons (plugins)** — a folder with a `plugin.py` (NAME, DESCRIPTION, TOOLS, call, and
  optionally build) installed with `kb addon install <folder>` and switched on like any add-on. Its tools
  join the list and go through the core's validation, paging, cursors, usage counting and per-project
  access; its build hook runs at the end of `kb update`. A broken plugin is reported and skipped. A plugin
  keeps its own licence, so paid add-ons can live in a private repository (`bin/plugins.py`).

## [0.14.1] — 2026-11-20

- **Fixed: a frontend call is linked from its enclosing function**, not from the whole file. With the file
  as the source, every function of an API module shared all its routes: `graph_routes` from one function
  could leave through another function's route, and `review_context` listed every importer of the module
  instead of the screens that call the changed route.
- **Fixed: `review_context` counts a controller whose class depends on the change** (constructor
  injection, a property) for all its routes, not only the actions that call it directly.
- **Fixed: frontend calls whose URL is on the next line** (`apiFetch<T>(` then the URL, as Prettier writes
  a long call) were not seen. On the author's Next.js app: 69 -> 111 linked calls.
- **Added: PHP calls through typed variables and properties** (`bin/link_php_calls.py`): `Rental $rental`
  then `$rental->total()`, or a promoted `private Payouts $payouts` then `$this->payouts->book()`, become
  INFERRED `calls` edges. 179 new edges on the author's Laravel app.
- **`graph_explain` / `graph_affected` on a name defined several times** answer for each match (up to
  3) instead of asking the agent to pick one.
- Benchmark questions can be chains (one grep per step). After these fixes, on the author's 10 questions:
  KB 100% of the expected files, grep 95%, with ~33x less text from the KB. The questions were written
  by the same person who fixed the misses they showed: a fresh set is needed before trusting 100%.

## [0.14.0] — 2026-11-20

- **Changed: README intro** — what the KB is in one paragraph: one search over related projects and their docs.

- **Added: `bench/graph_vs_grep.py`** — retrieval benchmark of KB answers against plain grep (expected
  files found, text read, extra searches); the questions live outside git. First run and its three kinds
  of misses: `docs/design.md`, "Graph vs grep, measured".

- **Added: `repo_map`** — a first look at a project within a token budget (default 1500): its domains,
  then the classes and frontend modules most of the code depends on, with their busiest methods and the
  routes they serve (`bin/repomap.py`, idea from Aider's repo map). The budget is split between the
  projects asked for. With `unused: true` it lists dead-code candidates instead: nothing in the graph
  depends on them and their names appear in no other indexed file; code that Laravel or a frontend
  framework wires by convention (jobs, listeners, policies, providers, file-system routes …) is never listed.
- **Added: `code_grep`** — exact text search with a regular expression over the indexed code (config
  keys, SQL, error messages, strings), only in the caller's projects; ripgrep when installed, else grep.

- **Faster first call after a rebuild.** The graph worker remembers the last 8 caller views and cuts
  them again in the background as soon as a build has finished (graph.json stopped changing and the
  build's end mark is newer). Before, the first call of each project set after every `kb update` paid
  2-9 s; on a server that rebuilds after each commit that was most of the slow calls. Measured on the
  author's machine: 5 s → 0.03 s for the first `graph_routes` after a rebuild.

- **Changed: usage counters live in SQLite, one row per call** (`usage-local.sqlite`, `usage.sqlite`;
  stdlib `sqlite3`). kb-api and every session's kb-mcp write the same counters; with the JSON file each
  saved its own copy over it and the last writer won. Rows give real p95 per tool, a "slowest calls"
  panel on the admin usage page and p95 in the `usage` tool. An old JSON file is imported once as totals
  and renamed `*.json.imported`. A diff passed to `review_context` is not stored as an argument.
- **Added: `review_context`.** The tool takes a git diff and answers what it touches:
  changed symbols (grouped by file), what depends on them in every project, the HTTP routes through
  them with their frontend callers, the docs that name the changed code and the NOTE/WHY comments next
  to it (`bin/review.py`, run in the graph worker on the caller's view).
- **Licence: AGPL-3.0-or-later** instead of GPL-3.0-or-later, from this version on. The same terms, plus
  one: whoever runs a changed KB as a network service must offer its users the source. The `version` tool
  and the admin pages show the licence and a source link (`KB_SOURCE_URL` for a changed copy).
  Releases up to 0.13.0 stay under GPL-3.0-or-later.

- **Faster `kb update`: only what changed is rebuilt.** A project whose staged files did not change
  (path, size, mtime, graphify version) keeps its graph; a changed one goes through graphify's
  incremental update, and a full `--force` pass runs only when graphify refuses a smaller graph
  (deleted code). Merging and post-processing are skipped when no input changed (project manifests,
  route tables, docs, config, the KB's own code). The qmd collection round runs only when the
  collection set changed. On the author's machine: 138 s → 10 s with nothing changed. `kb build` and
  `KB_FORCE=1` rebuild everything.

## [0.13.0] — 2026-11-20

- **Breaking: access protocol v1 is the only sign-in and access path** ([docs/access-protocol.md](docs/access-protocol.md)).
  Two webhooks: `KB_LOGIN_URL` (POST user name and password, then the code → `{token}` or
  `{two_factor: {challenge}}`) and `KB_PERMISSION_URL` (POST with the token and the KB's project names
  → `{user, projects: [...] | "*"}`), extra headers in `KB_HOOK_HEADERS`. Every request carries
  `"protocol": 1`. Removed: `KB_AUTH_URL` (GET identity endpoint), `KB_LOGIN_API`, `KB_LOGIN_PATH`,
  `KB_LOGIN_2FA_PATH`, `KB_LOGIN_2FA_SETUP_PATH`, `KB_LOGIN_HEADERS`, the `{data: …}` answer wrapper,
  the separate recovery-code field and the authenticator set-up step in `kb-login` (set it up in your
  own app). `kb-login --api` is now `--login-url`. **Upgrade:** add the two webhooks to your API and
  set `KB_PERMISSION_URL` and `KB_LOGIN_URL`; `kb-http` refuses to start without `KB_PERMISSION_URL`.

- **Changed: the README is a short start page** (94 lines instead of 646): what it is, the demo, five
  install steps, what the agent gets, the optional parts. The details moved to `docs/install.md`,
  `docs/server.md` and `docs/design.md`.
- **Added: add-ons, named by what they do** (`bin/addons.py`, `kb addon list|install|remove <name>`):
  `tool-multicall` (batch), `code-mode` (run_code and its sandbox, installed into the data folder),
  `fast-routes` (rustworkx), `metrics`, `admin`. Off by default; an add-on that is off publishes no tool,
  page or route, and only `kb addon install` fetches its pieces. State in `<data>/addons.json` or
  `KB_ADDONS`. **Upgrade note:** `batch` and `run_code` need `kb addon install tool-multicall` /
  `code-mode` now.
- **Added: the `metrics` add-on serves `GET /metrics`** (`KB_METRICS=on` works too) in the Prometheus text format (graph, docs, storage,
  builds, tool calls, errors, time, p95, memory) for Prometheus or PMM; loopback without a token,
  otherwise `KB_METRICS_TOKEN` as bearer or basic-auth password. No dependency, off by default.
- **Changed: the admin pages, redesigned** (Codex mockups, PMM style): tiles with sparklines from the KB's
  own history, build time and calls per day, graph, docs, storage and answer-time panels; the usage page
  is a Query-Analytics-style table per tool (calls sparkline, rate, mean, errors, p95) with filters by
  project, tool and user. Light and dark.
- **Added: usage is counted by every server** (`bin/usage.py`): kb-mcp and kb-api too (as user `local`),
  with hourly history, per-project counts and recent durations for p95.
- **Added: every build logs its time** (`logs/builds.log`, written by `bin/kb` whatever started it).
- **Fixed: build output was staged as code.** `.next` of a Next.js app (2.1 GB) and other build or cache
  folders (`.nuxt`, `.output`, `coverage`, `.turbo`, `.cache`) are no longer copied.

- **Added: the `doc-organization` skill** (`skills/doc-organization/SKILL.md`): the guide below as a
  checklist agents follow when they write, move or tidy docs.
- **Docs: `docs/documentation-guide.md`** — how to write and keep project docs so the graph links them to
  the code (domains, heads, plans apart, contract docs, links and names in backticks, checks with the KB).

- **Added: local MCP over HTTP and local admin pages in `kb-api`** (`make serve`): `POST /mcp` speaks
  MCP JSON-RPC, `/admin` shows the status, index charts and the rebuild button without sign-in, only
  without a token on loopback; forms carry a per-process token and a cross-site `Origin` is refused.

## [0.12.0] — 2026-11-19

- **Added: `batch`** — up to 10 tool calls in one request, each with the caller's own rights; one failing
  call is reported in its place.
- **Added: `run_code`** — an agent's JavaScript program that calls the tools through `kb(tool, args)` and
  returns only what it needs. Two walls: QuickJS compiled to WebAssembly (no `require`, `process`,
  network, files or timers; 64 MB, a 20 s deadline, 30 calls), inside a Node process with the permission
  model on (read access to `sandbox/` only, no network, no child processes, no writes), plus CPU and
  open-file limits. `run_code` and `batch` cannot be called from inside. Needs `npm ci` in `sandbox/`
  (`quickjs-emscripten` 0.32.0, pinned by the lock file). The program may be TypeScript: Node strips the
  types before QuickJS runs it, and the tool description declares `kb()` and `print()` with types.
- **Added: admin pages at `/admin`** for the users in `KB_ADMINS` or `<data>/admins.txt`: status with a
  rebuild button, an index page with charts (graph make-up, storage, documents and chunks per project,
  build times, answer time per tool, calls per day), usage, and sign-in clients with revoke. API login
  with its second factor, an HttpOnly SameSite=Strict session cookie, CSRF tokens; no scripts.

## [0.11.0] — 2026-11-19

The first outside users on a shared server (a developer team): browser sign-in, speed, paging and
several-way path search. **Breaking:** tool contract 2 — `docs_search` and `graph_query` are gone, use
`search` with `domain: docs` or `domain: code`.

- **Fixed: code search walked the domain anchors.** 2-7 of ~50 shown nodes were empty domain nodes, and a
  search could start from one; `search` with `domain: code` now runs on the graph without them, like
  path search and impact.

- **Added: cursors for long answers.** An answer longer than one page ends with a `cursor`; the next
  pages come from that same answer (`cursor` + `page`, no other arguments) for 10 minutes
  (`KB_CURSOR_TTL`, at most `KB_CURSOR_MAX` = 500 kept). Pages stay consistent even if the index is
  rebuilt meanwhile, and the query does not run again. A cursor works only for the tool, the caller and
  the project set that made it. `search` keeps a widened first answer (4x the hits and the code budget)
  behind its cursor; `search` with `page` and no cursor still pages by running again.

- **Added: `graph_routes` — several ways between two places, with the routes they cross.** Hops are
  priced by kind (calls and requests cheap, imports dearer, inheritance dearest), busy hubs and
  framework classes cost extra, a symbol merged across files costs a jump; Dijkstra finds the cheapest
  way, then again with the used edges made dearer for the next ways. Every HTTP hop prints its method,
  URI and controller. Pin a project in a name: `api:Customer`.
  With `rustworkx` in graphify's environment (`uv tool install graphifyy --with rustworkx`) the search
  runs on its Rust core, 2-3x faster with the same paths; networkx is the fallback.
- **Added: backend-to-backend HTTP calls.** A PHP backend listed with `calls`/`serves` (an API client of
  another service: `Http::get('v2/…')`, `->send($req, 'post', 'v1/…' . $id)`) is linked from its
  enclosing method to the controller of the other service. Test folders are skipped.
- **Added: paging on every tool but `version`: `page` and `page_length`** (characters, 1000-50000,
  default 6000). Lists are cut at blank lines or line ends and say how to get the next page; `search`
  pages docs by hits and code by graph nodes. The old cuts (8000 characters, 25 contracts, graphify's 20
  connections in `graph_explain`) are gone: `graph_explain` lists every connection.
- **Changed: optional arguments carry their `default` in the tool schemas.**

- **Faster: a caller's project set is cut from the graph in memory.** The graphify worker keeps the
  main graph parsed and makes each caller's view (`filter_graph`, and `strip_domains` for path and
  impact) from it, instead of a filtered file written by a subprocess and parsed again. The first call
  for a new project set on a 23k-node graph: 3-5 s -> 0.3-1.6 s, then milliseconds. The same access rules (the same
  function). Memory stays bounded: `KB_GRAPH_VIEWS` (8, two per project set) views and `KB_GRAPH_CACHE_FILES`
  (2) files; the worker measured 575 MB with four project sets. Each networkx graph gets its own
  `graph` attribute dict, so graphify's per-graph index never leaks from one view into another. Label suggestions follow the caller's projects.

- **Faster: long-lived graphify and qmd in the servers** (`bin/warm.py`, `bin/graph_worker.py`). Every
  call used to start graphify (parse a 34 MB graph, build networkx) or qmd (load the embedding model).
  On a 23k-node graph: graph_explain 4-7 s -> 0.01 s, code search 1-4 s -> 0.1 s, docs search 6-8 s ->
  0.2 s, same answers. A failing helper falls back to the old path; `KB_WARM=0` turns them off.
- **Changed: `KB_DOCS_MODE` defaults to `light`** (no LLM reranker). With the reranker a docs search
  took 1-2 minutes on a CPU; `full` is still there for a machine with a GPU.
- **Added: browser sign-in for MCP clients (OAuth 2.1, `bin/oauth.py`).** Protected-resource and
  authorization-server metadata, dynamic client registration, a login page that runs the API's own
  login with its second factor, PKCE S256, loopback redirects only; the access token is the API's dev
  token, so the access check is unchanged. On by default when the login settings are present
  (`KB_OAUTH=off` to disable).

- **Removed (breaking, tool contract 2): `docs_search` and `graph_query`.** They were aliases of
  `search` and confused new users and their skills. Use `search` with `domain: docs` or
  `domain: code`. `version` reports `api_contract: 2`.

- **Changed: `kb-login` keeps the token in a private file, not in a project's `.mcp.json`.** That
  file is often in git, and the server then worked in one project only. The token goes to
  `~/.claude/kb/token` (owner-only); `--add-to-claude` registers the server once in Claude Code's user
  scope with a `headersHelper` (`kb-login --headers`) that reads the file, so a new login changes no
  config. `--env-file` reads the settings without a shell (Windows). `--mcp-json` still exists and
  refuses a file that git tracks.
- **Fixed: `graph_affected` stopped at "ambiguous label" for a model named like its domain anchor**
  (`Order`). It now runs on the graph without anchors, like `graph_path`.
- **Fixed: a call site that lost its path prefix was linked to the wrong controller.**
  `${baseUrl.value}/send` matched the shortest route ending in `/send`. A suffix match now needs a
  base-only prefix (`api`, `v1`) or a single candidate.
- **Added: versioned API paths handed to list and export helpers count as calls**
  (`usePaginatedList(() => \`v2/…\`)`, `exportTable('v2/…')`). The `method:` of a call is no longer
  taken from the next call a few lines down.
- **Changed: `search` runs its docs and code halves in parallel, takes `budget` for the code half, and
  its truncation note names options that exist over MCP** (graphify's note pointed at CLI flags).
- **Added: server `instructions` say which tool answers which question and how far to trust an edge;
  a 401 answer carries a `hint` on how to get a token** (the server has no OAuth).

- **Fixed: a failed build left a half-built graph in place.** `graph.json` is rewritten step by step,
  so a build that died half-way (seen on a server: 104 failed builds in a row) served a graph with no
  `http_request` edges and no domains. The previous graph is now kept aside and put back when the build
  fails.
- **Fixed: one failing `route:list` broke the whole build.** The route table was deleted and the HTTP
  linker then crashed on the missing file. A failed run now keeps the previous table (or an empty one on
  the first build) and says so.

- **Fixed: `graph_path` reported symbols as connected when they only shared a domain.** Path search
  walked the `in_domain` edges, so any two members of a domain were two hops apart through the anchor.
  On the author's graph (5386 nodes) 21 of 105 found paths had no real route at all. Path search now
  runs on a copy of the caller's graph without the domain anchors (`access.path_graph`), cut after the
  access filter. `explain`, the code search and the dashboard still see the anchors.
- **Fixed: `graph_affected` stopped at the repo boundary.** graphify's default relation list does not
  include the relations this project adds, so a controller method showed no frontend callers and a
  table no models. The tool now also walks `http_request`, `eloquent`, `fk`, `sql`, `defines_table`
  and `documents`.
- **Fixed: `bin/install` took a graphify that does not start for an old one.** After a system Python
  upgrade the tool's environment can point at a missing interpreter; this is now reported as such.
- **Tests: `tests/test_graph_routes.py`** — access guards for the graph tools and a run against the
  real graphify (skipped when it is absent), which the stubbed tests could not catch.

- **Fixed: deleted code stayed in the graph.** With graphify's cache kept between builds, `graphify
  update` refused to overwrite a project graph that would shrink. The staged copy is the truth, so the
  build now passes `--force`.
- **Fixed: `KB_HOME` was half-honoured by the build.** Staged repos, route tables and `cluster-only`
  used the package directory instead of the data directory, so an installed or relocated KB clustered
  the wrong graph. `kb.hubs.txt` is read from the config directory when present.

- **Faster: linking docs into the graph took ~75% of a build.** Every doc was searched with one regex
  per symbol label (docs × symbols). Each doc is now split into its standalone tokens once and the
  labels are set lookups. Same edges, checked on a 20k-node graph with 370 docs: 141 s -> 1.1 s.

- **Added: one frontend may call several APIs.** `serves = ["api", "own-api"]` links each call site to
  whichever listed API has the route, and `problems` reports a call as unknown only when none has it.

- **Added: `project` on the graph tools** (`graph_path`, `graph_explain`, `graph_affected`, and the code
  half of `search`). It only narrows: a project outside the caller's `allowed` set is refused with the
  same message as an unknown one. Default = the session's project(s); an empty answer there widens to
  everything the caller may see. Every graph answer starts with `[projects: …]`.

- **Added: a shared MCP server over HTTP, off by default (`bin/kb-http`).** One index on a server for a
  team; each caller sees only their projects. Bearer tokens are checked against an identity endpoint
  you run (`KB_AUTH_URL`), cached per token hash. It refuses to start unless `KB_SERVER=on` and
  `KB_AUTH_URL` are set. Usage counters (`kb-http usage`, MCP tool `usage`), job limits per tool kind,
  `KB_DOCS_MODE=light|bm25` for a small server. Around it: `bin/kb-refresh` (rebuild on a new HEAD,
  for cron), `bin/kb-login` (get a token from your API, write `.mcp.json`; no default addresses),
  templates in `examples/server/`.
- **Added: per-caller project access in the core.** `kb_core.call_tool(name, args, allowed)`: graph
  tools run on a filtered graph copy (`bin/access.py`), doc hits and `docs_get` are checked by their
  resolved path, contract docs stay open to all. `allowed=None` (local use) changes nothing.
- **Added: `contracts_search`, `connections`, `problems` (`bin/contracts.py`).** Routes joined with
  their frontend call sites and contract docs; the service map from `<docs>/contracts/*.md`; the gaps
  between docs and code. Built by `kb update` after the graph.
- **Changed: argument values that start with `-` are refused** for every tool whose argument reaches a
  subprocess, so a value cannot read as a CLI option. MCP answers carry `isError` for a refused call.
- **Changed: docs are staged with `rsync --delete` in place** — search keeps working during a rebuild —
  and the staged docs of a project removed from the config are dropped.
- **Fixed: `$api<Type>('...')` calls were missed** — a TypeScript type argument between the name and
  the call. `php artisan route:list` runs with `LOG_CHANNEL=null`, so the indexed app gets no log files
  from the build; `.kt` files are counted in the staged sources.

- **Changed: one qmd collection per project.** Docs of project `<name>` are indexed as `kb-<name>`
  (the single aggregated `kb` collection is removed on the next `kb kb`). `search` takes a new
  `project` argument: a name, several comma-separated, or `all`. Without it the search is scoped to
  the project(s) owning the session's directory (`KB_SESSION_DIR` or the MCP server's cwd); if that
  scope finds nothing, it widens to all projects by itself. Every docs answer starts with
  `[projects: …]` so the scope is visible. `scope` still takes a raw collection and wins.
- **Fixed: Cyrillic and every other non-ASCII text was cut out of the doc index.** The staged copy
  was forced to ASCII — a workaround for a qmd 0.3.2 chunker crash. qmd ≥ 2.5.3 is required now and
  has no such crash, so the workaround only destroyed text: Russian docs were unsearchable.
- **Fixed: `kb update` did not re-stage docs.** It re-indexed the old staged copy, so new or edited
  docs never reached search until a full `kb build`. `update` now runs the docs stage too.
- **Changed: graphify's cache survives a rebuild.** `repos/<name>/graphify-out/` (with its extraction
  cache) is no longer deleted before each build; only the staged sources are re-copied. Measured: it
  does not make `kb update` noticeably faster (~2.5 min) — the time goes to clustering and enrichment.

- **Added: ask the index from the dashboard.** Two inputs run `graphify query` and the qmd docs search
  through `/search`. They work when the page is opened from `make serve` (<http://127.0.0.1:8899/>),
  which now serves the dashboard on the same origin — no CORS, so no other website can read the index.
  Opened from `file://` the page works as before and says how to enable them.
- **Added: show or hide domains.** Each domain in the sidebar has a checkbox, plus Show all / Hide all.
  Hiding filters what is drawn; expanding and edge rerouting are unaffected.
- **Security: tokenless kb-api answers loopback names only.** A request whose `Host` is not
  127.0.0.1 / localhost / [::1] gets 403, which closes DNS rebinding. With `--token` the token decides.
  This also changes the framing of 2026-07-26: the web page is still usable with nothing running, but
  with the server up it becomes a client of `/search`.

- **Fixed: the dashboard was empty.** Since 0.5.0 `web/index.html` loaded the generated data before
  `app.js`, so `kbManifest(…)` threw a `ReferenceError` and the page showed "no generated data" even
  after a good build. `app.js` now loads first and starts on `load`; a test pins the order.
- **Fixed: builds with a Laravel project crashed.** Since 0.3.1 `link_http.py` read the
  `route:list` table through the graph loader, which rejects anything that is not a graph. It reads
  plain JSON again; a test covers it.
- **Added: a demo.** `examples/demo/` holds a fictional shop (Laravel-style API + Vue) and its built
  dashboard data, so the dashboard can be seen right after cloning: `make demo`. `make demo-build`
  rebuilds it in a throwaway copy of the checkout, because `bin/kb` writes `repos/` and clusters
  relative to the checkout root and would overwrite your own graph.

- **Tables** are a *retrieval* problem, not a format one, and are still open: markdown tables chunk
  badly (a row separated from its header is close to meaningless), which is a chunking fix — repeat the
  header per row-group, or project rows into records. Measured, unfixed. Not the parsers' job.

- **Three surfaces, split by consumer** (framing fixed 2026-07-26 — the reason each exists,
  which decides what each may require):

  | Surface | Consumer | Job | Requires |
  |---|---|---|---|
  | **MCP** (stdio) | agents | search + traverse inside a session | the core; no port, no auth |
  | **HTTP API** | scripts, CI, cron | the same search, callable from bash/python | the core + a port + a token |
  | **web** | a human | *look at status*: the graph, index health, counts | generated data only — **no running process** |

  The consequence that corrects an earlier plan: **the web page is not a third client of the search
  API.** Its job is status, so it must open from `file://` with nothing running. Making it depend on
  the API would break the one property that makes it useful — that it works when nothing works. If
  the API happens to be up, the page may light up extra panels (progressive enhancement), never as a
  requirement.

- ~~**remote canon**: the index lives on a server, local agents reach it over MCP/SSH.~~
  **Deferred, and removed from the roadmap as a feature** (2026-07-27). Since 0.4.0 and 0.7.0 there is
  nothing left to build: install the tool on the box (`uv tool install`), point `KB_HOME` at a data
  directory, and a client reaches it either as `ssh box /abs/path/to/kb-mcp` or through an SSH tunnel to
  a loopback-bound `kb-api`. What remains is a *recipe* plus one prerequisite that lives in the corpus
  repo, not here: the ingest pipeline still assumes a local `qmd update && qmd embed`, and that step
  would lie the moment the index moved. Findings worth keeping regardless: **SQLite over a network
  mount is unsafe** (unreliable locks, page-at-a-time latency) while **plain files over sshfs are
  fine** (the dashboard is just files); embedding is **incremental by content hash**, so the cost to
  fear is the one-off initial embed, not the reindex interval; and a non-interactive SSH shell does
  **not** have `~/.local/bin` on `PATH`, so an MCP command must be an absolute path.
- **1.0.0** — when the MCP tool contract stops changing *and* a second independent consumer exists.

## [0.10.0] — 2026-07-27

### Language plugins, finished (steps 2–3, gated on a measurement)

The migration that stopped at v0.9.0 because the three enrichers had **no tests**. Done in the order
that made it safe, one commit each:

1. **Tests first.** `tests/test_enrichers.py` pins `enrich.py`, `link_data.py` and `link_rationale.py`
   at two levels: the pure functions that were about to move, and the whole scripts run as
   subprocesses against a fixture repo tree (an Eloquent relation, a `$table` declaration, a foreign
   key in a migration, a query-builder reference, a rationale comment). Every assertion checks the
   edge's **confidence** too — the one field that must never drift, because a regex-derived edge
   posing as an AST fact is a lie the rest of the graph rests on.
2. **The move.** `bin/lang/php_laravel.py` (class/layer vocabulary, `$table`, Eloquent relations,
   schema + FK patterns, query-builder refs, migration-name parsing, model scan) and
   `bin/lang/js_ts.py` (API client shapes, template/script suffixes, build-output dirs). The
   enrichers now compile **no patterns at all**, and a test asserts `re.compile` cannot come back.
3. **The leftovers.** With the patterns centralised, three private suffix lists became visible, each
   missing a file type the project has: `dedupe.py` (`.tsx`/`.py` labels treated as class-like, so
   files were merged **by name** — two `page.tsx` in one Next.js repo would collapse into one node),
   `link_rationale.py` (`.tsx` never scanned for markers — 132 files), `link_data.py` (same omission
   in the label index).

**Proven neutral, not assumed.** Same input graph, same three enrichers, before and after: 4731 nodes,
18881 edges, `edge_hash 07f5d50aa1f29ea0`, identical counts by relation and by type. The step-3 fixes
are **latent on the current corpus** and the changelog says so rather than claiming a fix: there are
zero rationale markers in `.tsx` today, and the duplicate `.tsx`/`.py` basenames in the staged repos
do not collide inside a single repo yet. What changed is that the next React repo will not silently
corrupt the graph.

Two boundaries drawn against the tidier-looking option: **no `sql.py`** (nothing parses `.sql`;
`DB::table` and `Schema::create` match PHP source, so a SQL "language plugin" would be a fake
boundary) and **route matching stays in `link_http.py`** (a Laravel route table meeting a JS call site
is a join between two languages and belongs to neither plugin).

The registry now has two tiers: `lang.plugins()` for modules that enrich a graph themselves,
`lang.patterns()` for modules that only carry language knowledge.

### Removed

A sweep for what the refactor made dead: `import json` in 7 files (left over from before `graph.py`
owned all JSON I/O), `import re` in two, `import pathlib` in one, a duplicate `BARE_CLASS` in
`declutter.py`, an unapplied `MAX_LABEL` in `lang/markdown.py`, and `paths.example_file()` (no caller
— `bin/install` resolves the `.example` twins in bash). One replacement rather than a deletion:
`link_http.py` re-split a node id by hand to get the repo tag, which is what `graph.repo_of` exists
for — that inline split was the bypass `graph.py` was written to end.

Tests: **176 → 200**.

## [0.9.0] — 2026-07-27

**Language knowledge becomes plugins, and documents enter the graph.** The four enrichers each carried
**their own copy** of the file-type list — `(".js", ".ts", ".tsx", ".vue")` appeared verbatim in four
files — while framework knowledge (Eloquent, Next/Nuxt HTTP clients) sat in "concern" files rather than
language ones. Adding a language meant editing four places, and forgetting one produced a **silently
partial graph**: the worst failure mode here, because nothing looks broken and edges are simply absent.

### Added
- **`bin/lang/` — the language layer**, with one shared source of truth for file-type facts and a
  plugin registry. The sets are **named by role and not merged**: `FRONTEND` (files that call a
  backend) stays narrower than `ALL_CODE`, because `link_http` deliberately scans only the former and
  a merged list would quietly change what it looks at. Comment openers per language live here too, so
  a new language no longer requires touching rationale extraction.
- **`bin/lang/markdown.py` — the first plugin for a format that is not code**, and the proof the
  contract holds beyond PHP/JS. Documents used to exist only in the search index: findable, but the
  graph could not answer *which document describes this service*. Now:
  - a `doc` node per markdown file under a project's docs root (rendered as an ellipse, legend updated);
  - `links_to` **doc → doc** from `[[wikilink]]` and relative markdown links — `EXTRACTED`, the link is
    literally in the file;
  - `documents` **doc → symbol** from a bare mention — `INFERRED` with a score, never `EXTRACTED`.
  The mention rule is deliberately stingy, because this is where noise would come from: only
  distinctive labels (≥5 chars, CamelCase / `method()` / namespaced), whole-word case-sensitive,
  **ambiguous labels skipped** (same rule as `dedupe.py`: when in doubt, do not invent a fact), and
  capped at 12 per document — a doc mentioning forty symbols is an index, not a description.
- `bin/link_docs.py` as the pipeline step (a module inside a package cannot be executed directly, and
  every other step is a `bin/*.py` script, so the plugin stays importable and testable).
- New relations in the graph contract: `links_to`, `documents`.
- Tests 159 → **176**: the registry (incl. a test that the removed duplication cannot come back), the
  distinctiveness rules, ambiguity skipping, the per-doc cap, partial-word rejection, vendor
  exclusion, and **idempotence** — the pipeline reruns, so nodes must not double.

### Fixed
- Wikilink resolution was broken on the first write: the index was keyed by a composite
  `"repo::path.md"` string and `Path("api::b.md").stem` is `api::b`, so `[[b]]` matched nothing. Two
  separate indexes now — by path for markdown links, by stem for wikilinks. Caught by a test that
  expected two edges and got one.

### Verified on the real graph
`+8 doc nodes, +24 edges` across three doc roots, with `links_to` marked `EXTRACTED` and `documents`
`INFERRED`.

## [0.8.0] — 2026-07-27

**Non-markdown documents can be indexed, one plugin per format.** Reversing the non-goal written a few
hours earlier the same day: tables, scans and PDFs, one file or plugin per parser so they do not mix. The
scope is deliberately narrower than "multimodality": **text extraction** through external CLIs, not layout reconstruction.

The trigger was concrete: two ConnectPay PDFs sit in a corpus and were **invisible to search**, because
the doc index only matches `**/*.md`. `pdftotext -layout` pulls 17k and 31k characters out of them.

### Added
- **`bin/parsers/` — one plugin per format**, each declaring exactly four things (`EXTENSIONS`,
  `REQUIRES`, `CONFIDENCE`, `extract`) so parsers cannot grow into one pile:
  - `pdf_text` — PDFs with a text layer (`pdftotext -layout`; `-layout` matters, pricing decks put
    figures in columns and without it a number loses its row);
  - `pdf_ocr` — scans and images (`pdftoppm` + `tesseract`, languages from `KB_OCR_LANGS`);
  - `office` — `.docx .odt .rtf .epub` → markdown **including tables**, via pandoc.
- **`kb-extract`** (`bin/extract`): `--list` shows what this machine can extract *and what to install
  for the rest*, `--into <dir>` writes `<name>.md`, otherwise stdout.
- **Extraction wired into the doc staging** of `kb build`: non-markdown files under a project's `docs`
  root are extracted into the same staged tree, so the index sees them. `KB_EXTRACT=0` opts out.
- `kb-install` now lists the formats and the missing tools for the rest.
- Tests 143 → **159**, including an end-to-end extraction of a hand-written PDF (the figure that
  matters must survive), the priority order, and the reported-not-crashed rule.

### Rules the registry enforces
- **A missing tool means the format is unsupported, not a crash** — reported once, build continues.
  A silent hole in the index is worse than a loud gap.
- **Text layer before OCR, always.** A PDF that yields almost nothing is *refused with an explanation*
  rather than indexed as an empty document.
- **Every extracted file carries provenance**: source, tool **and its version**, timestamp, and
  `confidence: EXTRACTED | OCR`. OCR output is noisy by nature and must never pass for authored text —
  a hit from an `OCR` document is a lead, not a quote.
- **The corpus is never modified**: extraction writes into the staged copy only.
- **Zero Python dependencies survive** — every parser is a wrapper over an external CLI, like
  `graphify` and `qmd`.

### Fixed
- Tool versions in the provenance header are **validated**. Poppler's `pdftotext` treats `--version` as
  a filename and prints an I/O error, which duly ended up inside a header on the first run; a candidate
  now has to look like a version and not like a complaint.

## [0.7.1] — 2026-07-27

### Changed
- **Scope boundaries are now explicit** (README §Non-goals): multimodality (PDF/scan/image/audio) is
  out, SaaS connectors and permission mirroring are out, a server-side canonical index is a deployment
  recipe rather than a feature, and there is no write path. Nothing was removed from the code — this
  states decisions that were already implicit, so that the comparison table reads as a boundary and not
  as a to-do list.
- The remote-canon roadmap item is struck through with the reasoning and with the findings that stay
  valid either way (SQLite over network mounts, incremental embedding, the SSH `PATH` trap).

## [0.7.0] — 2026-07-26

**Installable as a tool.** `uv tool install knowledge-index` / `pipx install knowledge-index` puts
`kb`, `kb-mcp`, `kb-api` and `kb-install` on `PATH`. Answering the question that prompted this
(«не лучше использовать composer?»): Composer is PHP, and this is Python + bash whose two real
dependencies come from *different* ecosystems — `graphify` via uv/pip, `qmd` via npm. No single
package manager can deliver that stack, which is why `kb-install` (a preflight, not a resolver)
stays.

### Added
- **`pyproject.toml`** (hatchling), version read from the `VERSION` file so there is still one source
  of truth. **No runtime Python dependencies** — standard library only, on purpose.
- **`knowledge_index/` — a thin launcher**, not a rewrite: each entry point `os.execv`s the shipped
  script, so signals, exit codes and the stdio pipe stay exactly as they are (`kb-mcp`'s whole
  protocol is that pipe). `bin/` remains the implementation and a plain checkout keeps working.
- **`bin/paths.py` — the split between code and data**, which packaging forces: `PACKAGE_ROOT`
  (read-only: `bin/`, `web/`, `VERSION`, examples), `DATA_HOME` (`graphify-out/`, `repos/`, `.docs/`),
  `CONFIG_HOME` (`kb.projects.toml`, `aliases.toml`). Installed it follows XDG; `KB_HOME` overrides
  both; **a checkout keeps everything in place, so an existing clone needs no migration**. `bash` and
  Python read the same resolution (`paths.py --sh`), so they cannot disagree.
- **`kb vendor` and `kb open` as subcommands** — they work in both modes, unlike the previous
  Makefile-only versions. The shell is **staged next to the data** when installed, because the page
  loads its data through relative `<script src>` and a `file://` page cannot fetch a sibling.
  A vendored renderer survives an upgrade: staging never touches `vendor/`.
- Tests 132 → **143**: checkout-vs-wheel detection, XDG and `KB_HOME` resolution, examples always
  read from the shipped copy, staging behaviour, and that the shell exports bash evals are complete
  and quoted (an empty one would make a build write to `/`).

### Fixed
- **An installed copy identified itself as a checkout and pointed its data directory at
  site-packages.** The first marker was `VERSION` + `bin/kb` — both of which the wheel ships. The
  marker is now `pyproject.toml`, which a wheel never installs. Caught by actually installing the
  tool and reading `kb --version`, not by reasoning about it.
- `kb-install` now prints the **console-script** path for `.mcp.json` when installed
  (`~/.local/bin/kb-mcp`) instead of a site-packages path that the next reinstall would break.

## [0.6.1] — 2026-07-26

### Added
- **README §"Where this sits (and where it does not)"** — an honest comparison against the code-side
  tools (Sourcegraph, Zoekt/OpenGrok, Meta's Glean, CodeQL) and the retrieval-side ones (Onyx, R2R,
  RAGFlow, txtai, the managed cloud primitives, Glean the company), with a **"what the others do
  better"** section: compiler-grade code semantics, connectors and permission mirroring, document
  understanding, scale and support. Facts stamped July 2026, because licences and pricing move.

### Fixed
- Requirements said python `>= 3.10`; the floor is **3.11** — `tomllib` reads the config and the alias
  table. `bin/install` already checked 3.11, so the README was the wrong half.

## [0.6.0] — 2026-07-26

**The lexical layer stops excluding the answer — without patching qmd.** qmd joins a phrase's terms
with `AND`, so a natural-language question turns every word into a hard filter: the page that holds
the answer is dropped when it lacks one of them, usually a function word. Measured on the 27-question
golden set, asking as a phrase gave **recall 0.14**.

The fix uses qmd's own documented multi-line form instead of forking or patching it: hand it a
**query document** — `intent:` plus one `lex:` line per content word plus a `vec:` line — which qmd
**fuses (RRF)** rather than intersects.

| Same corpus, same unpatched qmd | recall | MRR |
|---|---|---|
| one phrase (`AND` semantics) | 0.142 | 0.167 |
| **query document, one `lex:` per term** | **0.670** | **0.586** |

For comparison, patching qmd's `parseLexQuery` to `OR` reached recall 0.735 / MRR 0.526 — so this
keeps ~90% of the recall gain with *better* ranking and **nothing forked**: no maintenance of someone
else's fast-moving tool, and nothing that a `qmd` upgrade silently reverts.

### Added
- **`bin/query_build.py`** — the query document builder: content-word extraction, stop words for
  Russian and English, compound tokens kept whole (`doc-search`, `shop-api`, `graph.json`,
  `/srv/oldname` are the corpus's most discriminative words and splitting them destroys them), a cap
  of 12 terms, and a hand-written query document passed through untouched.
- **`bin/install` + `make install` / `make check`** — first-run setup: verifies python ≥ 3.11
  (`tomllib`), `graphify ≥ 0.9.25`, `qmd`, `rsync`; creates `kb.projects.toml` and `aliases.toml` from
  their examples; optionally vendors the renderer; prints the ready `.mcp.json` snippet with absolute
  paths and the three surface entry points. **Never overwrites an existing file**, never installs
  another project's tool behind your back (it prints the command), idempotent, and `--check` writes
  nothing at all.
- Tests 119 → **132**: stop words in both languages, compound tokens, ordering and deduplication, the
  term cap, the stop-words-only fallback, and that explicit `mode=lex|vec` is left alone.

### Changed
- `search` with `mode` `auto`/`hybrid` now sends a query document; `mode=lex` and `mode=vec` remain
  verbatim passthroughs, because an explicit mode is the caller's decision.
- **Side effect, deliberate: qmd's own LLM query expansion no longer runs for these searches.** It was
  actively harmful on unknown proper nouns — for one name it invented a domain ("… in philosophy") and
  dragged the vector search off the corpus, giving that question a recall of exactly zero.

### Superseded
- The planned "routing between the lexical and vector legs by question shape" is dropped: the document
  carries both legs and lets RRF weigh them, which needs no classifier and no heuristics about what a
  question "looks like".

## [0.5.0] — 2026-07-26

**The web surface is a source file now.** The whole interface used to be a `TEMPLATE` string inside
`bin/render_viz.py`, rendered into git-ignored `graphify-out/kb-graph.html` — an interface that could
not be diffed, linted or tested, and that nobody could fix without running the pipeline.

### Added
- **`web/` — the versioned shell**: `index.html`, `app.js`, `style.css`. No build step, no framework,
  no placeholders. Opens from `file://`; entry point is `make open`.
- **A Status panel** next to the graph, fed by build-time snapshots: index health (nodes, edges,
  domains, projects, when the graph was built), the docs collection (files, freshness), **the alias
  table** (renames are knowledge, not just configuration), search-quality numbers if a benchmark
  result is present, and the versions of the repo plus both CLIs. Every block is **stamped** — a stale
  number that admits its age is useful, one that pretends to be live is not.
- **`make vendor`** downloads the renderer into `web/vendor/` (git-ignored) so the page works
  offline. The page prefers the local copy and falls back to a CDN **with a visible banner**: a tool
  whose promise is "the corpus never leaves the machine" must not quietly fetch code from a third
  party. This was an inconsistency in the previous shell, which loaded vis-network from unpkg
  unconditionally.
- Tests 104 → **119**: the data contract the shell depends on (manifest keys, super-node `domkey`,
  per-domain member/intra split, cross-domain rerouting, search index shape, stamped status) and the
  shell's own rules — **no `.innerHTML`**, the local renderer first, no leftover placeholders, and the
  UI strings staying English.

### Changed
- **`bin/render_viz.py` emits data only** and takes an output *directory*: `kb-manifest.js`
  (`kbManifest({domains, superNodes, fileMap, dataPath, nodeCount, generatedAt})`), `kb-status.js`,
  and the per-domain `kb-graph-data/*.js` that already existed. The four `__PLACEHOLDER__`
  substitutions are gone.
- **Search results are built with `textContent`.** The old shell assigned node labels into
  `innerHTML`; a repository containing a symbol named `<img onerror=…>` would have executed it. The
  generator additionally escapes `</script` in every emitted file, so a label cannot end the script
  tag early.
- The stale `graphify-out/kb-graph.html` is removed by the build; graphify's own `graph.html` stays as
  the raw upstream view.
- `README.md` and `CLAUDE.md` point at the new entry point; the design rules gained three entries (the
  interface is a source file, snapshots are stamped, the renderer is local).

### Unchanged on purpose
- The lazy data split (one `.js` per domain, `_cross.js`, `_index.js`, loaded on demand) predates this
  release and stays: `.js` calls rather than JSON + fetch is what makes `file://` work.

## [0.4.0] — 2026-07-26

**One core, two machine transports.** The MCP server and a new HTTP API are the same contract:
both dispatch through `kb_core.call_tool()` and publish `kb_core.TOOLS`, so drift between them would
require deleting code rather than forgetting to copy it. The web surface is deliberately **not** part
of this contract — see 0.5.0 in Unreleased.

### Added
- **`bin/kb_core.py` — every operation, once, transport-free.** Schemas, shallow validation, process
  plumbing, label suggestions and dispatch moved out of the MCP server.
- **A generic `search`** replacing two narrow tools:
  `{q, domain: docs|code|both, scope, mode: auto|lex|vec|hybrid, limit, min_score, expand_aliases,
  explain}`. `domain` routes to qmd, to graphify, or to both with labelled sections; `mode` maps onto
  qmd's three entry points; `scope` picks a collection other than the default.
- **`bin/kb-api` + `bin/kb_api.py` — the HTTP adapter for scripts.** `GET /health`, `GET /version`,
  `GET /tools`, `POST /call {name, arguments}` (the MCP shape verbatim), plus ergonomic
  `POST /search`, `POST /graph/{path,explain,affected}`, `GET /doc?path=`. Routing is a **pure
  function** (`dispatch`), so the surface is tested without opening a socket. `make serve` runs it.
- **Security policy, enforced at startup:** default bind `127.0.0.1`; a token
  (`--token` / `KB_API_TOKEN`) is **required** for any non-loopback bind and the server **refuses to
  start** without one — verified live and pinned by tests. Requests off loopback are rejected even if
  that check were bypassed. Remote use means loopback + an SSH tunnel, not a wider bind.
- **`version` operation / `GET /version`** returning `{repo, api_contract, collection, aliases}`:
  the repo release and the surface's contract number are separate axes and are never merged.
- **Validation without a dependency:** required keys, primitive types (a `bool` is not an `integer`
  on the wire), enum membership — read from the same schema dicts the surfaces publish. An unknown
  argument is an error, because a silently ignored `mod=lex` looks like the mode did nothing.
- Tests 62 → **104**: schema/validation, `search` routing by domain, deprecated shims delegating
  rather than duplicating, alias expansion on/off per call, the timeout fallback firing only in
  `auto` mode, and the whole bind/token policy.

### Changed
- `bin/kb-mcp` is now transport only (~60 lines): JSON-RPC framing, `initialize` (now also reporting
  `apiContract`), `tools/list`, `tools/call`. A bad call comes back as tool text an agent can read and
  retry, instead of a protocol error it cannot see.
- **`docs_search` and `graph_query` are deprecated but working** — they translate to `search`, so
  there is one implementation. Callers written against 0.3.x keep working unchanged.
- README gained "Three surfaces, three consumers" with the API reference and the token rule.

## [0.3.1] — 2026-07-26

Refactor and tests, no behaviour change. Cleanup **before** the 0.4.0 core extraction, because the
weaknesses below would have been copied into the new layer.

### Added
- **`bin/graph.py` — the artefact contract in one module:** atomic load/save, the node-id format
  (`repo::path::symbol`) with parse/build, and `Relation` / `Confidence` vocabularies as constants.
  Previously eight scripts re-derived all of this by hand, so a graphify format change broke eight
  files silently and a relation typo produced a silently wrong edge instead of an error.
- **Atomic writes.** `graph.save()` writes a temp file in the same directory, `fsync`s and
  `os.replace()`s it; a failed serialization leaves the previous complete graph and no debris. The
  pipeline rewrites one shared artefact in place and has no recovery step, so a truncated
  `graph.json` was a real failure mode, not a theoretical one. `load`/`save` also refuse a value that
  is not a graph.
- **Tests where the risk is** — 26 → **62**. New: `dedupe` (merge into the single definition, collapse
  when there is none, **leave ambiguous labels untouched**, drop self-loops and duplicate edges, keep
  repos isolated), `declutter` (curated hubs only; a real service with the same degree survives;
  exact label match, so `Model` never eats `ModelFactory`), `graph` (atomicity, shape guards, id
  round-trip incl. namespaced symbols), `alias_expand` (directional, whole-token, path segments,
  partial table degrades instead of breaking). Previously only the pure parser was covered.
- **`tests/_kbtest.py`** — one bootstrap for importing `bin/` scripts, replacing per-file importlib
  boilerplate.
- **README §"Design rules that hold this together"** — the properties the code already has, written
  down so a future change does not trade them away silently.

### Changed
- The eight enrichers now use `graph.load` / `graph.save` / `graph.read_json` / `graph.write_json`
  instead of `json.dump(g, open(path, "w"))`; local variables no longer shadow the module.
- **Silent failure removed from the build:** a failing `php artisan route:list` now warns loudly and
  removes the stale route file, instead of `|| true` quietly dropping the cross-repo half of the graph.
- The clustering ↔ labeling double pass is documented as what it is — a **fixpoint through files**
  (`cluster-only` can renumber community ids, the labels file is keyed by them) — rather than looking
  like a redundant call.
- `bin/kb-mcp` imports its sibling module directly; the `sys.path` insert was unnecessary (a directly
  executed script already has its own directory on `sys.path[0]`).

## [0.3.0] — 2026-07-25

### Added
- **Alias library — directional query expansion** (`bin/alias_expand.py`, wired into the MCP
  `docs_search`). When a query contains an old name, the canonical term is appended before the query
  reaches the index; the original wording is preserved, and the expansion is echoed in the result so
  it is never silent.
  - **Deployment data, not code:** the table lives in a gitignored `aliases.toml`
    (`aliases.toml.example` is committed), exactly like `kb.projects.toml` — it names private
    projects, products and people. A missing file means the feature is simply off.
  - **Directional on purpose (old → canonical).** A symmetric expansion that dumps every spelling
    into the query pulls the *meta* layer to rank 1 — changelogs, planning notes and logs are the
    documents that contain all spellings at once. Measured and rejected.
  - **Matching happens on raw query text, before stemming.** FTS5's `porter unicode61` can collapse a
    brand into an ordinary word (a product named `Bookinger` stems to `booking`), which turns an alias query into a stopword
    query; matching the raw string keeps the alias an alias. Whole-token match, tolerant of path
    separators, so an old path segment (`/srv/oldname`) triggers too.
  - **Measured on a 27-question golden set** (lexical leg): alias-type recall **0.50 → 0.70**, overall
    recall **0.735 → 0.772**, MRR **0.526 → 0.536** — i.e. no rank regression, which was the risk.
    Honest limit: the two questions it rescues land at rank 6–10 (presence restored, rank not), and
    three of five alias questions already worked without it.

### Changed
- Roadmap reordered: the alias library became `0.3.0`; the write path moves to `0.4.0`.

## [0.2.0] — 2026-07-25

First tagged release. State of the tool as it is actually used in two private deployments (a
personal markdown wiki and a codebase indexing itself), not a rewrite.

### Added
- **MCP server** `bin/kb-mcp` (stdio, zero Python dependencies) exposing six tools:
  `graph_query`, `graph_path`, `graph_explain`, `graph_affected`, `docs_search`, `docs_get`.
- **Cross-repo graph edges**: frontend call sites matched to backend controllers
  (`calls` → `serves`), incl. Next/React and Nuxt/ofetch `$api()` matchers, tolerant of a Laravel
  `api/` prefix.
- **`confidence` on edges** — `EXTRACTED | INFERRED | AMBIGUOUS | DERIVED`, so regex-derived edges
  no longer masquerade as AST facts; rendered dotted in the visualisation.
- **rationale nodes** — `NOTE`/`WHY`/`HACK`/`SECURITY`/`RATIONALE` comments become `rationale`
  nodes with `explains` edges (`bin/link_rationale.py`, capped at 8 per file).
- **Doc half**: aggregated docs staged into one qmd collection (`kb` by default, `KB_COLLECTION`
  to override) with a selectable qmd backend (`bin/kb qmd-backend`).
- **Namespace labelling** and clustered graph rendering (`bin/label_namespaces.py`,
  `bin/render_viz.py`).
- **`bin/kb --version`** — prints the repo version plus the resolved `graphify` and `qmd` versions.
- Unit tests for the HTTP matcher, including `{id?}` query-splitting in `norm_path`.

### Changed
- Repository renamed `knowledge-base` → `knowledge-index` (2026-07-05). Old name still appears in
  historical notes; there is no compatibility shim.
- MCP `serverInfo.version` now reports the real version from `VERSION` (it previously reported a
  hardcoded `1.0`, which overstated stability).

### Requirements
- **`graphify >= 0.9.25`** (`Graphify-Labs/graphify`) — the code-graph half.
- **`qmd`** (verified with 2.5.3) — the doc-search half; embeddings run on-device.
- `python3`, `bash`. All generated artefacts (`graphify-out/`, `repos/`, `.docs/`,
  `kb.projects.toml`) are gitignored — a checkout builds them, it does not carry them.

### Known limits
- `qmd` filters by **collection only**; an arbitrary doc-id pre-filter is not supported, so
  fine-grained scoping needs one collection per scope (or an upstream patch).
- The graph covers the languages `graphify` parses; Elixir projects are out of scope.

## [0.1.0] — untagged (pre-2026-07-05)

Retroactive label for the pre-MCP era: a single-repo graphify wrapper under the old name
`knowledge-base`. No tag exists for it; recorded here so the numbering means something.
