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
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)(?:#[^)\s]*)?\)")   # `b.md#setup` links to b.md
DISTINCTIVE = re.compile(r"^(?:[A-Z][a-z0-9]+[A-Z]\w*|\w+\(\)|\w+::\w+)$")
MAX_MENTIONS_PER_DOC = 12
#: Bytes read from one document. A longer file is cut here: the head names what it is about.
MAX_DOC_BYTES = 2 * 1024 * 1024


def _is_distinctive(label: str) -> bool:
    """A label worth linking on a bare mention: specific enough not to match half the corpus."""
    return len(label) >= 5 and bool(DISTINCTIVE.match(label))


# The three label shapes _is_distinctive() admits are `CamelCase`, `name()` and `Class::member`.
def _words(text: str) -> set[str]:
    """Every standalone token of a doc that a distinctive label could equal.

    The result equals testing `(?<!\\w)label(?!\\w)` for every label: `A::b()` yields `A::b`, `b()`,
    `A` and `b`. Every such token starts where a run of word characters starts, so one linear pass
    over the runs is enough (a lookahead scan at every position was quadratic on one long word)."""
    def free(a: int, b: int) -> bool:
        return not (a and (text[a - 1].isalnum() or text[a - 1] == "_")) and \
            not (b < len(text) and (text[b].isalnum() or text[b] == "_"))
    out: set[str] = set()
    runs = [(m.start(), m.end()) for m in re.finditer(r"\w+", text)]
    for i, (a, b) in enumerate(runs):
        if free(a, b):
            out.add(text[a:b])
        if text.startswith("()", b) and free(a, b + 2):
            out.add(text[a:b + 2])
        if text.startswith("::", b) and i + 1 < len(runs) and runs[i + 1][0] == b + 2 \
                and free(a, runs[i + 1][1]):
            out.add(text[a:runs[i + 1][1]])
    return out


def _doc_files(root: str):
    """(path, relative path) of every markdown file under `root` that is safe to read: a regular
    file (not a device or a pipe) whose real path stays inside the docs root."""
    base = Path(root)
    if not base.is_dir():
        return
    real_base = base.resolve()
    for path in sorted(base.rglob("*.md")):
        if any(part in {"node_modules", ".git", "vendor"} for part in path.parts):
            continue
        try:
            real = path.resolve()
            real.relative_to(real_base)
        except (OSError, ValueError, RuntimeError):
            continue
        if not real.is_file():
            continue
        yield path, str(path.relative_to(base))


def _read_doc(path: Path) -> str | None:
    """The first MAX_DOC_BYTES of a document, or None when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_DOC_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None


def _doc_id(repo: str, rel: str) -> str:
    return graph_io.node_id(repo, rel)


def _mention(label: str) -> str:
    """How a doc writes the symbol: graphify labels a method `.name()`, a doc writes `name()`."""
    return label[1:] if label.startswith(".") and label.endswith("()") else label


def _symbol_index(graph: dict) -> dict[str, str]:
    """mention -> node id, for distinctive labels that are **unambiguous** in the graph."""
    seen: dict[str, str | None] = {}
    for node in graph_io.nodes(graph):
        label = _mention(node.get("label", ""))
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
        for path, rel in _doc_files(root):
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
        for path, rel in _doc_files(root):
            src = by_path.get((repo, rel))
            if not src:
                continue
            text = _read_doc(path)
            if text is None:
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
