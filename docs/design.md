# Design

Why the KB is built the way it is, what it is not, and how it compares.

## Where this sits (and where it does not)

**This is a small tool, not an enterprise search product.** It indexes a handful of related
repositories you own and exposes them to agents, scripts and a browser tab. Locally it serves one owner.
As a shared server (`kb-http`) it serves a small team: per-project access through a permission webhook
or the bundled users plugin, and a few admin pages (users, usage, status, settings). It has no connectors, no
permission mirroring from other systems, and no support. If you need those, the table below points at what to use instead.

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
| **knowledge-index** | markdown docs + the code graph | ✅ embeddings on device | no | per project (webhook or users plugin), no ACL mirroring | AGPL-3.0, free |
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
  reflect their ACLs. This project reads local files; its permissions are per project, set by you, not mirrored.
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

### Docs search: an honest not found, measured (2026-11-23)

A docs search always has nearest documents. Before this check, `search` gave all of them as a
confident answer, also for questions that no document answers. qmd's own score cannot tell: it is
rank-based, so the first hit always prints 100%. The raw best cosine and BM25 scores overlap too: for
10 of 10 no-answer questions they fell inside the range of the answerable ones.

The check (`bin/docs_grade.py`, behaviour in [tools](tools.md#when-search-finds-nothing-that-fits))
grades the first hit by *coverage*: the share of the question's idf weight that the hit holds. A word
that no document holds weighs most, so a question about a thing the docs never mention gets a low
grade. It reads qmd's keyword index directly: no model, a few milliseconds. Below 0.46: one
keyword-only search, then "not found" with the nearest 3 hits marked as weak.

`bench/search_baseline.py`, one corpus of 123 Markdown files, 38 answerable and 10 no-answer
questions, the `tool` backend with and without the check:

| | recall@5 | MRR | confident wrong | no-answer answered | answerable: wrong first file | answerable refused |
|---|---|---|---|---|---|---|
| without the check (`refuse: false`) | 0.92 | 0.81 | 0.42 (20 of 48) | 10 of 10 | 10 of 38 | 0 |
| with the check (default) | 0.87 | 0.78 | 0.23 (11 of 48) | 2 of 10 | 9 of 38 | 2 |

*Confident wrong* is the share of all questions that got a confident wrong answer: a no-answer
question with any hit, or an answerable question whose first file is not an expected one. A refusal
is never confident.

What it costs and what it does not do:

- Two answerable questions are refused. One had the right file first (coverage 0.42), the other at
  rank 4. That is the recall drop from 0.92 to 0.87.
- Two no-answer questions still get an answer. "Terraform state of the Hetzner server": the
  deployment doc holds every other word (coverage 0.57). "Sign in with Apple": every word, including
  "apple", is in some document (coverage 1.0).
- The check does not catch a wrong first file among the answerable questions (10 to 9 only): it
  judges whether the words are there, not whether the fact is.
- The keyword-only fallback rescued no question on this set. It stays because it is cheap (no model)
  and the roadmap asked for it; drop it if a larger set shows the same.
- The threshold was read off the same 48 questions, and the no-answer questions name foreign
  technologies, the easy case. A question whose words all exist but whose fact does not (a near
  miss) passes the check. Treat the numbers as an upper bound until a set written later confirms them.

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
- Small enough to read: about 13k lines of code and 8k lines of tests, without dependencies.

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
  and the visualisation draws ambiguous edges dashed. A heuristic may be wrong; it may not *pretend*.
- **Restraint over cleverness in destructive steps.** `dedupe.py` refuses to merge when a label has
  two real definitions (merging would invent a fact); `declutter.py` prunes only the curated hubs in
  `kb.hubs.txt`, with no degree-based auto-detection, because a high-degree definition-less node is
  usually a real service whose definition was never captured. Both restraints are pinned by tests.
- **Tests sit where the risk is.** The destructive transforms (`dedupe`, `declutter`) and the artefact
  contract (`graph`) are covered, not just the easy pure parser. Stdlib `unittest`, no test deps:
  `make test`. A mutation pass checks that the tests of the HTTP-edge matcher are not blind:
  `make mutation` (`bench/mutation.py`) breaks `bin/link_http.py` and its call-site patterns one
  small change at a time (a flipped regex, a GET default turned into POST, an inverted `endswith`,
  a dropped sort). It runs the `link_http` tests on a temporary copy for each change and fails if
  any change survives. A survivor is a test gap: add a test, do not weaken the mutant.
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
- [x] **The KB's own users for teams without an API** (closed 2026-11-23, done another way than planned). The
  plan was a `tokens.toml` file served as the two webhooks. The access plugin `contrib/users` does the job
  instead: an admin adds people and their projects on the Users tab, each person makes the own tokens.
- [x] **Precise PHP calls from SCIP (add-on).** Spike 2026-11-20 on one Laravel 12 API (215 files): `scip-php`
  indexed it in 10 s (2.2 MB) and found 125 cross-file method calls against 40 in the AST graph, for example
  calls through a typed property or an interface. Its edges would enter the graph as EXTRACTED `calls`.
  Limits seen: `scip-php` is stale (v0.0.2, 2023) and wants the project's `vendor/`; its old PHP parser (v4)
  clashes with Laravel's (v5), worked around by giving the indexed copy the same parser; it still missed calls
  to Eloquent model methods (`$order->totalDue()`). `scip-typescript` (maintained) is the TS side.
  Done 2026-11-23: the add-on `scip` (`bin/link_scip.py`, [tools](tools.md)). Real run the same day
  (`scip-php` dev-main, the same API, now larger): 1,719 `EXTRACTED` calls, 10 of 10 checked edges real.
  The run found and fixed one bug: missing autoload paths in the staged copy stopped the indexer.
- [x] **Docker image for the shared server**: one image (Python, graphify with rustworkx, qmd), the
  indexed code mounted read-only, add-ons switched by `KB_ADDONS`; the Laravel route table taken as a
  file from the app's own container. The `code-mode` sandbox as an optional second container without
  network. A local single-user install stays `uv tool install`.
  Done 2026-11-23: `Dockerfile`, `docker/compose.example.yml`, [docker.md](docker.md). Full `docker compose` run
  the same day (x86_64; arm64 not tried): build, CPU embeddings (first build 203 s, about 500 MB peak),
  MCP search, `graph_explain` and `run_code` through the sandbox without network. It found and fixed four
  bugs: the build context missed `datadict.toml.example`; the warm qmd worker started before the first
  build kept an empty index; `mode: vec` ran `qmd vsearch`, which downloads the 1.7B model; the users
  plugin could not find its modules in the image.
- [x] **Mutation testing for `bin/link_http.py`.** The matcher has unit tests
  (`make test`), but tests can pass while being blind to real regressions. Add a
  mutation pass — flip a regex, swap the GET default to POST, invert an
  `endswith`, drop the shortest-suffix sort — and confirm the suite *fails* on
  each (every mutant killed). Keep it dependency-light (a small stdlib harness +
  `make mutation`) or wire up `mutmut` in a venv.
  Done 2026-11-23: `make mutation`, 36 mutants, all killed; 12 tests added for the gaps it found.
- [x] **An "is this enough?" check and an honest refusal for `search`.** Today `search` always returns
  the nearest hits, also when nothing fits. Grade the result; on a low grade drop it and either run a
  fallback search or answer "not found". Report a third number beside recall and precision: the share
  of confident wrong answers. No query rewriting here: LLM expansion was switched off for these
  searches because it added noise.
  Done 2026-11-23: `bin/docs_grade.py`, measured above ("Docs search: an honest not found"). The
  threshold comes from the same 48 questions. Held-out check the same day (20 answerable and 10
  no-answer questions written after the threshold): it refused 4 of 10 no-answer questions (8 of 10 on
  the fit set) and 2 of 20 answerable ones that were right. Five no-answer questions passed with a
  coverage of 0.47-0.50, each with a word that no document holds. A candidate rule: also refuse when a
  question word is in no document. On both sets it catches 18 of 20 and loses no more right answers.
  Not applied yet. A question in English about a Russian document gets a low coverage: a known gap.
- [x] **A data dictionary over the tables the graph already knows (add-on).** Tables come from the PHP
  source (`Schema::create`, `DB::table`); `.sql` is still not parsed. Give a table node its meaning:
  the grain (what one row stands for), which columns may be summed, the business term for it. The
  source is a small hand-written file per project, like `aliases.toml`. Then the KB answers "which
  table holds revenue per customer", not only "where is this table in the code". Check before
  building more: 20 questions about data, before and after; keep it only if the answers get better.
  Done 2026-11-23: the add-on `datadict` ([data dictionary](data-dictionary.md)). 20-question check the
  same day on the SmartMonitor API graph (29 tables in the dictionary, `--domain code`): recall@5 of
  tables 0.125 off, 0.95 on (a model name counted as its table). The same person wrote the questions
  and the dictionary, and about half of the synonyms repeat words of the questions: an upper bound.
  Recheck with a dictionary and questions from different people.
- [x] **Admin pages in the core, a Settings page, and a setting for contract visibility.** Done 2026-11-23.
  The admin pages are no add-on any more: a shared server shows them when it has admins. The add-ons
  are `code-mode`, `tool-multicall`, `fast-routes`, `metrics`, `audit`, `questions`, `scip`, `datadict`
  and the installed plugins. `/admin/settings` switches them and edits the settings of `bin/settings.py`
  (`<config>/settings.json`, an env variable wins). The first setting is `contracts`: `all` (contracts
  open to every user, as before) or `own` (only the contracts that touch an allowed project, the other
  side cut to the bare endpoint). Decision: `all` stays the default, because the contracts are the
  interfaces a team shares; a server whose projects belong to different teams switches to `own`.
- [ ] **Golden-set questions from the team's real use.** The current set was written by the author.
  Ten or so questions that developers really asked, each with the expected answer, show where the KB
  misses in daily work. The collection is in place: the Questions page (add-on `questions`) lists the asked
  questions and exports the labelled ones (`/admin/questions.json`). Left: label the questions of a shared
  server, then use them in `bench/search_baseline.py`.

## Footprint

The indexed projects are **never modified**. Their code is rsynced (code-only) into `repos/`
(git-ignored) for scanning; all outputs live here in this repo.

See `CLAUDE.md` for the agent query workflow.
