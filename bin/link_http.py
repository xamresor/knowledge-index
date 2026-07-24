#!/usr/bin/env python3
"""Inject cross-repo / frontend->backend HTTP edges into a merged graphify graph.

Links frontend `api.<verb>('<path>')` call sites to the Laravel controller node
that serves the matching route (from `php artisan route:list --json`).

Usage:
  link_http.py <graph.json> <routes.json> <target_repo> <fe_root> [<fe_root> ...]

`target_repo` is the graphify repo tag whose controllers serve these routes
(e.g. "api" for a webclient->api link, or an admin app's tag for its own frontend).
Run once per (route table, frontend) pair; edges accumulate in the graph.
"""
from __future__ import annotations

import json
import os
import re
import sys

VERBS = ("get", "post", "put", "patch", "delete")
CALL_RE = re.compile(r"\bapi\.(" + "|".join(VERBS) + r")\(\s*[`'\"]([^`'\"]+)", re.IGNORECASE)
# Nuxt/ofetch style: $api('v2/objects', {method: 'POST', ...}) — verb lives in the
# options object (same line or the next few), GET when absent.
NUXT_CALL_RE = re.compile(r"\$api\(\s*[`'\"]([^`'\"]+)")
NUXT_METHOD_RE = re.compile(r"\bmethod\s*:\s*['\"](" + "|".join(VERBS) + r")['\"]", re.IGNORECASE)
NUXT_METHOD_WINDOW = 3  # lines after the call to look for `method:`


def iter_calls(lines: list[str]):
    """Yield (lineno, verb, raw_path) for every recognized API call site."""
    for i, line in enumerate(lines, 1):
        for verb, raw in CALL_RE.findall(line):
            yield i, verb.lower(), raw
        for m in NUXT_CALL_RE.finditer(line):
            window = line[m.end():] + "\n" + "\n".join(lines[i:i + NUXT_METHOD_WINDOW])
            vm = NUXT_METHOD_RE.search(window)
            yield i, (vm.group(1).lower() if vm else "get"), m.group(1)


def norm_path(p: str) -> str:
    p = p.split("?", 1)[0].strip().strip("/")
    p = re.sub(r"\$\{[^}]*\}", "{}", p)   # `${id}` -> {}
    p = re.sub(r"\{[^}?]*\??\}", "{}", p)  # `{id}` / `{id?}` -> {}
    p = re.sub(r":\w+", "{}", p)           # `:id` -> {}
    p = re.sub(r"//+", "/", p)
    return p.lower()


def action_parts(action: str) -> tuple[str, str | None] | None:
    """`App\\Http\\Controllers\\V1\\UserController@users`
       -> ("Http/Controllers/V1/UserController.php", "users")
    Drops the leading namespace segment so the suffix matches app/, src/ or packages/ roots."""
    cls, _, method = action.partition("@")
    if "Controller" not in cls:
        return None
    parts = cls.split("\\")
    suf = "/".join(parts[1:]) + ".php" if len(parts) > 1 else parts[0] + ".php"
    return suf, (method or None)


def load_routes(path: str) -> dict[tuple[str, str], tuple[str, str | None]]:
    routes: dict[tuple[str, str], tuple[str, str | None]] = {}
    for r in json.load(open(path)):
        ap = action_parts(r.get("action") or "")
        if not ap:
            continue
        for verb in (r.get("method") or "").split("|"):
            verb = verb.strip().lower()
            if verb in VERBS:
                routes[(verb, norm_path(r["uri"]))] = ap
    return routes


def main() -> int:
    graph_path, routes_path, target_repo = sys.argv[1], sys.argv[2], sys.argv[3]
    fe_roots = sys.argv[4:]
    graph = json.load(open(graph_path))
    routes = load_routes(routes_path)

    # class nodes in the target repo, keyed for suffix match; frontend file nodes by suffix
    api_nodes = [n for n in graph["nodes"]
                 if n.get("repo") == target_repo and "." not in n["label"] and n.get("source_file")]
    fe_nodes = [n for n in graph["nodes"]
                if n.get("repo") != target_repo and n["label"].endswith((".js", ".ts", ".vue"))
                and n.get("source_file")]

    # method nodes (label like ".toggleMic()") in the target repo, by source_file
    method_nodes = [n for n in graph["nodes"]
                    if n.get("repo") == target_repo and n.get("source_file")
                    and n["label"].startswith(".") and n["label"].endswith("()")]

    def api_node_for(suf: str, method: str | None):
        cls_file = None
        for n in api_nodes:
            if n["source_file"].lstrip("/").endswith(suf):
                cls_file = n["source_file"].lstrip("/")
                cls_id = n["id"]
                break
        else:
            return None
        if method:  # prefer the controller METHOD node so usage chain continues
            want = method.lower()
            for m in method_nodes:
                if m["source_file"].lstrip("/") == cls_file and m["label"].strip(".()").lower() == want:
                    return m["id"]
        return cls_id  # fallback: the controller class node

    def fe_node_for(rel: str):
        for n in fe_nodes:
            sf = n["source_file"].lstrip("/")
            if sf.endswith(rel) or rel.endswith(sf):
                return n["id"]
        return None

    edges, matched, unmatched, seen = [], 0, 0, set()
    for root in fe_roots:
        repo_root = os.path.dirname(root.rstrip("/"))
        for dp, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith((".js", ".ts", ".vue")):
                    continue
                fp = os.path.join(dp, fn)
                rel = os.path.relpath(fp, repo_root)
                try:
                    lines = open(fp, encoding="utf-8", errors="ignore").read().splitlines()
                except OSError:
                    continue
                src_node = fe_node_for(rel)
                for i, verb, raw in iter_calls(lines):
                    ap = routes.get((verb, norm_path(raw)))
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
                    edges.append({
                        "relation": "http_request",
                        "confidence": "EXTRACTED",
                        "confidence_score": 1.0,
                        "weight": 1.0,
                        "http_method": verb.upper(),
                        "http_uri": norm_path(raw),
                        "source_file": rel,
                        "source_location": f"L{i}",
                        "source": src_node,
                        "target": tgt,
                    })

    graph["links"].extend(edges)
    json.dump(graph, open(graph_path, "w"))
    pairs = {(e["source"].split("::")[0], e["target"].split("::")[0]) for e in edges}
    print(f"[{target_repo}] http edges: {len(edges)} matched, {unmatched} unmatched; pairs={pairs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
