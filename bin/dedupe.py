#!/usr/bin/env python3
"""Canonicalize duplicate symbol nodes in a merged graphify graph.

graphify emits a separate "reference" node for a class/interface/enum each place it is imported
or used, distinct from the node where it is defined — and never links them. So a class ends up
connected only to its own methods while the controllers/services that use it point at throwaway
reference copies (which then look like floating, unconnected elements).

This merges, per repo, all same-label class-like nodes into one canonical node:
  * if exactly one definition exists (a node with method/contains children) -> merge into it
  * if no definition exists                                                 -> merge into one shared node
  * if several definitions share the label (ambiguous)                      -> leave untouched
All edges are redirected to the canonical id; self-loops and duplicate edges are dropped.

Usage: dedupe.py <graph.json>
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict


def is_classlike(label: str) -> bool:
    return not label.endswith(("()", ".php", ".js", ".vue", ".ts"))


def main() -> int:
    path = sys.argv[1]
    g = json.load(open(path))
    N, L = g["nodes"], g["links"]

    defined = {e["source"] for e in L if e.get("relation") in ("method", "contains")}

    groups = defaultdict(list)
    for n in N:
        if is_classlike(n["label"]):
            groups[(n.get("repo", "?"), n["label"])].append(n)

    remap = {}                       # dup id -> canonical id
    merged = 0
    for (_repo, _label), grp in groups.items():
        if len(grp) < 2:
            continue
        defs = [n for n in grp if n["id"] in defined]
        if len(defs) > 1:            # ambiguous (real name collision) — don't merge
            continue
        canonical = defs[0] if defs else grp[0]
        for n in grp:
            if n["id"] != canonical["id"]:
                remap[n["id"]] = canonical["id"]
                merged += 1

    # rewrite edges onto canonical ids; drop self-loops + exact duplicates
    seen, new_links = set(), []
    for e in L:
        s = remap.get(e["source"], e["source"])
        t = remap.get(e["target"], e["target"])
        if s == t:
            continue
        key = (s, t, e.get("relation"))
        if key in seen:
            continue
        seen.add(key)
        e = dict(e, source=s, target=t)
        new_links.append(e)

    g["nodes"] = [n for n in N if n["id"] not in remap]
    g["links"] = new_links
    json.dump(g, open(path, "w"))
    print(f"deduped: merged {merged} reference nodes into {len(set(remap.values()))} canonical "
          f"symbols; nodes {len(N)}->{len(g['nodes'])}, edges {len(L)}->{len(new_links)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
