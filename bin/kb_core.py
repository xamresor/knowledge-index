#!/usr/bin/env python3
"""The read core: every operation this project offers, once, transport-free.

Why this module exists
----------------------
There are three surfaces, and they exist for three different consumers:

  * **MCP (stdio)** — so *agents* can search inside a session. No port, no auth.
  * **HTTP API**    — so *scripts* (bash, python, CI, cron) can run the same search.
  * **web**         — so *a human* can look at status: the graph, index health. Static files only;
                      it is deliberately **not** a client of this core (it must open from `file://`
                      when nothing is running).

The first two are the same contract for machines. Keeping them identical is not a promise to
remember: both adapters dispatch through `call_tool()` here and publish `TOOLS` from here, so a
drift between MCP and HTTP would require deleting code, not forgetting to copy it.

Everything is a subprocess wrapper around the `graphify` and `qmd` CLIs — no Python packages, so the
tool runs where installing packages is not an option.

A third transport, the shared HTTP MCP server (`bin/kb-http`, off by default), also dispatches here.
It passes `allowed` — the projects the caller may see — and the core applies it to the inputs of
every tool (see bin/access.py). `allowed=None` means everything: the local stdio and loopback API.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import access
import contracts
import paths
import query_build
from alias_expand import expand as expand_aliases, load_aliases

# Code and data are different roots once the tool is installed — see bin/paths.py.
KB = str(paths.data_home())
GRAPH = str(paths.graph_path())
COLLECTION = os.environ.get("KB_COLLECTION", "kb")

TIMEOUT_GRAPH = 60      # graph queries are sub-second; this is just a safety net
TIMEOUT_DOCS = 150      # qmd's first call loads the embedding model (cold start)
MAX_OUT = 8000

# How a docs search spends memory and time (KB_DOCS_MODE). The per-call `mode` still applies.
#   full  - `qmd query` with reranking: best ranking, the reranker model loads (~1.5 GB RAM).
#   light - the same query document with --no-rerank: BM25 + vectors, embedding model only
#           (~650 MB, ~2 s). Meant for a shared server.
#   bm25  - keywords only (`qmd search`), no model at all (~70 MB).
DOCS_MODE = os.environ.get("KB_DOCS_MODE", "full")

# The surface's own contract number, bumped when a tool's shape changes in a breaking way.
# Deliberately NOT the repo version: the repo can release many times without touching the contract,
# and a caller pinning behaviour cares about this number only.
API_CONTRACT = 1


def _read_version() -> str:
    """Repo version from the VERSION file (single source of truth); see CHANGELOG.md."""
    try:
        return (paths.PACKAGE_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0+unknown"
    except OSError:
        return "0+unknown"


VERSION = _read_version()

# Alias table is deployment data (gitignored aliases.toml); absent file = feature off.
# Loaded once: the table is small and hand-curated, not hot data.
ALIASES = load_aliases()


# --- projects (one qmd collection per project: <COLLECTION>-<name>) ------------------------------
def load_projects(cfg=None) -> dict[str, list[str]]:
    """name -> every path the project lists (code, docs, routes, calls), from kb.projects.toml.

    Same tiny reader as bin/kb (no TOML package): only `key = "str"` and `key = ["a", "b"]` lines."""
    cfg = cfg or paths.config_file("kb.projects.toml")
    try:
        txt = open(cfg, encoding="utf-8").read()
    except OSError:
        return {}
    out: dict[str, list[str]] = {}
    for block in re.split(r"(?m)^\[\[project\]\]\s*$", txt)[1:]:
        name = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', block)
        if not name:
            continue
        roots = []
        for key in ("code", "docs", "routes", "calls"):
            m = re.search(rf"(?m)^\s*{key}\s*=\s*(.+)$", block)
            if m:
                roots += re.findall(r'"([^"]+)"', m.group(1).split("#", 1)[0])
        out[name.group(1)] = [os.path.normpath(r) for r in roots]
    return out


def _has_docs(name: str, cfg=None) -> bool:
    cfg = cfg or paths.config_file("kb.projects.toml")
    try:
        txt = open(cfg, encoding="utf-8").read()
    except OSError:
        return False
    for block in re.split(r"(?m)^\[\[project\]\]\s*$", txt)[1:]:
        if re.search(rf'(?m)^\s*name\s*=\s*"{re.escape(name)}"', block):
            return bool(re.search(r"(?m)^\s*docs\s*=", block))
    return False


def projects_for_dir(cwd: str, projects: dict[str, list[str]]) -> list[str]:
    """Projects that own `cwd`: one of their roots is inside it, or it is inside one of their roots.

    /private/projects/rentget -> renget-api, renget-web, renget-knowledge (all live under it);
    /private/projects/rentget/apps/web/src -> renget-web. A dir that owns nothing (or the shared
    parent of every project) -> [], meaning: search everything."""
    cwd = os.path.normpath(cwd)
    def inside(a, b):
        return a == b or a.startswith(b.rstrip(os.sep) + os.sep)
    hit = [n for n, roots in projects.items() if any(inside(r, cwd) or inside(cwd, r) for r in roots)]
    return [] if len(hit) == len(projects) else hit


PROJECTS = load_projects()
# Only projects with a `docs` root get a qmd collection (bin/kb build_kb); code-only ones have none.
DOC_PROJECTS = {n for n in PROJECTS if _has_docs(n)}
# The MCP server is started by the consuming session, in that session's directory.
HOME_PROJECTS = projects_for_dir(os.environ.get("KB_SESSION_DIR") or os.getcwd(), PROJECTS)


def resolve_collections(args: dict) -> tuple[list[str], str]:
    """-> (qmd collections, scope label). Empty list = every collection in the index."""
    if args.get("scope"):
        return [args["scope"]], args["scope"]
    want = (args.get("project") or "").strip()
    if want.lower() == "all":
        return [], "all"
    if want:
        names = [n.strip() for n in want.split(",") if n.strip()]
        unknown = [n for n in names if n not in PROJECTS]
        if unknown:
            raise ToolError(f"unknown project: {', '.join(unknown)} (known: {', '.join(PROJECTS)})")
        docs = [n for n in names if n in DOC_PROJECTS]
        if not docs:
            raise ToolError(f"no docs indexed for: {', '.join(names)} "
                            f"(projects with docs: {', '.join(sorted(DOC_PROJECTS))})")
        return [f"{COLLECTION}-{n}" for n in docs], ", ".join(docs)
    home = [n for n in HOME_PROJECTS if n in DOC_PROJECTS]
    if home:
        return [f"{COLLECTION}-{n}" for n in home], ", ".join(home) + " (auto)"
    return [], "all"

# --- tool schemas -------------------------------------------------------------------------------
# One list, published by both adapters. `deprecated` entries still work (a caller written against an
# older surface must not break) but point at their replacement.
TOOLS = [
    {"name": "search",
     "description": "Search the knowledge base. `domain` picks what is searched: docs (qmd hybrid "
                    "vector+keyword), code (semantic subgraph from the merged cross-repo graph), or "
                    "both. Old names an alias table knows about are expanded to their canonical form "
                    "before the query runs.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "the question or terms"},
         "domain": {"type": "string", "enum": ["docs", "code", "both"],
                    "description": "what to search (default: both)"},
         "project": {"type": "string",
                     "description": "docs: which project(s) to search — a name from kb.projects.toml, "
                                    "several comma-separated, or 'all'. Default: the project(s) of the "
                                    "session's working directory, else all; an empty scoped search "
                                    "widens to all by itself"},
         "scope": {"type": "string", "description": "raw qmd collection to search instead (overrides project)"},
         "mode": {"type": "string", "enum": ["auto", "lex", "vec", "hybrid"],
                  "description": "docs retrieval mode; auto = hybrid with a keyword fallback"},
         "limit": {"type": "integer", "description": "max results (docs)"},
         "min_score": {"type": "number", "description": "drop docs results below this score"},
         "expand_aliases": {"type": "boolean", "description": "apply the alias table (default true)"},
         "explain": {"type": "boolean", "description": "include retrieval score traces"}},
         "required": ["q"]}},
    {"name": "graph_path",
     "description": "Shortest path between two symbols/files in the code graph, traversing real "
                    "relations incl. cross-repo http_request (frontend↔controller).",
     "inputSchema": {"type": "object", "properties": {"from": {"type": "string"},
                                                     "to": {"type": "string"}},
                     "required": ["from", "to"]}},
    {"name": "graph_explain",
     "description": "Explain a symbol: its node, neighbours and relation types (calls, eloquent, fk, "
                    "sql, http_request, …).",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}},
                     "required": ["symbol"]}},
    {"name": "graph_affected",
     "description": "Reverse impact analysis — what depends on / would be affected by a change to "
                    "the given symbol.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}},
                     "required": ["symbol"]}},
    {"name": "docs_get",
     "description": "Fetch a full document by its qmd path or id (e.g. from a search result).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "contracts_search",
     "description": "Search the HTTP contracts between the services: API routes with their "
                    "controller action and middleware, plus the frontend call sites of each route "
                    "and the contract docs that name it. Every word must match the method, URI, "
                    "route name, action or middleware, e.g. 'POST v2/users', 'UserController'. "
                    "Covers every project, whatever the caller's project access.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "connections",
     "description": "Service map: which services talk to which, over what (HTTP, queue, database, "
                    "external API, ...) and why, from the contract docs (<docs>/contracts/*.md) and "
                    "the HTTP calls found in code. With `service`: only its incoming and outgoing "
                    "connections. Each line names its contract doc; read it with docs_get.",
     "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}}},
    {"name": "problems",
     "description": "Gaps the build found: missing or incomplete contract docs, connections without "
                    "a contract doc, frontend calls to routes that do not exist. `project` narrows "
                    "to one project or one kind (e.g. 'unknown-route').",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "version",
     "description": "Repo version and the surface's own contract number — separate axes.",
     "inputSchema": {"type": "object", "properties": {}}},
    # --- deprecated: superseded by `search`, kept so older callers keep working -------------------
    {"name": "docs_search", "deprecated": True,
     "description": "DEPRECATED — use search({q, domain:'docs'}). Hybrid vector + keyword search "
                    "over the indexed docs.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "graph_query", "deprecated": True,
     "description": "DEPRECATED — use search({q, domain:'code'}). Semantic search over the merged "
                    "cross-repo code graph.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}},
                     "required": ["question"]}},
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}

_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "object": dict, "array": list}


class ToolError(ValueError):
    """Bad request: unknown tool, missing argument, wrong type, value outside an enum."""


def validate(name: str, args: dict) -> None:
    """Validate arguments against the tool's own schema — no `jsonschema` dependency.

    Deliberately shallow: required keys, primitive types, enum membership. That is what the surface
    actually promises; anything deeper would be a second, unversioned contract living in the checker.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name}")
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if args.get(key) in (None, ""):
            raise ToolError(f"{name}: '{key}' is required")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            raise ToolError(f"{name}: unknown argument '{key}'")
        expected = _JSON_TYPES.get(spec.get("type", ""))
        if expected and not isinstance(value, expected):
            raise ToolError(f"{name}: '{key}' must be {spec['type']}")
        if isinstance(value, bool) and spec.get("type") in ("integer", "number"):
            raise ToolError(f"{name}: '{key}' must be {spec['type']}")
        if spec.get("enum") and value not in spec["enum"]:
            raise ToolError(f"{name}: '{key}' must be one of {', '.join(spec['enum'])}")


# --- process plumbing ---------------------------------------------------------------------------
def run(cmd: list[str], cwd: str | None = None, timeout: int = TIMEOUT_GRAPH,
        limit: int | None = MAX_OUT) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.returncode and p.stderr else "")
        out = out.strip()
        return (out[:limit] if limit else out) or "(no results)"
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except Exception as e:  # noqa: BLE001
        return f"(error: {e})"


def warm_qmd() -> None:
    """Fire a throwaway qmd query in the background so the embedding model is loaded before the
    first real doc search — turns a >60s cold start into a ~2s warm call."""
    if DOCS_MODE == "bm25":
        return  # no model to load
    cmd = ["qmd", "query", "lex: warmup\nvec: warmup"] + (["--no-rerank"] if DOCS_MODE == "light" else [])
    try:
        subprocess.Popen(
            cmd,
            cwd=KB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass  # best-effort; a cold first query still works, just slower


_LABELS: dict[str, tuple[float, list[tuple[str, str, str, str]]]] = {}


def labels(graph: str | None = None) -> list[tuple[str, str, str, str]]:
    """Node labels (label, type, repo, source_file) of one graph file, cached by mtime, so an
    ambiguous graphify match can suggest exact labels. A filtered graph has its own cache entry."""
    graph = graph or GRAPH
    try:
        mtime = os.path.getmtime(graph)
    except OSError:
        return []
    hit = _LABELS.get(graph)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(graph, encoding="utf-8") as f:
            nodes = json.load(f).get("nodes", [])
        rows = [(n.get("label", ""), n.get("type", ""), n.get("repo", ""),
                 n.get("source_file", "")) for n in nodes if n.get("label")]
    except Exception:  # noqa: BLE001
        rows = []
    _LABELS[graph] = (mtime, rows)
    return rows


def suggest(term: str, limit: int = 20, graph: str | None = None) -> str:
    """List exact node labels matching `term` (graphify matches by substring, so an ambiguous term
    hits many). Exact and word-boundary matches rank first."""
    t = term.strip().lower()
    if not t:
        return ""
    hits = [row for row in labels(graph) if t in row[0].lower()]
    if not hits:
        return ""

    def rank(row: tuple[str, str, str, str]) -> tuple[int, int]:
        label = row[0].lower()
        if label == t:
            return (0, len(label))
        if re.search(rf"\b{re.escape(t)}\b", label):
            return (1, len(label))
        return (2, len(label))

    hits.sort(key=rank)
    seen, uniq = set(), []
    for row in hits:
        if row[0] not in seen:
            seen.add(row[0])
            uniq.append(row)
    lines = []
    for label, typ, repo, src in uniq[:limit]:
        meta = " ".join(p for p in (typ, repo, src) if p)
        lines.append(f"  {label}" + (f"  [{meta}]" if meta else ""))
    more = f"\n(+{len(uniq) - limit} more — refine the term)" if len(uniq) > limit else ""
    return (f'\n\nAmbiguous: "{term}" matches {len(uniq)} node label(s). '
            f"Re-run with one of these EXACT labels:\n" + "\n".join(lines) + more)


def with_suggestions(out: str, *terms: str, graph: str | None = None) -> str:
    """If graphify couldn't pin a unique node, append the candidate exact labels."""
    if "node match" not in out.lower():
        return out
    extra = "".join(suggest(t, graph=graph) for t in terms if t)
    return out + extra if extra else out


# --- operations ---------------------------------------------------------------------------------
def _qmd_cmd(args: dict, query: str) -> list[str]:
    """Build the qmd invocation for a docs search.

    `mode` maps to qmd's entry points — with one deliberate twist: for `auto`/`hybrid` we hand qmd a
    **query document** (one `lex:` line per content word + a `vec:` line) instead of a raw phrase,
    because its lexical layer joins a phrase's terms with AND and so excludes the very page that holds
    the answer. See bin/query_build.py for the measurement behind this.
    """
    mode = args.get("mode", "auto")
    if DOCS_MODE == "bm25":
        mode = "lex"  # the server has no model to spare
    verb = {"lex": "search", "vec": "vsearch"}.get(mode, "query")
    if verb == "query":
        query = query_build.build(query)
    cmd = [verb, query]
    if verb == "query" and DOCS_MODE == "light":
        cmd.append("--no-rerank")
    for c in resolve_collections(args)[0]:
        cmd += ["-c", c]
    if args.get("limit"):
        cmd += ["-n", str(args["limit"])]
    if args.get("min_score") is not None:
        cmd += ["--min-score", str(args["min_score"])]
    if args.get("explain"):
        cmd += ["--explain"]
    return ["qmd", *cmd]


def search_docs(args: dict, allowed: set[str] | None = None) -> str:
    """Docs half of `search`. Alias expansion happens on the raw query, before qmd stems it.

    With `allowed`, the search runs over every collection (contract docs of any project are open
    to all) and drops each hit whose resolved path is outside the allowed projects. More hits are
    asked for than are shown, so the filter still leaves enough."""
    q = args["q"]
    note = ""
    if args.get("expand_aliases", True):
        expanded = expand_aliases(q, ALIASES)
        if expanded != q:
            note = f"[alias expansion: {q!r} -> {expanded!r}]\n\n"
            q = expanded
    if allowed is not None:
        want = int(args.get("limit") or 5)
        scoped = {k: v for k, v in args.items() if k not in ("project", "scope")}
        out = _filter_hits(_docs_once({**scoped, "project": "all", "limit": want * 5}, q),
                           allowed, want)
        return note + f"[projects: {', '.join(sorted(allowed))} + contract docs]\n\n" + out
    out = _docs_once(args, q)
    label = resolve_collections(args)[1]
    if _empty(out) and label.endswith("(auto)"):
        # the session's own project had nothing — widen instead of returning empty
        out = _docs_once({**args, "project": "all"}, q)
        label = f"all (nothing in {label[:-7]})"
    return note + f"[projects: {label}]\n\n" + out


def _filter_hits(out: str, allowed: set[str], keep: int) -> str:
    """Keep the qmd text hits (each starts with a `qmd://` line) that the caller may see."""
    blocks = re.split(r"(?m)^(?=qmd://)", out)
    head, hits = blocks[0], [b for b in blocks[1:] if access.doc_allowed(b.split("\n", 1)[0], allowed)]
    if not hits:
        return "(no results in your projects)"
    head = head if head.startswith("[") else ""  # keep our own notes, drop qmd's progress lines
    return (head + "".join(hits[:keep])).strip()[:MAX_OUT]


def _empty(out: str) -> bool:
    return out in ("(no results)", "") or out.startswith("No results found")


def _docs_once(args: dict, q: str) -> str:
    out = run(_qmd_cmd(args, q), cwd=KB, timeout=TIMEOUT_DOCS)
    if out == "(timed out)" and args.get("mode", "auto") == "auto":
        # cold start exceeded the budget — fall back to instant BM25 rather than returning nothing
        out = run(_qmd_cmd({**args, "mode": "lex"}, q), cwd=KB, timeout=TIMEOUT_GRAPH)
        if out != "(timed out)" and not _empty(out):
            out = "[semantic search timed out — keyword (BM25) results below]\n\n" + out
    return out


def search_code(args: dict, allowed: set[str] | None = None) -> str:
    """Code half of `search`: a scoped subgraph for the question."""
    return run(["graphify", "query", args["q"], "--graph", graph_for(allowed), "--budget", "1800"])


def search(args: dict, allowed: set[str] | None = None) -> str:
    """The one search entry point. `domain` decides which halves run; `both` labels each section."""
    domain = args.get("domain", "both")
    if domain == "docs":
        return search_docs(args, allowed)
    if domain == "code":
        return search_code(args, allowed)
    return (f"=== docs ===\n{search_docs(args, allowed)}\n\n"
            f"=== code ===\n{search_code(args, allowed)}")


def graph_for(allowed: set[str] | None) -> str:
    """The graph file this caller may query (bin/access.py builds and caches filtered copies)."""
    if allowed is None:
        return GRAPH
    try:
        return access.graph_for(allowed, list(PROJECTS))
    except access.Denied as e:
        raise ToolError(str(e)) from None


def docs_get(path: str, allowed: set[str] | None = None) -> str:
    out = run(["qmd", "get", path], cwd=KB, timeout=TIMEOUT_DOCS)
    if allowed is None:
        return out
    # qmd resolves ids and partial paths fuzzily, so check the resolved path in the answer's first
    # line, not the argument. Same answer for "missing" and "not allowed": no existence leak.
    if not access.doc_allowed(out.lstrip().split("\n", 1)[0], allowed):
        return "(document not found in your projects)"
    return out


# Arguments that end up in a subprocess argv: a leading dash would read as a CLI option.
_ARGV_ARGS = {"q", "query", "question", "symbol", "from", "to", "path", "service", "project"}


def _no_options(name: str, args: dict) -> None:
    for key in _ARGV_ARGS & args.keys():
        v = args[key]
        if isinstance(v, str) and v.lstrip().startswith("-"):
            raise ToolError(f"{name}: '{key}' must not start with '-'")


def _index(fn, *a):
    """Run a contracts.py query; a missing index is the caller-visible 'not built yet'."""
    try:
        return fn(*a)
    except contracts.NotBuilt as e:
        raise ToolError(str(e)) from None
    except ValueError as e:
        raise ToolError(str(e)) from None


def call_tool(name: str, args: dict, allowed: set[str] | None = None) -> str:
    """Dispatch a validated tool call. Every adapter goes through here — that is the whole point.

    `allowed` = the projects the caller may see; None = everything (local use)."""
    args = dict(args or {})
    validate(name, args)
    _no_options(name, args)
    if allowed is not None:
        allowed = set(allowed)

    if name == "search":
        return search(args, allowed)
    if name in ("graph_path", "graph_explain", "graph_affected"):
        graph = graph_for(allowed)
        if name == "graph_path":
            a, b = args["from"], args["to"]
            return with_suggestions(run(["graphify", "path", a, b, "--graph", graph]), a, b,
                                    graph=graph)
        s = args["symbol"]
        verb = "explain" if name == "graph_explain" else "affected"
        return with_suggestions(run(["graphify", verb, s, "--graph", graph]), s, graph=graph)
    if name == "docs_get":
        return docs_get(args["path"], allowed)
    if name == "contracts_search":
        return _index(contracts.contracts_search, args["query"])
    if name == "connections":
        return _index(contracts.connections, args.get("service", ""))
    if name == "problems":
        return _index(contracts.problems, args.get("project", ""), allowed)
    if name == "version":
        info = version_info()
        if allowed is not None:  # a limited caller learns only its own project names
            info["projects"] = sorted(set(info["projects"]) & allowed)
            info["session_projects"] = []
        return json.dumps(info, indent=2)
    # deprecated shims: translate to `search` so there is one implementation, not two
    if name == "docs_search":
        return search({"q": args["query"], "domain": "docs"}, allowed)
    if name == "graph_query":
        return search({"q": args["question"], "domain": "code"}, allowed)
    raise ToolError(f"unknown tool: {name}")  # unreachable: validate() already checked


def version_info() -> dict:
    """Two axes, never merged: the repo release and the surface's contract."""
    return {"repo": VERSION, "api_contract": API_CONTRACT, "collection": COLLECTION,
            "projects": list(PROJECTS), "session_projects": HOME_PROJECTS,
            "aliases": len(ALIASES)}
