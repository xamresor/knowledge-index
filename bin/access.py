#!/usr/bin/env python3
"""Per-caller project access: what a caller limited to some projects may see.

Why this module exists
----------------------
A local session sees everything (`allowed = None`). A shared server (bin/kb-http) answers many
people, and each may see only some projects. The filter is applied to the *inputs* of the tools,
never by parsing their text output:

  * **graph tools** run on a filtered copy of graph.json that holds only the allowed repos' nodes
    (`filter_graph`), so graphify never sees the rest. The copy is built in a subprocess (loading
    the full graph costs a few hundred MB a long-running server would otherwise keep) and cached
    per allowed-set until the main graph changes.
  * **docs** are checked by their resolved qmd path: one collection per project
    (`<COLLECTION>-<project>`), so the project is in the path.
  * **contract docs** (`<project docs>/contracts/*.md`) and the cross-repo `http_request` edges are
    the interfaces between services. The setting `contracts` (bin/settings.py, env KB_CONTRACTS)
    decides who sees them: `all` (the default) shows them to every caller, whatever the filter;
    `own` shows only the contract docs of allowed projects and keeps no node of another project.
    `KB_CONTRACT_EDGES=0` hides the edges to other projects under `all` as well (`own` hides them
    anyway).

  * **path search** runs on a second copy without the domain anchors (`path_graph`). It is always
    cut from the graph the caller already may see, so it can only hold fewer nodes, never more.

CLI: `access.py filter [--contracts=keep|drop] <graph.json> <out.json> <project>...` (what graph_for runs),
     `access.py routes <graph.json> <out.json>` (what path_graph runs).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
import settings  # noqa: E402

GRAPH = str(paths.graph_path())
FILTERED_DIR = str(paths.out_dir() / "filtered")
COLLECTION = os.environ.get("KB_COLLECTION", "kb")
CONTRACT_DOCS_DIR = "contracts"
KEEP_CONTRACTS = os.environ.get("KB_CONTRACT_EDGES", "1") != "0"
# Node `repo` values that are not projects: domain anchors and db tables.
PSEUDO_REPOS = {"domain", "db"}


def contract_mode() -> str:
    """`all` or `own`: the setting `contracts` (bin/settings.py), read again on every call."""
    return settings.get("contracts")


def contracts_open() -> bool:
    """True when a limited caller sees the contracts of every project (`contracts: all`)."""
    return contract_mode() == "all"


class Denied(PermissionError):
    """The graph filter could not be built; the core turns it into a ToolError."""


# --------------------------------------------------------------------------- docs
def doc_project(path: str) -> str | None:
    """Project of a qmd path `qmd://<COLLECTION>-<project>/...`, None for any other collection."""
    m = re.match(r"qmd://([^/\s]+)/", path.strip())
    prefix = f"{COLLECTION}-"
    if not m or not m.group(1).startswith(prefix):
        return None
    return m.group(1)[len(prefix):] or None


def doc_allowed(path: str, allowed: set[str]) -> bool:
    """A doc of an allowed project, or (with `contracts: all`) a contract doc of any project."""
    project = doc_project(path)
    if project is None:
        return False
    if project in allowed:
        return True
    return contracts_open() and re.match(
        rf"qmd://[^/\s]+/{CONTRACT_DOCS_DIR}/", path.strip()) is not None


# --------------------------------------------------------------------------- graph
def filter_graph(g: dict, allowed: set[str], keep_contracts: bool | None = None) -> dict:
    """Graph `g` reduced to the nodes of the `allowed` repos.

    `keep_contracts` None: KEEP_CONTRACTS (KB_CONTRACT_EDGES) under `contracts: all`, False under `own`.

    - A node of a real repo stays only when its repo is allowed.
    - A pseudo node (repo `domain`/`db`) stays only when it links to a kept real node.
    - With keep_contracts, a node of another repo stays when an http_request edge links it to a
      kept node, and keeps only those http_request edges (the endpoint, not its internals).
    - Edges stay only between kept nodes. Community names that start with a hidden repo
      ("api · ...") become "community <id>", so they do not leak the hidden repo's layout.
    """
    if keep_contracts is None:
        keep_contracts = KEEP_CONTRACTS and contracts_open()
    nodes = g.get("nodes", [])
    ek = "links" if "links" in g else "edges"
    edges = g.get(ek, [])
    repo = {n["id"]: n.get("repo", "") for n in nodes}
    real = {nid for nid, r in repo.items() if r in allowed}
    contract: set[str] = set()
    pseudo: set[str] = set()
    for e in edges:
        s, t = e.get("source"), e.get("target")
        for a, b in ((s, t), (t, s)):
            if a in real and b not in real:
                if repo.get(b) in PSEUDO_REPOS:
                    pseudo.add(b)
                elif keep_contracts and e.get("relation") == "http_request":
                    contract.add(b)
    keep = real | pseudo | contract

    def edge_ok(e: dict) -> bool:
        s, t = e.get("source"), e.get("target")
        if s not in keep or t not in keep:
            return False
        if s in contract or t in contract:
            return e.get("relation") == "http_request" and (s in real or t in real)
        return True

    hidden = {r for r in set(repo.values()) if r and r not in allowed and r not in PSEUDO_REPOS}
    out_nodes = []
    for n in nodes:
        if n["id"] not in keep:
            continue
        cname = str(n.get("community_name", ""))
        if n["id"] in real and any(cname.startswith(f"{h} · ") for h in hidden):
            n = {**n, "community_name": f"community {n.get('community')}"}
        out_nodes.append(n)
    out = {k: v for k, v in g.items() if k not in ("nodes", ek, "hyperedges")}
    out["nodes"] = out_nodes
    out[ek] = [e for e in edges if edge_ok(e)]
    out["hyperedges"] = []
    return out


_filter_lock = threading.Lock()


def _source_id(path: str) -> list[int] | None:
    """(mtime_ns, size, inode) of a file, the identity a derived copy was cut from; None if missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def _fresh(copy: str, source: str) -> bool:
    """True when `copy` was cut from the `source` file that is there now. The identity of the source
    is taken before the cut starts and written next to the copy: a cut that started on an old
    graph.json and ended after a rebuild is stale, although its mtime is newer."""
    try:
        with open(copy + ".src", encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        return False
    return seen is not None and seen == _source_id(source) and os.path.exists(copy)


def _cut(argv, source: str, path: str, what: str) -> None:
    """Run the cut `argv(tmp)` (a subprocess that writes `tmp`) and install the result together with
    the identity of the source, taken before the cut reads it. Both temporary files have unique names
    (another process may cut the same copy at the same time), and the two renames happen under an
    inter-process lock, so a copy and its identity are always published as one pair."""
    src_id = _source_id(source)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    base = os.path.basename(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{base}.", suffix=".tmp", dir=folder)
    os.close(fd)
    fd, src_tmp = tempfile.mkstemp(prefix=f".{base}.src.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            p = subprocess.run(argv(tmp), capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                raise Denied(f"{what} failed: {p.stderr.strip()[:300]}")
            json.dump(src_id, f)
        with _publish_lock(path):
            os.replace(tmp, path)
            os.replace(src_tmp, path + ".src")
    finally:
        for left in (tmp, src_tmp):
            try:
                os.unlink(left)
            except OSError:
                pass


@contextlib.contextmanager
def _publish_lock(path: str):
    """An exclusive lock (flock) on `<path>.lock`, shared by every process that publishes this copy."""
    with open(path + ".lock", "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def view_key(allowed: set[str] | list[str], keep: bool) -> str:
    """The folder name of one filtered copy: a hash of the sorted project names and the contract view.
    Not the names joined with '+': the sets {a, b} and {"a+b"} must not share a copy."""
    canon = json.dumps([sorted(allowed), "keep" if keep else "drop"], separators=(",", ":"))
    return "v-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


def graph_for(allowed: set[str] | None, known: list[str] | set[str]) -> str:
    """Path of the graph file for this project set. `known` = every project the main graph can hold
    (the configured ones and the repos really in graph.json): a set that holds all of them gets
    the main graph."""
    if allowed is None:
        return GRAPH
    if known and set(known) <= allowed:
        return GRAPH
    keep = KEEP_CONTRACTS and contracts_open()
    # One copy per project set and contract view: a change of the setting never serves the other copy.
    path = f"{FILTERED_DIR}/{view_key(allowed, keep)}/graph.json"
    with _filter_lock:
        if not _fresh(path, GRAPH):
            _cut(lambda tmp: [sys.executable, os.path.abspath(__file__), "filter",
                              f"--contracts={'keep' if keep else 'drop'}", GRAPH, tmp, *sorted(allowed)],
                 GRAPH, path, "graph filter")
    return path


def strip_domains(g: dict) -> dict:
    """Graph `g` without domain anchors and their `in_domain` edges.

    An anchor links every member of a domain to one hub. That is right for the dashboard and for
    `explain`, and wrong for path search: two symbols that only share a domain look two hops apart.
    """
    ek = "links" if "links" in g else "edges"
    anchors = {n["id"] for n in g.get("nodes", [])
               if n.get("type") == "domain" or n.get("repo") == "domain"}
    out = {k: v for k, v in g.items() if k not in ("nodes", ek)}
    out["nodes"] = [n for n in g.get("nodes", []) if n["id"] not in anchors]
    out[ek] = [e for e in g.get(ek, [])
               if e.get("relation") != "in_domain"
               and e.get("source") not in anchors and e.get("target") not in anchors]
    return out


def path_graph(graph: str) -> str:
    """Path of the copy of `graph` used for path search (cached next to the filtered copies).

    `graph` is what graph_for returned for this caller: the main graph or a filtered copy. The
    result is derived from that file only, so the access filter is already applied.
    """
    if os.path.abspath(graph) == os.path.abspath(GRAPH):
        path = f"{FILTERED_DIR}/_routes/graph.json"
    else:
        path = os.path.join(os.path.dirname(graph), "routes.json")
    with _filter_lock:
        if not _fresh(path, graph):
            _cut(lambda tmp: [sys.executable, os.path.abspath(__file__), "routes", graph, tmp],
                 graph, path, "path graph")
    return path


def _main_routes(argv: list[str]) -> int:
    src, dst = argv
    with open(src, encoding="utf-8") as f:
        out = strip_domains(json.load(f))
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"{len(out['nodes'])} nodes, {len(out.get('links', out.get('edges', [])))} edges")
    return 0


def _main_filter(argv: list[str]) -> int:
    keep = None
    if argv and argv[0].startswith("--contracts="):
        keep = argv[0].split("=", 1)[1] == "keep"
        argv = argv[1:]
    src, dst, *projects = argv
    with open(src, encoding="utf-8") as f:
        g = json.load(f)
    out = filter_graph(g, set(projects), keep)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"{len(out['nodes'])} nodes, {len(out.get('links', out.get('edges', [])))} edges")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "filter":
        sys.exit(_main_filter(sys.argv[2:]))
    if len(sys.argv) == 4 and sys.argv[1] == "routes":
        sys.exit(_main_routes(sys.argv[2:]))
    print("usage: access.py filter [--contracts=keep|drop] <graph.json> <out.json> <project>...\n"
          "       access.py routes <graph.json> <out.json>", file=sys.stderr)
    sys.exit(2)
