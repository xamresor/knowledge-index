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

- **Per-language plugin migration, steps 2–3** (the risky half, gated on a measurement): `link_data`,
  `enrich` and `link_rationale` have **no tests**, so moving ~700 lines of framework regexes into
  `bin/lang/php_laravel.py`, `js_ts.py` and `sql.py` needs tests first and an edge-count-by-relation
  comparison before/after — otherwise "refactoring" silently loses edges and nobody notices for a month.
- **Tables** are a *retrieval* problem, not a format one, and are still open: markdown tables chunk
  badly (a row separated from its header is close to meaningless), which is a chunking fix — repeat the
  header per row-group, or project rows into records. Measured, unfixed. Not the parsers' job.

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

- ~~**remote canon**: the index lives on a server, local agents reach it over MCP/SSH.~~
  **Deferred, and removed from the roadmap as a feature** (2026-07-27). Since 0.4.0 and 0.7.0 there is
  nothing left to build: install the tool on the box (`uv tool install`), point `KB_HOME` at a data
  directory, and a client reaches it either as `ssh box /abs/path/to/kb-mcp` or through an SSH tunnel to
  a loopback-bound `kb-api`. What remains is a *recipe* plus one prerequisite that lives in the corpus
  repo, not here: the ingest pipeline still assumes a local `qmd update && qmd embed`, and that step
  would lie the moment the index moved. Findings worth keeping regardless: **SQLite over a network
  mount is unsafe** (unreliable locks, page-at-a-time latency) while **plain files over sshfs are
  fine** (the dashboard is just files); embedding is **incremental by content hash**, so the cost to
  fear is the one-off initial embed, not the reindex interval; and a non-interactive SSH shell does
  **not** have `~/.local/bin` on `PATH`, so an MCP command must be an absolute path.
- **1.0.0** — when the MCP tool contract stops changing *and* a second independent consumer exists.

## [0.9.0] — 2026-07-27

**Language knowledge becomes plugins, and documents enter the graph.** The four enrichers each carried
**their own copy** of the file-type list — `(".js", ".ts", ".tsx", ".vue")` appeared verbatim in four
files — while framework knowledge (Eloquent, Next/Nuxt HTTP clients) sat in "concern" files rather than
language ones. Adding a language meant editing four places, and forgetting one produced a **silently
partial graph**: the worst failure mode here, because nothing looks broken and edges are simply absent.

### Added
- **`bin/lang/` — the language layer**, with one shared source of truth for file-type facts and a
  plugin registry. The sets are **named by role and not merged**: `FRONTEND` (files that call a
  backend) stays narrower than `ALL_CODE`, because `link_http` deliberately scans only the former and
  a merged list would quietly change what it looks at. Comment openers per language live here too, so
  a new language no longer requires touching rationale extraction.
- **`bin/lang/markdown.py` — the first plugin for a format that is not code**, and the proof the
  contract holds beyond PHP/JS. Documents used to exist only in the search index: findable, but the
  graph could not answer *which document describes this service*. Now:
  - a `doc` node per markdown file under a project's docs root (rendered as an ellipse, legend updated);
  - `links_to` **doc → doc** from `[[wikilink]]` and relative markdown links — `EXTRACTED`, the link is
    literally in the file;
  - `documents` **doc → symbol** from a bare mention — `INFERRED` with a score, never `EXTRACTED`.
  The mention rule is deliberately stingy, because this is where noise would come from: only
  distinctive labels (≥5 chars, CamelCase / `method()` / namespaced), whole-word case-sensitive,
  **ambiguous labels skipped** (same rule as `dedupe.py`: when in doubt, do not invent a fact), and
  capped at 12 per document — a doc mentioning forty symbols is an index, not a description.
- `bin/link_docs.py` as the pipeline step (a module inside a package cannot be executed directly, and
  every other step is a `bin/*.py` script, so the plugin stays importable and testable).
- New relations in the graph contract: `links_to`, `documents`.
- Tests 159 → **176**: the registry (incl. a test that the removed duplication cannot come back), the
  distinctiveness rules, ambiguity skipping, the per-doc cap, partial-word rejection, vendor
  exclusion, and **idempotence** — the pipeline reruns, so nodes must not double.

### Fixed
- Wikilink resolution was broken on the first write: the index was keyed by a composite
  `"repo::path.md"` string and `Path("api::b.md").stem` is `api::b`, so `[[b]]` matched nothing. Two
  separate indexes now — by path for markdown links, by stem for wikilinks. Caught by a test that
  expected two edges and got one.

### Verified on the real graph
`+8 doc nodes, +24 edges` across three doc roots, with `links_to` marked `EXTRACTED` and `documents`
`INFERRED`.

## [0.8.0] — 2026-07-27

**Non-markdown documents can be indexed, one plugin per format.** Reversing the non-goal written a few
hours earlier the same day (Romans: «давай разберёмся с таблицами и сканами и pdf… можем сделать на
каждый parsing свой файл/plugin чтобы не мешать в кучу»). The scope is deliberately narrower than
"multimodality": **text extraction** through external CLIs, not layout reconstruction.

The trigger was concrete: two ConnectPay PDFs sit in a corpus and were **invisible to search**, because
the doc index only matches `**/*.md`. `pdftotext -layout` pulls 17k and 31k characters out of them.

### Added
- **`bin/parsers/` — one plugin per format**, each declaring exactly four things (`EXTENSIONS`,
  `REQUIRES`, `CONFIDENCE`, `extract`) so parsers cannot grow into one pile:
  - `pdf_text` — PDFs with a text layer (`pdftotext -layout`; `-layout` matters, pricing decks put
    figures in columns and without it a number loses its row);
  - `pdf_ocr` — scans and images (`pdftoppm` + `tesseract`, languages from `KB_OCR_LANGS`);
  - `office` — `.docx .odt .rtf .epub` → markdown **including tables**, via pandoc.
- **`kb-extract`** (`bin/extract`): `--list` shows what this machine can extract *and what to install
  for the rest*, `--into <dir>` writes `<name>.md`, otherwise stdout.
- **Extraction wired into the doc staging** of `kb build`: non-markdown files under a project's `docs`
  root are extracted into the same staged tree, so the index sees them. `KB_EXTRACT=0` opts out.
- `kb-install` now lists the formats and the missing tools for the rest.
- Tests 143 → **159**, including an end-to-end extraction of a hand-written PDF (the figure that
  matters must survive), the priority order, and the reported-not-crashed rule.

### Rules the registry enforces
- **A missing tool means the format is unsupported, not a crash** — reported once, build continues.
  A silent hole in the index is worse than a loud gap.
- **Text layer before OCR, always.** A PDF that yields almost nothing is *refused with an explanation*
  rather than indexed as an empty document.
- **Every extracted file carries provenance**: source, tool **and its version**, timestamp, and
  `confidence: EXTRACTED | OCR`. OCR output is noisy by nature and must never pass for authored text —
  a hit from an `OCR` document is a lead, not a quote.
- **The corpus is never modified**: extraction writes into the staged copy only.
- **Zero Python dependencies survive** — every parser is a wrapper over an external CLI, like
  `graphify` and `qmd`.

### Fixed
- Tool versions in the provenance header are **validated**. Poppler's `pdftotext` treats `--version` as
  a filename and prints an I/O error, which duly ended up inside a header on the first run; a candidate
  now has to look like a version and not like a complaint.

## [0.7.1] — 2026-07-27

### Changed
- **Scope boundaries are now explicit** (README §Non-goals): multimodality (PDF/scan/image/audio) is
  out, SaaS connectors and permission mirroring are out, a server-side canonical index is a deployment
  recipe rather than a feature, and there is no write path. Nothing was removed from the code — this
  states decisions that were already implicit, so that the comparison table reads as a boundary and not
  as a to-do list.
- The remote-canon roadmap item is struck through with the reasoning and with the findings that stay
  valid either way (SQLite over network mounts, incremental embedding, the SSH `PATH` trap).

## [0.7.0] — 2026-07-26

**Installable as a tool.** `uv tool install knowledge-index` / `pipx install knowledge-index` puts
`kb`, `kb-mcp`, `kb-api` and `kb-install` on `PATH`. Answering the question that prompted this
(«не лучше использовать composer?»): Composer is PHP, and this is Python + bash whose two real
dependencies come from *different* ecosystems — `graphify` via uv/pip, `qmd` via npm. No single
package manager can deliver that stack, which is why `kb-install` (a preflight, not a resolver)
stays.

### Added
- **`pyproject.toml`** (hatchling), version read from the `VERSION` file so there is still one source
  of truth. **No runtime Python dependencies** — standard library only, on purpose.
- **`knowledge_index/` — a thin launcher**, not a rewrite: each entry point `os.execv`s the shipped
  script, so signals, exit codes and the stdio pipe stay exactly as they are (`kb-mcp`'s whole
  protocol is that pipe). `bin/` remains the implementation and a plain checkout keeps working.
- **`bin/paths.py` — the split between code and data**, which packaging forces: `PACKAGE_ROOT`
  (read-only: `bin/`, `web/`, `VERSION`, examples), `DATA_HOME` (`graphify-out/`, `repos/`, `.docs/`),
  `CONFIG_HOME` (`kb.projects.toml`, `aliases.toml`). Installed it follows XDG; `KB_HOME` overrides
  both; **a checkout keeps everything in place, so an existing clone needs no migration**. `bash` and
  Python read the same resolution (`paths.py --sh`), so they cannot disagree.
- **`kb vendor` and `kb open` as subcommands** — they work in both modes, unlike the previous
  Makefile-only versions. The shell is **staged next to the data** when installed, because the page
  loads its data through relative `<script src>` and a `file://` page cannot fetch a sibling.
  A vendored renderer survives an upgrade: staging never touches `vendor/`.
- Tests 132 → **143**: checkout-vs-wheel detection, XDG and `KB_HOME` resolution, examples always
  read from the shipped copy, staging behaviour, and that the shell exports bash evals are complete
  and quoted (an empty one would make a build write to `/`).

### Fixed
- **An installed copy identified itself as a checkout and pointed its data directory at
  site-packages.** The first marker was `VERSION` + `bin/kb` — both of which the wheel ships. The
  marker is now `pyproject.toml`, which a wheel never installs. Caught by actually installing the
  tool and reading `kb --version`, not by reasoning about it.
- `kb-install` now prints the **console-script** path for `.mcp.json` when installed
  (`~/.local/bin/kb-mcp`) instead of a site-packages path that the next reinstall would break.

## [0.6.1] — 2026-07-26

### Added
- **README §"Where this sits (and where it does not)"** — an honest comparison against the code-side
  tools (Sourcegraph, Zoekt/OpenGrok, Meta's Glean, CodeQL) and the retrieval-side ones (Onyx, R2R,
  RAGFlow, txtai, the managed cloud primitives, Glean the company), with a **"what the others do
  better"** section: compiler-grade code semantics, connectors and permission mirroring, document
  understanding, scale and support. Facts stamped July 2026, because licences and pricing move.

### Fixed
- Requirements said python `>= 3.10`; the floor is **3.11** — `tomllib` reads the config and the alias
  table. `bin/install` already checked 3.11, so the README was the wrong half.

## [0.6.0] — 2026-07-26

**The lexical layer stops excluding the answer — without patching qmd.** qmd joins a phrase's terms
with `AND`, so a natural-language question turns every word into a hard filter: the page that holds
the answer is dropped when it lacks one of them, usually a function word. Measured on the 27-question
golden set, asking as a phrase gave **recall 0.14**.

The fix uses qmd's own documented multi-line form instead of forking or patching it: hand it a
**query document** — `intent:` plus one `lex:` line per content word plus a `vec:` line — which qmd
**fuses (RRF)** rather than intersects.

| Same corpus, same unpatched qmd | recall | MRR |
|---|---|---|
| one phrase (`AND` semantics) | 0.142 | 0.167 |
| **query document, one `lex:` per term** | **0.670** | **0.586** |

For comparison, patching qmd's `parseLexQuery` to `OR` reached recall 0.735 / MRR 0.526 — so this
keeps ~90% of the recall gain with *better* ranking and **nothing forked**: no maintenance of someone
else's fast-moving tool, and nothing that a `qmd` upgrade silently reverts.

### Added
- **`bin/query_build.py`** — the query document builder: content-word extraction, stop words for
  Russian and English, compound tokens kept whole (`doc-search`, `renget-api`, `graph.json`,
  `/srv/oldname` are the corpus's most discriminative words and splitting them destroys them), a cap
  of 12 terms, and a hand-written query document passed through untouched.
- **`bin/install` + `make install` / `make check`** — first-run setup: verifies python ≥ 3.11
  (`tomllib`), `graphify ≥ 0.9.25`, `qmd`, `rsync`; creates `kb.projects.toml` and `aliases.toml` from
  their examples; optionally vendors the renderer; prints the ready `.mcp.json` snippet with absolute
  paths and the three surface entry points. **Never overwrites an existing file**, never installs
  another project's tool behind your back (it prints the command), idempotent, and `--check` writes
  nothing at all.
- Tests 119 → **132**: stop words in both languages, compound tokens, ordering and deduplication, the
  term cap, the stop-words-only fallback, and that explicit `mode=lex|vec` is left alone.

### Changed
- `search` with `mode` `auto`/`hybrid` now sends a query document; `mode=lex` and `mode=vec` remain
  verbatim passthroughs, because an explicit mode is the caller's decision.
- **Side effect, deliberate: qmd's own LLM query expansion no longer runs for these searches.** It was
  actively harmful on unknown proper nouns — for one name it invented a domain ("… in philosophy") and
  dragged the vector search off the corpus, giving that question a recall of exactly zero.

### Superseded
- The planned "routing between the lexical and vector legs by question shape" is dropped: the document
  carries both legs and lets RRF weigh them, which needs no classifier and no heuristics about what a
  question "looks like".

## [0.5.0] — 2026-07-26

**The web surface is a source file now.** The whole interface used to be a `TEMPLATE` string inside
`bin/render_viz.py`, rendered into git-ignored `graphify-out/kb-graph.html` — an interface that could
not be diffed, linted or tested, and that nobody could fix without running the pipeline.

### Added
- **`web/` — the versioned shell**: `index.html`, `app.js`, `style.css`. No build step, no framework,
  no placeholders. Opens from `file://`; entry point is `make open`.
- **A Status panel** next to the graph, fed by build-time snapshots: index health (nodes, edges,
  domains, projects, when the graph was built), the docs collection (files, freshness), **the alias
  table** (renames are knowledge, not just configuration), search-quality numbers if a benchmark
  result is present, and the versions of the repo plus both CLIs. Every block is **stamped** — a stale
  number that admits its age is useful, one that pretends to be live is not.
- **`make vendor`** downloads the renderer into `web/vendor/` (git-ignored) so the page works
  offline. The page prefers the local copy and falls back to a CDN **with a visible banner**: a tool
  whose promise is "the corpus never leaves the machine" must not quietly fetch code from a third
  party. This was an inconsistency in the previous shell, which loaded vis-network from unpkg
  unconditionally.
- Tests 104 → **119**: the data contract the shell depends on (manifest keys, super-node `domkey`,
  per-domain member/intra split, cross-domain rerouting, search index shape, stamped status) and the
  shell's own rules — **no `.innerHTML`**, the local renderer first, no leftover placeholders, and the
  UI strings staying English.

### Changed
- **`bin/render_viz.py` emits data only** and takes an output *directory*: `kb-manifest.js`
  (`kbManifest({domains, superNodes, fileMap, dataPath, nodeCount, generatedAt})`), `kb-status.js`,
  and the per-domain `kb-graph-data/*.js` that already existed. The four `__PLACEHOLDER__`
  substitutions are gone.
- **Search results are built with `textContent`.** The old shell assigned node labels into
  `innerHTML`; a repository containing a symbol named `<img onerror=…>` would have executed it. The
  generator additionally escapes `</script` in every emitted file, so a label cannot end the script
  tag early.
- The stale `graphify-out/kb-graph.html` is removed by the build; graphify's own `graph.html` stays as
  the raw upstream view.
- `README.md` and `CLAUDE.md` point at the new entry point; the design rules gained three entries (the
  interface is a source file, snapshots are stamped, the renderer is local).

### Unchanged on purpose
- The lazy data split (one `.js` per domain, `_cross.js`, `_index.js`, loaded on demand) predates this
  release and stays: `.js` calls rather than JSON + fetch is what makes `file://` work.

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
