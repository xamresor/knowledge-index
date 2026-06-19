# SmartMonitor Knowledge Base

A standalone, read-only knowledge layer over the SmartMonitor projects (`api`, `webclient`,
`bo`/webadmin). It builds **one merged code-connection graph** — including cross-repo edges that
link frontend API calls to the Laravel controllers that serve them — and a **vectorized search
index** over the projects' docs. Everything runs **on-device with no API keys**.

Built on two existing tools: [**graphify**](https://github.com/) (AST code graph) and
[**qmd**](https://github.com/tobi/qmd) (on-device markdown vector search).

## Quick start

```bash
make build      # build the merged graph + qmd doc index
make open       # open the interactive graph.html
qmd query "how does auth work" -c smartmonitor
graphify path "useAuthFlow.js" "AuthMethodsController" --graph graphify-out/graph.json
```

## What you get

- `graphify-out/graph.json` — merged graph: every node tagged with its `repo`; `http_request`
  edges connect frontend call sites to backend controllers across repos.
- `graphify-out/graph.html` — full interactive visualization.
- `graphify-out/GRAPH_REPORT.md` — human-readable architecture report with communities.
- qmd `smartmonitor` collection (`.qmd/`) — hybrid BM25 + vector search over all docs.

## Configuration

Edit `kb.projects.toml` to add/remove a project or change which paths are scanned/indexed.
No symlinks — paths are explicit. `.graphifyignore` controls what graphify excludes.

## How the projects connect

`bin/link_http.py` reads each Laravel app's `php artisan route:list --json` and matches
`api.<verb>('<path>')` calls in the frontends against it, injecting `http_request` edges. This is
what makes `graphify path` traverse from a Vue file to the controller that serves its request.

## Footprint

The three projects are **never modified**. Their code is rsynced (code-only) into `repos/`
(git-ignored) for scanning; all outputs live here in `/var/www/kb`.

See `CLAUDE.md` for the agent query workflow.
