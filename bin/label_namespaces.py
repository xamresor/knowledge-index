#!/usr/bin/env python3
"""Name graphify communities by their dominant namespace / domain (deterministic, zero-cost).

Replaces "Community 204" with labels like "api · SmartCamera" or "webclient · stores",
derived from the source-file paths of each community's members. Writes
graphify-out/.graphify_labels.json; run `graphify cluster-only --no-label` afterwards to
bake the names into GRAPH_REPORT.md and graph.html.

Usage: label_namespaces.py <graph.json> <labels.json>
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict


def domain(node: dict) -> str | None:
    """Derive a namespace/domain string from a node's source file + repo."""
    sf = (node.get("source_file") or "").lstrip("/")
    if not sf:
        return None
    repo = node.get("repo", "?")
    p = sf.split("/")

    # Laravel package: packages/<Pkg>/src/... -> <Pkg>
    if p[0] in ("packages", "packages-dev") and len(p) > 1:
        return p[1]
    # Component domain: app/Components/<Domain>/... -> Components\<Domain>
    if len(p) > 2 and p[0] == "app" and p[1] == "Components":
        return f"Components\\{p[2]}"
    # app/<Layer>/<Sub?> -> Layer (or Layer\Sub for Http/Controllers/V2 etc.)
    if p[0] in ("app", "src") and len(p) > 1:
        if p[1] == "Http" and len(p) > 3:
            return f"Http\\{p[2]}\\{p[3]}" if p[2] == "Controllers" else f"Http\\{p[2]}"
        return p[1]
    # frontend: src/<area>/... or assets-js/<area>/...
    if p[0] in ("src", "assets-js") and len(p) > 1:
        return p[1]
    return p[0]


def main() -> int:
    graph_path, labels_path = sys.argv[1], sys.argv[2]
    graph = json.load(open(graph_path))

    by_comm: dict[int, Counter] = defaultdict(Counter)
    repos_by_comm: dict[int, Counter] = defaultdict(Counter)
    for n in graph["nodes"]:
        c = n.get("community")
        if c is None:
            continue
        d = domain(n)
        if d:
            by_comm[c][d] += 1
        repos_by_comm[c][n.get("repo", "?")] += 1

    labels: dict[str, str] = {}
    for c, counter in by_comm.items():
        if not counter:
            continue
        top = counter.most_common(2)
        repo = repos_by_comm[c].most_common(1)[0][0]
        name = top[0][0]
        # if a clear secondary domain shares the community, surface it
        if len(top) > 1 and top[1][1] >= max(2, top[0][1] * 0.6):
            name = f"{name} + {top[1][0]}"
        labels[str(c)] = f"{repo} · {name}"

    json.dump(labels, open(labels_path, "w"), indent=0)
    print(f"labeled {len(labels)} communities by namespace/domain")
    for c, l in sorted(labels.items(), key=lambda kv: int(kv[0]))[:12]:
        print(f"  {c}: {l}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
