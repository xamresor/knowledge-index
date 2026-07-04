# Code Knowledge Base — agent operating instructions

This repo is a **read-only knowledge layer** over a set of related code projects. It never modifies
them. Two layers, on-device / zero-API-key by default (only the optional `other` qmd embedding
backend can reach an external service):

| Layer | Tool | Artifact |
|---|---|---|
| **Code connection graph** (incl. cross-repo API routes) | **graphify** | `graphify-out/graph.json` + `graph.html` + `GRAPH_REPORT.md` |
| **Typed / domain visualization** | custom (`bin/render_viz.py`) | `graphify-out/kb-graph.html` |
| **Vectorized doc search** | **qmd** (MCP: `qmd`) | collection `kb` in `.qmd/` |

**`kb-graph.html`** is the primary visual: node **shape = type** (class=●, method=▲, db_table=■,
template=★, controller=⬡, model=cylinder, interface/trait=◆, enum=▽) and node **color = domain**
(e.g. `Order`, `Customer`, `Product`, `Invoice`, …). Domains group code across layers (a domain holds
its controllers + models + tables + services + frontend callers). It is **hierarchical + lazy-loaded with edge rerouting**: the shell (`kb-graph.html`, ~120 KB)
shows only the **domain super-nodes**; each domain's members (typed shapes) live in
`kb-graph-data/<domain>.js` and load **on demand**. **Double-click a domain** to expand — its
super-node is **removed** and every cross-domain edge **reroutes to the real member nodes** (a
member links to another domain's super-node while that domain is collapsed, or member↔member once
both are expanded, so you see true connections). **Double-click a member** to collapse its domain
(the super-node returns). Cross-domain edges load once from `kb-graph-data/_cross.js`; search uses
`_index.js` (loaded on first query). All via `<script>` injection — works on `file://`, no server.

Projects indexed are defined in `kb.projects.toml` — typically a Laravel API plus one or more Vue
frontends. The merged graph carries a `repo` tag on every node and **`http_request` edges** linking
a frontend call site to the Laravel **controller method** that serves it (e.g. a `.vue` file
`--http_request-->` a controller method) — so traversal continues from the Vue file through the
route into the method's own usages. Communities are named by **namespace/domain** (e.g.
`api · Models`, `webclient · stores`), derived deterministically — no LLM, no API cost.

## MCP server (how project agents reach the KB)

`bin/kb-mcp` is a zero-dependency stdio MCP server exposing the graph + docs to any agent. It is
registered as the `kb` server in each consuming project's `.mcp.json`, so those project agents query
the shared KB directly (restart the session to load it). Tools:
`graph_query`, `graph_path`, `graph_explain`, `graph_affected` (over `graphify-out/graph.json`),
and `docs_search`, `docs_get` (qmd `kb` collection). It wraps the `graphify`/`qmd` CLIs.

## How to answer questions

- **Code / how things connect** → query the graph (cheaper + more precise than grep):
  - `graphify query "<question>" --graph graphify-out/graph.json`
  - `graphify path "<A>" "<B>" --graph graphify-out/graph.json` — shortest path between two symbols
    (works across repos via `http_request` edges).
  - `graphify explain "<symbol>" --graph graphify-out/graph.json` — a node and its neighbors.
  - `graphify affected "<symbol>" --graph graphify-out/graph.json` — reverse impact.
  - Broad architecture → read `graphify-out/GRAPH_REPORT.md` or open `graphify-out/graph.html`.
- **Docs / "where did we write about X"** → qmd MCP tools (`mcp__qmd__query` → `get` → answer with
  citation), or CLI: `qmd query "<q>" -c kb` → `qmd get <path>`. Search by meaning, then
  read the full doc before answering.

## Maintenance

- `make update` — refresh the graph (AST extraction) + re-embed docs. No API cost with the `none`/
  `llama` backends; the `other` backend may cost depending on what you point it at. Run after the
  projects change.
- `make graph` / `make kb` — rebuild one layer.
- `make label` — (re)name communities by namespace/domain (deterministic, zero API cost).
- `make status` — graph diagnostics + qmd index health.
- Add/remove a project by editing `kb.projects.toml` (no symlinks; explicit paths).

## How it's built (see `bin/kb`)

Per project: code is rsynced (code-only) into `repos/<name>/`, graphified (AST, no LLM), then all
graphs are `merge-graphs`'d into `graphify-out/graph.json`. `bin/link_http.py` injects the
cross-repo `http_request` edges from each Laravel app's `route:list` + frontend `api.<verb>()`
call sites. `bin/dedupe.py` canonicalizes duplicate symbol nodes — graphify emits a separate reference node
per usage site, so it merges all same-label class-like nodes (per repo) into the one definition
(redirecting edges) so a class connects to its real users instead of leaving throwaway copies
floating. `bin/declutter.py` then strips framework "god nodes" (Laravel base types/traits/helpers
listed in `kb.hubs.txt` — `Model`, `FormRequest`, `.log()`, …) — **curated-only**, so consolidated
domain services are never pruned. Domain classes (those with their own members) are always kept.
`cluster-only` builds communities + `graph.html`, and `bin/label_namespaces.py` names them.
`bin/enrich.py` adds per-node **type** + **domain**, synthesizes **db_table** nodes from model
`$table` (`defines_table` edges) and **domain anchor** nodes (`in_domain`). `bin/link_data.py`
then adds the semantic data layer graphify's AST misses: **`eloquent`** model→model relations
(belongsTo/hasMany…), **`fk`** table→table (parsed from migration foreign keys), and **`sql`**
code→table (`DB::table()`/`->from()`). The function-usage graph (method→method `calls`) and
use/import/trait edges come from graphify directly. `bin/render_viz.py` emits `kb-graph.html`,
coloring edges by relation (http_request=red, eloquent=purple, fk=orange, sql=teal,
defines_table=green).
Tune framework pruning via `kb.hubs.txt`; domain folding threshold is `MIN_DOMAIN` in `enrich.py`.
