#!/usr/bin/env python3
"""The graph artefact's contract, in one place: I/O, node ids, relation and confidence vocabularies.

Why this module exists
----------------------
Eight scripts in this directory read `graphify-out/graph.json`, transform it and write it back.
Before this module each of them re-derived the contract by hand: `g["nodes"]`, `g["links"]`,
`id.split("::")`, bare `"http_request"` / `"INFERRED"` string literals, and
`json.dump(g, open(path, "w"))`. Two consequences, both real:

* **a format change broke eight files silently** — nothing declared the shape;
* **writes were not atomic** — an exception or Ctrl-C mid-write left a truncated graph.js,
  and the pipeline has no recovery step.

So: load/save live here (save is atomic), the id format is parsed and built here, and the
vocabularies are constants. A typo in a relation name is now an AttributeError instead of a
silently wrong edge.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Iterable

# --- node ids -----------------------------------------------------------------------------------
# graphify ids look like `repo::relative/path.php::Symbol` (the symbol part is optional).
ID_SEP = "::"


class Relation:
    """Edge relation names. Emitted by the enrichers in this directory."""

    HTTP_REQUEST = "http_request"   # frontend call site -> controller that serves the route
    ELOQUENT = "eloquent"           # model <-> model relation declared in an Eloquent method
    FK = "fk"                       # table -> table foreign key
    SQL = "sql"                     # code -> table, from a raw SQL string
    EXPLAINS = "explains"           # rationale node -> the symbol it explains
    IN_DOMAIN = "in_domain"         # symbol -> domain anchor
    DEFINES_TABLE = "defines_table"  # migration/model -> db_table node
    LINKS_TO = "links_to"           # doc -> doc, from a wikilink or a relative markdown link
    DOCUMENTS = "documents"         # doc -> symbol it describes (name match: INFERRED, never EXTRACTED)


class Confidence:
    """How an edge was obtained. Keep regex-derived edges from posing as AST facts."""

    EXTRACTED = "EXTRACTED"    # parsed from an AST: the fact is in the syntax
    INFERRED = "INFERRED"      # matched by heuristic (file suffix, route table, naming)
    AMBIGUOUS = "AMBIGUOUS"    # a guess with known alternatives (e.g. fk from a filename)
    DERIVED = "DERIVED"        # computed from other edges, not observed directly


def node_id(repo: str, path: str, symbol: str | None = None) -> str:
    """Build a node id. The inverse of `parse_id`."""
    parts = [repo, path] + ([symbol] if symbol else [])
    return ID_SEP.join(parts)


def parse_id(nid: str) -> tuple[str, str, str | None]:
    """Split a node id into (repo, path, symbol|None). Tolerates ids without a symbol."""
    parts = nid.split(ID_SEP)
    if len(parts) == 1:
        return parts[0], "", None
    if len(parts) == 2:
        return parts[0], parts[1], None
    return parts[0], parts[1], ID_SEP.join(parts[2:])


def repo_of(nid: str) -> str:
    """The repo tag a node belongs to — the cheap question, asked everywhere."""
    return nid.split(ID_SEP, 1)[0]


# --- I/O ----------------------------------------------------------------------------------------
def read_json(path: str) -> Any:
    """Read any JSON file (routes tables, label maps) with the handle closed deterministically."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(obj: Any, path: str, **dump_kwargs: Any) -> None:
    """Write JSON **atomically**: a temp file in the same directory, then `os.replace`.

    `os.replace` is atomic on POSIX and on Windows, so a reader either sees the old complete file
    or the new complete file — never a half-written one. The temp file is removed on failure, so a
    crashed run leaves no debris next to the artefact.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, **dump_kwargs)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path: str) -> dict:
    """Load a graph and fail loudly if it is not one — a truncated file must not look empty."""
    g = read_json(path)
    if not isinstance(g, dict) or "nodes" not in g or "links" not in g:
        raise ValueError(f"{path}: not a graphify graph (expected dict with 'nodes' and 'links')")
    return g


def save(graph: dict, path: str) -> None:
    """Save a graph atomically. Same shape check as `load`, so a bug cannot truncate the artefact."""
    if not isinstance(graph, dict) or "nodes" not in graph or "links" not in graph:
        raise ValueError("refusing to save: not a graphify graph (missing 'nodes'/'links')")
    write_json(graph, path)


def nodes(graph: dict) -> list[dict]:
    return graph["nodes"]


def links(graph: dict) -> list[dict]:
    return graph["links"]


def ids(graph: dict) -> Iterable[str]:
    return (n["id"] for n in graph["nodes"])
