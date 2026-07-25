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

- **0.4.0 — one contract, two transports** (design fixed 2026-07-25). Not "add a write tool to MCP":
  the HTTP API must be **identical to the MCP surface**, with a single generic search entry point.
  - **Extract the core.** `call_tool(name, args)` and the `TOOLS` schema list already are the core;
    `main()`/`send()` are just stdio JSON-RPC. Move the core to `bin/kb_core.py`; `bin/kb-mcp`
    becomes the stdio adapter and `bin/kb-api` the HTTP one. Identity of the two surfaces is then
    **structural** — both dispatch through the same function and advertise the same schemas —
    rather than a promise to keep them in sync by hand.
  - **One generic search.** `POST /search` with a body, and an MCP tool `search` generated from the
    same JSON Schema:
    `{q, domain: "docs"|"code"|"both", scope?, mode?: "auto"|"lex"|"vec"|"hybrid", limit?,
    min_score?, expand_aliases?: true, explain?: false}`. The core routes by `domain` (qmd for docs,
    graphify for code). `docs_search` / `graph_query` stay as thin deprecated aliases — same shim
    pattern used elsewhere, so existing callers do not break.
  - **Non-search graph operations stay separate** (`/graph/path`, `/graph/explain`,
    `/graph/affected`): they are traversals, not queries, and squeezing them into `/search` would
    make one endpoint mean four things.
  - **No write path — read-only by design** (decided 2026-07-25, correcting an earlier plan to put
    `docs_put` here). This project owns an **index**, not a corpus: it has nowhere of its own to store
    a document. Accepting writes would mean either writing into someone else's repository, or keeping
    a second copy of the text — and the second copy would quietly become a competing source of truth,
    which is exactly what the "files are the source of truth, the index is a rebuildable projection"
    rule exists to prevent. Writes belong to whoever owns the corpus; this project reads it and can be
    thrown away and rebuilt at any time.
  - **Scopes** become a first-class parameter of the unified surface (one collection per ownership
    boundary), rather than an implicit collection name in the environment.
  - **`GET /version`** returns `{repo, api_contract}` — the repo semver and the surface's own
    contract number, kept separate on purpose (the same "don't merge version axes" lesson learned in
    the sibling project).
  - ⚠️ **Security decision that comes with opening a port:** default bind is `127.0.0.1`, a token is
    **required** for any non-loopback bind, and the server refuses to start on `0.0.0.0` without
    one. A knowledge index whose whole value proposition is "nothing leaves the box" must not become
    the thing that leaves the box. Remote access, if wanted, goes through the `0.6.0` remote-canon
    design (SSH), not by binding wide.
  - **Zero-dependency constraint kept:** request validation reads the same schema dicts (types +
    required), no `jsonschema` dependency added.
  - Consequence of dropping the write path: nothing here depends on a block-anchor format any more,
    so `0.4.0` is unblocked — anchors are a corpus-side question for whoever implements the writer.
- **0.5.0** — rest of the query layer: stopword handling before the lexical leg, and routing between
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
- **0.5.0** — remote canon: the index lives on a server, local agents reach it over MCP/SSH.
- **1.0.0** — when the MCP tool contract stops changing *and* a second independent consumer exists.

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
