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
import stat
import tempfile
from typing import Any, Iterable

# --- node ids -----------------------------------------------------------------------------------
# A node id is `repo::slug`. graphify builds the slug from the file path and the symbol, lower-cased,
# with `/` and `.` turned into `_` (`shop-api::app_models_order_order`), so an id cannot be split
# back into a path: read `repo`, `source_file` and `label` from the node instead. Doc nodes
# (bin/lang/markdown.py) use `repo::relative/path.md`. Only the repo part is safe to parse.
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
    """Build the id of a node this project adds itself (a doc node): `repo::path[::symbol]`."""
    parts = [repo, path] + ([symbol] if symbol else [])
    return ID_SEP.join(parts)


def repo_of(nid: str) -> str:
    """The repo tag a node belongs to — the cheap question, asked everywhere."""
    return nid.split(ID_SEP, 1)[0]


def repo_file(path: str, repos_dir: str) -> tuple[str, str]:
    """A staged file `<repos_dir>/<repo>/<source_file>` -> (repo, source_file), the key that
    matches a node's `repo` and `source_file` exactly."""
    rel = os.path.relpath(path, repos_dir).replace(os.sep, "/")
    repo, _, sf = rel.partition("/")
    return repo, sf


def staged_root(path: str, repos_dir: str) -> str:
    """The staged project folder `<repos_dir>/<repo>` that holds `path`."""
    return os.path.join(repos_dir, repo_file(path, repos_dir)[0])


#: Bytes read from one source file. A longer file is cut here: no hand-written source is this long,
#: and a link to a device (/dev/zero) never ends.
MAX_SOURCE_BYTES = 4 * 1024 * 1024


def _opened_inside(fd: int, base: str) -> bool:
    """Is the open file `fd` inside `base`? Linux names the opened file in /proc; elsewhere only the
    path check of read_source holds."""
    try:
        opened = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return True
    return opened == base or opened.startswith(base + os.sep)


def read_source(path: str, root: str | None = None, limit: int = MAX_SOURCE_BYTES) -> str | None:
    """The text of a source file, or None when it is not safe to read: not a regular file (a device,
    a pipe), or, with `root`, a real path outside `root`. At most `limit` bytes are read."""
    try:
        real = os.path.realpath(path)
        if root is not None:
            base = os.path.realpath(root)
            if real != base and not real.startswith(base + os.sep):
                return None
        # O_NONBLOCK: opening a pipe must not wait for a writer; the fstat below refuses it.
        # O_NOFOLLOW: a link swapped in after the check above is refused, not followed.
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            if root is not None and not _opened_inside(fh.fileno(), base):
                return None   # a folder on the way became a link between the check and the open
            return fh.read(limit).decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None


def tag_repo(g: dict, repo: str) -> dict:
    """Give a one-project graph the shape `graphify merge-graphs` gives a merged one: every node gets
    `repo`, every id the prefix `<repo>::`, and the links follow the new ids."""
    def new(i):
        return i if isinstance(i, str) and i.startswith(repo + "::") else f"{repo}::{i}"
    for n in nodes(g):
        n["id"] = new(n["id"])
        n.setdefault("repo", repo)
    for e in links(g):
        e["source"], e["target"] = new(e["source"]), new(e["target"])
    return g


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


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == "tag-repo":   # graph.py tag-repo <in> <out> <repo>
        save(tag_repo(load(sys.argv[2]), sys.argv[4]), sys.argv[3])
    else:
        sys.exit("usage: graph.py tag-repo <in.json> <out.json> <repo>")
