<img width="1469" height="842" alt="Screenshot from 2026-07-09 22-06-41" src="https://github.com/user-attachments/assets/88c10be3-36cc-4173-b298-07163019cf17" />
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

## How the projects connect

`bin/link_http.py` reads each Laravel app's `php artisan route:list --json` and matches
`api.<verb>('<path>')` calls in the frontends against it, injecting `http_request` edges. This is
what makes `graphify path` traverse from a Vue file to the controller that serves its request.

## Footprint

The indexed projects are **never modified**. Their code is rsynced (code-only) into `repos/`
(git-ignored) for scanning; all outputs live here in this repo.

See `CLAUDE.md` for the agent query workflow.

## License

[GPL-3.0-or-later](LICENSE) — free software: use, study, share and modify it. Copyright (C) 2026 Romans Sereda. Derivative works must also be licensed under the GPL (copyleft).
