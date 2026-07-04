---
name: kb
description: "Query the cross-repo Code Knowledge Base (the `kb` MCP server) instead of grepping. Use when an agent needs to: (1) understand how a feature works across the indexed repos (e.g. a Laravel API + a Vue frontend), (2) trace a frontend call to the Laravel controller that serves it, (3) find what depends on a symbol before changing it (impact analysis), (4) locate the shortest path between two symbols/files, or (5) search project docs by meaning. Covers architecture questions, 'how does X work', 'what connects to Y', 'what breaks if I change Z'."
metadata:
  author: project
---

# Code Knowledge Base (`kb` MCP)

## Keywords
how does it work, architecture, cross-repo, frontend to controller, who calls, what depends on, impact analysis, blast radius, shortest path, trace request, knowledge base, code graph, docs search, where did we write about

## What this is

The Code Knowledge Base is a **read-only** knowledge layer over a set of related repos — typically a
Laravel API plus one or more Vue frontends. It exposes two layers — on-device and zero-API-key by
default — through the `kb` MCP server (registered in each consuming project's `.mcp.json`):

- **Code graph** (graphify) — one merged AST graph. Every node is tagged with its `repo`, and
  `http_request` edges link a frontend `api.<verb>('<path>')` call site to the Laravel controller
  method that serves it. So traversal crosses repo boundaries: Vue file → route → controller method
  → its own callers.
- **Doc search** (qmd) — hybrid BM25 + vector search over the projects' markdown docs.

## When to activate

- Any "how does X work / how do these connect" question spanning more than one file or repo.
- Before editing a shared symbol: run impact analysis to see the blast radius.
- "Which controller serves this frontend request?" / "where is this endpoint called from?"
- "Where did we document X?" → doc search.

Prefer the graph over `grep` for connection/architecture questions — it is cheaper and more precise
because it follows real edges (calls, eloquent, fk, sql, http_request) rather than text matches.

## Tools

| Tool | Use for | Arg |
|---|---|---|
| `graph_query` | Open-ended "how does X work / what connects to Y". Returns a scoped subgraph. | `question` |
| `graph_explain` | A specific symbol: its node, neighbours, relation types. | `symbol` (exact label) |
| `graph_affected` | Reverse impact — what depends on / breaks if you change a symbol. | `symbol` (exact label) |
| `graph_path` | Shortest path between two symbols/files (crosses repos via `http_request`). | `from`, `to` |
| `docs_search` | Find docs by meaning. Returns snippets + paths. | `query` |
| `docs_get` | Fetch a full doc by the path returned from `docs_search`. | `path` |

## How to use it well

1. **Start broad, then drill down.** `graph_explain` / `graph_affected` / `graph_path` match a
   node by substring, so a vague term (e.g. `User`) is **ambiguous** and fails with
   `No unique node match` — but the tool then **lists the matching exact labels** (model/file/
   method, ranked, with repo + path). Pick the precise one from that list and re-run, or run
   `graph_query` first to discover it (e.g. a specific controller or `.index()` method).
2. **Labels look like the graph emits them.** Methods are `.methodName()`, classes/files are bare
   names, frontend nodes carry a `src=...vue` path. Copy labels verbatim from prior tool output.
3. **Results are budget-truncated.** `graph_query` caps output (~1800 tokens) and tells you how to
   narrow (e.g. `context_filter=['call']`). If truncated, ask a tighter question rather than
   re-running the same one.
4. **Docs vs code.** Connection/architecture/impact → graph tools. "Where did we write about X" →
   `docs_search` → `docs_get` the most relevant path, then answer **with the citation**.
5. **Reach for the graph BEFORE grep — and before delegating.** A spawned `Explore`/general
   sub-agent defaults to text search and will *not* use these tools unless told. If you delegate a
   "how does X work" investigation, either run the graph query yourself first, or instruct the
   sub-agent explicitly to use the `kb` graph tools. Don't let a delegation silently downgrade the
   answer to grep.
6. **If the tools aren't visible, load them.** In a project with many MCP servers the host may
   defer the `kb` tools behind a tool-search index — they won't appear in the live tool list and a
   direct call fails until their schemas are loaded. Pull them in first via the host's tool-search
   (e.g. a query like
   `select:mcp__kb__graph_query,mcp__kb__graph_explain,mcp__kb__graph_affected,mcp__kb__graph_path,mcp__kb__docs_search`),
   then call them normally. Activating this skill is the cue to do that — not to fall back to grep.

## Known caveats

- **`docs_search` (qmd) has a one-time cold start** (loading the embedding model). The server
  warms it on startup and allows ~150s for the first call, and on timeout it auto-falls back to
  keyword (BM25) results flagged as such. Warm calls return in ~2s. For code questions the graph
  tools are sub-second; reach for docs only for prose/spec lookups.
- `graph_affected` / `graph_explain` / `graph_path` reject **ambiguous** symbols, but now return
  the candidate exact labels to choose from — pick one and re-run (no need to guess).
- The KB is a **snapshot** rsynced from the repos; it is not live. If a result looks stale, the KB
  may need a rebuild (`make update` in the KB checkout) — verify against the actual source file
  before acting on a surprising result.
- The graph and docs are read-only artifacts; never write to the KB checkout from a project task.

## CLI fallback

If the MCP server is unavailable, the same data is reachable via the CLIs it wraps (run from the
KB checkout):

```bash
graphify query   "<question>"  --graph graphify-out/graph.json
graphify explain "<symbol>"    --graph graphify-out/graph.json
graphify affected "<symbol>"   --graph graphify-out/graph.json
graphify path    "<A>" "<B>"   --graph graphify-out/graph.json
qmd query "<q>" -c kb   # then: qmd get <path>
```

Broad architecture overview without a query: read `graphify-out/GRAPH_REPORT.md` in the KB checkout.
