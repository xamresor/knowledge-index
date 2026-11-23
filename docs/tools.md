# Tools and add-ons

## What the agent gets

| Tool | Answers |
|---|---|
| `contracts_search` | a route: its controller, middleware and every file that calls it |
| `graph_explain` | one symbol and all its connections |
| `graph_affected` | what depends on a symbol, across repositories |
| `graph_routes` | several ways from A to B, with the routes on the way |
| `review_context` | a diff: what changed, what depends on it, the routes and screens it reaches, the docs to check |
| `search` | docs and code by meaning |
| `code_grep` | exact text with a regular expression: config keys, SQL, error messages |
| `repo_map` | a first look at a project: what most of the code leans on; with `unused`, dead-code candidates |

Long answers come in pages with a cursor; `connections` and `problems` show the service map and its gaps.

## When `search` finds nothing that fits

A docs search always has nearest documents, also when none of them answers the question. So the docs
half of `search` grades its first hit before it answers:

1. **Grade.** How much of the question's specific words does the first hit hold? Words that every
   document has count for almost nothing; a word that no document has counts most. The number is
   the hit's *coverage*, 0 to 1 (`bin/docs_grade.py`). It needs no model: it reads qmd's keyword index.
2. **Good grade** (coverage 0.46 or more, or a question too short to grade): the answer comes back
   unchanged.
3. **Weak grade:** one more search with keywords only (the same words, no vector line). If its first
   hit grades well, those results come back under a note that says so.
4. **Still weak:** the answer starts with `[not found: ...]`, names the words that no document holds,
   and lists at most 3 nearest hits marked as weak. They stay because the grade is a rule of thumb: it
   turns away some answerable questions, and the caller can still open them.

`refuse: false` switches the check off for one call and returns the plain nearest hits. `explain: true`
also prints the grade of a good answer. Only page 1 is graded; later pages are the plain nearest hits.
The check is about the docs half only; the code half answers as before.

How well this works on one corpus, and what it costs: [design, "Docs search: an honest not found"](design.md#docs-search-an-honest-not-found-measured-2026-11-23).

## Add-ons

Off until you install them; an add-on that is off adds no tool, page or route.

| Add-on | `kb addon install …` gives | Needs |
|---|---|---|
| `tool-multicall` | `batch`: several tool calls in one request | — |
| `code-mode` | `run_code`: a small program that calls the tools, in a sandbox | Node 22+ |
| `fast-routes` | `graph_routes` on a Rust core, 2-3x faster | graphify via uv or pipx |
| `metrics` | `GET /metrics` for Prometheus, Grafana or PMM | — |
| `admin` | `/admin`: status, charts, usage, sign-in clients | on a shared server: `KB_ADMINS` |
| `audit` | an Audit tab: sign-ins, admin actions, every tool call with user and argument | `admin` |
| `scip` | precise `calls` edges from a SCIP index at each `kb update` | `scip-php` or a ready `index.scip` |
| `datadict` | the meaning of each table (term, grain, columns to sum) in `search`, `graph_explain` and `problems`: [data dictionary](data-dictionary.md) | a `datadict/<project>.toml` file |

`kb addon list` shows what is on, `kb addon remove <name>` switches one off. An add-on can also live
outside this repository, under its own licence: `kb addon install <folder with plugin.py>` (see
`bin/plugins.py` for the four names a plugin defines). A team server with browser
sign-in and access per project: [the shared server](server.md); it asks your own system who may see
what through two webhooks, [access protocol](access-protocol.md).


## SCIP: precise calls (add-on `scip`)

The AST graph links a call only when it sees the class. A call through a typed property or an
interface needs the types. A SCIP index has them. On one Laravel API, `scip-php` found 125 cross-file
method calls; the AST graph found 40.

With the add-on on, `kb update` reads a SCIP index of each project and adds `calls` edges between
methods the graph already has. The edges are `EXTRACTED`, with `_origin: kb-scip`. A symbol that
does not map to a graph node is skipped. A pair of nodes that already has an edge gets no second one.
The step runs before the typed-call linker, so that linker does not add an `INFERRED` copy.

Where the index comes from, in this order:

1. The indexer. When `KB_SCIP_PHP` is set or `scip-php` is on the `PATH`, and the project root has
   `composer.json`, the indexer runs in the staged copy `repos/<project>`. The step links
   `composer.json`, `composer.lock` and `vendor/` from the project root into the copy for the run,
   then removes them. The index goes to `<data>/scip/<project>.scip`.
2. A ready index at `<data>/scip/<project>.scip` (from an earlier run, or put there by CI).
3. A ready `index.scip` in the project root.

Without any of them the step writes one line and skips the project. The project root is the
`routes` folder of the project, or else the parent of its first `code` folder.

Switch it on with `kb addon install scip`, then run `kb update`.

**Install `scip-php`.** The KB does not install it. `scip-php` (github.com/davidrjenni/scip-php) is
a Composer package. Install it as a standalone project, not as a dependency of the indexed project:

```bash
composer create-project --no-dev --stability=dev davidrjenni/scip-php /opt/scip-php dev-main
export KB_SCIP_PHP=/opt/scip-php/bin/scip-php
```

Use `dev-main`. The tags v0.0.1 and v0.0.2 need `google/protobuf` ^3.22, and Composer blocks that
version for a security advisory. Do not add it to another tool folder with `composer require`: it
then looks for its PHP stubs in the indexed project's `vendor/` and stops.

Check the command by hand first: run it in the project root and look for `index.scip`.

The step links `composer.json` and `vendor/` into the staged copy. `scip-php` stops when a path
in `autoload` or `autoload-dev` of `composer.json` is missing (`database/factories`, `tests/`), and
the code-only staging leaves those out. The step creates each missing path empty for the run and
removes it after.

Measured on one Laravel 12 API (2026-11-23, `dev-main`): the index of the staged copy took 17 s
and 13 MB. The step added 1,719 `EXTRACTED` calls in 3 s; 10 of 10 checked edges were real calls.

Known limits:

- `scip-php` indexes only the PSR-4 paths of the root `composer.json`. Path-repository packages
  (`packages/*` loaded through `vendor/`) get no edges.
- It does not resolve calls on typed method parameters (`$user->isSuperAdmin()` in a policy). The
  typed-call linker (`bin/link_php_calls.py`) still adds about 2,200 `INFERRED` calls after it on
  the same API, so keep both.
- It misses calls to Eloquent model methods (magic `__call` and static forwarding).
- It writes no enclosing range. The caller of a reference is the last method definition at or
  before its line.
- `KB_SCIP_TIMEOUT` (default 600 seconds) stops a hung indexer.

TypeScript: `scip-typescript` (npm `@sourcegraph/scip-typescript`, maintained) writes the same
format. The step does not run it. Run `scip-typescript index` in the project root yourself, and the
step reads the `index.scip` there.
