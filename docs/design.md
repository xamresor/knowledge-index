# Design

Why the KB is built the way it is, what it is not, and how it compares.

## Where this sits (and where it does not)

**This is a small local tool, not an enterprise search product.** It indexes a handful of related
repositories you own, on one machine, for one owner, and exposes them to agents, scripts and a browser
tab. It has no connectors, no multi-user permissions, no UI for administering anything, and no
support. If you need those, the table below points at what to use instead.

Facts below were checked in **July 2026**; licences and pricing move, so verify before deciding.

### Code side

| | Indexes | Cross-repo edges | Local / on-device | Daemon needed | Licence & cost |
|---|---|---|---|---|---|
| **knowledge-index** | code **+ docs** | ✅ incl. frontend call site → controller | ✅ | no (CLI/MCP; API optional) | AGPL-3.0, free |
| [Sourcegraph](https://sourcegraph.com) | code | ✅ (repo-wide search) | ◐ | yes | **proprietary since 2023**; free self-host deprecated, from ~$49/user/mo |
| [Zoekt](https://github.com/sourcegraph/zoekt) / [OpenGrok](https://oracle.github.io/opengrok/) | code (**search only**, no graph) | ❌ | ✅ | yes | OSS, free |
| [Glean (Meta)](https://glean.software/) | code, as schema-defined facts | ✅ within its schema | ✅ | yes (fact DB) | OSS, free — **compiler-grade, and heavy** |
| [CodeQL](https://codeql.github.com/) | code, as a query language | ◐ | ✅ | no | free for OSS, **licence-restricted commercially** |
| [graphify](https://github.com/Graphify-Labs/graphify) | code (AST graph) | — | ✅ | no | our dependency, not an alternative |

### Docs / retrieval side

| | Indexes | Local / on-device | Daemon needed | Permissions | Licence & cost |
|---|---|---|---|---|---|
| **knowledge-index** | markdown docs + the code graph | ✅ embeddings on device | no | **none — one owner by design** | AGPL-3.0, free |
| [Onyx](https://onyx.app) (ex-Danswer) | 45+ connectors | ◐ can self-host, LLM optional | yes (Docker stack) | ✅ mirrors source ACLs | MIT, free |
| [R2R](https://github.com/SciPhi-AI/R2R) | ingested documents | ◐ | yes | users/auth built in | Apache-2.0, free |
| [RAGFlow](https://github.com/infiniflow/ragflow) | **deep** PDF/scan parsing | ◐ | yes (Elasticsearch) | multi-tenant | Apache-2.0, free |
| [txtai](https://github.com/neuml/txtai) | embeddable, single package | ✅ | no | — | Apache-2.0, free |
| Azure AI Search · Bedrock KB · Vertex AI Search | managed RAG primitives | ❌ cloud | managed | query-time ACL / IAM | metered |
| [Glean (the company)](https://glean.com) | 100+ connectors, enterprise search | ❌ cloud | managed | ✅ permission mirroring | ~$45–50/seat/mo, ~100-seat minimum |

### What the others do better

- **Compiler-grade code semantics.** Meta's Glean, CodeQL and SCIP-based indexers resolve symbols the
  way a compiler does. This project uses AST extraction plus regex heuristics for the cross-repo hops,
  and says so on every edge via `confidence` (`EXTRACTED | INFERRED | AMBIGUOUS | DERIVED`) — honest,
  but weaker than a real indexer.
- **Connectors and permission mirroring.** Onyx and Glean read Slack, Drive, Confluence, Jira and
  reflect their ACLs. This project reads local files and has no permission model at all.
- **Document understanding.** RAGFlow parses scanned PDFs and tables properly; here a document is
  markdown, and anything else is out of scope.
- **Scale, operations, support.** A managed service is the right answer for a company; this is one
  process and a directory of generated files.

### Graph vs grep, measured (2026-11-20)

`bench/graph_vs_grep.py` asks the same questions twice, retrieval only: one KB tool call, one grep with the
obvious keyword (one per step for a chain). On one Laravel + Next.js project, 10 questions, two of them chains
(a frontend function to the payment service; a service to the screens that reach it) (frontend caller of a route, controller of a
route, users of a class, users of a method, screens behind a route, where objects are created, where an env
key is read, a first look at the API):

| | found the expected files | text the agent reads | extra searches |
|---|---|---|---|
| KB | 100% (was 69% before the fixes below) | ~5,300 chars per answer | 0 |
| grep | 95% | ~174,000 chars per answer | 1.5 on average (a frontend URL -> route table -> controller) |

The first run found 69% for the KB, and each miss was a graph gap that is now fixed: a method called on a
typed variable (`$order->totalDue()`, now linked from parameter and property types), a name defined twice
(now answered for each match), a frontend call whose URL Prettier moved to the next line (now read), and
a call linked from its file instead of its function (now from the function). The same person wrote the
questions and the fixes, so treat 100% as "these gaps are closed", not as a measured hit rate: run it on
your own code with questions written before looking at the answers.

### Non-goals (decided, not pending)

These are **out of scope on purpose** — the table above lists them as things others do better, which is
true, and none of them are a gap this project intends to close:

- **Document *understanding*.** Extraction exists (see below) but it is text extraction, not layout
  reconstruction: no table-structure recovery from a scan, no figure captioning, no audio. If you need
  that, use RAGFlow — that is what it is for.
- **Connectors to SaaS suites** (Slack, Drive, Confluence, Jira) and the **permission mirroring** they
  require. One owner, local files. See Onyx or Glean.
- **Issuing identities.** The shared server (`bin/kb-http`, off by default — see "Shared server")
  asks your system through two webhooks (access protocol v1, [access-protocol.md](access-protocol.md))
  who a token belongs to and which projects they may see; it never stores users, passwords or roles of
  its own.
- **A write path.** The index is a projection; whoever owns the corpus owns writing to it.

### Languages: one plugin per language

`graphify` parses the AST. The plugins in `bin/lang/` add what an AST cannot see — framework relations,
schema facts, design rationale in comments, and documents as graph nodes:

| Module | Role | Claims | Contributes |
|---|---|---|---|
| `markdown` | plugin | `.md` | `doc` nodes · `links_to` (wikilinks, relative links — `EXTRACTED`) · `documents` (symbol mentions — `INFERRED`, capped) |
| `php_laravel` | patterns | `.php` | class/layer vocabulary · `$table` · Eloquent relations · schema + FK · query-builder refs · migration-name parsing |
| `js_ts` | patterns | `.js .ts .tsx .vue` | HTTP call sites: `api.verb()`, `$api()`, `apiFetch`, `fetch(BASE)` · template/script suffixes · build-output dirs |

Two roles, and the difference is deliberate. A **plugin** defines `enrich(graph, roots)` and runs as a
pipeline step of its own; a **patterns** module carries only regexes and pure functions that the
enrichers call. The enrichers' pipelines are older and heavier than one function signature, so the
patterns moved first and the pipelines stayed — the cheap half of the split, separate from the risky
half. `lang.plugins()` and `lang.patterns()` keep the two apart, and a test asserts `re.compile` never
reappears in an enricher.

Two boundaries were drawn against the tidier-looking option. There is **no `sql.py`**: nothing here
parses `.sql` files, so `DB::table` and `Schema::create` — which match PHP source — live in
`php_laravel`, and a "SQL language plugin" would be a fake boundary. And **route matching stays in
`link_http.py`**: a Laravel route table meeting a JS call site is a join between two languages and
belongs to neither plugin.

The shared file-type facts live in `bin/lang/__init__.py` — one place, named by role
(`FRONTEND` ⊂ `ALL_CODE`), because the same list used to be copy-pasted into four enrichers and
forgetting one produced a silently partial graph.

### Document formats: one plugin per format

Not everything worth indexing is markdown. Extraction is handled by **one plugin per format** in
`bin/parsers/`, each a thin wrapper over an external CLI — so the zero-dependency rule survives and a
format you do not need costs nothing.

```bash
kb-extract --list                       # what can be extracted here, and what to install for the rest
kb-extract quote.pdf                    # to stdout, with a provenance header
kb-extract --into docs/ *.pdf           # write <name>.md next to your docs
```

| Plugin | Formats | Needs | Confidence |
|---|---|---|---|
| `pdf_text` | `.pdf` with a text layer (`pdftotext -layout`) | poppler | `EXTRACTED` |
| `pdf_ocr` | scanned `.pdf`, `.png/.jpg/.tif` | tesseract (+poppler for PDFs) | `OCR` |
| `office` | `.docx .odt .rtf .epub` → markdown incl. tables | pandoc | `EXTRACTED` |

Four rules the registry enforces:

- **A missing tool means the format is unsupported, not a crash.** It is reported once and the build
  continues — a silent hole in the index is worse than a loud gap.
- **Text layer before OCR, always** (`PRIORITY`). A PDF that yields almost nothing is refused with an
  explanation rather than indexed as an empty document.
- **Every extracted file carries provenance** — source, tool *and its version*, timestamp, and
  `confidence: EXTRACTED | OCR`. OCR output is noisy by nature; it must never pass for authored text.
  A search hit from an `OCR` document is a lead, not a quote.
- **The corpus is still never modified.** Extraction writes into the staged copy only, and
  `KB_EXTRACT=0` turns it off entirely.

### When this is the better fit

- Several **related repositories you own**, where the interesting facts are the *joins* — which
  frontend call hits which controller, which model backs which table.
- **Agents are the primary consumer**: MCP is a first-class surface, not an add-on.
- **Nothing may leave the machine**: embeddings run on device, no API key is required, and the HTTP
  surface refuses to bind off loopback without a token.
- The index is **disposable**: delete `graphify-out/` and rebuild. No migration, no lock-in — the
  corpus is your files.
- Small enough to read: ~4k lines including the dashboard and the tests.

## Three surfaces, three consumers

Each surface exists for a different reader, and that decides what it may require:

| Surface | For | Job | Requires |
|---|---|---|---|
| **MCP** (`bin/kb-mcp`, stdio) | agents | search + traverse inside a session | nothing — no port, no auth |
| **HTTP API** (`bin/kb-api`) | scripts, CI, cron | the same search from bash/python | a port; a token off loopback |
| **web** (`web/index.html`) | a human | look at status: the graph, index health | nothing running — opens from `file://` |
| **shared MCP** (`bin/kb-http`, **off by default**) | a team's agents | one index on a server, each caller limited to their projects | `KB_SERVER=on`, two webhooks of your system, TLS in front |

MCP and the API are the **same contract**: both dispatch through `kb_core.call_tool()` and publish
`kb_core.TOOLS`, so they cannot drift apart without deleting code. The web surface is not a client of
that contract — it must work when nothing is running, which is precisely when you want to look at it.

```bash
make serve                      # 127.0.0.1:8899, no token needed on loopback; dashboard at /
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
expand_aliases, explain, budget}`. The old names `docs_search` and `graph_query` were removed in tool
contract 2 (`api_contract` in `version`); call `search` with `domain: docs` or `domain: code`.

**Binding off loopback requires a token, and the server refuses to start without one.** The point of
this project is that the corpus never leaves the machine; an index published to the network without
auth would be the leak it exists to prevent. For remote use, bind loopback and tunnel over SSH.

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

- [x] **Access protocol v1: two webhooks instead of one identity endpoint** (0.13.0,
  [access-protocol.md](access-protocol.md)). A *login* webhook takes
  `{username, password, code?}` and answers `{token}` or `{two_factor: {challenge}}`; a *permission*
  webhook takes the token and the list of the KB's projects and answers `{user, projects: [...] | "*"}`.
  The API then needs to know nothing about the KB's project names, and a team without an API writes
  the two hooks in a few lines. The old identity endpoint (`KB_AUTH_URL`) is gone.
- [ ] **A token file for teams without an API**: `tokens.toml` with token → user and projects, served as
  the two webhooks by a small bundled script, for a team that has no system of its own at all.
- [ ] **Precise PHP calls from SCIP (add-on).** Spike 2026-11-20 on one Laravel 12 API (215 files): `scip-php`
  indexed it in 10 s (2.2 MB) and found 125 cross-file method calls against 40 in the AST graph, for example
  calls through a typed property or an interface. Its edges would enter the graph as EXTRACTED `calls`.
  Limits seen: `scip-php` is stale (v0.0.2, 2023) and wants the project's `vendor/`; its old PHP parser (v4)
  clashes with Laravel's (v5), worked around by giving the indexed copy the same parser; it still missed calls
  to Eloquent model methods (`$order->totalDue()`). `scip-typescript` (maintained) is the TS side.
- [ ] **Docker image for the shared server**: one image (Python, graphify with rustworkx, qmd), the
  indexed code mounted read-only, add-ons switched by `KB_ADDONS`; the Laravel route table taken as a
  file from the app's own container. The `code-mode` sandbox as an optional second container without
  network. A local single-user install stays `uv tool install`.
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
