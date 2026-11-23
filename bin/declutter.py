#!/usr/bin/env python3
"""Remove framework "god nodes" from a merged graphify graph before clustering/viz.

Domain code attaches to a handful of external base types/traits/helpers (Model, FormRequest,
Dispatchable, .log(), ...). Each is referenced by dozens of files, so a force-directed layout
collapses the whole graph into a central hairball. Stripping these meaningless hubs (and their
edges) declutters the layout while leaving every domain relationship intact.

The prune list is curated only: the symbols in kb.hubs.txt. There is no degree-based detection:
after dedupe, a high-degree node without a definition is usually a real domain service whose
definition graphify did not capture. A node the project DEFINES itself (it has `contains` or
`method` edges, a file node contains it, or it has an AST source_file and source_location) is never
pruned, even when its label is in the list: a project's own `Controller` base class is code, not
framework noise.

Usage: declutter.py <graph.json> <kb.hubs.txt>
"""
from __future__ import annotations

import sys

import graph
import lang


def main() -> int:
    graph_path, hubs_path = sys.argv[1], sys.argv[2]

    g = graph.load(graph_path)
    N, L = graph.nodes(g), graph.links(g)

    curated = set()
    with open(hubs_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                curated.add(line)

    # Nodes DEFINED in our staged code are never pruned: a node that owns members, a node that a
    # file node contains, and a node with an AST position (source_file + source_location). An
    # external base type has none of these; a project's own memberless `abstract class Controller {}`
    # (the Laravel 11/12 default) has all but the first.
    defined = {e["source"] for e in L if e.get("relation") in ("contains", "method")}
    by_id = {n["id"]: n for n in N}
    defined |= {e["target"] for e in L if e.get("relation") == "contains"
                and str(by_id.get(e["source"], {}).get("label", "")).endswith(lang.FILE_LABEL_SUFFIXES)}
    defined |= {n["id"] for n in N if n.get("source_file") and n.get("source_location")}
    prune_ids = {n["id"] for n in N if n["label"] in curated and n["id"] not in defined}

    before_n, before_e = len(N), len(L)
    g["nodes"] = [n for n in N if n["id"] not in prune_ids]
    g["links"] = [e for e in L if e["source"] not in prune_ids and e["target"] not in prune_ids]
    graph.save(g, graph_path)

    print(f"decluttered: -{before_n - len(g['nodes'])} nodes "
          f"({len(prune_ids)} hubs), -{before_e - len(g['links'])} edges "
          f"({(before_e - len(g['links'])) / max(before_e, 1) * 100:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
