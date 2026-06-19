# Code Knowledge Base

A standalone, read-only knowledge layer over a set of related code projects (for example a Laravel
API and a Vue frontend). It builds **one merged code-connection graph** — including cross-repo edges
that link frontend API calls to the Laravel controllers that serve them — and a **vectorized search
index** over the projects' docs. By default everything runs **on-device with no API keys** (the
`none` and `llama` embedding backends); only the optional `other` backend can reach an external
service.

Built on two existing tools: [**graphify**](https://github.com/safishamsi/graphify) (AST code graph)
and [**qmd**](https://github.com/tobi/qmd) (on-device markdown vector search).

## Requirements

These are external dependencies — not vendored. Install them and make sure they're on your `PATH`
before running a build:

| Tool                  | Version (tested) | Used for |
|-----------------------|---|---|
| [**graphify**](https://github.com/safishamsi/graphify) | `>= 0.8.39` | AST code graph (`update`, `merge-graphs`, `cluster-only`, `query`, `path`, `explain`, `affected`). The build and `bin/kb-mcp` shell out to it. |
| **qmd**               | `>= 2.5.3` | On-device markdown vector + BM25 doc search. |
| **python3**           | `>= 3.10` | The enrichment scripts in `bin/` (standard library only — no packages). |
| **rsync**             | any recent | Code-only staging of the indexed projects into `repos/`. |
| **php** + **artisan** | the indexed app's version | Only needed if a project sets `routes` — `bin/kb` runs `php artisan route:list --json` to build cross-repo `http_request` edges. |

By default everything runs **on-device with no API keys** (see the embedding-backend choice under
Configuration). Check what's installed:

```bash
graphify --version && qmd --version && python3 --version
```

## Quick start

```bash
make build      # build the merged graph + qmd doc index
make open       # open the typed/domain visualization (kb-graph.html)
qmd query "how does auth work" -c kb
graphify path "useAuthFlow.js" "AuthMethodsController" --graph graphify-out/graph.json
```

The qmd collection name defaults to `kb`; override it by setting `KB_COLLECTION` before a build.

## What you get

- `graphify-out/graph.json` — merged graph: every node tagged with its `repo`; `http_request`
  edges connect frontend call sites to backend controllers across repos.
- `graphify-out/kb-graph.html` — the **primary** visualization: typed/domain view (shape = type,
  color = domain), hierarchical and lazy-loaded. This is what `make open` opens.
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

## How the projects connect

`bin/link_http.py` reads each Laravel app's `php artisan route:list --json` and matches
`api.<verb>('<path>')` calls in the frontends against it, injecting `http_request` edges. This is
what makes `graphify path` traverse from a Vue file to the controller that serves its request.

## Footprint

The indexed projects are **never modified**. Their code is rsynced (code-only) into `repos/`
(git-ignored) for scanning; all outputs live here in this repo.

See `CLAUDE.md` for the agent query workflow.
