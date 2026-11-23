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
| `KB_PUBLIC_LOGIN_URL` | — | the address of the login webhook that a person can reach; the "How to" admin page shows it in the `kb-login` command |
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
`/local/login`); set them only to use another system.

With mail set up (`<data>/smtp.env`, owner only: `KB_SMTP_HOST`, `KB_SMTP_PORT`, `KB_SMTP_USER`,
`KB_SMTP_PASSWORD`, `KB_SMTP_FROM`, `KB_SMTP_TLS`, and `KB_PUBLIC_URL`, the address of the server that goes
into a mail), a user name is the e-mail of the person and nobody hands a key over: a new person gets a mail
with a link and chooses a password. "Forgot the password?" on the sign-in page and "Reset password" on the
Users tab send the same mail. The password signs the person in to the pages and at the browser sign-in of
an agent; tokens stay the way for a header.

**An access plugin.** The users above are a plugin, and so can be a system of your own: a `plugin.py` with
`login(body, core)` and `permissions(token, projects, core)` answers the two webhooks inside the server,
with no webhook service. It may bring its own tabs to the admin pages. The contract is in `bin/plugins.py`.
One server has one access plugin: the first by name answers.

**Admin pages (`/admin`).** For the users named in `KB_ADMINS` (comma-separated) or in `<data>/admins.txt`
(one per line), matched against the `uname`, `username`, `name`, `id` or `uid` the permission webhook
returns; nobody else gets more than a 404. Sign-in goes through the login webhook with its second factor, then
an HttpOnly, SameSite=Strict session cookie (8 hours) and a CSRF token on every form. Pages: status
(version, graph, the last builds, warm helpers, a rebuild button), index (charts: graph make-up,
storage per index, documents and embedded chunks per project, build times, answer time per tool,
calls per day), usage, and sign-in clients (revoke one). Plain HTML and server-drawn SVG, no scripts.

**Questions (`/admin/questions`, add-on `questions`, off by default).** The page lists the questions people
asked: the calls of `search`, `contracts_search` and `code_grep`, one row per question, with how often and by
whom. An admin gives each question a label: the path or symbol that must be found, "no answer in the corpus",
or "skip". A yellow mark beside the tab shows how many questions have no label. The labelled questions are
the golden set from real use: `/admin/questions.json`. A label stays after the calls are deleted (90 days).
Switch it on with `kb addon install questions`.

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
shapes — dotted clients (`api.<verb>('<path>')`), ofetch/fetch wrappers where the verb sits in an
options object (`$api(...)`, `apiFetch(...)`, and a raw `fetch(\`${API_BASE}/…\`)`) — and tolerates
the frontend omitting a base prefix that the base URL supplies (e.g. Laravel's `api/`). This is
what makes `graphify path` traverse from a Vue/React file to the controller that serves its request.
