"""Markdown as part of the graph — the first plugin for a format that is not code.

Until now documents lived only in the search index: you could *find* a document, but the graph could
not answer "which document describes this service" or "what does this note point at". This plugin adds
two things the AST cannot see:

    doc node                       one per markdown file under a project's docs root
    links_to   doc  -> doc         from `[[wikilink]]` and relative markdown links (EXTRACTED —
                                   the link is literally written in the file)
    documents  doc  -> symbol      the document mentions a symbol that exists in the graph (INFERRED —
                                   a name match is a heuristic, so it says so)

The `documents` edge is where noise would come from, so it is deliberately stingy:

* only **distinctive** labels are matched — at least 5 characters, and either CamelCase, or a method
  (`name()`), or containing a namespace separator. A doc mentioning "User" would otherwise attach
  itself to every `User` in every repo, which is worse than no edge at all.
* matching is whole-word, case-sensitive (a class name is written the way it is spelled).
* a label that is ambiguous in the graph (same label defined more than once) is **skipped**, following
  the same rule as `dedupe.py`: when in doubt, do not invent a fact.
* capped per document, because a doc that mentions forty symbols is an index, not a description.
"""
from __future__ import annotations

import re
from pathlib import Path

import graph as graph_io

from . import DOCS

NAME = "markdown"
EXTENSIONS = DOCS
COMMENT_PREFIXES = ("<!--",)
CONTRIBUTES = {"nodes": ["doc"], "relations": ["links_to", "documents"]}
PRIORITY = 80          # after the code plugins: it needs their symbols to point at

WIKILINK = re.compile(r"\[\[([^\]|#]+)")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)\)")
DISTINCTIVE = re.compile(r"^(?:[A-Z][a-z0-9]+[A-Z]\w*|\w+\(\)|\w+::\w+)$")
MAX_MENTIONS_PER_DOC = 12


def _is_distinctive(label: str) -> bool:
    """A label worth linking on a bare mention: specific enough not to match half the corpus."""
    return len(label) >= 5 and bool(DISTINCTIVE.match(label))


# The three label shapes _is_distinctive() admits are `CamelCase`, `name()` and `Class::member`.
def _words(text: str) -> set[str]:
    """Every standalone token of a doc that a distinctive label could equal.

    Each shape is searched on its own and with overlaps (`A::b()` yields `A::b`, `b()`, `A`, `b`),
    so the result equals testing `(?<!\\w)label(?!\\w)` for every label, one regex per label."""
    def free(a: int, b: int) -> bool:
        return not (a and (text[a - 1].isalnum() or text[a - 1] == "_")) and \
            not (b < len(text) and (text[b].isalnum() or text[b] == "_"))
    out: set[str] = set()
    for pattern in (r"(?=(\w+::\w+))", r"(?=(\w+\(\)))", r"(?=(\w+))"):
        for m in re.finditer(pattern, text):
            if free(m.start(1), m.end(1)):
                out.add(m.group(1))
    return out


def _doc_id(repo: str, rel: str) -> str:
    return graph_io.node_id(repo, rel)


def _symbol_index(graph: dict) -> dict[str, str]:
    """label -> node id, for distinctive labels that are **unambiguous** in the graph."""
    seen: dict[str, str | None] = {}
    for node in graph_io.nodes(graph):
        label = node.get("label", "")
        if not _is_distinctive(label):
            continue
        seen[label] = None if label in seen else node["id"]   # None marks "ambiguous"
    return {label: nid for label, nid in seen.items() if nid}


def enrich(graph: dict, roots: list[tuple[str, str]]) -> tuple[int, int]:
    """Add doc nodes and their edges. `roots` is [(project_name, docs_dir), …]. Mutates `graph`."""
    symbols = _symbol_index(graph)
    existing = {n["id"] for n in graph_io.nodes(graph)}
    # Two indexes, deliberately: paths resolve a relative markdown link, stems resolve a [[wikilink]].
    # (An earlier version keyed both off one composite string and took `Path("api::b.md").stem`,
    #  which is `api::b` — so wikilinks silently matched nothing.)
    by_path: dict[tuple[str, str], str] = {}      # (repo, relative path) -> node id
    by_stem: dict[tuple[str, str], str] = {}      # (repo, file stem)     -> node id
    new_nodes: list[dict] = []

    for repo, root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if any(part in {"node_modules", ".git", "vendor"} for part in path.parts):
                continue
            rel = str(path.relative_to(base))
            nid = _doc_id(repo, rel)
            by_path[(repo, rel)] = nid
            by_stem.setdefault((repo, path.stem), nid)
            if nid in existing:
                continue
            new_nodes.append({
                "id": nid, "label": path.name, "type": "doc", "repo": repo,
                "source_file": rel, "domain": "docs",
            })
            existing.add(nid)

    edges: list[dict] = []
    for repo, root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if any(part in {"node_modules", ".git", "vendor"} for part in path.parts):
                continue
            rel = str(path.relative_to(base))
            src = by_path.get((repo, rel))
            if not src:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            # doc -> doc: the link is written in the file, so this is EXTRACTED
            targets: set[str] = set()
            for name in WIKILINK.findall(text):
                stem = name.split("/")[-1].strip()
                if nid := by_stem.get((repo, stem)):
                    targets.add(nid)
            for href in MD_LINK.findall(text):
                resolved = (path.parent / href).resolve()
                try:
                    rel_target = str(resolved.relative_to(base.resolve()))
                except ValueError:
                    continue
                if nid := by_path.get((repo, rel_target)):
                    targets.add(nid)
            for tgt in sorted(targets - {src}):
                edges.append({"source": src, "target": tgt, "relation": "links_to",
                              "confidence": graph_io.Confidence.EXTRACTED, "weight": 1.0})

            # doc -> symbol: a name match is a guess, hence INFERRED and capped
            # One pass over the text, then set lookups: a regex per (doc, symbol) pair made this the
            # slowest build step by far (minutes on a ~12k-symbol graph). Same rule as before: the
            # label stands alone, not inside a longer word.
            mentions = 0
            words = _words(text)
            for label, nid in symbols.items():
                if mentions >= MAX_MENTIONS_PER_DOC:
                    break
                if label in words:
                    edges.append({"source": src, "target": nid, "relation": "documents",
                                  "confidence": graph_io.Confidence.INFERRED,
                                  "confidence_score": 0.6, "weight": 1.0})
                    mentions += 1

    graph_io.nodes(graph).extend(new_nodes)
    graph_io.links(graph).extend(edges)
    return len(new_nodes), len(edges)
