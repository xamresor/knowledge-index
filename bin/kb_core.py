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
import secrets
import shutil
import time
from collections import OrderedDict
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import access
import addons
import contracts
import paths
import query_build
import warm
from alias_expand import expand as expand_aliases, load_aliases

# Code and data are different roots once the tool is installed — see bin/paths.py.
KB = str(paths.data_home())
GRAPH = str(paths.graph_path())
COLLECTION = os.environ.get("KB_COLLECTION", "kb")

TIMEOUT_GRAPH = 60      # graph queries are sub-second; this is just a safety net
TIMEOUT_DOCS = 150      # qmd's first call loads the embedding model (cold start)
MAX_OUT = 200_000   # sanity cap on one producer's text; callers get it in pages (PAGE_CHARS)

# How a docs search spends memory and time (KB_DOCS_MODE). The per-call `mode` still applies.
#   light - (default) the query document without reranking: BM25 + vectors, embedding model only
#           (~650 MB; 0.2 s with the warm qmd, bin/warm.py).
#   full  - the same with an LLM reranker: a better order, but the reranker model loads (~1.5 GB) and
#           on a CPU each search takes 1-2 minutes (measured 2026-11-19: 81 s and 97 s).
#   bm25  - keywords only (`qmd search`), no model at all (~70 MB).
DOCS_MODE = os.environ.get("KB_DOCS_MODE", "light")

# The surface's own contract number, bumped when a tool's shape changes in a breaking way.
# Deliberately NOT the repo version: the repo can release many times without touching the contract,
# and a caller pinning behaviour cares about this number only.
# AGPL section 13: people who use the KB over a network can find its source. A changed copy points
# KB_SOURCE_URL at its own source.
SOURCE_URL = os.environ.get("KB_SOURCE_URL", "https://github.com/xamresor/knowledge-index")
API_CONTRACT = 2  # 2: docs_search and graph_query removed (use search)


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

    /srv/shop -> shop-api, shop-web, shop-docs (all live under it);
    /srv/shop/apps/web/src -> shop-web. A dir that owns nothing (or the shared
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

def scope_projects(args: dict, allowed: set[str] | None) -> tuple[set[str] | None, str, bool]:
    """The projects one call works on -> (set, or None = everything; label; chosen automatically?).

    `project` only ever NARROWS. The security boundary is `allowed` (set by the server from the
    caller's token); a project outside it is refused with the same message as an unknown one, so a
    limited caller cannot probe which other projects exist. Without `project`, the session's own
    projects are used (see HOME_PROJECTS), else everything the caller may see."""
    want = (args.get("project") or "").strip()
    if want and want.lower() != "all":
        names = {n.strip() for n in want.split(",") if n.strip()}
        bad = sorted(n for n in names if n not in PROJECTS or (allowed is not None and n not in allowed))
        if bad:
            raise ToolError(f"unknown or not allowed project: {', '.join(bad)}")
        return names, ", ".join(sorted(names)), False
    if not want:
        home = set(HOME_PROJECTS) if allowed is None else set(HOME_PROJECTS) & allowed
        if home:
            return home, ", ".join(sorted(home)) + " (auto)", True
    if allowed is None:
        return None, "all", False
    return set(allowed), ", ".join(sorted(allowed)), False


def _no_match(out: str) -> bool:
    """graphify found nothing for the term (it says so in a few wordings)."""
    low = out.lower()
    return "no node matching" in low or "no matching nodes" in low or "no unique node match" in low


# What `graph_affected` walks backwards. graphify's own default list stops at the repo boundary and
# knows nothing of the relations this project adds, so a controller showed no frontend callers and a
# table no models. Never `in_domain`: sharing a domain is not a dependency.
AFFECTED_RELATIONS = (
    "calls", "indirect_call", "references", "imports", "imports_from", "re_exports", "inherits",
    "extends", "implements", "uses", "mixes_in", "embeds",
    "http_request", "eloquent", "fk", "sql", "defines_table", "documents",
)


def graph_call(cmd: list[str], args: dict, allowed: set[str] | None, *terms: str) -> str:
    """Run a graphify command on the graph of the call's projects. An automatic (session) scope
    that finds nothing widens to everything the caller may see, like the docs search does."""
    def graph_of(projects: set[str] | None) -> str:
        graph = graph_for(projects)
        if cmd[1] not in ("path", "affected", "query"):
            return graph
        # path and impact: the same graph minus the domain anchors (see access.path_graph). Neither
        # walks `in_domain`, and an anchor shares its label with the model it is named after, so
        # with anchors `graph_affected Order` stopped at "ambiguous label".
        try:
            return access.path_graph(graph)
        except access.Denied as e:
            raise ToolError(str(e)) from None

    extra = ["--budget", str(_budget(args))] if cmd[1] == "query" else []
    if cmd[1] == "affected":
        extra = [x for r in AFFECTED_RELATIONS for x in ("--relation", r)]

    def ask(projects: set[str] | None) -> tuple[str, dict]:
        """-> (output, how to suggest labels). The warm worker cuts the caller's view of the main
        graph in memory; without it, a filtered file per project set is written and read."""
        worker = warm.graph()
        if worker is not None and os.path.exists(GRAPH):
            full = projects is None or set(PROJECTS) <= set(projects)
            view = {"allowed": None if full else sorted(projects),
                    "routes": cmd[1] in ("path", "affected", "query")}
            out = worker.run([*cmd[1:], "--graph", GRAPH, *extra], view)  # cmd[0] is "graphify"
            if out is not None:
                out = out.strip()
                return (out[:MAX_OUT] or "(no results)"), {
                    "graph": GRAPH, "repos": None if full else set(projects),
                    "no_domain": view["routes"]}
        graph = graph_of(projects)
        return run([*cmd, "--graph", graph, *extra]), {"graph": graph}

    projects, label, auto = scope_projects(args, allowed)
    out, where = ask(projects)
    if auto and _no_match(out):
        out, where = ask(allowed)
        label = f"all (nothing in {label[:-7]})"
    if cmd[1] == "explain":
        out = _full_connections(out, projects if not (auto and "all (" in label) else allowed)
    if terms:
        out = with_suggestions(out, *terms, **where)
    if cmd[1] == "query":
        out = _mcp_hint(out)
    return f"[projects: {label}]\n\n{out}"


def _full_connections(out: str, projects: set[str] | None) -> str:
    """graphify's `explain` lists 20 connections and "... and N more": replace that list with all of
    them (the answer is paged), from the same view of the graph."""
    m = re.search(r"(?m)^  ID:\s+(\S+)", out)
    if not m or not re.search(r"(?m)^\s+\.\.\. and \d+ more$", out):
        return out
    full = projects is None or set(PROJECTS) <= set(projects)
    view = {"allowed": None if full else sorted(projects), "routes": False}
    argv = ["neighbors", m.group(1), "--graph", GRAPH]
    worker = warm.graph()
    lst = worker.run(argv, view) if worker is not None else None
    if lst is None:
        lst = warm.graph_once(argv, view)
    if not lst:
        return out
    head = re.split(r"(?m)^Connections \(\d+\):$", out)[0].rstrip()
    return head + "\n\n" + lst.strip()


def graph_routes(args: dict, allowed: set[str] | None) -> str:
    """Several weighted paths (bin/routes.py), on the caller's view without domain anchors. A project
    pinned in a name (`api:Customer`) must be one the caller may see."""
    for end in (args["from"], args["to"]):
        m = re.match(r"^([\w-]+):", end)
        if m and m.group(1) in PROJECTS and allowed is not None and m.group(1) not in allowed:
            raise ToolError(f"unknown or not allowed project: {m.group(1)}")
    scoped = {**args, "project": args.get("project") or "all"}
    projects, label, _ = scope_projects(scoped, allowed)
    full = projects is None or set(PROJECTS) <= set(projects)
    view = {"allowed": None if full else sorted(projects), "routes": True}
    payload = json.dumps({"from": args["from"], "to": args["to"], "k": args.get("k") or 3})
    argv = ["routes", payload, "--graph", GRAPH]
    worker = warm.graph()
    out = worker.run(argv, view) if worker is not None else None
    if out is None:
        out = warm.graph_once(argv, view)
    if out is None:
        raise ToolError("graph_routes needs graphify's Python environment (networkx); it was not found")
    return f"[projects: {label}]\n\n{out.strip()}"


GREP_MAX, GREP_TIMEOUT = 500, 20


def code_grep(args: dict, allowed: set[str] | None) -> str:
    """Exact text search over the indexed code (the staged copies under <data>/repos), for what the
    graph does not hold: config keys, SQL, error messages, strings, comments. ripgrep when installed,
    else grep -E. Only the projects the caller may see are searched."""
    pattern = str(args["pattern"])
    if not pattern or len(pattern) > 500:
        raise ToolError("code_grep: 'pattern' must be 1-500 characters")
    projects, label, _ = scope_projects({**args, "project": args.get("project") or "all"}, allowed)
    names = sorted(PROJECTS) if projects is None else sorted(projects)
    roots = [(n, os.path.join(KB, "repos", n)) for n in names if os.path.isdir(os.path.join(KB, "repos", n))]
    if not roots:
        return f"[projects: {label}]\n\n(no indexed code for these projects)"
    limit = min(int(args.get("limit") or 50), GREP_MAX)
    glob = str(args.get("glob") or "")
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-config", "-n", "-H", "--no-heading", "--max-columns", "300", "--max-count", "20",
               "-g", "!graphify-out/**", "-e", pattern]
        if glob:
            cmd += ["-g", glob]
        if args.get("ignore_case"):
            cmd.append("-i")
    else:
        cmd = ["grep", "-rEnHI", "--exclude-dir=graphify-out", "-m", "20", "-e", pattern]
        if glob:
            cmd.append(f"--include={glob}")
        if args.get("ignore_case"):
            cmd.append("-i")
    lines: list[str] = []
    for name, root in roots:
        try:
            p = subprocess.run([*cmd, "--", "."], cwd=root, capture_output=True, text=True,
                               timeout=GREP_TIMEOUT, errors="replace")
        except subprocess.TimeoutExpired:
            lines.append(f"{name}: (search timed out: narrow the pattern or add 'glob')")
            continue
        if p.returncode not in (0, 1):
            raise ToolError(f"code_grep: bad pattern: {p.stderr.strip()[:200]}")
        for ln in p.stdout.splitlines():
            path, _, rest = ln.partition(":")
            lines.append(f"{name}:{path.removeprefix('./')}:{rest[:300]}")
            if len(lines) >= limit:
                break
        if len(lines) >= limit:
            break
    if not lines:
        return f"[projects: {label}]\n\n(no matches)"
    more = f"\n\n(stopped at {limit} lines: raise 'limit' or narrow the pattern)" if len(lines) >= limit else ""
    return f"[projects: {label}]\n\n" + "\n".join(lines) + more


def _visible_routes(allowed: set[str] | None) -> list[dict]:
    """Contract index rows of the projects the caller may see, callers from other projects dropped."""
    visible = set(PROJECTS) if allowed is None else set(allowed)
    try:
        rows = contracts.load_index(contracts.CONTRACTS)
    except contracts.NotBuilt:
        return []
    return [{**r, "callers": [c for c in r.get("callers", []) if c.get("repo") in visible]}
            for r in rows if r.get("project") in visible]


def _worker(argv: list[str], view: dict, what: str) -> str:
    worker = warm.graph()
    out = worker.run(argv, view) if worker is not None else None
    if out is None:
        out = warm.graph_once(argv, view)
    if out is None:
        raise ToolError(f"{what} needs graphify's Python environment; it was not found")
    return out.strip()


def repo_map(args: dict, allowed: set[str] | None) -> str:
    """The most depended-on parts of a project within a token budget (bin/repomap.py)."""
    projects, label, _ = scope_projects(args, allowed)
    names = sorted(PROJECTS) if projects is None else sorted(projects)
    budget = max(300, min(int(args.get("budget") or 1500), 8000))
    full = allowed is None and projects is None
    view = {"allowed": None if full else names, "routes": True}
    payload = json.dumps({"routes": _visible_routes(allowed), "projects": names, "budget": budget,
                          "unused": bool(args.get("unused")),
                          "roots": {n: os.path.join(KB, "repos", n) for n in names
                                    if os.path.isdir(os.path.join(KB, "repos", n))}})
    return f"[projects: {label}]\n\n" + _worker(["map", payload, "--graph", GRAPH], view, "repo_map")


def review_context(args: dict, allowed: set[str] | None) -> str:
    """What a change touches (bin/review.py): changed symbols, what depends on them, the HTTP routes
    and their frontend callers, the docs and notes next to them. On the caller's view."""
    import review
    try:
        if args.get("diff"):
            changes = review.parse_diff(str(args["diff"]))
        elif args.get("files"):
            changes = review.parse_files(list(args["files"]))
        else:
            raise ToolError("review_context: pass 'diff' (git diff output) or 'files'")
    except ValueError as e:
        raise ToolError(f"review_context: {e}") from None
    if not changes:
        return "(no changed files in the diff)"
    project = args.get("project") or ""
    if project and (project not in PROJECTS or (allowed is not None and project not in allowed)):
        raise ToolError(f"unknown or not allowed project: {project}")
    visible = set(PROJECTS) if allowed is None else set(allowed)
    full = allowed is None or set(PROJECTS) <= visible
    view = {"allowed": None if full else sorted(visible), "routes": True}
    payload = json.dumps({"changes": {p: sorted(v) for p, v in changes.items()},
                          "routes": _visible_routes(allowed), "project": project})
    return _worker(["review", payload, "--graph", GRAPH], view, "review_context")


CODE_BUDGET, CODE_BUDGET_MAX = 1800, 8000
# graphify's own advice names CLI options and tools this server does not have.
_CLI_HINT = re.compile(r"Narrow with context_filter=\[[^\]]*\] or use get_node for a specific symbol")


def _budget(args: dict) -> int:
    try:
        return max(300, min(int(args.get("budget") or CODE_BUDGET), CODE_BUDGET_MAX))
    except (TypeError, ValueError):
        return CODE_BUDGET


def _mcp_hint(out: str) -> str:
    return _CLI_HINT.sub(f"Next nodes: page: 2. Or raise `budget` (max {CODE_BUDGET_MAX}), narrow "
                         "`project`, or ask graph_explain about one symbol", out)


def instructions(projects: list[str] | None = None) -> str:
    """The `instructions` an MCP client shows the model in every session: which tool for which
    question, and how far to trust an answer. Short on purpose — it is paid for on every turn."""
    seen = f"Projects you can see: {', '.join(projects)}. " if projects else ""
    return (
        "Code knowledge base over several repos (a Laravel API and its frontends). " + seen +
        "First look at an unknown project -> repo_map. Which tool: an endpoint and who calls it -> contracts_search (fast, the best first step); "
        "one symbol and its neighbours -> graph_explain; what breaks if X changes -> graph_affected; "
        + ("several independent lookups -> batch; " if addons.on("tool-multicall") else "")
        + ("a lookup that needs the answer of another, or a filter over a long answer -> run_code "
           "(a small program calling kb(tool, args)); " if addons.on("code-mode") else "") +
        "how A reaches B -> graph_routes (several ways, with the routes they cross) or graph_path (one shortest); "
        "a diff or a pull request to review -> review_context (pass the git diff); an exact string, "
        "config key, SQL or error text -> code_grep; a free question -> search (domain docs, code or both). "
        "Trust: edges marked EXTRACTED come from the syntax; INFERRED ones (http_request, eloquent, "
        "doc mentions) are matched by name and can be wrong. in_domain only groups a symbol with its "
        "domain, it is not a dependency. The index is rebuilt after each deploy and can lag the "
        "code: check a claim against the file before you rely on it.")


# --- tool schemas -------------------------------------------------------------------------------
BATCH_MAX = int(os.environ.get("KB_BATCH_MAX", "10"))
RUN_CALLS = int(os.environ.get("KB_RUN_CALLS", "30"))
RUN_SECONDS = int(os.environ.get("KB_RUN_SECONDS", "20"))
# run_code's sandbox: installed by `kb addon install sandbox` into the data folder (a tool install keeps
# its package read-only); a checkout may also have it in place (`npm ci` in sandbox/).
_SANDBOX_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")
_SANDBOX_DATA = os.path.join(str(paths.data_home()), "sandbox")
SANDBOX = _SANDBOX_DATA if os.path.isdir(os.path.join(_SANDBOX_DATA, "node_modules")) else _SANDBOX_PKG
NOT_IN_BATCH = {"batch", "run_code"}

# One list, published by both adapters.
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
                     "description": "which project(s) to search (docs and code) — a name from kb.projects.toml, "
                                    "several comma-separated, or 'all'. Default: the project(s) of the "
                                    "session's working directory, else all; an empty scoped search "
                                    "widens to all by itself"},
         "scope": {"type": "string", "description": "raw qmd collection to search instead (overrides project)"},
         "mode": {"type": "string", "enum": ["auto", "lex", "vec", "hybrid"],
                  "description": "docs retrieval mode; auto = hybrid with a keyword fallback"},
         "limit": {"type": "integer", "description": "max results (docs)"},
         "min_score": {"type": "number", "description": "drop docs results below this score"},
         "expand_aliases": {"type": "boolean", "description": "apply the alias table (default true)"},
         "explain": {"type": "boolean", "description": "include retrieval score traces"},
         "budget": {"type": "integer", "description": "code: answer size in tokens (default 1800, "
                                                     "max 8000); raise it when the result says it was truncated"}},
         "required": ["q"]}},
    {"name": "graph_path",
     "description": "Shortest path between two symbols/files in the code graph, traversing real "
                    "relations incl. cross-repo http_request (frontend↔controller).",
     "inputSchema": {"type": "object", "properties": {"from": {"type": "string"}, "project": {"type": "string", "description": "limit to these project(s): a name, several comma-separated, or 'all'. Default: the session's project(s), widening to all when nothing is found"},
                                                     "to": {"type": "string"}},
                     "required": ["from", "to"]}},
    {"name": "graph_routes",
     "description": "Several ways from one place in the code to another, cheapest first, with the HTTP "
                    "routes each way crosses (frontend -> route -> controller, backend -> another "
                    "service). Calls and requests are cheap, imports dearer, busy hub classes dearest, "
                    "so a path reads like a real chain, not a shortcut through a framework class. Pin a "
                    "project as 'api:Customer', 'billing-api:TicketController'.",
     "inputSchema": {"type": "object", "properties": {
         "from": {"type": "string"}, "to": {"type": "string"},
         "k": {"type": "integer", "description": "how many different ways (default 3, max 6)"},
         "project": {"type": "string", "description": "limit to these project(s): a name, several comma-separated, or 'all'. Default: all the caller may see"}},
                     "required": ["from", "to"]}},
    {"name": "repo_map",
     "description": "A short map of a project for a first look: its domains, then the classes most of the "
                    "code depends on, with their busiest methods and the routes they serve, cut to a token "
                    "budget. Read it before exploring a repository you do not know.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "the project(s) to map: a name, several comma-separated, "
                                                     "or 'all'. Default: the session's project(s)"},
         "budget": {"type": "integer", "description": "answer size in tokens (default 1500, max 8000)"},
         "unused": {"type": "boolean", "description": "instead: classes and frontend modules nothing depends "
                                                     "on (dead code candidates, check before deleting)"}}}},
    {"name": "code_grep",
     "description": "Exact text search over the indexed code with a regular expression: config keys, SQL, "
                    "error messages, string literals, comments, anything the graph does not hold. "
                    "Answers project:path:line: text. For symbols and how they connect use graph_explain.",
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string", "description": "regular expression (ripgrep/ERE syntax)"},
         "glob": {"type": "string", "description": "only files matching this, e.g. '*.php' or 'config/**'"},
         "ignore_case": {"type": "boolean"},
         "limit": {"type": "integer", "description": f"lines to return (default 50, max {GREP_MAX})"},
         "project": {"type": "string", "description": "limit to these project(s): a name, several "
                                                     "comma-separated, or 'all'. Default: all the caller may see"}},
                     "required": ["pattern"]}},
    {"name": "review_context",
     "description": "Review a change: pass a unified diff (git diff base...head) or a list of files. "
                    "Answers which classes and methods changed, what depends on them in this and other "
                    "projects, which HTTP routes go through them and which frontend files call those "
                    "routes, which docs describe them and which NOTE/WHY comments sit next to them. "
                    "Use it before reviewing or merging a pull request.",
     "inputSchema": {"type": "object", "properties": {
         "diff": {"type": "string", "description": "unified diff text, up to 2 MB"},
         "files": {"type": "array", "items": {"type": "string"},
                   "description": "instead of a diff: paths, optionally with lines, e.g. 'app/X.php:10-20,31'"},
         "project": {"type": "string", "description": "the project the paths belong to, when a path could "
                                                     "match several"}}}},
    {"name": "batch",
     "description": "Several tool calls in one request, answered together in order: each call is "
                    "{tool, args}, as if called on its own, with the same access. Use it when the next "
                    "calls do not depend on each other's answers (look up three symbols, a route and its "
                    f"docs at once). At most {BATCH_MAX} calls; `batch` cannot hold `batch`.",
     "inputSchema": {"type": "object", "properties": {
         "calls": {"type": "array", "description": f"1-{BATCH_MAX} calls: [{{\"tool\": \"graph_explain\", "
                                                   "\"args\": {\"symbol\": \"Customer\"}}}, …]",
                   "items": {"type": "object", "properties": {
                       "tool": {"type": "string"}, "args": {"type": "object"}},
                       "required": ["tool"]}}},
                     "required": ["calls"]}},
    {"name": "run_code",
     "description": "Run a short TypeScript (or JavaScript) program that calls the other tools and returns "
                    "only what you need, in one request: loops, filters, joins of several answers. The "
                    "program is a function body with these globals: `declare function kb(tool: "
                    "'search'|'graph_routes'|'graph_path'|'graph_explain'|'graph_affected'|'docs_get'|"
                    "'contracts_search'|'connections'|'problems'|'review_context'|'code_grep'|'repo_map'|'version', args?: Record<string, unknown>): "
                    "string` (the tool's text answer, same arguments as the tool; throws on an error) and "
                    "`declare function print(...values: unknown[]): void`; `return` gives the result. Types "
                    "are stripped (no enums or namespaces). The program runs in a sandbox (QuickJS in "
                    "WebAssembly): no network, no files, "
                    f"no timers; at most {RUN_CALLS} kb() calls and {RUN_SECONDS} s. The tools answer with "
                    "your own access. Example: `const r = kb('contracts_search', {query: 'drivers'}); "
                    "return r.split('\\n').filter(l => l.startsWith('GET')).join('\\n')`",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string", "description": "the body of a function: use `return` for the result"}},
                     "required": ["code"]}},
    {"name": "graph_explain",
     "description": "Explain a symbol: its node, neighbours and relation types (calls, eloquent, fk, "
                    "sql, http_request, …).",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                     "project": {"type": "string", "description": "limit to these project(s): a name, several comma-separated, or 'all'. Default: the session's project(s), widening to all when nothing is found"}},
                     "required": ["symbol"]}},
    {"name": "graph_affected",
     "description": "Reverse impact analysis — what depends on / would be affected by a change to "
                    "the given symbol.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                     "project": {"type": "string", "description": "limit to these project(s): a name, several comma-separated, or 'all'. Default: the session's project(s), widening to all when nothing is found"}},
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
]

# Every tool that can answer at length takes `page`. Lists (contracts, connections, problems, impact,
# paths, documents) are cut into pages of PAGE_CHARS at blank lines or line ends; `search` pages by
# hits (docs) and by graph nodes (code). Each page says how to get the next one. Nothing is dropped.
PAGE_CHARS = int(os.environ.get("KB_PAGE_CHARS", "6000"))
PAGE_MIN, PAGE_MAX = 1000, 50_000
UNPAGED = {"version"}
SELF_PAGED = {"search"}
for _t in TOOLS:
    if _t["name"] not in UNPAGED:
        _t["inputSchema"].setdefault("properties", {})["page"] = {
            "type": "integer", "description": "page of a long answer, 1-based (default 1); an answer "
                                              "with more says so at its end"}
        _t["inputSchema"]["properties"]["cursor"] = {
            "type": "string", "description": "the cursor a long answer ends with: the next pages come "
                                             "from that same answer (kept 10 minutes), the other "
                                             "arguments are not needed again"}
        _t["inputSchema"]["properties"]["page_length"] = {
            "type": "integer", "description": f"characters per page (default {PAGE_CHARS}, "
                                              f"{PAGE_MIN}-{PAGE_MAX}); for search: the size of the code "
                                              "half's page (docs pages by `limit` hits)"}
# The values a missing optional argument takes, written into the schemas so a client shows and
# pre-fills them. `project` has none to write: without it the session's projects are used.
DEFAULTS = {
    "search": {"domain": "both", "mode": "auto", "limit": 5, "expand_aliases": True, "explain": False,
               "budget": CODE_BUDGET},
    "graph_routes": {"k": 3, "project": "all"},
}
for _t in TOOLS:
    _props = _t["inputSchema"].get("properties", {})
    for _key, _value in DEFAULTS.get(_t["name"], {}).items():
        _props[_key]["default"] = _value
    if "page" in _props:
        _props["page"]["default"] = 1
        _props["page_length"]["default"] = PAGE_CHARS
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def tools() -> list[dict]:
    """The tools this server publishes: the core ones and those of the add-ons that are on."""
    return [t for t in TOOLS if addons.tool_on(t["name"])]

_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "object": dict, "array": list}


class ToolError(ValueError):
    """Bad request: unknown tool, missing argument, wrong type, value outside an enum."""


def validate(name: str, args: dict, require: bool = True) -> None:
    """Validate arguments against the tool's own schema — no `jsonschema` dependency.

    Deliberately shallow: required keys, primitive types, enum membership. That is what the surface
    actually promises; anything deeper would be a second, unversioned contract living in the checker.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name}")
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    for key in schema.get("required", []) if require else ():
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
GRAPH_VERBS = {"query", "path", "explain", "affected"}


def run(cmd: list[str], cwd: str | None = None, timeout: int = TIMEOUT_GRAPH,
        limit: int | None = MAX_OUT) -> str:
    if cmd[:1] == ["graphify"] and cmd[1:2] and cmd[1] in GRAPH_VERBS:
        worker = warm.graph()
        out = worker.run(cmd[1:]) if worker else None
        if out is not None:  # None: no worker or it failed — fall through to a plain process
            out = out.strip()
            return (out[:limit] if limit else out) or "(no results)"
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
    if warm.ENABLED:  # load the model inside the long-lived qmd, and parse the graph once
        def _prime():
            worker = warm.qmd(KB)
            if worker:
                worker.query([{"type": "vec", "query": "warmup"}], [], 1, None, rerank=False)
            graph = warm.graph()
            if graph and os.path.exists(GRAPH):
                graph.run(["explain", "warmup", "--graph", GRAPH])
                # path search and impact use the view without domain anchors
                graph.run(["explain", "warmup", "--graph", GRAPH], {"allowed": None, "routes": True})
                # graph_routes builds its weighted graph once per view (~5 s on 23k nodes): now
                graph.run(["routes", json.dumps({"from": "", "to": ""}), "--graph", GRAPH],
                          {"allowed": None, "routes": True})
        threading.Thread(target=_prime, daemon=True).start()
        return
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


def suggest(term: str, limit: int = 20, graph: str | None = None, repos: set[str] | None = None,
            no_domain: bool = False) -> str:
    """List exact node labels matching `term` (graphify matches by substring, so an ambiguous term
    hits many). Exact and word-boundary matches rank first.

    `repos` limits the labels to those projects (a caller's view of the main graph): pseudo nodes
    (domains, tables) are left out then, since a hidden project's tables must not show.
    `no_domain` leaves out domain anchors (path and impact run without them)."""
    t = term.strip().lower()
    if not t:
        return ""
    rows = labels(graph)
    if repos is not None:
        rows = [r for r in rows if r[2] in repos]
    if no_domain:
        rows = [r for r in rows if r[1] != "domain" and r[2] != "domain"]
    hits = [row for row in rows if t in row[0].lower()]
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


def with_suggestions(out: str, *terms: str, graph: str | None = None, repos: set[str] | None = None,
                     no_domain: bool = False) -> str:
    """If graphify couldn't pin a unique node, append the candidate exact labels."""
    if "node match" not in out.lower():
        return out
    extra = "".join(suggest(t, graph=graph, repos=repos, no_domain=no_domain) for t in terms if t)
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
        projects, label, _ = scope_projects(args, allowed)
        narrow = sorted(n for n in (projects or ()) if n in DOC_PROJECTS)
        scoped = {k: v for k, v in args.items() if k not in ("project", "scope")}
        if narrow and set(narrow) != allowed:   # the caller narrowed: only those collections
            out = _filter_hits(_docs_once({**scoped, "project": ",".join(narrow), "limit": want * 5}, q),
                               set(narrow), want)
            return note + f"[projects: {label}]\n\n" + out
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


def _searches(args: dict, q: str) -> tuple[list[dict], str | None]:
    """The typed sub-queries (and intent) `qmd query` would run for this mode — for the qmd worker."""
    mode = "lex" if DOCS_MODE == "bm25" else args.get("mode", "auto")
    if mode == "lex":
        return [{"type": "lex", "query": q}], None
    if mode == "vec":
        return [{"type": "vec", "query": q}], None
    searches, intent = [], None
    for line in query_build.build(q).split("\n"):
        kind, sep, text = line.partition(":")
        kind, text = kind.strip(), text.strip()
        if not sep or not text:
            continue
        if kind == "intent":
            intent = text
        elif kind in ("lex", "vec", "hyde"):
            searches.append({"type": kind, "query": text})
    return searches or [{"type": "lex", "query": q}, {"type": "vec", "query": q}], intent


def _docs_warm(args: dict, q: str) -> str | None:
    """The docs search through the long-lived qmd (model already loaded); None = use the CLI."""
    if args.get("explain"):
        return None  # score traces exist only in the CLI
    worker = warm.qmd(KB)
    if worker is None:
        return None
    searches, _intent = _searches(args, q)
    hits = worker.query(searches, resolve_collections(args)[0], args.get("limit") or 5,  # CLI default
                        args.get("min_score"), rerank=DOCS_MODE == "full")
    return None if hits is None else warm.format_hits(hits)[:MAX_OUT]


def _docs_once(args: dict, q: str) -> str:
    out = _docs_warm(args, q)
    if out is not None:
        return out
    out = run(_qmd_cmd(args, q), cwd=KB, timeout=TIMEOUT_DOCS)
    if out == "(timed out)" and args.get("mode", "auto") == "auto":
        # cold start exceeded the budget — fall back to instant BM25 rather than returning nothing
        out = run(_qmd_cmd({**args, "mode": "lex"}, q), cwd=KB, timeout=TIMEOUT_GRAPH)
        if out != "(timed out)" and not _empty(out):
            out = "[semantic search timed out — keyword (BM25) results below]\n\n" + out
    return out


def search_code(args: dict, allowed: set[str] | None = None) -> str:
    """Code half of `search`: a scoped subgraph for the question."""
    return graph_call(["graphify", "query", args["q"]], args, allowed)


def _page_hits(out: str, page: int, want: int) -> str:
    """Hits `want*(page-1)` … `want*page` of a docs answer that holds the first `want*page` hits."""
    blocks = re.split(r"(?m)^(?=qmd://)", out)
    head = blocks[0] if blocks[0].startswith("[") else ""
    hits = blocks[1:]
    if not hits:
        return out
    chosen = hits[want * (page - 1): want * page]
    if not chosen:
        return head + "(no more documents)"
    more = (f"\n\n[page {page} — call again with page: {page + 1} for more documents]"
            if len(hits) >= want * page else "")
    return (head + "".join(chosen)).rstrip() + more


def _code_page(args: dict, allowed: set[str] | None, page: int) -> str:
    """Page `page` of a code search: the nodes a budget of `budget*page` adds to `budget*(page-1)`."""
    if page == 1:
        return search_code(args, allowed)
    b = _budget(args)
    now = search_code({**args, "budget": min(b * page, CODE_BUDGET_MAX * 4)}, allowed)
    before = set(search_code({**args, "budget": min(b * (page - 1), CODE_BUDGET_MAX * 4)}, allowed)
                 .splitlines())
    lines = now.splitlines()
    head = [ln for ln in lines[:3] if ln.startswith(("[projects:", "Traversal:")) or not ln]
    new = [ln for ln in lines if ln.startswith(("NODE ", "EDGE ")) and ln not in before]
    if not new:
        return "\n".join(head) + "\n(no more nodes)"
    more = "truncated" in now.lower() or "TRUNCATED" in now
    tail = f"\n\n[page {page} — call again with page: {page + 1} for more nodes]" if more else ""
    return "\n".join(head + new) + tail


def search(args: dict, allowed: set[str] | None = None) -> str:
    """The one search entry point. `domain` decides which halves run; `both` labels each section."""
    page = args.pop("_page", 1)
    if page > 1:
        want = int(args.get("limit") or 5)

        def docs_page():
            return _page_hits(search_docs({**args, "limit": want * page}, allowed), page, want)

        def code_page():
            return _code_page(args, allowed, page)
        domain = args.get("domain", "both")
        if domain == "docs":
            return docs_page()
        if domain == "code":
            return code_page()
        return f"=== docs ===\n{docs_page()}\n\n=== code ===\n{code_page()}"
    domain = args.get("domain", "both")
    if domain == "docs":
        return search_docs(args, allowed)
    if domain == "code":
        return search_code(args, allowed)
    # The halves are independent subprocesses (qmd, graphify): run them side by side, not in turn.
    with ThreadPoolExecutor(max_workers=2) as pool:
        docs = pool.submit(search_docs, args, allowed)
        code = pool.submit(search_code, args, allowed)
        return f"=== docs ===\n{docs.result()}\n\n=== code ===\n{code.result()}"


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


# A local server (kb-mcp, kb-api) sets this to count its calls; kb-http counts its own (with the user).
LOCAL_USAGE = None


def call_tool(name: str, args: dict, allowed: set[str] | None = None, caller: str | None = None) -> str:
    """Count the call when this process is a local server, then run it (see _call_tool)."""
    if LOCAL_USAGE is None or caller is not None:
        return _call_tool(name, args, allowed, caller)
    start, ok = time.monotonic(), False
    try:
        out = _call_tool(name, args, allowed, caller)
        ok = True
        return out
    finally:
        LOCAL_USAGE.record(name, "local", dict(args or {}), ok, time.monotonic() - start)


def _call_tool(name: str, args: dict, allowed: set[str] | None = None, caller: str | None = None) -> str:
    """Dispatch a validated tool call. Every adapter goes through here — that is the whole point.

    `allowed` = the projects the caller may see; None = everything (local use). `caller` names who
    asks (the shared server's user): a cursor is only valid for the caller and the projects that made it."""
    args = dict(args or {})
    if name in TOOLS_BY_NAME and not addons.tool_on(name):
        raise ToolError(f"unknown tool: {name} (the '{addons.TOOLS[name]}' add-on is off: kb addon install "
                        f"{addons.TOOLS[name]})")
    validate(name, args, require=not args.get("cursor"))  # a cursor stands for the original arguments
    _no_options(name, args)
    if allowed is not None:
        allowed = set(allowed)
    page = args.pop("page", None)
    page = 1 if page is None else page
    if page < 1:
        raise ToolError(f"{name}: 'page' must be 1 or more")
    length = args.pop("page_length", None)
    if length is not None and not PAGE_MIN <= length <= PAGE_MAX:
        raise ToolError(f"{name}: 'page_length' must be {PAGE_MIN}-{PAGE_MAX}")
    cursor = args.pop("cursor", None)
    if name in ("batch", "run_code") and not cursor:
        text = (batch(args["calls"], allowed, caller) if name == "batch"
                else run_code(args["code"], allowed, caller))
        first = paginate(text, page, length)
        if "[page " not in first:
            return first
        return paginate(text, page, length, CURSORS.put(text, name, (caller, frozenset(allowed) if allowed is not None else None)))
    if name in UNPAGED:
        return _dispatch(name, args, allowed)
    owner = (caller, frozenset(allowed) if allowed is not None else None)
    if cursor:
        text = CURSORS.get(cursor, name, owner)
        return paginate(text, page, length, cursor)
    if name in SELF_PAGED:
        if length and "budget" not in args:  # ~4 characters per token
            args["budget"] = max(300, min(length // 4, CODE_BUDGET_MAX))
        if page > 1:  # without a cursor: page by hits and nodes, run again
            return _dispatch(name, {**args, "_page": page}, allowed)
        # page 1 keeps a longer answer (4x the hits and the code budget) behind a cursor
        want = int(args.get("limit") or 5)
        args = {**args, "limit": want * CURSOR_WIDEN,
                "budget": min(_budget(args) * CURSOR_WIDEN, CODE_BUDGET_MAX * CURSOR_WIDEN)}
    text = _dispatch(name, args, allowed)
    first = paginate(text, page, length)
    if "[page " not in first:
        return first
    token = CURSORS.put(text, name, owner)
    return paginate(text, page, length, token)


class Cursors:
    """Answers kept for their next pages: a random token -> (text, tool, owner, expiry). In memory, per
    process; at most CURSOR_MAX of them and CURSOR_TTL seconds each. A token works only for the tool,
    the caller and the projects that made it: another caller gets "expired", not someone's answer."""

    def __init__(self, ttl: float, cap: int):
        self.ttl, self.cap = ttl, cap
        self.items: "OrderedDict[str, tuple[str, str, tuple, float]]" = OrderedDict()
        self.lock = threading.Lock()

    def put(self, text: str, tool: str, owner: tuple) -> str:
        token = "c_" + secrets.token_urlsafe(12)
        with self.lock:
            self._expire()
            while len(self.items) >= self.cap:
                self.items.popitem(last=False)
            self.items[token] = (text, tool, owner, time.monotonic() + self.ttl)
        return token

    def get(self, token: str, tool: str, owner: tuple) -> str:
        with self.lock:
            self._expire()
            hit = self.items.get(token)
        if not hit or hit[1] != tool or hit[2] != owner:
            raise ToolError("cursor expired or unknown: run the query again (cursors live "
                            f"{int(self.ttl // 60)} minutes)")
        return hit[0]

    def _expire(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self.items.items() if v[3] < now]:
            del self.items[k]


CURSOR_TTL = float(os.environ.get("KB_CURSOR_TTL", "600"))
CURSOR_MAX = int(os.environ.get("KB_CURSOR_MAX", "500"))
CURSOR_WIDEN = 4
CURSORS = Cursors(CURSOR_TTL, CURSOR_MAX)


def paginate(text: str, page: int, size: int | None = None, cursor: str | None = None) -> str:
    """Page `page` of `text`. A leading `[projects: …]` line is repeated on every page; the cut falls on
    a blank line when it can, else on a line end."""
    size = size or PAGE_CHARS
    head = ""
    if text.startswith("[projects:"):
        first, _, rest = text.partition("\n")
        head, text = first + "\n\n", rest.lstrip("\n")
    pages, rest = [], text
    while rest:
        if len(rest) <= size:
            pages.append(rest)
            break
        cut = rest.rfind("\n\n", 0, size)
        if cut < size // 3:
            cut = rest.rfind("\n", 0, size)
        if cut < size // 3:
            cut = size
        pages.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    pages = pages or [""]
    if page > len(pages):
        return head + f"(no page {page}: the answer has {len(pages)} page(s))"
    if len(pages) == 1:
        return head + pages[0]
    nav = f"\n\n[page {page} of {len(pages)}"
    if page < len(pages):
        nav += f" — next: page: {page + 1}"
        nav += f', cursor: "{cursor}" (kept {int(CURSOR_TTL // 60)} min, no other arguments needed)]' \
            if cursor else " for the rest]"
    else:
        nav += "]"
    return head + pages[page - 1] + nav


def run_code(code: str, allowed: set[str] | None, caller: str | None) -> str:
    """An agent's program in the sandbox (sandbox/runner.mjs): QuickJS in WebAssembly, inside a Node
    process with the permission model on (read access to the sandbox folder only: no network, no
    child processes, no workers, no writes). Each kb() call comes back here and runs through
    call_tool with the caller's own rights; run_code and batch cannot be called from inside."""
    import resource
    import select

    if not os.path.isdir(os.path.join(SANDBOX, "node_modules", "quickjs-emscripten")):
        raise ToolError("run_code: the sandbox is not installed (kb addon install code-mode)")
    if len(code) > 20_000:
        raise ToolError("run_code: the program is longer than 20000 characters")

    def limits():  # the Node process itself: CPU seconds and open files
        resource.setrlimit(resource.RLIMIT_CPU, (RUN_SECONDS + 10, RUN_SECONDS + 10))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    proc = subprocess.Popen(
        ["node", "--permission", f"--allow-fs-read={SANDBOX}", os.path.join(SANDBOX, "runner.mjs")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=SANDBOX,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NODE_OPTIONS": ""}, preexec_fn=limits)
    deadline = time.monotonic() + RUN_SECONDS + 15  # the script's own deadline + tool time
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps({"code": code, "deadline_ms": RUN_SECONDS * 1000,
                                     "max_calls": RUN_CALLS, "memory_mb": 64}) + "\n")
        proc.stdin.flush()
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([proc.stdout], [], [], left)[0]:
                raise ToolError(f"run_code: stopped after {RUN_SECONDS + 15} s")
            line = proc.stdout.readline()
            if not line:
                err = (proc.stderr.read() if proc.stderr else "")[-400:]
                raise ToolError(f"run_code: the sandbox stopped ({err.strip() or 'no message'})")
            msg = json.loads(line)
            if msg.get("done"):
                break
            tool, args = str(msg.get("call", "")), msg.get("args") or {}
            if tool in NOT_IN_BATCH:
                reply = {"error": f"'{tool}' cannot be called from run_code"}
            else:
                try:
                    reply = {"result": call_tool(tool, dict(args), allowed, caller)}
                except ToolError as e:
                    reply = {"error": str(e)}
            proc.stdin.write(json.dumps(reply) + "\n")
            proc.stdin.flush()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
    parts = []
    if msg.get("output"):
        parts.append(msg["output"].rstrip())
    if msg.get("error"):
        parts.append(f"(error: {msg['error']})")
    value = msg.get("value")
    if value is not None:
        parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
    parts.append(f"[run_code: {msg.get('calls', 0)} kb() call(s)]")
    return "\n\n".join(parts)


def batch(calls: list, allowed: set[str] | None, caller: str | None) -> str:
    """Each call through call_tool with the caller's own rights, side by side; one failing call is
    reported in its place and does not stop the others."""
    if not 1 <= len(calls) <= BATCH_MAX:
        raise ToolError(f"batch: 1-{BATCH_MAX} calls")
    for c in calls:
        if not isinstance(c, dict) or not isinstance(c.get("tool"), str):
            raise ToolError("batch: every call is {tool, args}")
        if c["tool"] in NOT_IN_BATCH:
            raise ToolError(f"batch: '{c['tool']}' cannot be inside a batch")
        if not isinstance(c.get("args", {}), dict):
            raise ToolError("batch: args must be an object")

    def one(c: dict) -> str:
        try:
            return call_tool(c["tool"], dict(c.get("args") or {}), allowed, caller)
        except ToolError as e:
            return f"(error: {e})"
    with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
        answers = list(pool.map(one, calls))
    return "\n\n".join(f"=== [{i}] {c['tool']} {json.dumps(c.get('args') or {}, ensure_ascii=False)} ===\n{a}"
                         for i, (c, a) in enumerate(zip(calls, answers), 1))


def _dispatch(name: str, args: dict, allowed: set[str] | None) -> str:
    if name == "search":
        return search(args, allowed)
    if name == "graph_path":
        return graph_call(["graphify", "path", args["from"], args["to"]], args, allowed,
                          args["from"], args["to"])
    if name == "graph_routes":
        return graph_routes(args, allowed)
    if name == "review_context":
        return review_context(args, allowed)
    if name == "code_grep":
        return code_grep(args, allowed)
    if name == "repo_map":
        return repo_map(args, allowed)
    if name in ("graph_explain", "graph_affected"):
        verb = "explain" if name == "graph_explain" else "affected"
        return graph_call(["graphify", verb, args["symbol"]], args, allowed, args["symbol"])
    if name == "docs_get":
        return docs_get(args["path"], allowed)
    if name == "contracts_search":
        return _index(contracts.contracts_search, args["query"], None, 100_000)
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
    raise ToolError(f"unknown tool: {name}")  # unreachable: validate() already checked


def version_info() -> dict:
    """Two axes, never merged: the repo release and the surface's contract."""
    return {"repo": VERSION, "api_contract": API_CONTRACT, "collection": COLLECTION,
            "projects": list(PROJECTS), "session_projects": HOME_PROJECTS,
            "aliases": len(ALIASES), "license": "AGPL-3.0-or-later", "source": SOURCE_URL}
