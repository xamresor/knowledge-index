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
    the interfaces between services; they stay visible to every caller, whatever the filter
    (`KB_CONTRACT_EDGES=0` hides the edges too).

CLI: `access.py filter <graph.json> <out.json> <project>...` (what graph_for runs).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

GRAPH = str(paths.graph_path())
FILTERED_DIR = str(paths.out_dir() / "filtered")
COLLECTION = os.environ.get("KB_COLLECTION", "kb")
CONTRACT_DOCS_DIR = "contracts"
KEEP_CONTRACTS = os.environ.get("KB_CONTRACT_EDGES", "1") != "0"
# Node `repo` values that are not projects: domain anchors and db tables.
PSEUDO_REPOS = {"domain", "db"}


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
    """A doc of an allowed project, or a contract doc of any project."""
    project = doc_project(path)
    if project is None:
        return False
    return project in allowed or re.match(
        rf"qmd://[^/\s]+/{CONTRACT_DOCS_DIR}/", path.strip()) is not None


# --------------------------------------------------------------------------- graph
def filter_graph(g: dict, allowed: set[str], keep_contracts: bool = KEEP_CONTRACTS) -> dict:
    """Graph `g` reduced to the nodes of the `allowed` repos.

    - A node of a real repo stays only when its repo is allowed.
    - A pseudo node (repo `domain`/`db`) stays only when it links to a kept real node.
    - With keep_contracts, a node of another repo stays when an http_request edge links it to a
      kept node, and keeps only those http_request edges (the endpoint, not its internals).
    - Edges stay only between kept nodes. Community names that start with a hidden repo
      ("api · ...") become "community <id>", so they do not leak the hidden repo's layout.
    """
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


def graph_for(allowed: set[str] | None, known: list[str] | set[str]) -> str:
    """Path of the graph file for this project set (`known` = every configured project)."""
    if allowed is None:
        return GRAPH
    if known and set(known) <= allowed:
        return GRAPH
    key = "+".join(sorted(allowed)) or "_none"
    path = f"{FILTERED_DIR}/{key}/graph.json"
    with _filter_lock:
        try:
            fresh = os.path.getmtime(path) >= os.path.getmtime(GRAPH)
        except OSError:
            fresh = False
        if not fresh:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp.{os.getpid()}"
            p = subprocess.run([sys.executable, os.path.abspath(__file__), "filter", GRAPH, tmp,
                                *sorted(allowed)], capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                raise Denied(f"graph filter failed: {p.stderr.strip()[:300]}")
            os.replace(tmp, path)
    return path


def _main_filter(argv: list[str]) -> int:
    src, dst, *projects = argv
    with open(src, encoding="utf-8") as f:
        g = json.load(f)
    out = filter_graph(g, set(projects))
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"{len(out['nodes'])} nodes, {len(out.get('links', out.get('edges', [])))} edges")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "filter":
        sys.exit(_main_filter(sys.argv[2:]))
    print("usage: access.py filter <graph.json> <out.json> <project>...", file=sys.stderr)
    sys.exit(2)
