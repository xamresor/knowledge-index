#!/usr/bin/env python3
"""Remove framework "god nodes" from a merged graphify graph before clustering/viz.

Domain code attaches to a handful of external base types/traits/helpers (Model, FormRequest,
Dispatchable, .log(), ...). Each is referenced by dozens of files, so a force-directed layout
collapses the whole graph into a central hairball. Stripping these meaningless hubs (and their
edges) declutters the layout while leaving every domain relationship intact.

Two prune sources:
  1. curated symbols in kb.hubs.txt
  2. auto-detected: bare ClassName-style symbol nodes with degree >= --cap (logged for transparency)

Usage: declutter.py <graph.json> <kb.hubs.txt> [--cap N]
"""
from __future__ import annotations

import re
import sys

import graph
from collections import Counter



def main() -> int:
    graph_path, hubs_path = sys.argv[1], sys.argv[2]
    cap = 40
    if "--cap" in sys.argv:
        cap = int(sys.argv[sys.argv.index("--cap") + 1])

    g = graph.load(graph_path)
    N, L = graph.nodes(g), graph.links(g)

    curated = set()
    for line in open(hubs_path):
        line = line.split("#", 1)[0].strip()
        if line:
            curated.add(line)

    deg = Counter()
    defined = set()  # nodes DEFINED in our staged code (own members) — never prune these
    for e in L:
        deg[e["source"]] += 1
        deg[e["target"]] += 1
        if e.get("relation") in ("contains", "method"):
            defined.add(e["source"])

    # curated-only: prune the vetted framework symbols in kb.hubs.txt and nothing else.
    # (No degree-based auto-detection — after dedupe, high-degree def-less nodes are usually
    # real domain services whose definition graphify didn't capture; we must keep those.)
    prune_ids, auto = set(), []
    for n in N:
        if n["label"] in curated:
            prune_ids.add(n["id"])

    before_n, before_e = len(N), len(L)
    g["nodes"] = [n for n in N if n["id"] not in prune_ids]
    g["links"] = [e for e in L if e["source"] not in prune_ids and e["target"] not in prune_ids]
    graph.save(g, graph_path)

    print(f"decluttered: -{before_n - len(g['nodes'])} nodes "
          f"({len(prune_ids)} hubs), -{before_e - len(g['links'])} edges "
          f"({(before_e - len(g['links']))/before_e*100:.0f}%)")
    if auto:
        print("  auto-detected hubs (degree>=%d):" % cap,
              ", ".join(f"{l}({d})" for l, d in sorted(auto, key=lambda x: -x[1])[:15]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
