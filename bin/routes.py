#!/usr/bin/env python3
"""Several weighted paths between two places in the code graph, with the HTTP routes they cross.

`graph_path` answers with graphify's shortest path: the fewest hops, whatever they are. On a real graph
that is often a shortcut through a hub everyone touches (`Illuminate\\Http\\Response`, a base class, a
store that imports half the app), so the answer is correct and useless. This module asks a different
question: *which ways does the code really go from A to B*, and *through which routes*.

How (plain words first):
  * Each edge has a price by its kind. A call, a method of a class or a frontend->controller request
    is cheap; an import or a type reference costs more; inheritance more still. Doc links and the
    domain grouping are not code paths and are left out.
  * Passing through a node costs extra the more connected it is (log of its degree), and a node with
    no source file (a framework class) costs a lot: a path through `Request` is a shortcut, not a way.
  * Dijkstra on those prices gives the cheapest path. For the next ones the edges already used get
    dearer and Dijkstra runs again, so each next path goes another way where one exists. Paths that
    repeat the same routes or differ only in a hop or two are skipped.
  * Every frontend->API hop is printed with its route (`GET v2/customers/{}`) and the controller method
    it lands in, so a path reads like a story: this page calls this route, the controller uses this
    service, ...

Runs inside the graphify worker (bin/graph_worker.py), which already holds the caller's view of the
graph; networkx comes from graphify's environment.
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict

import networkx as nx

try:  # the same Dijkstra with a Rust core: ~2x faster here (measured 2026-11-19), optional
    import rustworkx as rx
except ImportError:  # pragma: no cover - depends on the environment
    rx = None

# Price of one hop by relation. Missing = not a code path (left out).
COST = {
    "http_request": 1.0, "calls": 1.0, "indirect_call": 1.2, "method": 1.0, "contains": 1.0,
    "eloquent": 1.5, "fk": 1.5, "sql": 1.5, "defines_table": 1.5,
    "references": 2.0, "references_constant": 2.5, "imports": 2.0, "imports_from": 2.0,
    "re_exports": 2.0, "dynamic_import": 2.0, "case_of": 2.5, "uses_config": 3.0,
    "inherits": 3.0, "implements": 3.0, "mixes_in": 3.0, "extends": 3.0, "uses": 2.0, "embeds": 2.0,
}
HUB_WEIGHT = 0.6          # extra price per log2(degree) of a node passed through
NO_SOURCE_PENALTY = 6.0   # a node without a source file: a framework or vendor symbol
MERGED_LOCAL = 12.0       # `contains` into a symbol several files contain (a merged local name)
PSEUDO_REPOS = {"domain"}
TYPE_RANK = {"model": 0, "controller": 0, "class": 1, "service": 1, "code": 2, "method": 3, "file": 4}
PENALTY = 3.0             # price factor on the edges of a path already shown
MAX_K = 6


def _build(data: dict) -> tuple[nx.Graph, dict]:
    """Undirected weighted graph of the code relations; per pair the cheapest relation is kept, with
    its direction and attributes for the printout."""
    nodes = {n["id"]: n for n in data.get("nodes", []) if n.get("repo") not in PSEUDO_REPOS}
    links = data.get("links") or data.get("edges") or []
    best: dict[tuple[str, str], tuple[float, dict]] = {}
    degree: dict[str, int] = {}
    # A symbol "contained" by several files is a merged duplicate (same local name in two components,
    # bin/dedupe.py): `today` in one calendar and in another. Walking through it links two files that
    # do not know each other, so such a `contains` hop is priced as a jump, not a step.
    containers: dict[str, int] = {}
    for e in links:
        if e.get("relation") == "contains":
            containers[e.get("target")] = containers.get(e.get("target"), 0) + 1
    for e in links:
        rel = e.get("relation")
        s, t = e.get("source"), e.get("target")
        if rel not in COST or s not in nodes or t not in nodes or s == t:
            continue
        degree[s] = degree.get(s, 0) + 1
        degree[t] = degree.get(t, 0) + 1
        key = (s, t) if s < t else (t, s)
        price = COST[rel]
        if rel == "contains" and containers.get(t, 0) > 1:
            price = MERGED_LOCAL
        if key not in best or price < best[key][0]:
            best[key] = (price, e)

    def node_price(nid: str) -> float:
        n = nodes[nid]
        p = HUB_WEIGHT * math.log2(1 + degree.get(nid, 0))
        if not n.get("source_file"):
            p += NO_SOURCE_PENALTY
        return p

    g = nx.Graph()
    for (a, b), (price, e) in best.items():
        # half of each end's price on the edge: a path pays every inner node twice-halved = once
        g.add_edge(a, b, w=price + (node_price(a) + node_price(b)) / 2, edge=e)
    return g, nodes


def _pin(repo_label: str) -> tuple[str | None, str]:
    """`api:Customer` -> ("api", "Customer"); `Customer` -> (None, "Customer")."""
    m = re.match(r"^([\w-]+):(.+)$", repo_label.strip())
    return (m.group(1), m.group(2).strip()) if m else (None, repo_label.strip())


def resolve(term: str, nodes: dict, g: nx.Graph) -> tuple[str | None, list[dict]]:
    """The node a name means: exact label first (case-insensitive), then a word match, then a
    substring; a class or model before a file or method; then the better connected one."""
    repo, name = _pin(term)
    low = name.lower()
    cands = [n for n in nodes.values() if n["id"] in g and (repo is None or n.get("repo") == repo)]

    def score(n: dict) -> tuple:
        label = n.get("label", "").lower().strip("()").lstrip(".")
        bare = re.sub(r"\.(php|vue|ts|js|tsx)$", "", label)
        exact = 0 if low in (label, bare) else 1 if re.search(rf"\b{re.escape(low)}\b", label) else 2
        return (exact, TYPE_RANK.get(n.get("type", ""), 5), -g.degree(n["id"]), len(label))

    hits = sorted((n for n in cands if low in n.get("label", "").lower()), key=score)
    return (hits[0]["id"] if hits else None), hits[:6]


def _describe(n: dict) -> str:
    where = n.get("source_file") or ""
    loc = n.get("source_location") or ""
    meta = " ".join(p for p in (n.get("type", ""), n.get("repo", ""), f"{where}:{loc}" if loc else where) if p)
    return f"{n.get('label', n['id'])}  [{meta}]"


def _hop(a: str, b: str, g: nx.Graph, nodes: dict) -> str:
    e = g.edges[a, b]["edge"]
    rel = e.get("relation", "?")
    forward = e.get("source") == a
    arrow = f"--{rel}-->" if forward else f"<--{rel}--"
    route = ""
    if rel == "http_request" and e.get("http_uri"):
        route = f"   HTTP {e.get('http_method', '?')} {e['http_uri']}"
    return f"   {arrow}{route}"


def _routes_of(path: list[str], g: nx.Graph, nodes: dict) -> list[str]:
    out = []
    for a, b in zip(path, path[1:]):
        e = g.edges[a, b]["edge"]
        if e.get("relation") == "http_request" and e.get("http_uri"):
            ctl = nodes[e["target"]]
            out.append(f"{e.get('http_method', '?')} {e['http_uri']} -> {ctl.get('label')} ({ctl.get('repo')})")
    return out


_BUILT: "OrderedDict[int, tuple[list, nx.Graph, dict, object]]" = OrderedDict()
MAX_BUILT = 4  # weighted graphs kept (one per caller view in use)


def _built(data: dict) -> tuple[nx.Graph, dict]:
    """The weighted graph of `data`, built once per graph (the worker passes the same dict back)."""
    key = id(data.get("nodes"))
    hit = _BUILT.get(key)
    if hit and hit[0] is data.get("nodes"):
        _BUILT.move_to_end(key)
        return hit[1], hit[2]
    g, nodes = _build(data)
    while len(_BUILT) >= MAX_BUILT:
        _BUILT.popitem(last=False)
    _BUILT[key] = (data.get("nodes"), g, nodes, _rx_copy(g))
    return g, nodes


def _rx_copy(g: nx.Graph):
    """The weighted graph as a rustworkx graph (None without rustworkx): (graph, index of node id,
    node id of index). Edge payload = the price."""
    if rx is None:
        return None
    rg = rx.PyGraph(multigraph=False)
    index = {n: rg.add_node(n) for n in g.nodes}
    for a, b, d in g.edges(data=True):
        rg.add_edge(index[a], index[b], d["w"])
    return rg, index


def _cheapest(g: nx.Graph, s: str, t: str, extra: dict, rgx) -> list[str] | None:
    """The cheapest path with the edge prices multiplied by `extra`; None when there is none."""
    if rgx is not None:
        rg, index = rgx
        changed = []
        for (a, b), factor in extra.items():  # apply this request's penalties, undo them after
            for eid in rg.edge_indices_from_endpoints(index[a], index[b]):
                changed.append((eid, rg.get_edge_data_by_index(eid)))
                rg.update_edge_by_index(eid, changed[-1][1] * factor)
        try:
            paths = rx.dijkstra_shortest_paths(rg, index[s], target=index[t], weight_fn=float)
            return [rg[i] for i in paths[index[t]]] if index[t] in paths else None
        finally:
            for eid, w in changed:
                rg.update_edge_by_index(eid, w)

    def weight(a, b, d):
        return d["w"] * extra.get((a, b) if a < b else (b, a), 1.0)
    try:
        return nx.bidirectional_dijkstra(g, s, t, weight=weight)[1]  # searches from both ends
    except nx.NetworkXNoPath:
        return None


def find(data: dict, src: str, dst: str, k: int = 3) -> str:
    k = max(1, min(int(k or 3), MAX_K))
    g, nodes = _built(data)
    rgx = _BUILT[id(data.get("nodes"))][3]
    s, s_alts = resolve(src, nodes, g)
    t, t_alts = resolve(dst, nodes, g)
    if not s or not t:
        missing = src if not s else dst
        return f'No node matches "{missing}". Use a class, file or method name; pin a project as "api:Customer".'
    head = [f"From: {_describe(nodes[s])}", f"To:   {_describe(nodes[t])}"]
    for label, alts, chosen in (("from", s_alts, s), ("to", t_alts, t)):
        others = [a for a in alts if a["id"] != chosen][:3]
        if others:
            head.append(f"  (other matches for {label}: " + "; ".join(
                f"{a.get('repo')}:{a.get('label')}" for a in others) + ")")
    if s == t:
        return "\n".join(head + ["", "Both names resolve to the same node."])
    # k different ways: the cheapest path, then the same search with the edges of every path found
    # so far made dearer (x PENALTY), so the next one goes another way where one exists. k Dijkstra
    # runs instead of Yen's dozens: well under a second on a 23k-node graph.
    found: list[tuple[list[str], float]] = []
    seen_routes: set[tuple] = set()
    extra: dict[tuple[str, str], float] = {}

    for _ in range(k * 3):
        path = _cheapest(g, s, t, extra, rgx)
        if path is None:
            break
        for a, b in zip(path, path[1:]):
            key = (a, b) if a < b else (b, a)
            extra[key] = extra.get(key, 1.0) * PENALTY
        routes = tuple(_routes_of(path, g, nodes))
        inner = set(path[1:-1])
        if any(path == p or len(inner ^ set(p[1:-1])) <= 2 for p, _ in found) or \
                (routes and routes in seen_routes):
            continue
        seen_routes.add(routes)
        found.append((path, sum(g.edges[a, b]["w"] for a, b in zip(path, path[1:]))))
        if len(found) >= k:
            break
    if not found:
        return "\n".join(head + ["", "No code path between them (doc links and domain grouping do not count)."])
    out = head + [""]
    for i, (path, cost) in enumerate(found, 1):
        routes = _routes_of(path, g, nodes)
        out.append(f"Path {i}: {len(path) - 1} hops, cost {cost:.1f}"
                   + (f", routes: {'; '.join(routes)}" if routes else ", no HTTP route"))
        out.append(f"  {_describe(nodes[path[0]])}")
        for a, b in zip(path, path[1:]):
            out.append(_hop(a, b, g, nodes))
            out.append(f"  {_describe(nodes[b])}")
        out.append("")
    out.append("Cost: calls, methods and frontend->API requests are cheap; imports and type references cost "
               "more; busy hub nodes and framework classes cost extra. INFERRED edges (http_request, "
               "eloquent) are matched by name: check them in the file.")
    return "\n".join(out).rstrip()


def neighbors(data: dict, node_id: str) -> str:
    """Every connection of one node, in graphify's `explain` line format (it prints the first 20)."""
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    if node_id not in nodes:
        return ""
    out = []
    for e in data.get("links") or data.get("edges") or []:
        s, t = e.get("source"), e.get("target")
        if node_id not in (s, t) or s == t:
            continue
        other = nodes.get(t if s == node_id else s)
        if other is None:
            continue
        arrow = "-->" if s == node_id else "<--"
        where = e.get("source_file") or ""
        if where and e.get("source_location"):
            where += f":{e['source_location']}"
        out.append(f"  {arrow} {other.get('label', other['id'])} [{e.get('relation', '?')}] "
                   f"[{e.get('confidence', '')}] {where}".rstrip())
    return f"All connections ({len(out)}):\n" + "\n".join(out)
