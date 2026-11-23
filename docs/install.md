# Install and configure

The short path is in the [README](../README.md#quick-start). This page has the details: the requirements, what a build produces, the configuration, and how to wire the KB into each project's agent.

Two ways to install: `uv tool install knowledge-index` for one person on one machine (this page), or the
Docker image for a shared server ([Docker](docker.md)).

## Requirements

These are external dependencies — not vendored. Install them and make sure they're on your `PATH`
before running a build:

| Tool                  | Version (tested) | Used for |
|-----------------------|---|---|
| [**graphify**](https://github.com/Graphify-Labs/graphify) | `>= 0.9.25` | AST code graph (`update`, `merge-graphs`, `cluster-only`, `query`, `path`, `explain`, `affected`). The build and `bin/kb-mcp` shell out to it. |
| **qmd**               | `>= 2.5.3` (the `query`/`search`/`vsearch` CLI) | On-device markdown vector + BM25 doc search. **Must be the modern lineage**: `bin/kb-mcp` and the docs use `qmd query`. The older `qsearch` lineage (e.g. 0.3.x) has an incompatible command set and will break doc search — see the note below. |
| **python3**           | `>= 3.11` | The scripts in `bin/` — standard library only, no packages. 3.11 is the floor because the config and alias table are read with `tomllib`. |
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
> `query`; the docs half of `search` will fail against it. If you share one KB across several
> machines, **install the same qmd lineage on all of them** — a `query`-based checkout and a
> `qsearch`-based checkout cannot both work from the same `bin/kb-mcp`.

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
which paths are scanned/indexed. No symlinks — paths are explicit. The staging copy (`bin/kb`) decides
what graphify sees; the list is in [configuration](configuration.md). The file is real TOML.
A missing or empty code folder, or two code folders with the same folder name, make the build exit
non-zero; the project keeps its previous graph until the folder is back.

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

Restart the agent session to load it. It exposes `search`, `graph_routes`, `graph_path`, `graph_explain`,
`graph_affected`, `docs_get`, `contracts_search`, `connections`, `problems`, `batch`, `run_code`, `version`.

`graph_routes` gives several ways from one place to another with the HTTP routes on the way
(`from: "shop-web:OrdersView.vue", to: "api:Order"`). Every tool but `version` takes `page` and
`page_length`; a long answer ends with a `cursor` that serves its next pages for 10 minutes without
running the query again.

`batch` runs up to 10 calls in one request. `run_code` runs a short JavaScript program that calls the tools
through `kb(tool, args)` and returns only what it needs (a filter over a long answer, a lookup that needs
another's result). The program runs in QuickJS compiled to WebAssembly, inside a Node process started with
the permission model (`--permission`, read access to `sandbox/` only): no network, no files, no child
processes, at most 30 tool calls and 20 s. Install it once with `npm ci` in `sandbox/`; without it the
tool answers that it is not installed. (This repo's own `.mcp.json` only registers `qmd`,
which `kb-mcp` calls internally — projects do **not** need the `qmd` entry.)

### 2. The `kb` skill

The canonical skill lives here at [`skills/kb/SKILL.md`](../skills/kb/SKILL.md). Install it into a
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

A second skill, [`skills/doc-organization/SKILL.md`](../skills/doc-organization/SKILL.md), is the checklist
version of [`docs/documentation-guide.md`](documentation-guide.md): agents follow it when they write,
move or tidy a project's docs, so new docs land where the KB links them to the code. Install it the same
way, or once for every project in `~/.claude/skills/doc-organization`.

### 3. Auto-reminder hook (recommended)

**Skip this and the install reliably under-performs.** The skill only fires on the agent's own
judgement, and the `kb` MCP tools may be **deferred** behind a tool-search index when a project has
many MCP servers — so an agent can default to `grep`/`Explore` on a "how does X work / what connects
to Y / impact" prompt and never reach the graph (exactly the failure this hook prevents). A
`UserPromptSubmit` hook closes that gap deterministically: it scans the prompt for connection/impact
trigger words and injects a one-line reminder to use the `kb` graph tools first.

Add it in the consuming project (not here — this repo is read-only and isn't a Claude Code project).
A ready-made matcher template ships at [`skills/kb/install/kb-reminder.py`](../skills/kb/install/kb-reminder.py)
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

# 4. qmd is the compatible lineage (the docs half of `search` needs `qmd query`)
qmd query --help >/dev/null 2>&1 && echo "qmd query CLI: YES" || echo "qmd query CLI: NO — wrong qmd lineage"
```

Then **restart the agent session** (or open `/hooks` once) so it loads the new `.mcp.json` server,
the skill, and the freshly-created `settings.json` — the settings watcher only tracks `.claude/` if a
settings file existed there at session start.

## The dashboard: how it is made and how to rebuild it

The dashboard is two halves that meet in the browser:

| Half | Files | In git? | Made by |
|---|---|---|---|
| **Shell** — markup, styling, behaviour | `web/index.html`, `web/app.js`, `web/style.css` | yes | hand-written |
| **Data** — the graph split per domain | `graphify-out/kb-manifest.js`, `kb-status.js`, `kb-graph-data/*.js` | **no** | `bin/render_viz.py`, last step of `make build` |
| **Renderer** — vis-network | `web/vendor/vis-network.min.js` | no | `make vendor` (CDN fallback with a banner if absent) |

The data stays out of git on purpose: it is a map of the indexed projects' code, and those projects
are usually private. Everyone rebuilds it from their own checkouts:

```bash
cp kb.projects.toml.example kb.projects.toml   # once: point it at your repos
make vendor                                    # once: offline renderer
make build                                     # graph + docs; render_viz.py writes the data
make open                                      # open web/index.html
```

**In the browser** the sidebar has three inputs and a domain list:

| Control | Does | Needs |
|---|---|---|
| **Filter** | finds nodes by name, click to jump | nothing — works from `file://` |
| **graphify query** | "how does X work" → a scoped subgraph as text | `make serve` |
| **qmd docs** | search the indexed docs by meaning | `make serve` (first query loads the model, ~1 min) |
| **Domains** | tick to show or hide a domain; click a name to expand it | nothing |

For the two queries run `make serve` and open <http://127.0.0.1:8899/>: the API serves the dashboard
itself, so the page and `/search` share one origin and no CORS header is ever sent (one would let any
open website read the index from 127.0.0.1). Opened as a file, those two inputs are disabled and say so.

Only the graph changed? `make graph && make open` is enough — the docs index is not involved.
`bin/render_viz.py graphify-out/graph.json` re-splits an existing graph without rebuilding it.

How the split works: the shell opens with only one node per domain (`kb-manifest.js`). A domain's
members load from `kb-graph-data/<domain>.js` when you double-click it; cross-domain edges come from
`_cross.js`, search from `_index.js`. Every data file is a `<script>` that calls a function in
`app.js` (`kbManifest(…)`, `kbRecv(…)`), which is how a page on `file://` reads its siblings.
