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

import contextlib
import contextvars
import fnmatch
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import tomllib
from collections import OrderedDict
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import access
import addons
import contracts
import datadict
import docs_grade
import paths
import query_build
import settings
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
def _project_blocks(cfg=None) -> list[dict]:
    """The `[[project]]` tables of kb.projects.toml; [] when the file is missing or broken."""
    cfg = cfg or paths.config_file("kb.projects.toml")
    try:
        with open(cfg, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    blocks = data.get("project")
    return [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []


def load_projects(cfg=None) -> dict[str, list[str]]:
    """name -> every path the project lists (code, docs, routes, calls), from kb.projects.toml."""
    out: dict[str, list[str]] = {}
    for block in _project_blocks(cfg):
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        roots = []
        for key in ("code", "docs", "routes", "calls"):
            value = block.get(key)
            for r in ([value] if isinstance(value, str) else value if isinstance(value, list) else []):
                if isinstance(r, str) and r:
                    roots.append(os.path.normpath(r))
        out[name] = roots
    return out


def _has_docs(name: str, cfg=None) -> bool:
    return any(b.get("name") == name and "docs" in b for b in _project_blocks(cfg))


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
    if "_collections" in args:   # set by the core for a limited caller: never empty
        names = list(args["_collections"])
        if not names:
            raise ToolError("no collections to search")
        return names, ", ".join(names)
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
    return _same_grant(allowed), ", ".join(sorted(allowed)), False


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

    extra = ["--budget", str(args.get("_wide") or _budget(args))] if cmd[1] == "query" else []
    if cmd[1] == "affected":
        extra = [x for r in AFFECTED_RELATIONS for x in ("--relation", r)]

    if cmd[1] in ("explain", "affected") and terms:
        cmd, args, terms = _pinned(cmd, args, allowed, terms)

    warm_ok = [True]   # the worker failed once in this call: the rest of the call runs processes

    def ask(projects: set[str] | None, c: list[str] | None = None) -> tuple[str, dict]:
        """-> (output, how to suggest labels). The warm worker cuts the caller's view of the main
        graph in memory; without it, a filtered file per project set is written and read."""
        c = c or cmd
        worker = warm.graph() if warm_ok[0] else None
        if worker is not None and os.path.exists(GRAPH):
            full = _full(projects)
            view = {"allowed": None if full else sorted(projects),
                    "routes": c[1] in ("path", "affected", "query")}
            out = worker.run([*c[1:], "--graph", GRAPH, *extra], view)  # c[0] is "graphify"
            if out is not None:
                out = out.strip()
                return (out[:MAX_OUT] or "(no results)"), {
                    "graph": GRAPH, "repos": None if full else set(projects),
                    "no_domain": view["routes"]}
            warm_ok[0] = False
        graph = graph_of(projects)
        return run([*c, "--graph", graph, *extra], warm_ok=False), {"graph": graph}

    def answer_picked(projects: set[str] | None, label: str) -> str | None:
        """The answer for a name that the picks below resolve (a member, a class, a name defined
        twice) in this scope; None when they do not apply."""
        if cmd[1] not in ("explain", "affected") or not terms:
            return None
        scope = projects if projects is not None else allowed
        picked = (_pick_member(terms[0], scope) or _pick_classes(terms[0], scope)
                  or _pick_same_name(terms[0], scope))
        if picked:
            classes, others = picked
            kind = "classes" if all(r[1] in CLASS_TYPES for r in classes) else "symbols"
            parts = []
            for lab, typ, repo, file, nid in classes[:AMBIGUOUS_MAX]:
                if cmd[1] == "explain":   # graphify's explain finds nodes by label only, never by ID
                    sub = _explain_node((lab, typ, repo, file, nid), projects)
                else:
                    sub, _ = ask(projects, [cmd[0], cmd[1], nid])
                parts.append(sub if len(classes) == 1 else f"### {lab}  ({typ}, {repo} {file})\n{sub}")
            out = parts[0] if len(classes) == 1 else (
                f"'{terms[0]}' names {len(classes)} {kind}; the answer for each:\n\n" + "\n\n".join(parts))
            if len(classes) > AMBIGUOUS_MAX:
                out += f"\n\n(+{len(classes) - AMBIGUOUS_MAX} more {kind} with this name: pin one as repo:Name)"
            if others:
                shown = "; ".join(f"{r[0]} ({r[1]}, {r[2]} {r[3]})" for r in others[:5])
                more = f" and {len(others) - 5} more" if len(others) > 5 else ""
                out += (f"\n\n(Also named like this, not shown: {shown}{more}. Ask for one by its node ID "
                        "or as Class.method.)")
            return f"[projects: {label}]\n\n{out}"
        return None

    projects, label, auto = scope_projects(args, allowed)
    picked_out = answer_picked(projects, label)
    if picked_out is not None:
        return picked_out
    out, where = ask(projects)
    if auto and _no_match(out):
        # the session's projects had nothing: everything below works on the widened scope
        projects, label = allowed, f"all (nothing in {label[:-7]})"
        picked_out = answer_picked(projects, label)
        if picked_out is not None:
            return picked_out
        out, where = ask(projects)
    if cmd[1] == "explain":
        out = _full_connections(out, projects)
    if terms:
        out = with_suggestions(out, *terms, **where)
    if cmd[1] in ("explain", "affected") and "EXACT labels" in out:
        # A name defined twice (an interface and its implementation, a model method and a helper)
        # used to stop here and cost the agent a second call. Answer for each match instead (up to 3).
        def norm(x: str) -> str:
            return x.strip().strip(".").removesuffix("()").lower()
        want = norm(terms[0]) if terms else ""
        exact = [r for r in labels(GRAPH) if norm(r[0]) == want and r[1] != "domain" and r[4]
                 and (projects is None or r[2] in projects)]
        parts = []
        for lab, typ, repo, file, nid in exact[:AMBIGUOUS_MAX]:
            if cmd[1] == "explain":   # graphify's explain finds nodes by label only, never by ID
                sub = _explain_node((lab, typ, repo, file, nid), projects)
            else:
                sub, _ = ask(projects, [cmd[0], cmd[1], nid])
            parts.append(f"### {lab}  ({typ}, {repo} {file})\n{sub}")
        if parts:
            more = f"\n\n(+{len(exact) - AMBIGUOUS_MAX} more matches: name one exactly)" if len(exact) > AMBIGUOUS_MAX else ""
            out = (f"'{terms[0] if terms else ''}' names {len(exact)} symbols; the answer for each:\n\n"
                   + "\n\n".join(parts) + more)
    if cmd[1] == "query":
        out = _mcp_hint(out)
    return f"[projects: {label}]\n\n{out}"


_PIN = re.compile(r"^([\w-]+):(?!:)(.+)$")


def _pinned(cmd: list[str], args: dict, allowed: set[str] | None, terms: tuple):
    """`api:Customer` in graph_explain / graph_affected: the name `Customer` in the project `api`. The
    prefix pins only when it is a known project (`OrderService::create` stays a name); a project the
    caller may not see is refused like an unknown one, and a pin outside an explicit `project` is
    refused too. -> (cmd, args, terms) with the bare name and the scope narrowed to the project."""
    m = _PIN.match(terms[0].strip())
    if not m or m.group(1) not in PROJECTS:
        return cmd, args, terms
    repo, name = m.group(1), m.group(2).strip()
    if allowed is not None and repo not in allowed:
        raise ToolError(f"unknown or not allowed project: {repo}")
    want = (args.get("project") or "").strip()
    if want and want.lower() != "all" and repo not in {n.strip() for n in want.split(",")}:
        raise ToolError(f"'{terms[0]}' pins the project {repo}, which is not in project: {want}")
    return ([*cmd[:2], name, *cmd[3:]], {**args, "project": repo}, (name, *terms[1:]))


CLASS_TYPES = {"model", "controller", "class", "service", "interface", "trait", "enum"}


def _pick_classes(term: str, projects: set[str] | None):
    """A name typed exactly as a class (`Customer`, `ObjectController`) means the class, not the
    methods and templates that share it in lower case (`.customer()`). -> (classes, others), or None
    when no class has exactly this name (then graphify's own matching answers, as before)."""
    name = term.strip()
    if not name or ":" in name or "(" in name or "." in name:
        return None

    def norm(x: str) -> str:
        return x.strip().strip(".").removesuffix("()").lower()
    rows = [r for r in labels(GRAPH) if r[1] != "domain" and r[4]
            and (projects is None or r[2] in projects) and norm(r[0]) == name.lower()]
    classes = [r for r in rows if r[0].strip() == name and r[1] in CLASS_TYPES]
    if not classes or len(rows) < 2:
        return None
    others = [r for r in rows if r not in classes]
    return classes, others


# `Class::method`, `Class.method` or `Class.method()`; a namespace before the class is allowed.
_MEMBER = re.compile(r"^([A-Za-z_\\][\w\\]*)(?:::|\.)([A-Za-z_$][\w$]*)(?:\(\))?$")
_FILE_EXT = {"php", "vue", "ts", "tsx", "js", "jsx", "mjs", "md", "json", "blade"}


def _pick_member(term: str, projects: set[str] | None):
    """`OrderService::create` / `OrderService.create`: the methods named `create` of the classes named
    `OrderService`, found through the class's `method`/`contains` edges. -> (rows, []) or None (not
    that shape, a node already carries the whole name, or no such member: graphify answers)."""
    m = _MEMBER.match(term.strip())
    if not m or m.group(2).lower() in _FILE_EXT:
        return None
    cls, member = m.group(1).rsplit("\\", 1)[-1], m.group(2)
    index = _graph_index(GRAPH)
    rows = [r for r in index["rows"] if r[1] != "domain" and r[4] and (projects is None or r[2] in projects)]
    whole = term.strip().removesuffix("()")
    if any(r[0].strip().removesuffix("()") == whole for r in rows):
        return None

    def bare(label: str) -> str:
        return label.strip().lstrip(".").removesuffix("()")
    by_id = {r[4]: r for r in rows}
    found = []
    for r in rows:
        if r[1] in CLASS_TYPES and r[0].strip() == cls:
            kids = [by_id[k] for k in index["members"].get(r[4], []) if k in by_id]
            same = [k for k in kids if bare(k[0]) == member] or [k for k in kids if bare(k[0]).lower() == member.lower()]
            found += [k for k in same if k not in found]
    return (found, []) if found else None


def _pick_same_name(term: str, projects: set[str] | None):
    """A name that several nodes carry exactly as typed (two `.create()` methods, a function and a
    template), of any type. -> (rows, []) to answer each by its node ID, or None (one or no match:
    graphify's own matching answers)."""
    name = term.strip().strip(".").removesuffix("()")
    if not name or ":" in name or "(" in name:
        return None
    rows = [r for r in labels(GRAPH) if r[1] != "domain" and r[4]
            and (projects is None or r[2] in projects) and r[0].strip().strip(".").removesuffix("()") == name]
    return (rows, []) if len(rows) >= 2 else None


def _explain_node(row: tuple, projects: set[str] | None) -> str:
    """graph_explain for one node picked by ID: its header and every connection (bin/routes.py)."""
    lab, typ, repo, file, nid = row
    full = _full(projects)
    view = {"allowed": None if full else sorted(projects), "routes": False}
    argv = ["neighbors", nid, "--graph", GRAPH]
    worker = warm.graph()
    lst = worker.run(argv, view) if worker is not None else None
    if lst is None:
        lst = _graph_once(argv, view)
    lst = _cap_text((lst or "").strip()) or "All connections (0):"
    return f"Node: {lab}\n  ID:        {nid}\n  Source:    {file}\n  Type:      {typ}\n  Project:   {repo}\n\n{lst}"


def _full_connections(out: str, projects: set[str] | None) -> str:
    """graphify's `explain` lists 20 connections and "... and N more": replace that list with all of
    them (the answer is paged), from the same view of the graph."""
    m = re.search(r"(?m)^  ID:\s+(\S+)", out)
    if not m or not re.search(r"(?m)^\s+\.\.\. and \d+ more$", out):
        return out
    full = _full(projects)
    view = {"allowed": None if full else sorted(projects), "routes": False}
    argv = ["neighbors", m.group(1), "--graph", GRAPH]
    worker = warm.graph()
    lst = worker.run(argv, view) if worker is not None else None
    if lst is None:
        lst = _graph_once(argv, view)
    if not lst:
        return out
    head = re.split(r"(?m)^Connections \(\d+\):$", out)[0].rstrip()
    return head + "\n\n" + _cap_text(lst.strip())


def _cap_text(text: str) -> str:
    """At most MAX_OUT characters of a producer's text, cut on a line end, with a note."""
    if len(text) <= MAX_OUT:
        return text
    cut = text.rfind("\n", 0, MAX_OUT)
    return text[:cut if cut > 0 else MAX_OUT] + f"\n… (cut at {MAX_OUT} characters: narrow the call)"


def graph_routes(args: dict, allowed: set[str] | None) -> str:
    """Several weighted paths (bin/routes.py), on the caller's view without domain anchors. A project
    pinned in a name (`api:Customer`) must be one the caller may see."""
    for end in (args["from"], args["to"]):
        m = re.match(r"^([\w-]+):", end)
        if m and m.group(1) in PROJECTS and allowed is not None and m.group(1) not in allowed:
            raise ToolError(f"unknown or not allowed project: {m.group(1)}")
    scoped = {**args, "project": args.get("project") or "all"}
    projects, label, _ = scope_projects(scoped, allowed)
    full = _full(projects)
    view = {"allowed": None if full else sorted(projects), "routes": True}
    payload = json.dumps({"from": args["from"], "to": args["to"], "k": args.get("k") or 3})
    argv = ["routes", payload, "--graph", GRAPH]
    worker = warm.graph()
    out = worker.run(argv, view) if worker is not None else None
    if out is None:
        out = _graph_once(argv, view)
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
    limit = max(1, min(int(args.get("limit") or 50), GREP_MAX))
    glob = str(args.get("glob") or "")
    rg = shutil.which("rg")
    if rg:
        # --sort path: the same lines in the same order on every call, so `page` is stable
        cmd = [rg, "--no-config", "--sort", "path", "-n", "-H", "--no-heading", "--max-columns", "300", "--max-count", "20",
               "-g", "!graphify-out/**", "-g", "!.env*", "-e", pattern]
        if glob:
            cmd += ["-g", glob]
        if args.get("ignore_case"):
            cmd.append("-i")
        cmd += ["-g", "!bootstrap/cache/**"]          # Laravel's generated route/config caches
    else:
        # grep reads hidden files (ripgrep does not): keep secrets and VCS data out
        cmd = ["grep", "-rEnHI", "--exclude-dir=graphify-out", "--exclude-dir=.git", "-m", "20", "-e", pattern]
        if glob and "/" not in glob:  # grep's --include sees the file name only; a path glob is matched below
            cmd.append(f"--include={glob}")
        # after --include: with GNU grep the first of them decides what a file matching neither gets
        cmd.append("--exclude=.env*")
        if args.get("ignore_case"):
            cmd.append("-i")
    path_glob = glob if glob and "/" in glob and not rg else ""
    lines: list[str] = []
    for name, root in roots:
        timed_out = _grep_root([*cmd, "--", "."], root, name, path_glob, lines, limit)
        if timed_out:
            lines.append(f"{name}: (search timed out: narrow the pattern or add 'glob')")
        if len(lines) >= limit:
            break
    if not lines:
        return f"[projects: {label}]\n\n(no matches)"
    more = f"\n\n(stopped at {limit} lines: raise 'limit' or narrow the pattern)" if len(lines) >= limit else ""
    return f"[projects: {label}]\n\n" + "\n".join(lines) + more


def _grep_hidden(path: str) -> bool:
    """Files code_grep never shows, whichever tool searched: Laravel's generated caches
    (bootstrap/cache, at any depth) and .env files (secrets)."""
    return ("/" + path).find("/bootstrap/cache/") >= 0 or os.path.basename(path).startswith(".env")


def _grep_root(cmd: list[str], root: str, name: str, path_glob: str, lines: list[str], limit: int) -> bool:
    """Add the matches under `root` to `lines` until `limit`; reading stops there and the process is
    stopped. -> True when the search timed out. A bad pattern is a ToolError."""
    stopped = threading.Event()
    with _Job(GRAPH_JOBS, "graph"), tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=err, text=True,
                                errors="replace")

        def stop() -> None:
            stopped.set()
            proc.kill()
        timer = threading.Timer(GREP_TIMEOUT, stop)
        timer.start()
        full = False
        try:
            assert proc.stdout
            for ln in proc.stdout:
                path, _, rest = ln.rstrip("\n").partition(":")
                path = path.removeprefix("./")
                if _grep_hidden(path):
                    continue
                if path_glob and not fnmatch.fnmatch(path, path_glob.removeprefix("./")):
                    continue
                lines.append(f"{name}:{path}:{rest[:300]}")
                if len(lines) >= limit:
                    full = True
                    break
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()
            code = proc.wait()
        if full:
            return False
        if stopped.is_set():
            return True
        if code not in (0, 1):
            # Exit 2 also means "a file could not be read", even when other files matched: keep
            # those matches. Only an error that is not about a file is a bad pattern.
            err.seek(0)
            text = err.read(20_000)
            other = [e for e in text.splitlines() if e.strip() and not _FILE_ERROR.search(e)]
            if other:
                raise ToolError(f"code_grep: bad pattern: {' '.join(e.strip() for e in other)[:200]}")
            lines.append(f"{name}: (some files could not be read; the other matches are shown)")
    return False


# grep / rg messages about one file, not about the pattern
_FILE_ERROR = re.compile(r"Permission denied|No such file or directory|Input/output error|Is a directory|"
                         r"Too many levels of symbolic links|Operation not permitted|binary file matches",
                         re.IGNORECASE)


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
        out = _graph_once(argv, view)
    if out is None:
        raise ToolError(f"{what} needs graphify's Python environment; it was not found")
    return out.strip()


def repo_map(args: dict, allowed: set[str] | None) -> str:
    """The most depended-on parts of a project within a token budget (bin/repomap.py).

    `unused`: the candidates come from the mapped projects, but what depends on them is looked up in
    every project the caller may see (a class used only by another project is not unused). The worker
    finds the candidates; the file scan for their names runs here, so it does not hold the worker."""
    import repomap
    projects, label, _ = scope_projects(args, allowed)
    names = sorted(PROJECTS) if projects is None else sorted(projects)
    budget = max(300, min(int(args.get("budget") or 1500), 8000))
    if args.get("unused"):
        visible = sorted(PROJECTS) if allowed is None else sorted(allowed)
        view = {"allowed": None if _full(allowed) else visible, "routes": True}
        payload = json.dumps({"projects": names, "unused": True, "raw": True})
        raw = _worker(["map", payload, "--graph", GRAPH], view, "repo_map")
        try:
            found = {str(k): [str(x) for x in v] for k, v in json.loads(raw).items()}
        except (ValueError, AttributeError):
            return f"[projects: {label}]\n\n{raw}"
        roots = {n: os.path.join(KB, "repos", n) for n in visible if os.path.isdir(os.path.join(KB, "repos", n))}
        note = ""
        if roots:
            found, complete = _unused_scan(found, roots)
            if not complete:
                note = (f"\n\n(the file scan stopped after {UNUSED_SCAN_SECONDS} s: some of these may be "
                        "named in files it did not read)")
        return f"[projects: {label}]\n\n" + repomap.format_unused(found, scanned=bool(roots)) + note
    full = allowed is None and projects is None
    view = {"allowed": None if full else names, "routes": True}
    payload = json.dumps({"routes": _visible_routes(allowed), "projects": names, "budget": budget})
    return f"[projects: {label}]\n\n" + _worker(["map", payload, "--graph", GRAPH], view, "repo_map")


UNUSED_SCAN_SECONDS = int(os.environ.get("KB_UNUSED_SCAN_SECONDS", "20"))
_UNUSED_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_UNUSED_LOCK = threading.Lock()


def _unused_scan(found: dict[str, list[str]], roots: dict[str, str]) -> tuple[dict[str, list[str]], bool]:
    """repomap.scan_names in a graph job slot, within UNUSED_SCAN_SECONDS. A complete scan is kept
    per graph build (the staged code changes with it), candidates and roots."""
    import repomap
    try:
        mtime = os.path.getmtime(GRAPH)
    except OSError:
        mtime = 0.0
    key = json.dumps([mtime, sorted(found.items()), sorted(roots.items())])
    with _UNUSED_LOCK:
        hit = _UNUSED_CACHE.get(key)
        if hit is not None:
            _UNUSED_CACHE.move_to_end(key)
            return hit
    with _Job(GRAPH_JOBS, "graph"):
        out = repomap.scan_names(found, roots, time.monotonic() + UNUSED_SCAN_SECONDS)
    if out[1]:
        with _UNUSED_LOCK:
            _UNUSED_CACHE[key] = out
            while len(_UNUSED_CACHE) > 16:
                _UNUSED_CACHE.popitem(last=False)
    return out


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
            raise ValueError("pass 'diff' (git diff output) or 'files'")
    except ValueError as e:
        raise ToolError(f"review_context: {e}") from None
    if not changes:
        return "(no changed files in the diff)"
    project = args.get("project") or ""
    if project and (project not in PROJECTS or (allowed is not None and project not in allowed)):
        raise ToolError(f"unknown or not allowed project: {project}")
    visible = set(PROJECTS) if allowed is None else set(allowed)
    view = {"allowed": None if _full(allowed) else sorted(visible), "routes": True}
    payload = json.dumps({"changes": {p: sorted(v) for p, v in changes.items()},
                          "routes": _visible_routes(allowed), "project": project})
    return _worker(["review", payload, "--graph", GRAPH], view, "review_context")


AMBIGUOUS_MAX = 3   # a name matching several symbols is answered for this many of them
CODE_BUDGET, CODE_BUDGET_MAX = 1800, 8000
# graphify's own advice names CLI options and tools this server does not have.
_CLI_HINT = re.compile(r"Narrow with context_filter=\[[^\]]*\] or use get_node for a specific symbol")


def _budget(args: dict) -> int:
    try:
        return max(300, min(int(args.get("budget") or CODE_BUDGET), CODE_BUDGET_MAX))
    except (TypeError, ValueError):
        return CODE_BUDGET


def _mcp_hint(out: str) -> str:
    return _CLI_HINT.sub(f"Raise `budget` (max {CODE_BUDGET_MAX}), narrow `project`, or ask graph_explain "
                         "about one symbol", out)


def _widened(args: dict) -> int:
    """The budget every page of a code search runs with: CURSOR_WIDEN times the caller's budget, so a
    longer answer is kept behind the cursor. It goes to graphify as `_wide`, past _budget()'s cap."""
    return _budget(args) * CURSOR_WIDEN


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
        + ("which table holds some data -> search (its data dictionary block names the table, grain and "
           "the columns that may be summed); " if addons.on("datadict") else "") +
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
RUN_SLACK = 15   # seconds past RUN_SECONDS before a silent sandbox is stopped (see _run_sandbox)
# run_code's sandbox: installed by `kb addon install sandbox` into the data folder (a tool install keeps
# its package read-only); a checkout may also have it in place (`npm ci` in sandbox/).
_SANDBOX_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")
_SANDBOX_DATA = os.path.join(str(paths.data_home()), "sandbox")
SANDBOX = _SANDBOX_DATA if os.path.isdir(os.path.join(_SANDBOX_DATA, "node_modules")) else _SANDBOX_PKG
# Set: run_code connects to a sandbox service on this Unix socket (sandbox/socket-server.mjs, e.g. a
# second container without network, docs/docker.md) instead of starting Node here. Same protocol.
SANDBOX_SOCKET = os.environ.get("KB_SANDBOX_SOCKET", "")
# What one program may give back: its printed output and its return value each up to this many
# characters (the runner cuts them too), and one protocol line from the sandbox up to RUN_MAX_LINE.
RUN_MAX_VALUE = int(os.environ.get("KB_RUN_MAX_OUTPUT", "60000"))
RUN_MAX_LINE = 4 * RUN_MAX_VALUE + 1_000_000
NOT_IN_BATCH = {"batch", "run_code"}
RUN_JOBS = threading.BoundedSemaphore(int(os.environ.get("KB_MAX_RUN_JOBS", "2")))

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
         "limit": {"type": "integer", "description": "max results (docs), 1-50; more documents: raise it"},
         "min_score": {"type": "number", "description": "drop docs results below this score"},
         "expand_aliases": {"type": "boolean", "description": "apply the alias table (default true)"},
         "explain": {"type": "boolean", "description": "include retrieval score traces"},
         "refuse": {"type": "boolean",
                    "description": "docs: grade the first hit; when it holds too little of the question's "
                                   "words, try a keyword-only search, then answer 'not found' and mark the "
                                   "nearest hits as weak (default true; false = always the nearest hits)"},
         "budget": {"type": "integer", "description": "code: answer size in tokens (default 1800, "
                                                     "max 8000); raise it when the result says it was truncated"}},
         "required": ["q"]}},
    {"name": "graph_path",
     "description": "The one best path between two symbols/files in the code graph: calls and HTTP "
                    "requests (frontend -> controller) are cheap, busy hub classes dear, so it reads "
                    "like a real chain. For several alternatives use graph_routes.",
     "inputSchema": {"type": "object", "properties": {"from": {"type": "string"}, "project": {"type": "string", "description": "limit to these project(s): a name, several comma-separated, or 'all'. Default: all the caller may see"},
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
         "diff": {"type": "string", "description": "unified diff text, up to 1 MB (the shared server "
                                                        "takes request bodies up to 1 MB in all)"},
         "files": {"type": "array", "items": {"type": "string"},
                   "description": "instead of a diff: paths, optionally with lines, e.g. 'app/X.php:10-20,31'; "
                                  "at most 1000 items and 200000 lines in all"},
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
                    f"no timers; at most {RUN_CALLS} kb() calls and {RUN_SECONDS} s, the time of the kb() calls included "
                    "(a call that runs finishes first). The tools answer with "
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
                    "Covers every project under the default setting `contracts: all`; with "
                    "`contracts: own` a caller limited to some projects sees only the routes that "
                    "touch them.",
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

# Every tool that can answer at length takes `page`. Answers are cut into pages of PAGE_CHARS at blank
# lines or line ends; `search` cuts one text too (its docs hits and its widened code answer), so a page
# is the same with or without the cursor. Each page says how to get the next one. Nothing is dropped.
PAGE_CHARS = int(os.environ.get("KB_PAGE_CHARS", "6000"))
PAGE_MIN, PAGE_MAX = 1000, 50_000
UNPAGED = {"version"}
SELF_PAGED = {"search"}
# External add-ons (bin/plugins.py): their tools join the list while they are installed and on.
PLUGIN_TOOLS: dict[str, object] = {}
try:
    import plugins as _plugins
    for _mod in _plugins.active(addons.enabled()):
        for _t in getattr(_mod, "TOOLS", []):
            if _t.get("name") and not any(x["name"] == _t["name"] for x in TOOLS):
                TOOLS.append({**_t, "inputSchema": json.loads(json.dumps(_t.get("inputSchema") or {"type": "object"}))})
                PLUGIN_TOOLS[_t["name"]] = _mod
except Exception as _e:  # noqa: BLE001 — a broken plugin never takes the core down
    sys.stderr.write(f"kb: plugins not loaded: {_e}\n")
for _t in TOOLS:
    if _t["name"] not in UNPAGED:
        _t["inputSchema"].setdefault("properties", {})["page"] = {
            "type": "integer", "description": "page of a long answer, 1-based (default 1); an answer "
                                              "with more says so at its end"}
        _t["inputSchema"]["properties"]["cursor"] = {
            "type": "string", "description": "the cursor a long answer ends with: alone it gives the "
                                             "next page of that same answer (kept 10 minutes); add "
                                             "`page` to jump. Other arguments are not needed again"}
        _t["inputSchema"]["properties"]["page_length"] = {
            "type": "integer", "description": f"characters per page (default {PAGE_CHARS}, "
                                              f"{PAGE_MIN}-{PAGE_MAX}); for search it also sets the code "
                                              "budget when `budget` is not given"}
# The values a missing optional argument takes, written into the schemas so a client shows and
# pre-fills them. `project` has none to write: without it the session's projects are used.
DEFAULTS = {
    "search": {"domain": "both", "mode": "auto", "limit": 5, "expand_aliases": True, "explain": False,
               "refuse": True, "budget": CODE_BUDGET},
    "graph_routes": {"k": 3, "project": "all"},
    "graph_path": {"project": "all"},
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

# Job slots: how many graphify and qmd processes may run at once (qmd loads a model each time). They
# are taken where a process starts, not around a tool call, so a nested call (batch, run_code, the
# two halves of `search`) cannot start more. A call that waits longer than JOB_WAIT seconds for a
# slot is refused as busy. The warm workers (bin/warm.py) start no process per call and take none.
GRAPH_JOBS = threading.BoundedSemaphore(int(os.environ.get("KB_MAX_GRAPH_JOBS", "3")))
DOCS_JOBS = threading.BoundedSemaphore(int(os.environ.get("KB_MAX_DOCS_JOBS", "1")))
JOB_WAIT = float(os.environ.get("KB_JOB_WAIT", "120"))
# A run_code program sets this to the end of its own time: its nested calls wait for a slot only
# until then, not JOB_WAIT. A context variable, so each program (thread) has its own.
_WAIT_UNTIL: contextvars.ContextVar[float | None] = contextvars.ContextVar("kb_wait_until", default=None)


def _wait_left() -> float:
    """Seconds a call may wait for a job slot or a lock: JOB_WAIT, or less inside a run_code program."""
    until = _WAIT_UNTIL.get()
    return JOB_WAIT if until is None else max(0.0, min(JOB_WAIT, until - time.monotonic()))


class _Job:
    """`with _Job(DOCS_JOBS, "docs"):` one process in a slot, or ToolError "busy"."""

    def __init__(self, slots: threading.BoundedSemaphore, kind: str):
        self.slots, self.kind = slots, kind

    def __enter__(self):
        if not self.slots.acquire(timeout=_wait_left()):
            raise ToolError(f"busy: every {self.kind} job slot is taken; try again in a minute")
        return self

    def __exit__(self, *exc) -> None:
        self.slots.release()


# The filtered-copy cut of bin/access.py loads the whole graph in a Python process: it takes a
# graph slot like graphify does, and waits for its lock no longer than a slot.
access.job_slot = lambda: _Job(GRAPH_JOBS, "graph")
access.wait_seconds = _wait_left


def _slots(cmd: list[str]) -> _Job | None:
    if cmd[:1] == ["qmd"]:
        return _Job(DOCS_JOBS, "docs")
    if cmd[:1] == ["graphify"]:
        return _Job(GRAPH_JOBS, "graph")
    return None


PROC_MAX_BYTES = 16_000_000   # a process whose whole output is parsed (qmd --format json)


def run(cmd: list[str], cwd: str | None = None, timeout: int = TIMEOUT_GRAPH,
        limit: int | None = MAX_OUT, warm_ok: bool = True) -> str:
    """Run a qmd or graphify command. A graph lookup goes to the warm worker first, unless the caller
    already saw the worker fail (`warm_ok=False`)."""
    if warm_ok and cmd[:1] == ["graphify"] and cmd[1:2] and cmd[1] in GRAPH_VERBS:
        worker = warm.graph()
        out = worker.run(cmd[1:]) if worker else None
        if out is not None:  # None: no worker or it failed — fall through to a plain process
            out = out.strip()
            return (out[:limit] if limit else out) or "(no results)"
    try:
        # stdout is read as it comes and the process is stopped past the byte limit (4 bytes per
        # character at most in UTF-8), so a huge answer never sits whole in memory
        with _slots(cmd) or contextlib.nullcontext():
            code, stdout, stderr, cut = warm.run_capped(cmd, (limit or 0) * 4 or PROC_MAX_BYTES,
                                                        timeout, cwd=cwd)
        if cut:
            code = 0   # stopped by us at the limit, not a failure of the tool
        out = (stdout or "") + (("\n[stderr] " + stderr) if code and stderr else "")
        out = out.strip()
        return (out[:limit] if limit else out) or "(no results)"
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        return f"(error: {e})"


def _graph_once(argv: list[str], view: dict | None) -> str | None:
    """warm.graph_once (one graph worker process for one request) inside a graph job slot."""
    with _Job(GRAPH_JOBS, "graph"):
        return warm.graph_once(argv, view)


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


_LABELS: dict[str, tuple[float, dict]] = {}
_LABELS_LOCK = threading.Lock()   # one parse per graph file and mtime, whatever the threads


def _graph_index(graph: str | None = None) -> dict:
    """What the core reads from one graph file without graphify, cached by mtime: `rows` (label,
    type, repo, source_file, id) of the labelled nodes, `repos` (every project that has a node) and
    `members` (class node id -> the ids of its methods, from `method`/`contains` edges)."""
    graph = graph or GRAPH
    try:
        mtime = os.path.getmtime(graph)
    except OSError:
        return {"rows": [], "repos": set(), "members": {}}
    hit = _LABELS.get(graph)
    if hit and hit[0] == mtime:
        return hit[1]
    with _LABELS_LOCK:
        hit = _LABELS.get(graph)          # another thread may have parsed it while this one waited
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(graph, encoding="utf-8") as f:
                data = json.load(f)
            nodes = data.get("nodes", [])
            rows = [(n.get("label", ""), n.get("type", ""), n.get("repo", ""),
                     n.get("source_file", ""), n.get("id", "")) for n in nodes if n.get("label")]
            repos = {n.get("repo") for n in nodes if n.get("repo")} - access.PSEUDO_REPOS
            kind = {n.get("id"): n.get("type", "") for n in nodes}
            members: dict[str, list[str]] = {}
            for e in data.get("links", data.get("edges", [])):
                if e.get("relation") not in ("method", "contains"):
                    continue
                a, b = e.get("source"), e.get("target")
                if kind.get(b) in CLASS_TYPES and kind.get(a) not in CLASS_TYPES:
                    a, b = b, a        # the class is the container, whatever the edge's direction
                if kind.get(a) in CLASS_TYPES and kind.get(b) not in CLASS_TYPES and b in kind:
                    members.setdefault(a, []).append(b)
            index = {"rows": rows, "repos": repos, "members": members}
        except Exception:  # noqa: BLE001
            index = {"rows": [], "repos": set(), "members": {}}
        _LABELS[graph] = (mtime, index)
        return index


def labels(graph: str | None = None) -> list[tuple[str, str, str, str, str]]:
    """Node labels (label, type, repo, source_file, id) of one graph file, cached by mtime, so an
    ambiguous graphify match can suggest exact labels. A filtered graph has its own cache entry."""
    return _graph_index(graph)["rows"]


class AllProjects(set):
    """A grant of every project ("*" from the permission webhook), not a list of names. Only this grant
    and a local caller (None) may use the unfiltered main graph: a list of names, even one that holds
    every project now, gets that list as its view, since the worker can load a newer graph.json with a
    new project between the check and the answer."""


def _same_grant(allowed: set[str]) -> set[str]:
    """A copy of a caller's project set that keeps an all-projects grant one."""
    return AllProjects(allowed) if isinstance(allowed, AllProjects) else set(allowed)


def _full(projects) -> bool:
    """May this project set use the unfiltered main graph? Only a local caller (None) and a grant of
    every project (AllProjects). A limited caller always sends its explicit list: checking that the
    list holds every repo of graph.json and then sending "no filter" raced with a rebuild that adds a
    project."""
    return projects is None or isinstance(projects, AllProjects)


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

    def rank(row: tuple) -> tuple[int, int]:
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
    for label, typ, repo, src, _id in uniq[:limit]:
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
    verb = "search" if mode == "lex" else "query"
    if mode == "vec":
        # Not `qmd vsearch`: it always expands the query with the 1.7B generation model (a 1.3 GB
        # download and minutes on a CPU). A `vec:` query document embeds the text as it is, as the
        # warm worker does (_searches).
        query = "vec: " + " ".join(query.split())   # one line: a newline would start a bad one
    elif verb == "query":
        query = query_build.build(query, want_vector=not args.get("_lex_only"))
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

    With `allowed`, the caller's own collections are searched by name (`-c`), and (with the setting
    `contracts: all`) the collections of the other projects for their contract docs. Every hit is
    checked by the path qmd itself reports, before any text is made of it (see _docs_allowed)."""
    q = args["q"]
    note = ""
    if args.get("expand_aliases", True):
        expanded = expand_aliases(q, ALIASES)
        if expanded != q:
            note = f"[alias expansion: {q!r} -> {expanded!r}]\n\n"
            q = expanded
    head, out = _docs_scoped(args, q, allowed)
    if args.get("refuse", True):
        out = _judge(args, q, out, lambda extra: _docs_scoped({**args, **extra}, q, allowed)[1], allowed)
    return note + head + out


def _doc_collections(names) -> list[str]:
    """The qmd collections of these projects (code-only projects have none)."""
    return [f"{COLLECTION}-{n}" for n in sorted(names) if n in DOC_PROJECTS]


def _docs_scoped(args: dict, q: str, allowed: set[str] | None) -> tuple[str, str]:
    """(the `[projects: ...]` header, the hits) of one docs search inside the caller's projects."""
    if allowed is not None:
        want = int(args.get("limit") or 5)
        projects, label, auto = scope_projects(args, allowed)
        scoped = {k: v for k, v in args.items() if k not in ("project", "scope")}
        narrow = _doc_collections(projects or ())
        if projects is not None and projects != allowed:      # narrowed: only those collections
            if narrow:
                return f"[projects: {label}]\n\n", _docs_allowed(scoped, q, allowed, [(narrow, want)], want)
            if not auto:
                raise ToolError(f"no docs indexed for: {label} "
                                f"(projects with docs: {', '.join(sorted(DOC_PROJECTS & allowed))})")
        groups = []
        own = _doc_collections(allowed)
        if own:
            groups.append((own, want))
        shared = access.contracts_open()
        hidden = _doc_collections(set(DOC_PROJECTS) - allowed) if shared else []
        if hidden:   # only their contract docs pass the check; ask for more, few of them are contracts
            groups.append((hidden, want * 5))
        return (f"[projects: {', '.join(sorted(allowed))}{' + contract docs' if shared else ''}]\n\n",
                _docs_allowed(scoped, q, allowed, groups, want))
    out = _docs_once(args, q)
    label = resolve_collections(args)[1]
    if _empty(out) and label.endswith("(auto)"):
        # the session's own project had nothing — widen instead of returning empty
        out = _docs_once({**args, "project": "all"}, q)
        label = f"all (nothing in {label[:-7]})"
    return f"[projects: {label}]\n\n", out


WEAK_SHOWN = 3   # hits kept under a refusal, marked weak (see _judge)


def _judge(args: dict, q: str, out: str, rerun, allowed: set[str] | None = None) -> str:
    """The "is this enough?" check (bin/docs_grade.py) on a docs answer.

    A good first hit: the answer is returned unchanged. A weak one: in auto/hybrid mode, one
    keyword-only search (the same query document without its `vec:` line, no model, cheap) and its
    first hit is graded again; if that one is good, it is returned under a note. Otherwise the
    answer is "not found". The nearest hits stay below the refusal, at most WEAK_SHOWN, marked as
    weak: the grade is a heuristic and turns away some answerable questions (docs/design.md, "Docs search: an honest not found"),
    so the caller can still open them, but it cannot mistake them for a confident answer.

    For a limited caller the word counts come from its own collections and (under `contracts: all`)
    the contract docs only, so the grade (and its "in no document" list) says nothing about the docs of hidden projects."""
    if docs_grade.first_hit(out) is None:
        return out
    index = (docs_grade.open_index(KB) if allowed is None
             else docs_grade.open_index(KB, _doc_collections(allowed),
                                        f"{COLLECTION}-" if access.contracts_open() else None))
    try:
        grade = docs_grade.grade(q, out, index)
        if not grade.weak:
            return f"[grade: {grade.label()}]\n\n{out}" if args.get("explain") else out
        if args.get("mode", "auto") in ("auto", "hybrid") and DOCS_MODE != "bm25":
            lex = rerun({"_lex_only": True})
            lex_grade = docs_grade.grade(q, lex, index)
            if docs_grade.first_hit(lex) and not lex_grade.weak:
                return (f"[the first result was weak ({grade.label()}); keyword-only results below, "
                        f"grade: {lex_grade.label()}]\n\n{lex}")
    finally:
        if index is not None:
            index.close()
    blocks = re.split(r"(?m)^(?=qmd://)", out)
    head = blocks[0] if blocks[0].startswith("[") else ""
    weak = "".join(blocks[1:1 + WEAK_SHOWN]).rstrip()
    return (f"{docs_grade.REFUSED_MARK} no document answers this well enough. Grade: {grade.label()}]\n"
            f"The nearest hits below are weak: probably not the answer. Search with other words, or "
            f"pass refuse: false for the plain nearest hits.\n\n{head}{weak}")


def _docs_allowed(args: dict, q: str, allowed: set[str], groups: list[tuple[list[str], int]],
                  keep: int) -> str:
    """The hits of one docs search per group of collections that the caller may see, best first.

    Each hit is checked by the path in qmd's own result (JSON or the worker's structured hits),
    before it is formatted. Never by splitting text: a snippet can hold a line that looks like the
    start of another hit."""
    found: list[dict] = []
    notes: list[str] = []
    failed = ""
    for collections, limit in groups:
        hits, note, raw = _docs_hits({**args, "_collections": collections, "limit": limit}, q)
        if hits is None:
            failed = failed or raw
            continue
        notes.append(note)
        found += [h for h in hits if access.doc_allowed(warm.hit_path(h), allowed)]
    if not found:
        if failed and not notes:
            return failed if failed.startswith("(") else "(docs search failed: qmd gave no readable answer)"
        return "(no results in your projects)"
    found.sort(key=lambda h: -warm._num(h.get("score")))   # stable: the own collections first on a tie
    note = next((n for n in notes if n), "")
    return (note + warm.format_hits(found[:keep]))[:MAX_OUT]


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
    for line in query_build.build(q, want_vector=not args.get("_lex_only")).split("\n"):
        kind, sep, text = line.partition(":")
        kind, text = kind.strip(), text.strip()
        if not sep or not text:
            continue
        if kind == "intent":
            intent = text
        elif kind in ("lex", "vec", "hyde"):
            searches.append({"type": kind, "query": text})
    return _cap_searches(searches) or [{"type": "lex", "query": q}, {"type": "vec", "query": q}], intent


QMD_MAX_SEARCHES = 10   # qmd's MCP `query` refuses a call with more sub-queries than this


def _cap_searches(searches: list[dict]) -> list[dict]:
    """At most QMD_MAX_SEARCHES sub-queries. The vec/hyde lines stay; the lex lines that go are the
    least specific ones (the shortest words), and the kept ones keep their order."""
    if len(searches) <= QMD_MAX_SEARCHES:
        return searches
    other = [i for i, s in enumerate(searches) if s["type"] != "lex"][:QMD_MAX_SEARCHES]
    lex = [i for i, s in enumerate(searches) if s["type"] == "lex"]
    room = QMD_MAX_SEARCHES - len(other)
    keep = set(other) | set(sorted(lex, key=lambda i: (-len(searches[i]["query"]), i))[:room])
    return [s for i, s in enumerate(searches) if i in keep]


def _docs_warm(args: dict, q: str) -> list[dict] | None:
    """qmd's hits through the long-lived qmd (model already loaded); None = use the CLI."""
    if args.get("explain"):
        return None  # score traces exist only in the CLI
    worker = warm.qmd(KB)
    if worker is None:
        return None
    searches, _intent = _searches(args, q)
    return worker.query(searches, resolve_collections(args)[0], args.get("limit") or 5,  # CLI default
                        args.get("min_score"), rerank=DOCS_MODE == "full")


def _json_hits(raw: str) -> list[dict] | None:
    """The hits of `qmd ... --format json`; None when the text is not that (an error, a timeout)."""
    for m in re.finditer(r"(?m)^\[", raw):
        try:
            data, _ = json.JSONDecoder().raw_decode(raw[m.start():])
        except ValueError:
            continue
        if isinstance(data, list):
            return [h for h in data if isinstance(h, dict)]
    return None


def _docs_cli(args: dict, q: str, timeout: int) -> str:
    cmd = _qmd_cmd(args, q)
    if "-n" not in cmd[3:]:
        cmd += ["-n", "5"]   # the CLI's own default; --format json would give 20
    return run([*cmd, "--format", "json"], cwd=KB, timeout=timeout, limit=None)


def _docs_hits(args: dict, q: str) -> tuple[list[dict] | None, str, str]:
    """One docs search -> (qmd's hits or None, a note for the answer, qmd's raw text when no hits)."""
    hits = _docs_warm(args, q)
    if hits is not None:
        return hits, "", ""
    raw = _docs_cli(args, q, TIMEOUT_DOCS)
    hits = _json_hits(raw)
    if hits is None and raw == "(timed out)" and args.get("mode", "auto") == "auto":
        # cold start exceeded the budget — fall back to instant BM25 rather than returning nothing
        raw = _docs_cli({**args, "mode": "lex"}, q, TIMEOUT_GRAPH)
        hits = _json_hits(raw)
        if raw != "(timed out)" and not _empty(raw) and hits != []:
            return hits, "[semantic search timed out — keyword (BM25) results below]\n\n", raw
    return hits, "", raw


def _docs_once(args: dict, q: str) -> str:
    """The text of one docs search for a caller who may see everything."""
    hits, note, raw = _docs_hits(args, q)
    if hits is None:
        return note + raw
    return (note + warm.format_hits(hits))[:MAX_OUT]


def search_code(args: dict, allowed: set[str] | None = None) -> str:
    """Code half of `search`: a scoped subgraph for the question. With the `datadict` add-on, the
    tables whose meaning matches the question come first (bin/datadict.py)."""
    out = graph_call(["graphify", "query", args["q"]], args, allowed)
    if addons.on("datadict"):
        out = datadict.with_search(out, args["q"], datadict.entries(_dict_scope(args, allowed)))
    return out


def _dict_scope(args: dict, allowed: set[str] | None) -> set[str] | None:
    """Whose data dictionaries a call reads: the named projects, else every project the caller may
    see (a dictionary is small, so a session's own scope is not narrowed further)."""
    want = (args.get("project") or "").strip()
    if want and want.lower() != "all":
        names = {n.strip() for n in want.split(",") if n.strip()}
        return names if allowed is None else names & allowed
    return allowed


def _explain(args: dict, allowed: set[str] | None) -> str:
    """graph_explain. With the `datadict` add-on, a table node shows its meaning, and a business term
    that names no node is read as the table it stands for."""
    symbol = args["symbol"]
    if not addons.on("datadict"):
        return graph_call(["graphify", "explain", symbol], args, allowed, symbol)
    book = datadict.entries(_dict_scope(args, allowed))
    note = ""
    low = symbol.strip().lower()
    # Only the nodes the caller may see decide: a node of a hidden project must not change the answer.
    if book and not any(r[0].lower() == low and (allowed is None or r[2] in allowed) for r in labels(GRAPH)):
        tables = datadict.term_tables(symbol, book)
        if len(tables) == 1:
            note = f"[data dictionary: {symbol!r} is the table {tables[0]}]\n\n"
            symbol = tables[0]
        elif tables:
            note = f"[data dictionary: {symbol!r} names several tables: {', '.join(tables)}; ask for one]\n\n"
    out = graph_call(["graphify", "explain", symbol], args, allowed, symbol)
    return note + datadict.with_explain(out, book)


def _docs_skipped(args: dict, allowed: set[str] | None) -> str | None:
    """A note when every project named in `project` is code-only: `both` then searches the code only.
    None when there are docs to search, or when the name is unknown (the code half refuses it)."""
    want = (args.get("project") or "").strip()
    if args.get("scope") or not want or want.lower() == "all":
        return None
    names = [n.strip() for n in want.split(",") if n.strip()]
    if any(n not in PROJECTS or (allowed is not None and n not in allowed) for n in names):
        return None
    if any(n in DOC_PROJECTS for n in names):
        return None
    return f"(no docs indexed for: {', '.join(names)}; only the code was searched)"


SEARCH_LIMIT_MAX = 50
SEARCH_PAGE_MAX = 500   # the longest search answer (two halves of MAX_OUT) in pages of PAGE_MIN


def search(args: dict, allowed: set[str] | None = None) -> str:
    """The one search entry point. `domain` decides which halves run; `both` labels each section.
    `limit` (docs hits) is held to 1..SEARCH_LIMIT_MAX: it sizes the qmd query."""
    if args.get("limit") is not None:
        args = {**args, "limit": max(1, min(int(args["limit"]), SEARCH_LIMIT_MAX))}
    domain = args.get("domain", "both")
    skipped = _docs_skipped(args, allowed) if domain == "both" else None
    if domain == "docs":
        return search_docs(args, allowed)
    if domain == "code":
        return search_code(args, allowed)
    if skipped:
        return f"=== docs ===\n{skipped}\n\n=== code ===\n{search_code(args, allowed)}"
    # The halves are independent subprocesses (qmd, graphify): run them side by side, not in turn.
    with ThreadPoolExecutor(max_workers=2) as pool:
        # each half runs in a copy of this context: a run_code program's slot deadline goes along
        docs = pool.submit(contextvars.copy_context().run, search_docs, args, allowed)
        code = pool.submit(contextvars.copy_context().run, search_code, args, allowed)
        return f"=== docs ===\n{docs.result()}\n\n=== code ===\n{code.result()}"


def graph_for(allowed: set[str] | None) -> str:
    """The graph file this caller may query (bin/access.py builds and caches filtered copies)."""
    if _full(allowed):
        return GRAPH
    try:
        # No `known` projects: a limited caller gets a filtered copy even when it lists every project.
        return access.graph_for(set(allowed), ())
    except access.Denied as e:
        raise ToolError(str(e)) from None


def docs_get(path: str, allowed: set[str] | None = None) -> str:
    """One document by its path or id. The answer is read up to MAX_OUT characters (the process is
    stopped there). A limited caller: a `qmd://` path of a project it may not see is refused before
    qmd runs; a fuzzy id or partial path is checked on the path qmd resolved."""
    if allowed is not None and path.strip().startswith("qmd://") and not access.doc_allowed(path, allowed):
        return "(document not found in your projects)"
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


def _contract_view(allowed: set[str] | None) -> set[str] | None:
    """The projects whose contracts this caller sees: None = every contract (a local session, or the
    setting `contracts: all`), else the caller's own projects (`contracts: own`, bin/settings.py)."""
    if allowed is None or access.contracts_open():
        return None
    return set(allowed)


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


def call_tool(name: str, args: dict, allowed: set[str] | None = None, caller: str | None = None,
              nested: bool = False) -> str:
    """Count the call when this process is a local server, then run it (see _call_tool).
    `nested`: a call from inside run_code or batch."""
    if LOCAL_USAGE is None or caller is not None:
        return _call_tool(name, args, allowed, caller, nested)
    start, ok = time.monotonic(), False
    try:
        out = _call_tool(name, args, allowed, caller, nested)
        ok = True
        return out
    finally:
        LOCAL_USAGE.record(name, "local", dict(args or {}), ok, time.monotonic() - start)


def _call_tool(name: str, args: dict, allowed: set[str] | None = None, caller: str | None = None,
               nested: bool = False) -> str:
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
        allowed = _same_grant(allowed)
    page = args.pop("page", None)
    page_given = page is not None
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
        return paginate(text, page, length, CURSORS.put(text, name, _cursor_owner(caller, allowed),
                                                        length, page))
    if name in UNPAGED:
        return _dispatch(name, args, allowed)
    owner = _cursor_owner(caller, allowed)
    if cursor:   # a cursor alone: the page after the last one served; the size it was cut with
        if nested:   # read a page, never move the caller's own cursor
            text, state = CURSORS.get(cursor, name, owner)
            return paginate(text, page if page_given else state["page"] + 1, state["size"])
        text, size, page = CURSORS.advance(cursor, name, owner, page if page_given else None)
        return paginate(text, page, size, cursor)
    if name in SELF_PAGED:
        if page > SEARCH_PAGE_MAX:
            raise ToolError(f"{name}: 'page' must be 1-{SEARCH_PAGE_MAX}")
        if length and "budget" not in args:  # ~4 characters per token
            args["budget"] = max(300, min(length // 4, CODE_BUDGET_MAX))
        # Every page, with or without a cursor, is cut from the same text: the code half runs with
        # 4x the budget, the docs half with `limit` hits (a caller who asks for 2 documents gets 2).
        # So `page: 2` without the cursor is the cursor's page 2, as page 1's footer promises.
        args = {**args, "_wide": _widened(args)}
    text = _dispatch(name, args, allowed)
    if nested:   # a call from run_code or batch: the whole answer, no cursor of its own
        return _whole(text, page if page_given else None, length)
    first = paginate(text, page, length)
    if "[page " not in first:
        return first
    token = CURSORS.put(text, name, owner, length, page)
    return paginate(text, page, length, token)


def _cursor_owner(caller, allowed: set[str] | None) -> tuple:
    """Who may read a cursor: the caller, its projects, the kind of grant (a list of names or every
    project), the contracts mode and the settings generation. A change of any of them (a new grant, a
    switch of `contracts`, any settings save) makes the old cursors "expired" instead of serving text
    cut under the old policy."""
    gen = getattr(settings, "generation", None)   # missing in a copy without it
    return (caller, frozenset(allowed) if allowed is not None else None,
            isinstance(allowed, AllProjects), access.contract_mode(), gen() if callable(gen) else None)


def _whole(text: str, page: int | None, length: int | None) -> str:
    """The answer of a nested call: all of it up to MAX_OUT, or the page asked for without a cursor
    (a program's calls must not push its caller's own cursors out)."""
    if page is not None:
        return paginate(text, page, length)
    if len(text) <= MAX_OUT:
        return text
    return text[:MAX_OUT] + f"\n… (cut at {MAX_OUT} characters: narrow the call)"


class Cursors:
    """Answers kept for their next pages: a random token -> (text, tool, owner, expiry, state, bytes).
    In memory, per process, bounded four ways: at most `cap` answers, `max_bytes` of text in all,
    `per_owner` answers and `owner_bytes` bytes per caller (one caller cannot push the others' answers
    out; under pressure a caller's own oldest answers go first); each lives `ttl`
    seconds. A token works only for the tool, the caller and the projects that made it: another
    caller gets "expired", not someone's answer."""

    def __init__(self, ttl: float, cap: int, max_bytes: int = 64_000_000, per_owner: int = 50,
                 owner_bytes: int | None = None):
        self.ttl, self.cap, self.max_bytes, self.per_owner = ttl, cap, max_bytes, per_owner
        # one caller may hold at most this many bytes, so it cannot use up the budget of the others
        self.owner_bytes = owner_bytes if owner_bytes is not None else max(1, max_bytes // 8)
        self.items: "OrderedDict[str, tuple]" = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    def put(self, text: str, tool: str, owner: tuple, size: int | None = None, page: int = 1) -> str | None:
        """-> the token; None when the answer alone is larger than the whole budget (not kept)."""
        nbytes = len(text.encode("utf-8", "replace"))
        if nbytes > min(self.max_bytes, self.owner_bytes):
            return None
        token = "c_" + secrets.token_urlsafe(12)
        with self.lock:
            self._expire()
            mine = [k for k, v in self.items.items() if v[2] == owner]
            held = sum(self.items[k][5] for k in mine)
            while mine and (len(mine) >= self.per_owner or held + nbytes > self.owner_bytes):
                held -= self.items[mine[0]][5]
                self._drop(mine.pop(0))

            def full() -> bool:
                return bool(self.items) and (len(self.items) >= self.cap
                                             or self.bytes + nbytes > self.max_bytes)
            while mine and full():      # under pressure the caller's own oldest answers go first
                self._drop(mine.pop(0))
            while full():
                self._drop(next(iter(self.items)))
            self.items[token] = (text, tool, owner, time.monotonic() + self.ttl,
                                 {"size": size, "page": page}, nbytes)
            self.bytes += nbytes
        return token

    def get(self, token: str, tool: str, owner: tuple) -> tuple[str, dict]:
        """-> (text, a copy of the state: the page size the answer was cut with, the last page served)."""
        with self.lock:
            hit = self._hit(token, tool, owner)
            return hit[0], dict(hit[4])

    def advance(self, token: str, tool: str, owner: tuple, page: int | None) -> tuple[str, int | None, int]:
        """-> (text, page size, the page to serve). `page` None = the page after the last one served.
        The page moves under the lock, so two calls with one cursor never serve the same page twice."""
        with self.lock:
            hit = self._hit(token, tool, owner)
            state = hit[4]
            state["page"] = state["page"] + 1 if page is None else page
            return hit[0], state["size"], state["page"]

    def _hit(self, token: str, tool: str, owner: tuple) -> tuple:
        self._expire()
        hit = self.items.get(token)
        if not hit or hit[1] != tool or hit[2] != owner:
            raise ToolError("cursor expired or unknown: run the query again (cursors live "
                            f"{int(self.ttl // 60)} minutes)")
        return hit

    def _drop(self, token: str) -> None:
        hit = self.items.pop(token, None)
        if hit:
            self.bytes -= hit[5]

    def _expire(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self.items.items() if v[3] < now]:
            self._drop(k)


CURSOR_TTL = float(os.environ.get("KB_CURSOR_TTL", "600"))
CURSOR_MAX = int(os.environ.get("KB_CURSOR_MAX", "500"))
CURSOR_BYTES = int(os.environ.get("KB_CURSOR_BYTES", str(64_000_000)))
CURSOR_PER_OWNER = int(os.environ.get("KB_CURSOR_PER_OWNER", "50"))
CURSOR_WIDEN = 4
CURSORS = Cursors(CURSOR_TTL, CURSOR_MAX, CURSOR_BYTES, CURSOR_PER_OWNER)


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
        nav += f', cursor: "{cursor}" (kept {int(CURSOR_TTL // 60)} min; the cursor alone gives the next page)]' \
            if cursor else " for the rest]"
    else:
        nav += "]"
    return head + pages[page - 1] + nav


def run_code(code: str, allowed: set[str] | None, caller: str | None) -> str:
    """An agent's program in the sandbox (sandbox/runner.mjs): QuickJS in WebAssembly, inside a Node
    process with the permission model on (read access to the sandbox folder only: no network, no
    child processes, no workers, no writes). Each kb() call comes back here and runs through
    call_tool with the caller's own rights; run_code and batch cannot be called from inside.
    At most RUN_JOBS programs run at once; each one's output and value are cut at RUN_MAX_VALUE."""
    if len(code) > 20_000:
        raise ToolError("run_code: the program is longer than 20000 characters")
    with _Job(RUN_JOBS, "run_code"):
        msg = _run_sandbox(code, allowed, caller)
    parts = []
    if msg.get("output"):
        parts.append(_cut(str(msg["output"]).rstrip(), "output"))
    if msg.get("error"):
        parts.append(f"(error: {str(msg['error'])[:2000]})")
    value = msg.get("value")
    if value is not None:
        parts.append(_cut(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2),
                          "returned value"))
    parts.append(f"[run_code: {msg.get('calls', 0)} kb() call(s)]")
    return "\n\n".join(parts)


def _cut(text: str, what: str) -> str:
    if len(text) <= RUN_MAX_VALUE:
        return text
    return text[:RUN_MAX_VALUE] + f"\n… (the {what} is cut at {RUN_MAX_VALUE} characters: return less)"


def _run_sandbox(code: str, allowed: set[str] | None, caller: str | None) -> dict:
    """Run the program; -> the runner's last message ({done, output, value, error, calls})."""
    import resource
    import select

    proc = sock = None
    if SANDBOX_SOCKET:  # the sandbox service (sandbox/socket-server.mjs) starts the runner and limits it
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SANDBOX_SOCKET)
        except OSError as e:
            sock.close()
            raise ToolError(f"run_code: the sandbox service at {SANDBOX_SOCKET} does not answer "
                            f"({e.strerror or e})") from None
        rd, wr = sock.makefile("r", encoding="utf-8"), sock.makefile("w", encoding="utf-8")
    else:
        if not os.path.isdir(os.path.join(SANDBOX, "node_modules", "quickjs-emscripten")):
            raise ToolError("run_code: the sandbox is not installed (kb addon install code-mode)")

        def limits():  # the Node process itself: CPU seconds and open files
            resource.setrlimit(resource.RLIMIT_CPU, (RUN_SECONDS + 10, RUN_SECONDS + 10))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

        try:
            proc = subprocess.Popen(
                ["node", "--permission", f"--allow-fs-read={SANDBOX}", os.path.join(SANDBOX, "runner.mjs")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=SANDBOX,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NODE_OPTIONS": ""}, preexec_fn=limits)
        except OSError as e:  # no `node` on the server's PATH (nvm sets it only in interactive shells)
            raise ToolError(f"run_code: cannot start node ({e.strerror or e})") from None
        rd, wr = proc.stdout, proc.stdin
    # A safety net for a runner that stops answering: the script's own deadline + RUN_SLACK. The
    # time the nested kb() calls take is added to it, because the runner counts that time against
    # the script itself and stops it ("time is up") with its output kept.
    deadline = time.monotonic() + RUN_SECONDS + RUN_SLACK
    token = _WAIT_UNTIL.set(time.monotonic() + RUN_SECONDS)
    try:
        assert rd and wr
        wr.write(json.dumps({"code": code, "deadline_ms": RUN_SECONDS * 1000, "max_output": RUN_MAX_VALUE,
                             "max_calls": RUN_CALLS, "memory_mb": 64}) + "\n")
        wr.flush()
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([rd], [], [], left)[0]:
                raise ToolError(f"run_code: the sandbox did not answer for {RUN_SECONDS + RUN_SLACK:g} s")
            line = rd.readline(RUN_MAX_LINE)
            if line and not line.endswith("\n"):
                raise ToolError("run_code: the sandbox sent a message longer than "
                                f"{RUN_MAX_LINE} characters")
            if not line:
                err = ((proc.stderr.read() if proc and proc.stderr else "") or "")[-400:]
                raise ToolError(f"run_code: the sandbox stopped ({err.strip() or 'no message'})")
            try:
                msg = json.loads(line)
            except ValueError:
                raise ToolError("run_code: the sandbox sent a message that is not JSON") from None
            if not isinstance(msg, dict):
                raise ToolError("run_code: the sandbox sent a message that is not an object")
            if msg.get("done"):
                break
            tool, args = str(msg.get("call", "")), msg.get("args") or {}
            started = time.monotonic()
            if tool in NOT_IN_BATCH:
                reply = {"error": f"'{tool}' cannot be called from run_code"}
            elif not isinstance(args, dict):
                reply = {"error": f"{tool}: the arguments must be an object"}
            else:
                try:
                    reply = {"result": call_tool(tool, dict(args), allowed, caller, nested=True)}
                except ToolError as e:
                    reply = {"error": str(e)}
                except Exception as e:  # noqa: BLE001 — a fault in one tool: the program gets an error
                    reply = {"error": f"{tool} failed inside the server: {type(e).__name__}"}
            deadline += time.monotonic() - started
            wr.write(json.dumps(reply) + "\n")
            wr.flush()
    finally:
        _WAIT_UNTIL.reset(token)
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        for pipe in (wr, rd, proc.stderr if proc else None):
            if pipe:
                try:
                    pipe.close()
                except OSError:  # the other end is gone: nothing left to flush
                    pass
        if sock is not None:
            sock.close()
    return msg


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
            return call_tool(c["tool"], dict(c.get("args") or {}), allowed, caller, nested=True)
        except ToolError as e:
            return f"(error: {e})"
        except Exception as e:  # noqa: BLE001 — one failing call does not drop the whole batch
            return f"(error: the tool failed inside the server: {type(e).__name__})"
    with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
        answers = list(pool.map(one, calls))
    return "\n\n".join(f"=== [{i}] {c['tool']} {json.dumps(c.get('args') or {}, ensure_ascii=False)} ===\n{a}"
                         for i, (c, a) in enumerate(zip(calls, answers), 1))


def _dispatch(name: str, args: dict, allowed: set[str] | None) -> str:
    if name == "search":
        return search(args, allowed)
    if name == "graph_path":
        # The cheapest weighted path (bin/routes.py), not graphify's fewest hops: hop counting went
        # through hub classes, framework interfaces and `.index()` (5 of 10 paths were nonsense in the
        # 2026-11-20 run). Without graphify's Python (networkx) the old unweighted path answers.
        try:
            return graph_routes({**args, "k": 1}, allowed)
        except ToolError as e:
            if "networkx" not in str(e):
                raise
        return graph_call(["graphify", "path", args["from"], args["to"]],
                          {**args, "project": args.get("project") or "all"}, allowed, args["from"], args["to"])
    if name == "graph_routes":
        return graph_routes(args, allowed)
    if name == "review_context":
        return review_context(args, allowed)
    if name == "code_grep":
        return code_grep(args, allowed)
    if name == "repo_map":
        return repo_map(args, allowed)
    if name == "graph_explain":
        return _explain(args, allowed)
    if name == "graph_affected":
        return graph_call(["graphify", "affected", args["symbol"]], args, allowed, args["symbol"])
    if name == "docs_get":
        return docs_get(args["path"], allowed)
    if name == "contracts_search":
        return _index(contracts.contracts_search, args["query"], None, 100_000, None, _contract_view(allowed))
    if name == "connections":
        return _index(contracts.connections, args.get("service", ""), None, _contract_view(allowed))
    if name == "problems":
        extra = (datadict.problems(GRAPH, allowed, set(PROJECTS)) if addons.on("datadict") else None)
        return _index(contracts.problems, args.get("project", ""), allowed, None, extra,
                      _contract_view(allowed))
    if name in PLUGIN_TOOLS:
        return str(PLUGIN_TOOLS[name].call(name, args, allowed, sys.modules[__name__]))
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
