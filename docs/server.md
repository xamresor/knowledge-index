# The shared server

One build on one server for a whole team: sign-in through the browser, per-project access, the warm helpers that make answers fast, the admin pages and the Prometheus metrics. All of it is off until you switch it on.

## Shared server (off by default)

One build on one server, reached by every developer's agent over MCP "streamable HTTP", where each
caller sees only the projects they may see. It is the same core: `bin/kb-http` dispatches through
`kb_core.call_tool(name, args, allowed)`, and the access filter (`bin/access.py`) works on the inputs —
graph tools run on a filtered copy of the graph, doc hits are checked by their resolved path. Contract
docs and the cross-repo `http_request` edges stay visible to everyone: they are the interfaces between
services (`KB_CONTRACT_EDGES=0` hides the edges too).

**It does not start unless you switch it on.** A local install never opens a port by accident:

```bash
KB_SERVER=on KB_PERMISSION_URL=https://api.example.com/kb/permissions \
  KB_LOGIN_URL=https://api.example.com/kb/login kb-http   # 127.0.0.1:8790; TLS proxy in front
```

Identity is yours, not this tool's. The server talks to your system through **access protocol v1**, two
webhooks ([access-protocol.md](access-protocol.md)): the *login* webhook turns a user name, password
and code into a token; the *permission* webhook gets that token and the KB's project names and answers
with the allowed ones, or `"*"`. Your system needs to know nothing about the KB. 401 stays 401, any
other refusal is 403, an unreachable webhook is 503 (never cached). Verdicts are cached
`KB_AUTH_CACHE_SECONDS` (60) per SHA-256 of the token; the token is never logged.

| Variable | Default | Meaning |
|---|---|---|
| `KB_SERVER` | `off` | must be `on` |
| `KB_PERMISSION_URL` | — | permission webhook (required) |
| `KB_LOGIN_URL` | — | login webhook: browser sign-in, admin pages, `kb-login` |
| `KB_HOOK_HEADERS` | — | extra headers for both webhooks, a JSON object |
| `KB_HTTP_HOST` / `KB_HTTP_PORT` | `127.0.0.1` / `8790` | bind address; put a TLS proxy in front |
| `KB_DOCS_MODE` | `light` | no reranker (~650 MB RAM); `full` adds an LLM reranker (1-2 min per search on a CPU); `bm25` = keywords only (~70 MB) |
| `KB_MAX_GRAPH_JOBS` / `KB_MAX_DOCS_JOBS` | `3` / `1` | parallel graphify / qmd processes |
| `KB_AUTH_CACHE_SECONDS` / `KB_AUTH_TIMEOUT` | `60` / `10` | auth cache and timeout |
| `KB_CONTRACT_EDGES` | `1` | `0` hides other repos' endpoints of `http_request` edges |
| `KB_USAGE_FILE` | `<data>/logs/usage.json` | tool usage counters (`kb-http usage` prints them with user names; the MCP tool `usage` without) |
| `KB_WARM` | `1` | `0` = no long-lived graphify / qmd: one process per call, as before |
| `KB_GRAPH_CACHE_FILES` / `KB_GRAPH_VIEWS` | `2` / `8` | graph files the graphify worker keeps parsed / views it keeps cut in memory (two per caller project set) |
| `KB_OAUTH` | `on` | `off` = no browser sign-in, bearer tokens only |
| `KB_LOGIN_ENV` | `<data>/kb-login.env`, then `<data>/deploy/kb-login.env` | a KEY=VALUE file with `KB_LOGIN_URL` and `KB_HOOK_HEADERS` |
| `KB_OAUTH_TOKEN_TTL` | `28800` | `expires_in` reported to the client (the API decides the real lifetime) |

**Speed.** The servers keep two helpers running (`bin/warm.py`): graphify in its own interpreter
with the graph already parsed (`bin/graph_worker.py`), and `qmd mcp` with the embedding model loaded.
On a 23k-node graph a graph lookup drops from 1-6 s to a few milliseconds, a code search from ~6 s to
0.1 s and a docs search from 4-7 s to 0.2 s. The first call for a caller's project set cuts that view from the
parsed graph in memory (~0.3-1.6 s on that graph; then milliseconds). A helper that fails or is missing falls back to the old one-process-per-call path.

**Locally:** `make serve` (`bin/kb-api` on 127.0.0.1:8899) also answers MCP over HTTP at `/mcp` and shows
the same admin pages at `/admin`, without sign-in: loopback without a token is the trust boundary.

**Admin pages (`/admin`).** For the users named in `KB_ADMINS` (comma-separated) or in `<data>/admins.txt`
(one per line), matched against the `uname`, `username`, `name`, `id` or `uid` the permission webhook
returns; nobody else gets more than a 404. Sign-in goes through the login webhook with its second factor, then
an HttpOnly, SameSite=Strict session cookie (8 hours) and a CSRF token on every form. Pages: status
(version, graph, the last builds, warm helpers, a rebuild button), index (charts: graph make-up,
storage per index, documents and embedded chunks per project, build times, answer time per tool,
calls per day), usage, and sign-in clients (revoke one). Plain HTML and server-drawn SVG, no scripts.

**Browser sign-in (MCP OAuth).** With `KB_LOGIN_URL` set, an MCP client signs in
by itself: a 401 points it to `/.well-known/oauth-protected-resource`, it registers (dynamic client
registration), opens `/authorize` in the browser, the person types the user name, password and
code, and the client receives the login webhook's token as its access token (PKCE S256,
loopback redirect URIs only, refresh while the API token lives). `bin/oauth.py` has the details and the
limits. Behind a proxy, pass `X-Forwarded-Proto`, `X-Forwarded-Host` and the stripped path as
`X-Forwarded-Prefix`; the discovery documents are also served at the host root
(`/.well-known/oauth-authorization-server/<prefix>`), see `examples/server/`.

Around it:

- **`bin/kb-refresh`** — cron, every minute: runs `kb update` when an indexed checkout has a new HEAD.
  `KB_REFRESH_WAIT_LOCK=<file>` makes it wait while your deploy holds that lock.
- **`bin/kb-login`** — the terminal alternative to the browser sign-in: logs a developer in to *your*
  API, keeps the token in `~/.claude/kb/token` and registers the server once in Claude Code's user scope
  with a `headersHelper` (`--add-to-claude`). No default addresses: `--env-file`, or `KB_LOGIN_URL`,
  `KB_URL` and `KB_HOOK_HEADERS`.
- **`examples/server/`** — a systemd unit, an nginx vhost and a crontab, with `kb.example.com`
  placeholders.

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
shapes — dotted clients (`api.<verb>('<path>')`), ofetch/fetch wrappers where the verb sits in an
options object (`$api(...)`, `apiFetch(...)`, and a raw `fetch(\`${API_BASE}/…\`)`) — and tolerates
the frontend omitting a base prefix that the base URL supplies (e.g. Laravel's `api/`). This is
what makes `graphify path` traverse from a Vue/React file to the controller that serves its request.
