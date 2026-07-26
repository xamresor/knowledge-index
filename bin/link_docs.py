#!/usr/bin/env python3
"""Pipeline step: add documents to the graph via the markdown language plugin.

A thin wrapper on purpose. The plugin lives in `bin/lang/markdown.py` because that is where language
knowledge belongs, but a module inside a package cannot be executed directly (relative imports break),
and every other pipeline step is a `bin/*.py` script. So this is the entry point and the plugin stays
importable and testable.

Usage: link_docs.py <graph.json> <project>=<docs_dir> [...]
Run AFTER the code enrichers — `documents` edges point at symbols they add.
"""
from __future__ import annotations

import sys

import graph as graph_io
from lang import markdown


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: link_docs.py <graph.json> <project>=<docs_dir> [...]", file=sys.stderr)
        return 2
    graph_path = sys.argv[1]
    roots: list[tuple[str, str]] = []
    for spec in sys.argv[2:]:
        name, sep, root = spec.partition("=")
        if not sep:
            print(f"skip {spec}: expected <project>=<docs_dir>", file=sys.stderr)
            continue
        roots.append((name, root))
    g = graph_io.load(graph_path)
    nodes, edges = markdown.enrich(g, roots)
    graph_io.save(g, graph_path)
    print(f"docs: +{nodes} doc nodes, +{edges} edges "
          f"(links_to = EXTRACTED, documents = INFERRED, capped at "
          f"{markdown.MAX_MENTIONS_PER_DOC}/doc)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
