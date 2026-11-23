#!/usr/bin/env python3
"""Inject cross-repo / frontend->backend HTTP edges into a merged graphify graph.

Links frontend `api.<verb>('<path>')` call sites to the Laravel controller node
that serves the matching route (from `php artisan route:list --json`).

Usage:
  link_http.py [--from <caller_repo>] <graph.json> <routes.json> <target_repo> <fe_root> [<fe_root> ...]

`target_repo` is the graphify repo tag whose controllers serve these routes
(e.g. "api" for a webclient->api link, or an admin app's tag for its own frontend).
Run once per (route table, frontend) pair; edges accumulate in the graph.

`--from` is the repo tag of the calling project. A call site is resolved to a node of that repo only,
so two frontends with the same relative path (`src/stores/cart.js`) do not swap their calls. Without
`--from`, a root folder named like a repo decides; else a path found in two repos gives no edge.
"""
from __future__ import annotations

import os
import re
import sys

import graph
import lang
from lang import js_ts, php_laravel

VERBS = js_ts.VERBS


def iter_calls(lines: list[str]):
    """Every recognised API call site — the client shapes live in bin/lang/js_ts.py."""
    return js_ts.iter_calls(lines)


def norm_path(p: str) -> str:
    p = p.strip()
    p = re.sub(r"^\$\{[^}]*\}", "", p)    # drop leading base-URL var: `${API_BASE}/x` -> `/x`
    p = re.sub(r"\$\{[^}]*\}", "{}", p)   # remaining `${id}` -> {}
    # collapse path params BEFORE stripping the query, so an optional Laravel
    # param `{id?}` isn't cut in half by the `?` split below.
    p = re.sub(r"\{[^}]*\}", "{}", p)     # `{id}` / `{id?}` -> {}
    p = re.sub(r":\w+", "{}", p)          # `:id` -> {}
    p = p.split("?", 1)[0]                # strip query string
    p = p.strip().strip("/")
    p = re.sub(r"//+", "/", p)
    return p.lower()


def action_parts(action: str) -> tuple[str, str | None] | None:
    """`App\\Http\\Controllers\\V1\\UserController@users`
       -> ("App/Http/Controllers/V1/UserController.php", "users")
    The whole namespace path is kept: `controller_file()` matches it against real files segment by
    segment, so a namespace root that differs from its folder (`SmartMonitor\\Sw` in
    `packages/sw/src`) still matches."""
    cls, _, method = action.partition("@")
    if "Controller" not in cls:
        return None
    return "/".join(cls.split("\\")) + ".php", (method or None)


def min_segments(ns_path: str) -> int:
    """How many trailing segments must match: from `Http/` on when the namespace has it, else
    the folder and the file. A bare file-name match is too weak (`app/Legacy/X.php`)."""
    parts = ns_path.split("/")
    if "Http" in parts:
        return len(parts) - parts.index("Http")
    return min(2, len(parts))


def common_tail(a: list[str], b: list[str]) -> int:
    """Number of equal trailing path segments (case-insensitive: `App` is the folder `app`)."""
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n].lower() == b[-1 - n].lower():
        n += 1
    return n


def controller_file(ns_path: str, files: list[str]) -> str | None:
    """The file among `files` (same base name) whose path shares the longest tail with the
    namespace path. Two files with the same best tail are ambiguous: no match."""
    want = ns_path.split("/")
    need = min_segments(ns_path)
    scored = [(common_tail(want, f.split("/")), f) for f in files]
    scored = [(s, f) for s, f in scored if s >= need]
    if not scored:
        return None
    best = max(s for s, _ in scored)
    top = [f for s, f in scored if s == best]
    return top[0] if len(top) == 1 else None


def tail_match(rel: str, sf: str) -> int:
    """Overlap of a call-site path and a node path when one ends with the other at a segment
    boundary (a `calls` root can sit above or below the staged code root); 0 when they differ."""
    if rel == sf:
        return len(rel)
    if rel.endswith("/" + sf):
        return len(sf)
    if sf.endswith("/" + rel):
        return len(rel)
    return 0


def load_routes(path: str) -> dict[tuple[str, str], tuple[str, str | None]]:
    routes: dict[tuple[str, str], tuple[str, str | None]] = {}
    for r in graph.read_json(path):  # a route table is a list, not a graph
        ap = action_parts(r.get("action") or "")
        if not ap:
            continue
        for verb in (r.get("method") or "").split("|"):
            verb = verb.strip().lower()
            if verb in VERBS:
                routes[(verb, norm_path(r["uri"]))] = ap
    return routes


def match_route(routes, verb: str, path: str):
    """Exact (verb, path); else a route whose uri is `<base prefix>/<path>` for the same verb —
    the frontend omits the base prefix its base URL supplies (Laravel `api/`, an API version).

    A prefix of base segments only (`api`, `v1`, …) is that case: the shortest one wins. Any other
    prefix means the call site lost part of its own path (`${baseUrl.value}/send` -> `send`): then
    only a single candidate counts. Two (`…/invoices/{}/send` and `…/receipts/{}/send`) is no match —
    picking the shortest once linked a receipt editor to the invoice controller."""
    hit = routes.get((verb, path))
    if hit:
        return hit
    if not path:
        return None
    cands = sorted((u for (v, u) in routes if v == verb and u.endswith("/" + path)), key=len)
    based = [u for u in cands
             if all(php_laravel.BASE_SEGMENT.match(seg) for seg in u[:-len(path) - 1].split("/"))]
    if based:
        return routes[(verb, based[0])]
    targets = {routes[(verb, u)] for u in cands}
    return targets.pop() if len(targets) == 1 else None


def parse_args(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split off `--from <repo>`; the rest are the positional arguments."""
    args = list(argv)
    caller = None
    if "--from" in args:
        i = args.index("--from")
        caller = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    return caller, args


def main() -> int:
    caller_repo, args = parse_args(sys.argv[1:])
    graph_path, routes_path, target_repo = args[0], args[1], args[2]
    fe_roots = args[3:]
    g = graph.load(graph_path)
    routes = load_routes(routes_path)

    # Target side: controller class nodes by file (and file base name), method nodes by (file, name).
    api_class: dict[str, str] = {}
    api_by_base: dict[str, list[str]] = {}
    api_method: dict[tuple[str, str], str] = {}
    # Caller side, keyed by (repo, source_file): a call site is looked up in its own repo only.
    fe_funcs: dict[tuple[str, str], list[tuple[int, str]]] = {}
    fe_files: dict[tuple[str, str], str] = {}
    php_methods: dict[tuple[str, str], list[tuple[int, str]]] = {}
    php_files: dict[tuple[str, str], str] = {}
    by_base: dict[str, set[tuple[str, str]]] = {}
    for n in graph.nodes(g):
        sf = (n.get("source_file") or "").lstrip("/")
        if not sf:
            continue
        repo, label = n.get("repo"), str(n.get("label", ""))
        loc = str(n.get("source_location") or "")
        line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
        is_method = label.startswith(".") and label.endswith("()")
        if repo == target_repo:
            if is_method:
                api_method.setdefault((sf, label.strip(".()").lower()), n["id"])
            elif "." not in label and sf not in api_class:
                api_class[sf] = n["id"]
                api_by_base.setdefault(os.path.basename(sf), []).append(sf)
            # A project whose own frontend calls its own routes (serves = itself): its nodes are
            # call sites too, so they also go into the caller maps below.
            if repo != caller_repo:
                continue
        key = (repo, sf)
        if sf.endswith(lang.FRONTEND):
            if label.endswith(lang.FRONTEND):
                fe_files.setdefault(key, n["id"])
            if line is not None and label.endswith("()"):
                fe_funcs.setdefault(key, []).append((line, n["id"]))
        elif sf.endswith(".php"):
            if is_method and line is not None:
                php_methods.setdefault(key, []).append((line, n["id"]))
            elif label == os.path.basename(sf):
                php_files.setdefault(key, n["id"])
        else:
            continue
        by_base.setdefault(os.path.basename(sf), set()).add(key)

    def api_node_for(ns_path: str, method: str | None):
        cls_file = controller_file(ns_path, api_by_base.get(os.path.basename(ns_path), []))
        if not cls_file:
            return None
        if method:  # prefer the controller METHOD node so usage chain continues
            mid = api_method.get((cls_file, method.lower()))
            if mid:
                return mid
        return api_class[cls_file]  # fallback: the controller class node

    def caller_key(rel: str, hints: set[str]) -> tuple[str, str] | None:
        """The (repo, source_file) of a call site. `--from` names the repo; without it, a root
        folder named like a repo decides, else the path must be unique across the repos."""
        cands = [(tail_match(rel, sf), (repo, sf))
                 for repo, sf in by_base.get(os.path.basename(rel), ())]
        cands = [c for c in cands if c[0]]
        if caller_repo:
            cands = [c for c in cands if c[1][0] == caller_repo]
        else:
            repos = {k[0] for _, k in cands}
            if len(repos) > 1:
                cands = [c for c in cands if c[1][0] in hints]
        if not cands:
            return None
        best = max(score for score, _ in cands)
        top = {k for score, k in cands if score == best}
        return top.pop() if len(top) == 1 else None

    def enclosing(funcs: list[tuple[int, str]], line: int) -> str | None:
        before = [f for f in funcs if f[0] <= line]
        return max(before)[1] if before else None

    # PHP backends calling another service: the call's source is its enclosing method (the method
    # node with the latest start line not after the call), else the file node of that file.
    def php_node_for(key: tuple[str, str] | None, line: int):
        if not key:
            return None
        return enclosing(php_methods.get(key, []), line) or php_files.get(key)

    # A frontend call's source is its enclosing function (the function node with the latest start line
    # not after the call), else the file. Linking the file made every function of an API module share
    # all of its routes, so a path from one function could leave through another function's route.
    def fe_node_for(key: tuple[str, str] | None, line: int = 0):
        if not key:
            return None
        return (enclosing(fe_funcs.get(key, []), line) if line else None) or fe_files.get(key)

    edges, matched, unmatched, seen = [], 0, 0, set()
    for root in fe_roots:
        repo_root = os.path.dirname(root.rstrip("/"))
        hints = {os.path.basename(repo_root), os.path.basename(root.rstrip("/"))}
        for dp, dirs, files in os.walk(root):
            # don't scan dependency / build output — not our source, and slow
            dirs[:] = js_ts.prune(dirs)
            for fn in files:
                backend = fn.endswith(".php")
                if not (backend or fn.endswith(lang.FRONTEND)):
                    continue
                if backend and re.search(r"(^|/)tests?(/|$)", os.path.relpath(dp, root)):
                    continue  # a test calls its own API through the test client, not another service
                fp = os.path.join(dp, fn)
                rel = os.path.relpath(fp, repo_root)
                try:
                    text = open(fp, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                if backend:
                    calls = list(php_laravel.iter_backend_calls(text))
                else:
                    calls = list(iter_calls(text.splitlines()))
                key = caller_key(rel, hints) if calls else None
                for i, verb, raw in calls:
                    src_node = php_node_for(key, i) if backend else fe_node_for(key, i)
                    ap = match_route(routes, verb, norm_path(raw))
                    if not ap:
                        unmatched += 1
                        continue
                    tgt = api_node_for(ap[0], ap[1])
                    if not (src_node and tgt):
                        unmatched += 1
                        continue
                    sig = (src_node, tgt, verb, norm_path(raw))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    matched += 1
                    # INFERRED, not EXTRACTED: the route match is exact, but both endpoints
                    # are resolved by file-suffix heuristics (see api_node_for/fe_node_for).
                    edges.append({
                        "relation": "http_request",
                        "confidence": "INFERRED",
                        "confidence_score": 0.9,
                        "weight": 1.0,
                        "http_method": verb.upper(),
                        "http_uri": norm_path(raw),
                        "source_file": rel,
                        "source_location": f"L{i}",
                        "source": src_node,
                        "target": tgt,
                    })

    graph.links(g).extend(edges)
    graph.save(g, graph_path)
    pairs = {(graph.repo_of(e["source"]), graph.repo_of(e["target"])) for e in edges}
    print(f"[{target_repo}] http edges: {len(edges)} matched, {unmatched} unmatched; pairs={pairs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
