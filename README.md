<img width="1469" height="842" alt="Screenshot from 2026-07-09 22-06-41" src="https://github.com/user-attachments/assets/88c10be3-36cc-4173-b298-07163019cf17" />
# Code Knowledge Base

A standalone, read-only knowledge layer over a set of related code projects (for example a Laravel
API and a Vue frontend). It builds **one merged code-connection graph** — including cross-repo edges
that link frontend API calls to the Laravel controllers that serve them — and a **vectorized search
index** over the projects' docs. By default everything runs **on-device with no API keys** (the
`none` and `llama` embedding backends); only the optional `other` backend can reach an external
service.

Built on two existing tools: [**graphify**](https://github.com/Graphify-Labs/graphify) (AST code graph)
and [**qmd**](https://github.com/tobi/qmd) (on-device markdown vector search).

## Requirements

These are external dependencies — not vendored. Install them and make sure they're on your `PATH`
before running a build:

| Tool                  | Version (tested) | Used for |
|-----------------------|---|---|
| [**graphify**](https://github.com/Graphify-Labs/graphify) | `>= 0.9.25` | AST code graph (`update`, `merge-graphs`, `cluster-only`, `query`, `path`, `explain`, `affected`). The build and `bin/kb-mcp` shell out to it. |
| **qmd**               | `>= 2.5.3` (the `query`/`search`/`vsearch` CLI) | On-device markdown vector + BM25 doc search. **Must be the modern lineage**: `bin/kb-mcp` and the docs use `qmd query`. The older `qsearch` lineage (e.g. 0.3.x) has an incompatible command set and will break doc search — see the note below. |
| **python3**           | `>= 3.10` | The enrichment scripts in `bin/` (standard library only — no packages). |
| **rsync**             | any recent | Code-only staging of the indexed projects into `repos/`. |
| **php** + **artisan** | the indexed app's version | Only needed if a project sets `routes` — `bin/kb` runs `php artisan route:list --json` to build cross-repo `http_request` edges. |

By default everything runs **on-device with no API keys** (see the embedding-backend choice under
Configuration). Check what's installed:

```bash
graphify --version && qmd --version && python3 --version
qmd query --help >/dev/null 2>&1 && echo "qmd: query CLI ok" || echo "qmd: WRONG lineage (no 'query' command)"
```

> **qmd version compatibility.** This KB targets the modern qmd CLI (`query`/`search`/`vsearch`,
> 2.5.3+). A divergent older lineage uses `qmd qsearch --no-rerank --no-expand` instead and lacks
> `query`; `bin/kb-mcp`'s `docs_search` will fail against it. If you share one KB across several
> machines, **install the same qmd lineage on all of them** — a `query`-based checkout and a
> `qsearch`-based checkout cannot both work from the same `bin/kb-mcp`.

## Quick start

```bash
make install    # checks requirements, creates config from the examples, prints the MCP snippet
                # never overwrites an existing file; `make check` reports without writing
```


```bash
make build      # build the merged graph + qmd doc index
make vendor     # once: fetch the renderer for offline use (web/vendor/)
make open       # open the dashboard: graph + status (web/index.html)
qmd query "how does auth work" -c kb
graphify path "useAuthFlow.js" "AuthMethodsController" --graph graphify-out/graph.json
```

The qmd collection name defaults to `kb`; override it by setting `KB_COLLECTION` before a build.

## What you get

- `graphify-out/graph.json` — merged graph: every node tagged with its `repo`; `http_request`
  edges connect frontend call sites to backend controllers across repos.
- `web/index.html` — the **dashboard**: the typed/domain graph plus a Status panel (index health, the
  alias table, search-quality numbers). A versioned, dependency-free shell; the graph data is
  generated next to the index. Old note, kept for orientation: this used to be a generated
  `graphify-out/kb-graph.html`. (shape = type,
  color = domain), hierarchical and lazy-loaded. This is what `make open` opens. The hierarchical
  lazy-loading (domain super-nodes expand on demand, edges reroute) is built to stay usable on
  **large codebases — 5k+ files is fine**; it never renders the whole graph at once.
- `graphify-out/graph.html` — graphify's full interactive visualization (fallback).
- `graphify-out/GRAPH_REPORT.md` — human-readable architecture report with communities.
- qmd `kb` collection (`.qmd/`) — hybrid BM25 + vector search over all docs.

## Configuration

Copy `kb.projects.toml.example` to `kb.projects.toml` and edit it to add/remove a project or change
which paths are scanned/indexed. No symlinks — paths are explicit. `.graphifyignore` controls what
graphify excludes.

### qmd embedding backend

The first build asks **where to plug qmd's embeddings** and remembers the choice in `.kb.local`
(gitignored):

| Choice | What it does |
|---|---|
| `none` | Skip vectors entirely — BM25 keyword search only. No model download. |
| `llama` | On-device llama.cpp embeddings (qmd's default; downloads a small GGUF model). |
| `other` | Bring your own embedder: runs `$KB_QMD_EMBED_CMD` if set, otherwise plain `qmd embed` so you can point qmd at your own model/endpoint via its env. |

Skip the prompt by setting `KB_QMD_BACKEND=none|llama|other` (e.g. in CI). Change it later with
`make qmd-backend` (or `bin/kb qmd-backend`).

## Install into a project

Each consuming project needs two things: the **MCP server config** so its agent can query the KB,
and the **`kb` skill** so the agent knows when/how to use it. Both point back here — this repo is
the single source of truth.

### 1. MCP server config

Add the `kb` server to the project's `.mcp.json` (`mcpServers` object). No env, no install — it's a
zero-dependency stdio server (use the absolute path to this checkout):

```json
{
  "mcpServers": {
    "kb": { "command": "/path/to/kb/bin/kb-mcp" }
  }
}
```

Restart the agent session to load it. It exposes `graph_query`, `graph_path`, `graph_explain`,
`graph_affected`, `docs_search`, `docs_get`. (This repo's own `.mcp.json` only registers `qmd`,
which `kb-mcp` calls internally — projects do **not** need the `qmd` entry.)

### 2. The `kb` skill

The canonical skill lives here at [`skills/kb/SKILL.md`](skills/kb/SKILL.md). Install it into a
project by symlinking, so updates here propagate everywhere:

```bash
# Laravel projects that keep skills in .ai/skills with a .claude/skills symlink:
ln -s /path/to/kb/skills/kb /path/to/project/.ai/skills/kb
ln -s ../../.ai/skills/kb   /path/to/project/.claude/skills/kb

# Or, for a project that reads skills straight from .claude/skills:
ln -s /path/to/kb/skills/kb /path/to/project/.claude/skills/kb
```

Match each project's existing skill layout (check a sibling skill: some use `.ai/skills` + a
`.claude/skills` symlink, others use `.claude/skills` directly). Restart the session to pick it up.
If you prefer a self-contained copy over a symlink, `cp -r` the directory instead.

### 3. Auto-reminder hook (recommended)

**Skip this and the install reliably under-performs.** The skill only fires on the agent's own
judgement, and the `kb` MCP tools may be **deferred** behind a tool-search index when a project has
many MCP servers — so an agent can default to `grep`/`Explore` on a "how does X work / what connects
to Y / impact" prompt and never reach the graph (exactly the failure this hook prevents). A
`UserPromptSubmit` hook closes that gap deterministically: it scans the prompt for connection/impact
trigger words and injects a one-line reminder to use the `kb` graph tools first.

Add it in the consuming project (not here — this repo is read-only and isn't a Claude Code project).
A ready-made matcher template ships at [`skills/kb/install/kb-reminder.py`](skills/kb/install/kb-reminder.py)
(EN + RU triggers; extend `TRIGGERS` with your team's languages):

```bash
# 1. copy the matcher template (don't symlink — projects extend its TRIGGERS list)
mkdir -p /path/to/project/.claude/hooks
cp /path/to/kb/skills/kb/install/kb-reminder.py /path/to/project/.claude/hooks/

# 2. wire it in .claude/settings.json (merge — never clobber existing hooks/keys)
```
```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/kb-reminder.py\" 2>/dev/null || true",
        "statusMessage": "Checking for kb-graph relevance" } ] }
    ]
  }
}
```

The script reads the hook JSON on stdin, prints
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<reminder>"}}`
on a trigger hit, and stays silent + exit 0 otherwise so it can never block a prompt. Also add a
**project-guideline line** ("for how-does-X / what-connects / impact questions, use the `kb` skill
before grep, and tell delegated sub-agents to do the same"). See SKILL.md "How to use it well"
points 5–6 for the rationale.

### 4. Verify the install

Run from the project root — every line should report present/YES:

```bash
# 1. MCP server registered and the binary is runnable
python3 -c "import json;print('kb cmd:',json.load(open('.mcp.json'))['mcpServers']['kb']['command'])"
test -x "$(python3 -c "import json;print(json.load(open('.mcp.json'))['mcpServers']['kb']['command'])")" && echo "kb-mcp executable: YES"

# 2. skill reachable (whichever layout the project uses)
test -f .claude/skills/kb/SKILL.md && echo "skill: YES"

# 3. hook present, valid, and actually fires on a sample prompt
test -f .claude/hooks/kb-reminder.py && echo "hook script: present"
jq -e '.hooks.UserPromptSubmit' .claude/settings.json >/dev/null && echo "hook wired: YES"
echo '{"prompt":"how does auth work"}' | python3 .claude/hooks/kb-reminder.py | jq -e .hookSpecificOutput >/dev/null && echo "hook fires: YES"

# 4. qmd is the compatible lineage (kb-mcp's docs_search needs `qmd query`)
qmd query --help >/dev/null 2>&1 && echo "qmd query CLI: YES" || echo "qmd query CLI: NO — wrong qmd lineage"
```

Then **restart the agent session** (or open `/hooks` once) so it loads the new `.mcp.json` server,
the skill, and the freshly-created `settings.json` — the settings watcher only tracks `.claude/` if a
settings file existed there at session start.

## Three surfaces, three consumers

Each surface exists for a different reader, and that decides what it may require:

| Surface | For | Job | Requires |
|---|---|---|---|
| **MCP** (`bin/kb-mcp`, stdio) | agents | search + traverse inside a session | nothing — no port, no auth |
| **HTTP API** (`bin/kb-api`) | scripts, CI, cron | the same search from bash/python | a port; a token off loopback |
| **web** (`web/index.html`) | a human | look at status: the graph, index health | nothing running — opens from `file://` |

MCP and the API are the **same contract**: both dispatch through `kb_core.call_tool()` and publish
`kb_core.TOOLS`, so they cannot drift apart without deleting code. The web surface is not a client of
that contract — it must work when nothing is running, which is precisely when you want to look at it.

```bash
make serve                      # 127.0.0.1:8899, no token needed on loopback
PORT=9000 TOKEN=secret make serve
```

```bash
curl -s localhost:8899/version
curl -s localhost:8899/tools                            # the list MCP publishes
curl -s -X POST localhost:8899/search \
     -d '{"q":"how does auth work","domain":"both"}'
curl -s -X POST localhost:8899/graph/affected -d '{"symbol":"BookingService"}'
curl -s "localhost:8899/doc?path=docs/AUTH.md"
curl -s -X POST localhost:8899/call \
     -d '{"name":"search","arguments":{"q":"deposit","domain":"docs","mode":"lex","limit":5}}'
```

`search` takes `{q, domain: docs|code|both, scope, mode: auto|lex|vec|hybrid, limit, min_score,
expand_aliases, explain}`. `docs_search` and `graph_query` still work and are marked deprecated —
they translate to `search`, so there is one implementation rather than two.

**Binding off loopback requires a token, and the server refuses to start without one.** The point of
this project is that the corpus never leaves the machine; an index published to the network without
auth would be the leak it exists to prevent. For remote use, bind loopback and tunnel over SSH.

## How the projects connect

`bin/link_http.py` reads each Laravel app's `php artisan route:list --json` and matches the
frontend's API call sites against it, injecting `http_request` edges. It recognizes the common
shapes — dotted clients (`api.<verb>('<path>')`), ofetch/fetch wrappers where the verb sits in an
options object (`$api(...)`, `apiFetch(...)`, and a raw `fetch(\`${API_BASE}/…\`)`) — and tolerates
the frontend omitting a base prefix that the base URL supplies (e.g. Laravel's `api/`). This is
what makes `graphify path` traverse from a Vue/React file to the controller that serves its request.

## Design rules that hold this together

Not aspirations — these are the properties the code already has, kept explicit so a future change
(or a future agent) does not quietly trade them away.

- **The corpus is never touched; the index is disposable.** Indexed projects are read-only, every
  artefact under `graphify-out/`, `repos/`, `.docs/` is generated and git-ignored. Delete the whole
  output and `make build` reconstructs it. Nothing here is a source of truth, which is also why this
  project has **no write path** — see the changelog entry for 0.4.0.
- **One artefact, a pipeline of small filters.** Each `bin/*.py` does one transform:
  read `graph.json` → change one thing → write it back → print a single summary line. `bin/kb`
  composes them. Every script has the same shape (`main()`, `argv`, docstring first,
  `raise SystemExit(main())`), so opening an unfamiliar one costs nothing.
- **The artefact contract lives in exactly one module.** `bin/graph.py` owns loading, saving, the
  node-id format (`repo::path::symbol`) and the relation/confidence vocabularies. A relation typo is
  an `AttributeError`, not a silently wrong edge; a format change has one place to update instead of
  eight.
- **Writes are atomic.** `graph.save()` writes a temp file in the same directory and `os.replace()`s
  it, so a crash or Ctrl-C leaves the previous complete graph rather than a truncated one — the
  pipeline has no recovery step, so it must not need one. Save also refuses a value that is not a
  graph.
- **Uncertainty is data, not a vibe.** Every edge carries `confidence`
  (`EXTRACTED | INFERRED | AMBIGUOUS | DERIVED`): AST facts and regex guesses are visibly different,
  and the visualisation draws inferred edges dotted. A heuristic may be wrong; it may not *pretend*.
- **Restraint over cleverness in destructive steps.** `dedupe.py` refuses to merge when a label has
  two real definitions (merging would invent a fact); `declutter.py` prunes only the curated hubs in
  `kb.hubs.txt`, with no degree-based auto-detection, because a high-degree definition-less node is
  usually a real service whose definition was never captured. Both restraints are pinned by tests.
- **Tests sit where the risk is.** The destructive transforms (`dedupe`, `declutter`) and the artefact
  contract (`graph`) are covered, not just the easy pure parser. Stdlib `unittest`, no test deps:
  `make test`.
- **Zero runtime dependencies.** Stdlib Python and bash only — the MCP server speaks JSON-RPC by
  hand, config is `tomllib`. This is a deliberate constraint: the tool has to run on a machine where
  installing packages is not an option.
- **Deployment data is not code.** `kb.projects.toml` and `aliases.toml` are git-ignored with
  committed `.example` twins: the repository is public, the corpus map and the alias table are not.
- **Failures are loud.** A missing route table warns instead of silently dropping the cross-repo half
  of the graph; steps that must run twice (clustering ↔ labeling is a fixpoint through files) say why
  in a comment rather than looking like a copy-paste slip.
- **Setup reports, it does not surprise.** `bin/install` never overwrites an existing file, never
  installs someone else's tool behind your back (it prints the exact command), and is idempotent;
  `--check` writes nothing at all. An installer that violates those is worse than no installer.
- **The interface is a source file, not an artefact.** `web/` holds the shell (markup, styling,
  behaviour) as versioned, diffable, testable files with no build step; the generator emits data only
  (`kb-manifest.js`, `kb-status.js`, per-domain files). Data arrives as `.js` calls rather than
  JSON + fetch because a page opened from `file://` cannot fetch a sibling file — the dashboard has to
  work when nothing is running.
- **Snapshots say when they were taken.** The Status panel is a build-time snapshot, stamped. A stale
  number that admits its age is useful; one that pretends to be live is not.
- **The renderer is local.** `make vendor` fetches vis-network into `web/vendor/` once; the page
  prefers that copy and falls back to a CDN **with a visible banner**. A tool whose promise is "the
  corpus never leaves the machine" should not quietly fetch code from a third party.
- **The interface is English.** Code, comments, commit messages and the web surface are in English —
  it is a public project read by people who did not write the corpus. The indexed content keeps its
  own language; the interface is not the corpus. No i18n layer (it would break the
  zero-dependency rule and let UI strings drift).
- **Documentation explains *why*, at the top of the file it belongs to.** Each script opens with the
  upstream behaviour that makes it necessary — e.g. `dedupe.py` starts from *how* graphify emits
  reference nodes. Docstrings age better than commit messages.

## Roadmap

- [ ] **Extract the read core** (`bin/kb_core.py`) so the stdio MCP adapter and a future HTTP
  adapter dispatch through one function and advertise one schema — identity by construction. Details
  and the generic `/search` shape are in `CHANGELOG.md` under 0.4.0.
- [ ] **Mutation testing for `bin/link_http.py`.** The matcher has unit tests
  (`make test`), but tests can pass while being blind to real regressions. Add a
  mutation pass — flip a regex, swap the GET default to POST, invert an
  `endswith`, drop the shortest-suffix sort — and confirm the suite *fails* on
  each (every mutant killed). Keep it dependency-light (a small stdlib harness +
  `make mutation`) or wire up `mutmut` in a venv.

## Footprint

The indexed projects are **never modified**. Their code is rsynced (code-only) into `repos/`
(git-ignored) for scanning; all outputs live here in this repo.

See `CLAUDE.md` for the agent query workflow.

## License

[GPL-3.0-or-later](LICENSE) — free software: use, study, share and modify it. Copyright (C) 2026 Romans Sereda. Derivative works must also be licensed under the GPL (copyleft).
