#!/usr/bin/env python3
"""Review context for a change: what a diff touches in the code graph, and who should look.

A reviewer of a pull request asks the same questions every time: which classes and methods changed,
what depends on them (in this repository and in the others), which HTTP routes go through them and
which frontend screens call those routes, which docs describe them, and which NOTE/WHY comments sit
next to them. Every answer is already in the graph; this module puts them together for one diff.

Input: a unified diff (`git diff base...head`) or a list of files, each with optional line ranges.
Line numbers are mapped to the symbol that starts at or above them in the same file, so the match is
as good as the graph's source locations (the start line of each method and class). The graph is built
from the indexed checkout, so a symbol added by the diff itself is not in it yet: such files are listed
as "not in the graph".

`analyze(graph, changes, routes)` is pure: the graph as node-link JSON (`nodes`, `links`/`edges`),
`changes` from `parse_diff` / `parse_files`, `routes` the rows of the contract index (contracts.json).
It runs in the graph worker (bin/graph_worker.py) on the caller's view, so access per project holds.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

MAX_DIFF = 2_000_000
SYMBOL_TYPES = {"method", "function", "symbol", "class", "model", "controller", "interface", "trait",
                "service", "request", "resource", "enum"}
CONTAINER_TYPES = {"class", "model", "controller", "interface", "trait", "service", "request", "resource", "enum"}
# edge source depends on edge target for these relations: a change to the target can break the source
DEPENDS = {"calls", "references", "imports", "imports_from", "inherits", "implements", "uses",
           "indirect_call", "mixes_in", "eloquent", "http_request", "references_constant", "re_exports",
           "uses_config"}
DOC_RELATIONS = {"documents", "explains", "links_to"}
DEPTH = 2
LIST_MAX = 40

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> dict[str, set[int]]:
    """Unified diff -> {path: changed line numbers}. Added lines count at their new number, removed
    lines at the new-file position where they were; a deleted file keeps its old numbers."""
    if len(text) > MAX_DIFF:
        raise ValueError(f"the diff is larger than {MAX_DIFF // 1_000_000} MB: pass 'files' instead")
    out: dict[str, set[int]] = {}
    old_path = new_path = None
    cur: set[int] | None = None
    new_no = old_no = 0
    deleted = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, old_path, new_path = None, None, None
            continue
        if line.startswith("--- "):
            old_path = _strip(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _strip(line[4:])
            deleted = new_path is None
            path = new_path or old_path
            cur = out.setdefault(path, set()) if path else None
            continue
        m = _HUNK.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            continue
        if cur is None or not line:
            continue
        tag = line[0]
        if tag == "+":
            cur.add(new_no)
            new_no += 1
        elif tag == "-":
            cur.add(old_no if deleted else new_no)
            old_no += 1
        elif tag == " ":
            new_no += 1
            old_no += 1
    return out


def _strip(p: str) -> str | None:
    p = p.split("\t", 1)[0].strip()
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_files(items: list[str]) -> dict[str, set[int]]:
    """["app/X.php", "app/Y.php:10-20,31"] -> {path: lines}; no lines = the whole file."""
    out: dict[str, set[int]] = {}
    for item in items:
        path, _, spec = str(item).partition(":")
        lines = out.setdefault(path.strip(), set())
        for part in filter(None, spec.split(",")):
            a, _, b = part.partition("-")
            if a.strip().isdigit():
                lo = int(a)
                hi = int(b) if b.strip().isdigit() else lo
                lines.update(range(lo, min(hi, lo + 5000) + 1))
    return out


def _line(n: dict) -> int:
    m = re.match(r"L(\d+)", str(n.get("source_location") or ""))
    return int(m.group(1)) if m else 0


def _name(n: dict) -> str:
    label = str(n.get("label") or n.get("id"))
    return f"{n.get('repo', '')}:{label}" if n.get("repo") else label


def analyze(graph: dict, changes: dict[str, set[int]], routes: list[dict] | None = None,
            project: str = "") -> str:
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("links") if "links" in graph else graph.get("edges", [])
    by_file: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for n in nodes.values():
        if n.get("source_file") and (not project or n.get("repo") == project):
            by_file[(n.get("repo", ""), n["source_file"])].append(n)
    files = sorted({f for _, f in by_file})

    def find(path: str) -> list[tuple[str, str]]:
        """Graph files for a diff path: the same path, or the diff path ends with it (a monorepo
        or a project whose code root is a sub-folder). The longest match wins."""
        hits = [f for f in files if path == f or path.endswith("/" + f)]
        if not hits:
            return []
        best = max(len(f) for f in hits)
        return sorted(k for k in by_file if k[1] in hits and len(k[1]) == best)

    changed: dict[str, str] = {}      # node id -> why ("changed lines" / "file changed")
    missing: list[str] = []
    for path, lines in sorted(changes.items()):
        keys = find(path)
        if not keys:
            missing.append(path)
            continue
        for key in keys:
            syms = sorted((n for n in by_file[key] if n.get("type") in SYMBOL_TYPES), key=_line)
            if not lines:
                for n in syms:
                    if n.get("type") in CONTAINER_TYPES:
                        changed[n["id"]] = "file changed"
                continue
            for ln in sorted(lines):
                owner = None
                for n in syms:
                    if 0 < _line(n) <= ln:
                        owner = n
                    elif _line(n) > ln:
                        break
                if owner is not None:
                    changed.setdefault(owner["id"], "changed lines")

    # who depends on the changed symbols: walk edges backwards (source depends on target)
    rev: dict[str, list[tuple[str, str]]] = defaultdict(list)
    docs: dict[str, set[str]] = defaultdict(set)
    notes: dict[str, list[str]] = defaultdict(list)
    members: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        rel, s, t = e.get("relation"), e.get("source"), e.get("target")
        if s not in nodes or t not in nodes:
            continue
        if rel in DEPENDS:
            s, t = _orient(e, nodes[s], nodes[t])
            rev[t].append((s, rel))
        elif rel in DOC_RELATIONS and "doc" in (nodes[s].get("type"), nodes[t].get("type")):
            d, x = (s, t) if nodes[s].get("type") == "doc" else (t, s)
            docs[x].add(d)
        elif rel == "rationale_for":
            r, x = (s, t) if nodes[s].get("type") == "rationale" or nodes[t].get("type") != "rationale" else (t, s)
            notes[x].append(r)
        elif rel in ("contains", "method"):
            if nodes[t].get("type") in CONTAINER_TYPES and nodes[s].get("type") not in CONTAINER_TYPES:
                s, t = t, s          # undirected graph: the class is the container
            members[s].append(t)
    # a changed class reaches its callers through its methods too
    seeds = set(changed)
    for nid in list(changed):
        if nodes[nid].get("type") in CONTAINER_TYPES and changed[nid] == "file changed":
            seeds.update(m for m in members.get(nid, []) if nodes[m].get("type") in SYMBOL_TYPES)
    impact: dict[str, tuple[int, str, str]] = {}   # id -> (depth, via relation, from id)
    q = deque((s, 0) for s in seeds)
    while q:
        cur, d = q.popleft()
        if d >= DEPTH:
            continue
        for src, rel in rev.get(cur, []):
            if src in seeds or src in impact:
                continue
            impact[src] = (d + 1, rel, cur)
            q.append((src, d + 1))

    out: list[str] = []
    ch = [nodes[i] for i in changed]
    out.append(f"## Review context\n\nChanged: {len(changes)} file(s), {len(ch)} symbol(s) in the graph.")
    # routes whose controller method changed or depends on the change
    touched = seeds | set(impact)
    route_hits = []
    for r in routes or []:
        cls, _, meth = str(r.get("action") or "").partition("@")
        if not cls:
            continue
        short = cls.rsplit("\\", 1)[-1]
        for nid in touched:
            n = nodes[nid]
            label = str(n.get("label") or "")
            if n.get("repo") != r.get("project"):
                continue
            # one class per file in Laravel: the controller is the file's name
            stem = str(n.get("source_file") or "").rsplit("/", 1)[-1].split(".", 1)[0]
            hit_method = meth and label.strip(".").rstrip("()") == meth and stem == short
            # the controller class itself depends on the change (constructor injection, a property, a
            # parent): every action of it does
            hit_class = label == short and n.get("type") in CONTAINER_TYPES
            if hit_method or hit_class:
                route_hits.append((r, "changed" if nid in seeds else "depends on the change"))
                break
    if route_hits:
        out.append(f"\n### HTTP routes touched ({len(route_hits)})")
        for r, why in route_hits[:LIST_MAX]:
            callers = r.get("callers") or []
            who = "; ".join(f"{c['repo']} {c['at']}" for c in callers[:8]) or "no frontend call site found"
            more = f" (+{len(callers) - 8})" if len(callers) > 8 else ""
            out.append(f"- {r.get('method')} /{r.get('uri')}  → {r.get('action', '').rsplit(chr(92), 1)[-1]}"
                       f"  [{why}]\n  called from: {who}{more}")

    if impact:
        home = _repo_of(changed, nodes)   # other projects first: those are the surprises
        # a file node next to its own class says the same twice: list files only from other projects
        # (a frontend module is a file node and is exactly what a backend reviewer needs to see)
        rows = sorted(((i, v) for i, v in impact.items()
                       if nodes[i].get("type") != "file" or nodes[i].get("repo") != home),
                      key=lambda kv: (kv[1][0], nodes[kv[0]].get("repo") == home, _name(nodes[kv[0]])))
        cross = sum(1 for i in impact if nodes[i].get("repo") not in {nodes[c].get("repo") for c in changed})
        out.append(f"\n### Depends on the change ({len(impact)}, {cross} in other projects)")
        for nid, (d, rel, via) in rows[:LIST_MAX]:
            n = nodes[nid]
            out.append(f"- {_name(n)}  ({n.get('source_file', '')}:{n.get('source_location', '')}) "
                       f"— {rel} {nodes[via].get('label')}" + ("" if d == 1 else f", {d} steps"))
        if len(rows) > LIST_MAX:
            out.append(f"- … +{len(rows) - LIST_MAX} more (graph_affected on a symbol lists them all)")

    doc_names: dict[str, set[str]] = defaultdict(set)
    for s_id in seeds:
        for dn in docs.get(s_id, ()):
            d = nodes[dn]
            doc_names[f"{d.get('repo', '')}:{d.get('source_file') or d.get('label')}"].add(str(nodes[s_id].get("label")))
    if doc_names:
        out.append(f"\n### Docs that describe the changed code ({len(doc_names)}): check they still hold")
        for path, syms in sorted(doc_names.items())[:LIST_MAX]:
            out.append(f"- {path}  (names {', '.join(sorted(syms)[:5])})")

    note_rows = [(nodes[n], nodes[s]) for s in seeds for n in notes.get(s, ())]
    if note_rows:
        out.append(f"\n### Notes in the code next to the change ({len(note_rows)}): read before merging")
        for note, sym in note_rows[:LIST_MAX]:
            text = str(note.get("label") or "")[:200]
            out.append(f"- {sym.get('label')}: {text}")

    if ch:
        per_file: dict[str, list[str]] = defaultdict(list)
        for n in sorted(ch, key=lambda n: (n.get("repo", ""), n.get("source_file", ""), _line(n))):
            per_file[f"{n.get('repo', '')}:{n.get('source_file')}"].append(str(n.get("label")))
        out.append(f"\n### Changed symbols ({len(ch)} in {len(per_file)} files)")
        for i, (f, labels) in enumerate(per_file.items()):
            if i == LIST_MAX:
                out.append(f"- … +{len(per_file) - LIST_MAX} more files")
                break
            more = f" +{len(labels) - 8}" if len(labels) > 8 else ""
            out.append(f"- {f}: {', '.join(labels[:8])}{more}")

    if missing:
        out.append(f"\n### Not in the graph ({len(missing)}): new files, tests, or outside the indexed code")
        out.extend(f"- {p}" for p in missing[:LIST_MAX])
    if not ch and not missing:
        out.append("\n(no changed lines found in the diff)")
    out.append("\nTrust: symbols come from the graph's start lines; edges marked INFERRED by the graph "
               "(http_request, eloquent, doc mentions) are matched by name.")
    return "\n".join(out)


def _orient(e: dict, a: dict, b: dict) -> tuple[str, str]:
    """(dependent, dependency) of an edge. The graph is undirected, so `source`/`target` of the JSON
    can come either way round; the edge's own `source_file` is where it was written, which is the
    dependent's file (the caller, the importer). An http_request edge is ours (link_http.py) and
    always points frontend -> controller."""
    if e.get("relation") == "http_request":
        return a["id"], b["id"]
    where = e.get("source_file")
    if where and b.get("source_file") == where and a.get("source_file") != where:
        return b["id"], a["id"]
    return a["id"], b["id"]


def _repo_of(changed: dict, nodes: dict) -> str:
    repos = [nodes[i].get("repo", "") for i in changed]
    return max(set(repos), key=repos.count) if repos else ""
