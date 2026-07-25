# Changelog

All notable changes to **knowledge-index** are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/) with `0.x` semantics: **breaking changes bump the minor**
until the MCP tool contract stabilises at `1.0.0`.

The single source of truth for the current version is the `VERSION` file. It is reported by
`bin/kb --version` and by the MCP server in `initialize` → `serverInfo.version`.

> **Not the same axis:** a consumer may carry its own component-level compatibility field (e.g. a
> plugin/cartridge manifest version). Those numbers and this repo's version are independent and will
> diverge — don't sync them.

## [Unreleased]

- **Three surfaces, split by consumer** (framing fixed by Romans 2026-07-26 — the reason each exists,
  which decides what each may require):

  | Surface | Consumer | Job | Requires |
  |---|---|---|---|
  | **MCP** (stdio) | agents | search + traverse inside a session | the core; no port, no auth |
  | **HTTP API** | scripts, CI, cron | the same search, callable from bash/python | the core + a port + a token |
  | **web** | a human | *look at status*: the graph, index health, counts | generated data only — **no running process** |

  The consequence that corrects an earlier plan: **the web page is not a third client of the search
  API.** Its job is status, so it must open from `file://` with nothing running. Making it depend on
  the API would break the one property that makes it useful — that it works when nothing works. If
  the API happens to be up, the page may light up extra panels (progressive enhancement), never as a
  requirement.

- **0.5.0 — the web surface moves out of the generated directory.** Today the whole UI is a
  `TEMPLATE` string inside `bin/render_viz.py`, rendered into gitignored `graphify-out/kb-graph.html`:
  an interface that cannot be diffed, linted or tested. Extract it to a versioned `web/`
  (`index.html` + `app.js` + `style.css`, no build step, zero dependencies) and replace the four
  string placeholders with **one generated `manifest.js`** (`kbManifest({domains, fileMap, counts,
  legend, generated_at, versions})`).
  - **The data split already exists and stays:** `render_viz.py` writes 210 per-domain `.js` files
    (~5 MB) that call `kbRecv(domain, {nodes, edges})`, loaded on demand. `.js` rather than JSON+fetch
    is deliberate: a page opened from `file://` cannot fetch a sibling file but can load a sibling
    `<script>`.
  - Panels, all fed by build-time snapshots: the graph, index health (graph diagnostics + `qmd
    status`), the alias table, search-quality numbers from the golden set. Each snapshot shows **when
    it was taken** — a stale number that admits its age is honest; one that pretends to be live is not.
  - Requirement carried over from the sibling project's read-only views: values reach the DOM through
    `textContent`, never `innerHTML`. This page renders content from other people's repositories.
  - **The front end is English-only.** It ships with a public repository and is read by people who did
    not write the corpus, so the interface is in the language the project is published in. No
    localisation layer: one language in the source means UI strings cannot drift out of sync, and it
    keeps the zero-dependency rule (no i18n framework, no message catalogues). Indexed **content**
    keeps whatever language it is written in — the interface is not the corpus.
- **0.6.0** — rest of the query layer: stopword handling before the lexical leg, and routing between
  the lexical and vector legs by question shape (measured: the lexical leg wins on pinpoint questions,
  the hybrid on multi-page ones).
- **Backlog — entity registry with an owner flag.** The alias table shipped in 0.3.0 restores
  *presence* but not *rank*: a page can now be found by its old name yet still sit at position 6–10,
  because nothing distinguishes "this page owns the subject" from "this page mentions it in passing"
  (including incidental hits inside paths). The form that fixes it is
  `entities(id, prefLabel, altLabels[])` + `mentions(entity_id, doc, role)` with
  `role ∈ {owner, mention, incidental}`, built at ingest. Worth it when client corpora arrive, not for
  the few points it would add here.
- Auto-discovery of alias candidates (git renames via `--diff-filter=R`, edited wikilinks, fuzzy name
  similarity) — **review-gated**, never automatic, or determinism is gone.
- **0.7.0** — remote canon: the index lives on a server, local agents reach it over MCP/SSH.
- **1.0.0** — when the MCP tool contract stops changing *and* a second independent consumer exists.

## [0.4.0] — 2026-07-26

**One core, two machine transports.** The MCP server and a new HTTP API are the same contract:
both dispatch through `kb_core.call_tool()` and publish `kb_core.TOOLS`, so drift between them would
require deleting code rather than forgetting to copy it. The web surface is deliberately **not** part
of this contract — see 0.5.0 in Unreleased.

### Added
- **`bin/kb_core.py` — every operation, once, transport-free.** Schemas, shallow validation, process
  plumbing, label suggestions and dispatch moved out of the MCP server.
- **A generic `search`** replacing two narrow tools:
  `{q, domain: docs|code|both, scope, mode: auto|lex|vec|hybrid, limit, min_score, expand_aliases,
  explain}`. `domain` routes to qmd, to graphify, or to both with labelled sections; `mode` maps onto
  qmd's three entry points; `scope` picks a collection other than the default.
- **`bin/kb-api` + `bin/kb_api.py` — the HTTP adapter for scripts.** `GET /health`, `GET /version`,
  `GET /tools`, `POST /call {name, arguments}` (the MCP shape verbatim), plus ergonomic
  `POST /search`, `POST /graph/{path,explain,affected}`, `GET /doc?path=`. Routing is a **pure
  function** (`dispatch`), so the surface is tested without opening a socket. `make serve` runs it.
- **Security policy, enforced at startup:** default bind `127.0.0.1`; a token
  (`--token` / `KB_API_TOKEN`) is **required** for any non-loopback bind and the server **refuses to
  start** without one — verified live and pinned by tests. Requests off loopback are rejected even if
  that check were bypassed. Remote use means loopback + an SSH tunnel, not a wider bind.
- **`version` operation / `GET /version`** returning `{repo, api_contract, collection, aliases}`:
  the repo release and the surface's contract number are separate axes and are never merged.
- **Validation without a dependency:** required keys, primitive types (a `bool` is not an `integer`
  on the wire), enum membership — read from the same schema dicts the surfaces publish. An unknown
  argument is an error, because a silently ignored `mod=lex` looks like the mode did nothing.
- Tests 62 → **104**: schema/validation, `search` routing by domain, deprecated shims delegating
  rather than duplicating, alias expansion on/off per call, the timeout fallback firing only in
  `auto` mode, and the whole bind/token policy.

### Changed
- `bin/kb-mcp` is now transport only (~60 lines): JSON-RPC framing, `initialize` (now also reporting
  `apiContract`), `tools/list`, `tools/call`. A bad call comes back as tool text an agent can read and
  retry, instead of a protocol error it cannot see.
- **`docs_search` and `graph_query` are deprecated but working** — they translate to `search`, so
  there is one implementation. Callers written against 0.3.x keep working unchanged.
- README gained "Three surfaces, three consumers" with the API reference and the token rule.

## [0.3.1] — 2026-07-26

Refactor and tests, no behaviour change. Cleanup **before** the 0.4.0 core extraction, because the
weaknesses below would have been copied into the new layer.

### Added
- **`bin/graph.py` — the artefact contract in one module:** atomic load/save, the node-id format
  (`repo::path::symbol`) with parse/build, and `Relation` / `Confidence` vocabularies as constants.
  Previously eight scripts re-derived all of this by hand, so a graphify format change broke eight
  files silently and a relation typo produced a silently wrong edge instead of an error.
- **Atomic writes.** `graph.save()` writes a temp file in the same directory, `fsync`s and
  `os.replace()`s it; a failed serialization leaves the previous complete graph and no debris. The
  pipeline rewrites one shared artefact in place and has no recovery step, so a truncated
  `graph.json` was a real failure mode, not a theoretical one. `load`/`save` also refuse a value that
  is not a graph.
- **Tests where the risk is** — 26 → **62**. New: `dedupe` (merge into the single definition, collapse
  when there is none, **leave ambiguous labels untouched**, drop self-loops and duplicate edges, keep
  repos isolated), `declutter` (curated hubs only; a real service with the same degree survives;
  exact label match, so `Model` never eats `ModelFactory`), `graph` (atomicity, shape guards, id
  round-trip incl. namespaced symbols), `alias_expand` (directional, whole-token, path segments,
  partial table degrades instead of breaking). Previously only the pure parser was covered.
- **`tests/_kbtest.py`** — one bootstrap for importing `bin/` scripts, replacing per-file importlib
  boilerplate.
- **README §"Design rules that hold this together"** — the properties the code already has, written
  down so a future change does not trade them away silently.

### Changed
- The eight enrichers now use `graph.load` / `graph.save` / `graph.read_json` / `graph.write_json`
  instead of `json.dump(g, open(path, "w"))`; local variables no longer shadow the module.
- **Silent failure removed from the build:** a failing `php artisan route:list` now warns loudly and
  removes the stale route file, instead of `|| true` quietly dropping the cross-repo half of the graph.
- The clustering ↔ labeling double pass is documented as what it is — a **fixpoint through files**
  (`cluster-only` can renumber community ids, the labels file is keyed by them) — rather than looking
  like a redundant call.
- `bin/kb-mcp` imports its sibling module directly; the `sys.path` insert was unnecessary (a directly
  executed script already has its own directory on `sys.path[0]`).

## [0.3.0] — 2026-07-25

### Added
- **Alias library — directional query expansion** (`bin/alias_expand.py`, wired into the MCP
  `docs_search`). When a query contains an old name, the canonical term is appended before the query
  reaches the index; the original wording is preserved, and the expansion is echoed in the result so
  it is never silent.
  - **Deployment data, not code:** the table lives in a gitignored `aliases.toml`
    (`aliases.toml.example` is committed), exactly like `kb.projects.toml` — it names private
    projects, products and people. A missing file means the feature is simply off.
  - **Directional on purpose (old → canonical).** A symmetric expansion that dumps every spelling
    into the query pulls the *meta* layer to rank 1 — changelogs, planning notes and logs are the
    documents that contain all spellings at once. Measured and rejected.
  - **Matching happens on raw query text, before stemming.** FTS5's `porter unicode61` can collapse a
    brand into an ordinary word (a product named `Bookinger` stems to `booking`), which turns an alias query into a stopword
    query; matching the raw string keeps the alias an alias. Whole-token match, tolerant of path
    separators, so an old path segment (`/srv/oldname`) triggers too.
  - **Measured on a 27-question golden set** (lexical leg): alias-type recall **0.50 → 0.70**, overall
    recall **0.735 → 0.772**, MRR **0.526 → 0.536** — i.e. no rank regression, which was the risk.
    Honest limit: the two questions it rescues land at rank 6–10 (presence restored, rank not), and
    three of five alias questions already worked without it.

### Changed
- Roadmap reordered: the alias library became `0.3.0`; the write path moves to `0.4.0`.

## [0.2.0] — 2026-07-25

First tagged release. State of the tool as it is actually used in two private deployments (a
personal markdown wiki and a codebase indexing itself), not a rewrite.

### Added
- **MCP server** `bin/kb-mcp` (stdio, zero Python dependencies) exposing six tools:
  `graph_query`, `graph_path`, `graph_explain`, `graph_affected`, `docs_search`, `docs_get`.
- **Cross-repo graph edges**: frontend call sites matched to backend controllers
  (`calls` → `serves`), incl. Next/React and Nuxt/ofetch `$api()` matchers, tolerant of a Laravel
  `api/` prefix.
- **`confidence` on edges** — `EXTRACTED | INFERRED | AMBIGUOUS | DERIVED`, so regex-derived edges
  no longer masquerade as AST facts; rendered dotted in the visualisation.
- **rationale nodes** — `NOTE`/`WHY`/`HACK`/`SECURITY`/`RATIONALE` comments become `rationale`
  nodes with `explains` edges (`bin/link_rationale.py`, capped at 8 per file).
- **Doc half**: aggregated docs staged into one qmd collection (`kb` by default, `KB_COLLECTION`
  to override) with a selectable qmd backend (`bin/kb qmd-backend`).
- **Namespace labelling** and clustered graph rendering (`bin/label_namespaces.py`,
  `bin/render_viz.py`).
- **`bin/kb --version`** — prints the repo version plus the resolved `graphify` and `qmd` versions.
- Unit tests for the HTTP matcher, including `{id?}` query-splitting in `norm_path`.

### Changed
- Repository renamed `knowledge-base` → `knowledge-index` (2026-07-05). Old name still appears in
  historical notes; there is no compatibility shim.
- MCP `serverInfo.version` now reports the real version from `VERSION` (it previously reported a
  hardcoded `1.0`, which overstated stability).

### Requirements
- **`graphify >= 0.9.25`** (`Graphify-Labs/graphify`) — the code-graph half.
- **`qmd`** (verified with 2.5.3) — the doc-search half; embeddings run on-device.
- `python3`, `bash`. All generated artefacts (`graphify-out/`, `repos/`, `.docs/`,
  `kb.projects.toml`) are gitignored — a checkout builds them, it does not carry them.

### Known limits
- `qmd` filters by **collection only**; an arbitrary doc-id pre-filter is not supported, so
  fine-grained scoping needs one collection per scope (or an upstream patch).
- The graph covers the languages `graphify` parses; Elixir projects are out of scope.

## [0.1.0] — untagged (pre-2026-07-05)

Retroactive label for the pre-MCP era: a single-repo graphify wrapper under the old name
`knowledge-base`. No tag exists for it; recorded here so the numbering means something.
