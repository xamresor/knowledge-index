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
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

from alias_expand import expand as expand_aliases, load_aliases

# Resolve the KB root from this file's location so the core is portable across checkouts.
KB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPH = f"{KB}/graphify-out/graph.json"
COLLECTION = os.environ.get("KB_COLLECTION", "kb")

TIMEOUT_GRAPH = 60      # graph queries are sub-second; this is just a safety net
TIMEOUT_DOCS = 150      # qmd's first call loads the embedding model (cold start)

# The surface's own contract number, bumped when a tool's shape changes in a breaking way.
# Deliberately NOT the repo version: the repo can release many times without touching the contract,
# and a caller pinning behaviour cares about this number only.
API_CONTRACT = 1


def _read_version() -> str:
    """Repo version from the VERSION file (single source of truth); see CHANGELOG.md."""
    try:
        return (pathlib.Path(KB) / "VERSION").read_text(encoding="utf-8").strip() or "0+unknown"
    except OSError:
        return "0+unknown"


VERSION = _read_version()

# Alias table is deployment data (gitignored aliases.toml); absent file = feature off.
# Loaded once: the table is small and hand-curated, not hot data.
ALIASES = load_aliases()

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
         "scope": {"type": "string", "description": "qmd collection to search instead of the default"},
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
def run(cmd: list[str], cwd: str | None = None, timeout: int = TIMEOUT_GRAPH) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.returncode and p.stderr else "")
        return out.strip()[:8000] or "(no results)"
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except Exception as e:  # noqa: BLE001
        return f"(error: {e})"


def warm_qmd() -> None:
    """Fire a throwaway qmd query in the background so the embedding model is loaded before the
    first real doc search — turns a >60s cold start into a ~2s warm call."""
    try:
        subprocess.Popen(
            ["qmd", "query", "warmup", "-c", COLLECTION],
            cwd=KB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass  # best-effort; a cold first query still works, just slower


_LABELS: list[tuple[str, str, str, str]] | None = None  # (label, type, repo, source_file)


def labels() -> list[tuple[str, str, str, str]]:
    """Lazy-load + cache node labels so an ambiguous graphify match can suggest exact labels."""
    global _LABELS
    if _LABELS is None:
        try:
            with open(GRAPH, encoding="utf-8") as f:
                nodes = json.load(f).get("nodes", [])
            _LABELS = [(n.get("label", ""), n.get("type", ""), n.get("repo", ""),
                        n.get("source_file", "")) for n in nodes if n.get("label")]
        except Exception:  # noqa: BLE001
            _LABELS = []
    return _LABELS


def suggest(term: str, limit: int = 20) -> str:
    """List exact node labels matching `term` (graphify matches by substring, so an ambiguous term
    hits many). Exact and word-boundary matches rank first."""
    t = term.strip().lower()
    if not t:
        return ""
    hits = [row for row in labels() if t in row[0].lower()]
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


def with_suggestions(out: str, *terms: str) -> str:
    """If graphify couldn't pin a unique node, append the candidate exact labels."""
    if "node match" not in out.lower():
        return out
    extra = "".join(suggest(t) for t in terms if t)
    return out + extra if extra else out


# --- operations ---------------------------------------------------------------------------------
def _qmd_cmd(args: dict, query: str) -> list[str]:
    """Build the qmd invocation for a docs search. `mode` maps to qmd's three entry points."""
    mode = args.get("mode", "auto")
    verb = {"lex": "search", "vec": "vsearch"}.get(mode, "query")
    cmd = [verb, query, "-c", args.get("scope") or COLLECTION]
    if args.get("limit"):
        cmd += ["-n", str(args["limit"])]
    if args.get("min_score") is not None:
        cmd += ["--min-score", str(args["min_score"])]
    if args.get("explain"):
        cmd += ["--explain"]
    return ["qmd", *cmd]


def search_docs(args: dict) -> str:
    """Docs half of `search`. Alias expansion happens on the raw query, before qmd stems it."""
    q = args["q"]
    note = ""
    if args.get("expand_aliases", True):
        expanded = expand_aliases(q, ALIASES)
        if expanded != q:
            note = f"[alias expansion: {q!r} -> {expanded!r}]\n\n"
            q = expanded
    out = run(_qmd_cmd(args, q), cwd=KB, timeout=TIMEOUT_DOCS)
    if out == "(timed out)" and args.get("mode", "auto") == "auto":
        # cold start exceeded the budget — fall back to instant BM25 rather than returning nothing
        out = run(_qmd_cmd({**args, "mode": "lex"}, q), cwd=KB, timeout=TIMEOUT_GRAPH)
        if out not in ("(timed out)", "(no results)"):
            out = "[semantic search timed out — keyword (BM25) results below]\n\n" + out
    return note + out


def search_code(args: dict) -> str:
    """Code half of `search`: a scoped subgraph for the question."""
    return run(["graphify", "query", args["q"], "--graph", GRAPH, "--budget", "1800"])


def search(args: dict) -> str:
    """The one search entry point. `domain` decides which halves run; `both` labels each section."""
    domain = args.get("domain", "both")
    if domain == "docs":
        return search_docs(args)
    if domain == "code":
        return search_code(args)
    return (f"=== docs ===\n{search_docs(args)}\n\n=== code ===\n{search_code(args)}")


def call_tool(name: str, args: dict) -> str:
    """Dispatch a validated tool call. Both adapters go through here — that is the whole point."""
    args = dict(args or {})
    validate(name, args)

    if name == "search":
        return search(args)
    if name == "graph_path":
        a, b = args["from"], args["to"]
        return with_suggestions(run(["graphify", "path", a, b, "--graph", GRAPH]), a, b)
    if name == "graph_explain":
        s = args["symbol"]
        return with_suggestions(run(["graphify", "explain", s, "--graph", GRAPH]), s)
    if name == "graph_affected":
        s = args["symbol"]
        return with_suggestions(run(["graphify", "affected", s, "--graph", GRAPH]), s)
    if name == "docs_get":
        return run(["qmd", "get", args["path"]], cwd=KB, timeout=TIMEOUT_DOCS)
    if name == "version":
        return json.dumps(version_info(), indent=2)
    # deprecated shims: translate to `search` so there is one implementation, not two
    if name == "docs_search":
        return search({"q": args["query"], "domain": "docs"})
    if name == "graph_query":
        return search({"q": args["question"], "domain": "code"})
    raise ToolError(f"unknown tool: {name}")  # unreachable: validate() already checked


def version_info() -> dict:
    """Two axes, never merged: the repo release and the surface's contract."""
    return {"repo": VERSION, "api_contract": API_CONTRACT, "collection": COLLECTION,
            "aliases": len(ALIASES)}
