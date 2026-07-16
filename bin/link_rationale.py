#!/usr/bin/env python3
"""Mine rationale comments into graph nodes (idea borrowed from autonomic-workspace designs).

Comments that explain WHY code is the way it is — `NOTE:`, `WHY:`, `HACK:`, `SECURITY:`,
`RATIONALE:` — carry design knowledge the AST can't see. Each becomes a `rationale` node with an
`explains` edge to the class node defined in that file (fallback: the file node). Plain TODOs are
deliberately skipped — they are work items, not rationale, and would flood the graph.

Run AFTER enrich.py (wants node types/domains in place). Usage:
  link_rationale.py <graph.json> <repos_dir>
"""
from __future__ import annotations

import json
import os
import re
import sys

MARKERS = ("NOTE", "WHY", "HACK", "SECURITY", "RATIONALE")
# `// WHY: ...`, `# NOTE - ...`, `* SECURITY: ...` — a marker word at a comment start
COMMENT_RE = re.compile(
    r"(?://|#|/\*+|\*)\s*(" + "|".join(MARKERS) + r")\b[:\-–]\s*(.+)", re.IGNORECASE)
EXTS = (".php", ".js", ".ts", ".vue")
MAX_LABEL = 110
MAX_PER_FILE = 8  # a file drowning in markers is noise, not knowledge


def main() -> int:
    graph_path, repos_dir = sys.argv[1], sys.argv[2]
    g = json.load(open(graph_path))
    N, L = g["nodes"], g["links"]

    defined = {e["source"] for e in L if e.get("relation") in ("method", "contains")}
    classnode_by_file: dict[str, str] = {}
    anyclass_by_file: dict[str, str] = {}
    filenode_by_file: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for n in N:
        sf = (n.get("source_file") or "").lstrip("/")
        if not sf:
            continue
        if not n["label"].endswith(EXTS + ("()",)):
            anyclass_by_file.setdefault(sf, n["id"])  # memberless classes still attach
            if n["id"] in defined:
                classnode_by_file.setdefault(sf, n["id"])
        if n["label"].endswith(EXTS):
            filenode_by_file.setdefault(sf, n["id"])
        meta[n["id"]] = n

    def attach_for(fp: str) -> str | None:
        for table in (classnode_by_file, anyclass_by_file, filenode_by_file):
            for sf, nid in table.items():
                if fp.endswith(sf):
                    return nid
        return None

    nodes, edges, skipped = [], [], 0
    for dp, _, files in os.walk(repos_dir):
        for fn in files:
            if not fn.endswith(EXTS):
                continue
            fp = os.path.join(dp, fn)
            try:
                lines = open(fp, encoding="utf-8", errors="ignore").read().splitlines()
            except OSError:
                continue
            hits = []
            for i, line in enumerate(lines, 1):
                m = COMMENT_RE.search(line)
                if m:
                    hits.append((i, m.group(1).upper(), m.group(2).strip().rstrip("*/ ")))
            if not hits:
                continue
            if len(hits) > MAX_PER_FILE:
                skipped += len(hits) - MAX_PER_FILE
                hits = hits[:MAX_PER_FILE]
            tgt = attach_for(fp)
            if not tgt:
                skipped += len(hits)
                continue
            host = meta[tgt]
            rel = os.path.relpath(fp, repos_dir)
            for i, marker, text in hits:
                label = f"{marker}: {text}"
                if len(label) > MAX_LABEL:
                    label = label[: MAX_LABEL - 1] + "…"
                rid = f"rationale::{rel}:L{i}"
                nodes.append({"id": rid, "label": label, "type": "rationale",
                              "marker": marker, "domain": host.get("domain", "misc"),
                              "repo": host.get("repo", "misc"),
                              "source_file": rel, "source_location": f"L{i}",
                              "community": host.get("community")})
                # EXTRACTED: the comment text and its file are literal; only the class-vs-file
                # attach point involves a (same-file) heuristic
                edges.append({"source": rid, "target": tgt, "relation": "explains",
                              "confidence": "EXTRACTED", "weight": 0.5})

    g["nodes"] = N + nodes
    g["links"] = L + edges
    json.dump(g, open(graph_path, "w"))
    from collections import Counter
    by_marker = Counter(n["marker"] for n in nodes)
    print(f"rationale: +{len(nodes)} nodes (explains edges), {skipped} skipped "
          f"(no attach node / per-file cap); {dict(by_marker)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
