# The shared server

One build on one server for a whole team: sign-in through the browser, per-project access, the warm helpers that make answers fast, the admin pages and the Prometheus metrics. All of it is off until you switch it on.

## Shared server (off by default)

One build on one server, reached by every developer's agent over MCP "streamable HTTP", where each
caller sees only the projects they may see. It is the same core: `bin/kb-http` dispatches through
`kb_core.call_tool(name, args, allowed)`, and the access filter (`bin/access.py`) works on the inputs —
graph tools run on a filtered copy of the graph, doc hits are checked by their resolved path. Who sees
the contracts between services is a setting, `contracts`: by default everyone sees all of them (see
[who sees the contracts](#who-sees-the-contracts)).

To run it in Docker, with graphify, rustworkx and qmd in one image, see [Docker](docker.md).

**It does not start unless you switch it on.** A local install never opens a port by accident:

```bash
KB_SERVER=on KB_PERMISSION_URL=https://api.example.com/kb/permissions \
  KB_LOGIN_URL=https://api.example.com/kb/login kb-http   # 127.0.0.1:8790; TLS proxy in front
```

Identity is yours, not this tool's. The server talks to your system through **access protocol v1**, two
webhooks ([access-protocol.md](access-protocol.md)): the *login* webhook turns a user name, password
and code into a token; the *permission* webhook gets that token and the KB's project names and answers
with the allowed ones, or `"*"`. Your system needs to know nothing about the KB. 401 stays 401, any
other 4xx refusal is 403, an unreachable webhook, a 5xx answer or a body of another shape is 503 (never cached). Verdicts are cached
`KB_AUTH_CACHE_SECONDS` (60) per SHA-256 of the token; the token is never logged.

| Variable | Default | Meaning |
|---|---|---|
| `KB_SERVER` | `off` | must be `on` |
| `KB_PERMISSION_URL` | — | permission webhook (required) |
| `KB_LOGIN_URL` | — | login webhook: browser sign-in, admin pages, `kb-login` |
| `KB_PUBLIC_LOGIN_URL` | — | the address of the login webhook that a person can reach; the "How to" admin page shows it in the `kb-login` command |
| `KB_PUBLIC_URL` | — | the public address of this server (`https://kb.example.com/kb`): the host of a webhook URL that is this server, and the address in a mail of the users add-on. kb-http reads it, and `KB_PUBLIC_LOGIN_URL`, from the environment, else from `<data>/smtp.env` |
| `KB_HOOK_HEADERS` | — | extra headers for both webhooks, a JSON object |
| `KB_HTTP_HOST` / `KB_HTTP_PORT` | `127.0.0.1` / `8790` | bind address; put a TLS proxy in front |
| `KB_DOCS_MODE` | `light` | no reranker (~650 MB RAM); `full` adds an LLM reranker (1-2 min per search on a CPU); `bm25` = keywords only (~70 MB) |
| `KB_MAX_GRAPH_JOBS` / `KB_MAX_DOCS_JOBS` | `3` / `1` | graphify / qmd processes at once, in any server |
| `KB_MAX_RUN_JOBS` | `2` | `run_code` processes at once |
| `KB_JOB_WAIT` | `120` | seconds a call waits for a free job slot; then it answers "busy" |
| `KB_CURSOR_BYTES` / `KB_CURSOR_PER_OWNER` | `64000000` / `50` | memory of the paging cursors in all / cursors per caller |
| `KB_RUN_MAX_OUTPUT` | `60000` | characters of `run_code` output |
| `KB_AUTH_CACHE_SECONDS` / `KB_AUTH_TIMEOUT` | `60` / `10` | auth cache (refusals too) and the timeout of the permission webhook |
| `KB_AUTH_MAX_CHECKS` / `KB_AUTH_CHECK_WAIT` | `16` / `2` | permission webhook calls at once; one more waits this many seconds, then `503`. A malformed token gets `401` without a call |
| `KB_LOGIN_TIMEOUT` | `30` | the timeout of the login webhook (sign-in pages, `kb-login`) |
| `KB_HTTP_IDLE_TIMEOUT` | `60` | a connection that sends nothing for this long closes |
| `KB_LOGIN_LIMIT` / `KB_LOGIN_LIMIT_ALL` / `KB_LOGIN_WINDOW` | `20` / `2000` / `300` | password attempts per client address in the window (seconds) at `/admin/login`, `/authorize` and `/local/login`; then `429`. While all addresses together pass `KB_LOGIN_LIMIT_ALL`, each address gets 5 attempts: a flood slows down, a person at a fresh address still signs in. A request of this machine counts under its address; only kb-http's own calls of its webhooks (in-process, or a secret header of the process) do not. `0` = no limit |
| `KB_CONTRACT_EDGES` | `1` | `0` hides other repos' endpoints of `http_request` edges |
| `KB_CONTRACTS` | `all` | who sees the contracts: `all` or `own`; set, it locks the value of the Settings page ([below](#who-sees-the-contracts)) |
| `KB_USAGE_FILE` | `<data>/logs/usage.json` | tool usage: one SQLite row per call in `usage.sqlite` next to this name (an old `usage.json` is imported once); every call is written at once, rows older than 90 days go once an hour. `kb-http usage` prints the report with user names; the MCP tool `usage` without |
| `KB_WARM` | `1` | `0` = no long-lived graphify / qmd: one process per call, as before |
| `KB_GRAPH_CACHE_FILES` / `KB_GRAPH_VIEWS` | `2` / `8` | graph files the graphify worker keeps parsed / views it keeps cut in memory (two per caller project set) |
| `KB_OAUTH` | `on` | `off` = no browser sign-in of MCP clients, bearer tokens only. The admin pages and their sign-in stay |
| `KB_LOGIN_ENV` | `<data>/kb-login.env`, then `<data>/deploy/kb-login.env` | a KEY=VALUE file with `KB_LOGIN_URL` and `KB_HOOK_HEADERS` |
| `KB_OAUTH_TOKEN_TTL` | `28800` | lifetime of an access token of the browser sign-in (`kbo_…`), the `expires_in` the client is told. It stands for the login webhook's token; a refresh gives a new one while that token lives. Access tokens live in memory: a restart means one more browser sign-in |
| `KB_OAUTH_MAX_CLIENTS` / `KB_OAUTH_MAX_URIS` / `KB_OAUTH_MAX_URI_LENGTH` | `1000` / `10` / `512` | registered clients, redirect URIs per client, characters per URI. `/token` answers a client it does not know (revoked by an admin, or pushed out of a full list) with `401 invalid_client`: the MCP client then registers again |
| `KB_OAUTH_MAX_TXNS` / `KB_OAUTH_TXNS_PER_CLIENT` / `KB_OAUTH_TXNS_PER_ADDRESS` | `1000` / `20` / `10` | browser sign-ins in progress, in all, per client and per client address. An address over its limit loses its own oldest; a full list loses one of the busiest address or one older than a minute, never a fresh sign-in of another person: then the new one gets `503` |
| `KB_TRUSTED_PROXIES` | — | proxy addresses or networks besides loopback whose last `X-Forwarded-For` entry is the client (e.g. `172.17.0.0/16` for nginx outside a Docker container) |
| `KB_OAUTH_AUTHORIZE_LIMIT` | `60` | `GET /authorize` per address in 5 minutes |
| `KB_OAUTH_CLIENT_DAYS` / `KB_OAUTH_REFRESH_DAYS` | `1` / `30` | a client nobody signed in with goes after this, and first when the client list is full; a refresh token ends after this |
| `KB_OAUTH_TOKENS_PER_CLIENT` | `50` | live access and refresh tokens of one client; the oldest go first. The access token a refresh replaces ends 60 s later |
| `KB_OAUTH_REGISTER_LIMIT` / `KB_OAUTH_REGISTER_LIMIT_ALL` | `20` / `2000` | client registrations per address per hour; then `429`. Over the second, each address gets 5. A registration that cannot be saved gets `503` |

**Speed.** The servers keep two helpers running (`bin/warm.py`): graphify in its own interpreter
with the graph already parsed (`bin/graph_worker.py`), and `qmd mcp` with the embedding model loaded.
On a 23k-node graph a graph lookup drops from 1-6 s to a few milliseconds, a code search from ~6 s to
0.1 s and a docs search from 4-7 s to 0.2 s. The first call for a caller's project set cuts that view from the
parsed graph in memory (~0.3-1.6 s on that graph; then milliseconds). A helper that fails or is missing falls back to the old one-process-per-call path.

**MCP over HTTP.** `POST /mcp` takes one JSON-RPC message or a batch of at most 50; a longer batch gets
`400` before anything runs. The answers of a batch stop at 16 MB: the messages after that get an error
answer and do not run. A notification (no `id`) runs nothing. kb-http and kb-api share these rules
(`bin/mcp_rpc.py`).

**Locally:** `make serve` (`bin/kb-api` on 127.0.0.1:8899) also answers MCP over HTTP at `/mcp` and shows
the same admin pages at `/admin`, without sign-in: loopback without a token is the trust boundary.

**The KB's own users (the plugin `contrib/users`).** Without a system of your own to answer the two webhooks, let the
server keep the users: `kb addon install contrib/users`, then `python3 contrib/users/plugin.py add <name> --admin` prints the
key of the first admin. That admin adds the other people on the Users tab: a name, the projects (a checkbox per project of the server, or "all" for
all) and the admin right, and gives each person the first key. With it the person signs in to the pages
and opens "My tokens" with the own tokens. The Users tab of an admin also shows who is online (a request
in the last 15 minutes). The person makes one token for each computer or agent and removes a token there. A person who is no
admin reaches only "My tokens" and "How to". An agent sends a token as `Authorization: Bearer <token>`,
or the person signs in with the user name and a token in the password field of the browser sign-in or of
`kb-login`. A key or token shows once; `<data>/users.json` (owner only) keeps its SHA-256. It lives until
its owner or an admin removes it, or an admin disables or removes the user. With the add-on on,
`KB_PERMISSION_URL` and `KB_LOGIN_URL` default to the server itself (`/local/permissions`,
`/local/login`); set them only to use another system. kb-http calls the plugin in-process for both, with
no HTTP request, when the URL is this server: loopback on its own port, or the host of `KB_PUBLIC_URL` /
`KB_PUBLIC_LOGIN_URL` with any path prefix (`https://kb.example.com/kb/local/login`). Each sign-in then counts
under the address of the person, not of the server. kb-http takes `KB_PUBLIC_URL` and `KB_PUBLIC_LOGIN_URL`
from the environment, else from `<data>/smtp.env`. With an access plugin on, kb-http does not start when
`KB_PERMISSION_URL` ends in `/local/permissions` but is not this server: every token would get `403`. A
`KB_LOGIN_URL` of that kind gets a warning in the log. `/local/permissions` answers only this server; from the network it is
`403` (an open, uncached token check would be a proxy to the plugin). `/local/login` stays public for
`kb-login`.

Every password sign-in makes a sign-in token. An agent (browser sign-in of an MCP client, `kb-login`) keeps
its 10 most recently used, and the admin pages their own 10: page sign-ins never push out the token of an
agent. A page sign-in token ends with its session (sign-out, 8 hours, or an admin change). A sign-in token
also ends after 30 days without a request. At most 4 password hashes run at once (about 16 MiB each); one
more waits 5 seconds, then the sign-in gets `503`. A users file that cannot be written (a full disk, a
read-only folder) does not stop the tokens: the "last used" time stays in memory, the server logs it once,
and a password sign-in gets a clear `503`.

With mail set up (`<data>/smtp.env`, owner only: `KB_SMTP_HOST`, `KB_SMTP_PORT`, `KB_SMTP_USER`,
`KB_SMTP_PASSWORD`, `KB_SMTP_FROM`, `KB_SMTP_TLS`, and `KB_PUBLIC_URL`, the address of the server that goes
into a mail), a user name is the e-mail of the person and nobody hands a key over: a new person gets a mail
with a link and chooses a password. "Forgot the password?" on the sign-in page and "Reset password" on the
Users tab send the same mail. The password signs the person in to the pages and at the browser sign-in of
an agent; tokens stay the way for a header. A "forgot" mail does not end a pending invite: both links work
until one sets the password. The link carries its one-time value after `#` (`/admin/reset#t=…`): a browser
never sends that part, so no access log of nginx or of a proxy keeps it. A small script of the page (the
only script of the admin pages, allowed by its hash in the CSP) puts it into the form. A link of an older
version (`?t=`) still works. "New key" and a save that takes nothing away keep the person signed in; a
revoke, a removal, a disable, a lost admin right or a lost project end the sessions of the person.

One user gets at most one password mail in 5 minutes and `KB_RESET_PER_DAY` (default 3) in 24 hours; the
count lives in the users file, so a restart does not reset it, and an invite does not count. Over the cap,
"Forgot the password?" sends nothing and answers the same as always; "Reset password" of an admin says so.
The answer to "Forgot the password?" comes at once and is the same for every name; one background thread
sends the mail, so the time of the answer does not tell whether a name exists. A reload of the page of a
mail link loses the link value (the page script takes it out of the address): the page then asks to open
the link from the mail again. A users file that cannot be read gives `503` at sign-in, not "wrong
password". A change whose save fails (a full disk) does not stay in memory: the store reads the file again.
A browser sign-in that never reached `/token`, a revoked sign-in client and an ended refresh token end
their sign-in token, so they do not push out the tokens an agent still uses.

Five wrong passwords from one address shut the sign-in of that name from that address for 15 minutes; the
owner signs in from another address. An admin cannot save the own row without a project: MCP refuses a token
without a project. An admin whose projects are all gone (a project left `kb.projects.toml`), or who has
none, still signs in to the admin pages (Users, Settings, Usage, Rebuild) and can fix the row: the plugin
answers an empty grant for an admin, kb-http lets it into the admin pages and refuses it on MCP. A member
without a project stays out. The admin right comes from the plugin's role or from `KB_ADMINS` matched on
the stable login fields only (`uname`, `username`, `id`, `uid`).

**An access plugin.** The users above are a plugin, and so can be a system of your own: a `plugin.py` with
`login(body, core)` and `permissions(token, projects, core)` answers the two webhooks inside the server,
with no webhook service; `body["ip"]` is the client address. Put `ACCESS = True` in its `plugin.py`: the
Settings page reads that line without loading the plugin (without the line, only a `plugin.py` that defines
both functions itself counts). It may bring its own tabs to the admin pages.
The contract is in `bin/plugins.py`. One server has one access plugin: the first by name answers, and the
server logs a warning when more are on. The Settings page does not switch an access plugin off and does
not switch on a second one: after a restart nobody could sign in to undo it. The plugin the server
started with is protected by its name, whatever its source looks like. Do it on the server
(`kb addon off <name>`, `kb addon on <name>`: only the switch, nothing is downloaded), then restart. Without an access plugin and without
`KB_PERMISSION_URL`, or with `KB_PERMISSION_URL` on the server's own `/local/permissions` and no access
plugin, kb-http does not start and says how to switch the plugin back on. The admin pages show the access
plugin the server started with until the next restart.

**Admin pages (`/admin`).** They are part of the core, not an add-on. For the users named in `KB_ADMINS` (comma-separated) or in `<data>/admins.txt`
(one per line), matched against the `uname`, `username`, `id` or `uid` the permission webhook
returns, or for the admins an access plugin names (`contrib/users`); nobody else gets more than a 404. The
display `name` never makes an admin: a system that lets a person choose the own name must not let that
person become one. Give the webhook a `uname` (or `id`) and name that in `KB_ADMINS`. Without a login
webhook (`KB_LOGIN_URL`) there is no sign-in and the pages are a 404; `KB_OAUTH=off` does not remove them.
Sign-in goes through the login webhook with its second factor, then
an HttpOnly, SameSite=Strict session cookie (8 hours) and a CSRF token on every form. Every request checks
the token of the session again (cached `KB_AUTH_CACHE_SECONDS`): a removed token or a new password ends the
session, and an admin who lost the right keeps only the pages of a member. Pages: status
(version, graph, the last builds, warm helpers, a rebuild button: it runs `kb-refresh --force` and says
"A build is already running" when another build holds the lock, exit code 75), index (charts: graph make-up,
storage per index, documents and embedded chunks per project, build times, answer time per tool,
calls per day), usage, sign-in clients (revoke one) and settings. Plain HTML and server-drawn SVG; the only
script is the one of a password link (above), allowed by its hash.
An `addons.json` of an older version that still names `admin` keeps working: the name is ignored.

**Settings (`/admin/settings`).** Admins only; a member gets 403. Two parts:

- **Add-ons.** Every core add-on and every installed plugin, with a switch that writes `<data>/addons.json`
  (atomically). Most switches take effect at once. `fast-routes` and the plugins take effect after a restart;
  the access plugin (for example `users`) is chosen when the server starts, and only the command line switches it. A switched-on add-on that lacks a
  piece says so where a cheap check can tell: `code-mode` without Node or its sandbox packages, `scip` without
  `scip-php`. With `KB_ADDONS` set, the switches are read-only.
- **Settings.** Each setting of `bin/settings.py` with its value, default and description, and a control. The
  value goes to `<config>/settings.json` and takes effect without a restart. A value set by its env variable
  (for example `KB_CONTRACTS`) is shown as locked.

Each change goes to the server log and, with the add-on `audit` on, to the Audit tab, with the admin's name.

**Who sees the contracts.** The setting `contracts` (env `KB_CONTRACTS`, or the Settings page) decides it
for a caller limited to some projects. A local session (`kb-api`, `kb-mcp`) sees everything either way.

| | `all` (default) | `own` |
|---|---|---|
| `contracts_search` | every route, its action, middleware and every call site | the routes of allowed projects in full, with the call sites of allowed projects only. A route of a hidden project shows only when an allowed project calls it, as the bare endpoint (method and URI): no name, action, middleware, file or project. Its words match that endpoint only |
| `connections` | every connection | a row whose contract doc belongs to an allowed project, as it is; any other row only when one end is an allowed project, the other end shown as `(hidden)`, without the summary and path of a hidden doc. An unknown or hidden service name lists only the services the caller sees |
| contract docs (`search`, `docs_get`) | the contract docs of every project: the `*.md` files directly in `contracts/`, not its README or sub-folders | the contract docs of allowed projects only |
| graph tools | a node of another project stays when an `http_request` edge links it to an allowed node (`KB_CONTRACT_EDGES=0` drops it) | no node of another project |
| `problems` | the problems of allowed projects; a message may name the other service an allowed project calls | the problems of allowed projects; another project the caller may not see is written as `(hidden)` |

`KB_CONTRACT_EDGES=0` still works: under `all` it hides the graph edges to other projects but keeps
`contracts_search`, `connections` and the contract docs open. `own` closes all four.

**Questions (`/admin/questions`, add-on `questions`, off by default).** The page lists the questions people
asked: the calls of `search`, `contracts_search` and `code_grep`, one row per question, with how often and by
whom. An admin gives each question a label: the path or symbol that must be found, "no answer in the corpus",
or "skip". A yellow mark beside the tab shows how many questions have no label. The labelled questions are
the golden set from real use: `/admin/questions.json`. A label stays after the calls are deleted (90 days).
Switch it on with `kb addon install questions`.

**Browser sign-in (MCP OAuth).** With `KB_LOGIN_URL` set, an MCP client signs in
by itself: a 401 points it to `/.well-known/oauth-protected-resource`, it registers (dynamic client
registration), opens `/authorize` in the browser, the person types the user name, password and
code, and the client receives an access token of this server (`kbo_…`) that stands for the login
webhook's token and ends after `KB_OAUTH_TOKEN_TTL` (PKCE S256, loopback redirect URIs only, in their
plain form: no user part, no backslash; refresh while the login webhook's token lives). Revoking a client
on the admin page ends its access tokens at once. `bin/oauth.py` has the details and the limits. Behind a proxy, pass `X-Forwarded-Proto`, `X-Forwarded-Host` and the stripped path as
`X-Forwarded-Prefix`; the discovery documents are also served at the host root
(`/.well-known/oauth-authorization-server/<prefix>`), see `examples/server/`.

Around it:

- **`bin/kb-refresh`** — cron, every minute: runs `kb update` when an indexed checkout has a new HEAD.
  `KB_REFRESH_WAIT_LOCK=<file>` makes it wait while your deploy holds that lock. One build runs at a
  time (`<data>/logs/update.lock`): `kb-refresh --force` during a build exits with code 75 and `busy`.
- **`bin/kb-login`** — the terminal alternative to the browser sign-in: logs a developer in to *your*
  API, keeps the token in `~/.claude/kb/token` and registers the server once in Claude Code's user scope
  with a `headersHelper` (`--add-to-claude`). No default addresses: `--env-file`, or `KB_LOGIN_URL`,
  `KB_URL` and `KB_HOOK_HEADERS`.
- **`examples/server/`** — a systemd unit, an nginx vhost and a crontab, with `kb.example.com`
  placeholders.

## Review context on pull requests

`review_context` takes a unified diff (or a list of files with lines) and answers what a reviewer asks
every time: the changed classes and methods, what depends on them in every project (frontend modules
first, they are the surprises), the HTTP routes that go through them and the frontend files that call
those routes, the docs that name the changed code and the NOTE/WHY comments next to it
(`bin/review.py`). The graph is undirected, so each dependency edge is oriented by the file it was
written in (the caller's file).

An agent or a CI job passes `git diff origin/master...HEAD` and gets the answer as text.

## Contracts between services

Three tools answer what the graph answers badly — they read indexes `kb update` builds next to the
graph (`bin/contracts.py`):

- **`contracts_search`** — an API route with its controller action, middleware, the frontend files
  that call it, and the contract docs that name it. Words match all of them (`POST v2/users`).
- **`connections`** — the service map: who talks to whom, over what, and why. Built from contract docs,
  one file per connection in `<project docs>/contracts/<from>-<to>.md` with front matter `from`, `to`,
  `protocol`, `summary`, plus one generated HTTP line per frontend→API pair the code shows.
- **`problems`** — the gaps: a project without contract docs, a contract doc with incomplete front
  matter, a connection the code shows but no doc describes, a frontend call to a route that does not
  exist. Check it before finishing work on a project.

A checkout of the KB itself may hold `docs/contracts/` too (the KB's own connections); it is indexed
as project `kb` (`KB_SELF_DOCS_NAME`).

## How the projects connect

`bin/link_http.py` reads each Laravel app's `php artisan route:list --json` and matches the
frontend's API call sites against it, injecting `http_request` edges. It recognizes the common
shapes — dotted clients (`api.<verb>('<path>')`), common client objects with a path argument
(`axios.get('/x')`, `this.$http.post(...)`, `http.get('x/y')`), ofetch/fetch wrappers where the verb
sits in an options object (`$api(...)`, `apiFetch(...)`, and a raw `fetch(\`${API_BASE}/…\`)`) — and
tolerates the frontend omitting a base prefix that the base URL supplies (e.g. Laravel's `api/`). This
is what makes `graphify path` traverse from a Vue/React file to the controller that serves its request.

The call site is resolved in the calling project only (`--from <project>`), so two frontends with the
same file path do not swap their calls. The controller is found by its namespace path, matched against
the file path from the end: `SmartMonitor\Sw\Http\Controllers\VehicleController` matches
`packages/sw/src/Http/Controllers/VehicleController.php`. At least the part from `Http/` on must match,
and two files with an equal best match give no edge.
