#!/usr/bin/env python3
"""Add-on `scip`: precise `calls` edges from a SCIP index of a project.

SCIP (https://github.com/sourcegraph/scip) is a code index that a type-aware indexer writes:
`scip-php` for PHP, `scip-typescript` for TypeScript. It knows the type of every receiver, so it
sees a call through a typed property or an interface that the AST graph misses. A spike on one
Laravel API (2026-11-20) found 125 cross-file method calls with `scip-php` and 40 in the AST graph.

What this step does, for each project, when the add-on is on:
  1. Get an `index.scip`: reuse the last run's index when the staged code did not change
     (`<data>/scip/<project>.scip.key`), else run the indexer on the staged copy under
     `repos/<project>` (PHP only, `KB_SCIP_PHP` or `scip-php` on the PATH), else take a ready index
     (`<data>/scip/<project>.scip` or `index.scip` in the project root), else skip the project.
  2. Decode the protobuf with the standard library (only the fields used here).
  3. Map each method symbol to the graph's method node in the same file (by name, then line).
  4. A reference to a method from inside another method becomes a `calls` edge, EXTRACTED
     (the indexer resolved it), `_origin: kb-scip`. Only edges between nodes the graph has; a
     symbol that does not map is skipped, and a pair that already has an edge gets no second one.

It runs before bin/link_php_calls.py, so the INFERRED typed-call linker does not repeat these edges.

Usage: link_scip.py <graph.json> <repos_dir> [<project>=<project root> ...]
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph  # noqa: E402

ADDON = "scip"
DEFINITION = 0x1                 # SymbolRole.Definition
METHOD_RE = re.compile(r"(`[^`]+`|[^#/.`()\s]+)\([^)]*\)\.$")   # descriptor of a method: `name(+1).`


# --- protobuf (wire format) -------------------------------------------------------------------
def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def fields(buf: bytes):
    """Yield (field number, wire type, value) of one message. Value: int (varint) or bytes."""
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        num, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _varint(buf, pos)
        elif wt == 2:
            size, pos = _varint(buf, pos)
            if pos + size > end:
                raise ValueError("truncated field")
            val, pos = buf[pos:pos + size], pos + size
        elif wt == 1:
            val, pos = buf[pos:pos + 8], pos + 8
        elif wt == 5:
            val, pos = buf[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield num, wt, val


def _ints(wt: int, val) -> list[int]:
    """A repeated int32: packed (one length-delimited field) or one varint per field."""
    if wt == 0:
        return [val]
    out, pos = [], 0
    while pos < len(val):
        v, pos = _varint(val, pos)
        out.append(v)
    return out


def _occurrence(buf: bytes) -> dict:
    occ = {"range": [], "symbol": "", "roles": 0, "enclosing": []}
    for num, wt, val in fields(buf):
        if num == 1:
            occ["range"] += _ints(wt, val)
        elif num == 2 and wt == 2:
            occ["symbol"] = val.decode("utf-8", "replace")
        elif num == 3 and wt == 0:
            occ["roles"] = val
        elif num == 7:
            occ["enclosing"] += _ints(wt, val)
    return occ


def _document(buf: bytes) -> dict:
    doc = {"path": "", "occurrences": [], "symbols": []}
    for num, wt, val in fields(buf):
        if num == 1 and wt == 2:
            doc["path"] = val.decode("utf-8", "replace")
        elif num == 2 and wt == 2:
            doc["occurrences"].append(_occurrence(val))
        elif num == 3 and wt == 2:          # SymbolInformation: field 1 is the symbol
            for n2, w2, v2 in fields(val):
                if n2 == 1 and w2 == 2:
                    doc["symbols"].append(v2.decode("utf-8", "replace"))
    return doc


def decode(buf: bytes) -> list[dict]:
    """The documents of a SCIP `Index` (field 2): path, occurrences and the symbols defined there."""
    return [_document(val) for num, wt, val in fields(buf) if num == 2 and wt == 2]


# --- mapping ----------------------------------------------------------------------------------
def method_name(symbol: str) -> str | None:
    """`… App/Escrow#charge().` -> `charge`; None for anything that is not a method or function."""
    if not symbol or symbol.startswith("local "):
        return None
    m = METHOD_RE.search(symbol)
    return m.group(1).strip("`") if m else None


def _line(rng: list[int]) -> int:
    return rng[0] if rng else -1


def _end_line(rng: list[int]) -> int | None:
    if len(rng) == 4:
        return rng[2]
    if len(rng) == 3:
        return rng[0]
    return None


def link(g: dict, repo: str, docs: list[dict]) -> int:
    """Add the `calls` edges of one project's SCIP documents to the graph. Returns the count added."""
    files: dict[str, list[tuple[str, int, str]]] = {}      # graph file -> [(name, line, node id)]
    for n in graph.nodes(g):
        if (n.get("repo") or graph.repo_of(n["id"])) != repo:
            continue
        label, f = str(n.get("label") or ""), n.get("source_file") or ""
        if not f or not label.endswith("()"):
            continue
        loc = str(n.get("source_location") or "")
        line = int(loc[1:]) if loc[1:].isdigit() else -1
        files.setdefault(f, []).append((label.strip(".").removesuffix("()").lower(), line, n["id"]))

    def graph_file(path: str) -> str | None:
        """The graph's file for a SCIP path. The index may be rooted above or below the staged copy."""
        if path in files:
            return path
        parts = path.split("/")
        for i in range(1, len(parts)):          # the index is rooted higher: drop leading folders
            if "/".join(parts[i:]) in files:
                return "/".join(parts[i:])
        hits = [f for f in files if f.endswith("/" + path)]     # rooted lower: a unique suffix
        return hits[0] if len(hits) == 1 else None

    # symbol -> graph node, from the definitions; per document the method definitions with their span
    sym_node: dict[str, str] = {}
    spans: list[tuple[dict, str | None, list[tuple[int, int | None, str]]]] = []
    for doc in docs:
        f = graph_file(doc["path"])
        defs = []
        for occ in doc["occurrences"]:
            if not occ["roles"] & DEFINITION:
                continue
            name = method_name(occ["symbol"])
            if not name:
                continue
            line = _line(occ["range"])
            enc = occ["enclosing"]
            defs.append((_line(enc) if enc else line, _end_line(enc) if enc else None, occ["symbol"]))
            if f is None or occ["symbol"] in sym_node:
                continue
            cands = [(abs(ln - (line + 1)), nid) for nm, ln, nid in files.get(f, []) if nm == name.lower()]
            if cands:
                sym_node[occ["symbol"]] = min(cands)[1]
        defs.sort()
        spans.append((doc, f, defs))

    def caller(defs: list[tuple[int, int | None, str]], line: int) -> str | None:
        inside = [d for d in defs if d[1] is not None and d[0] <= line <= d[1]]
        if inside:
            return max(inside)[2]                # the innermost span
        before = [d for d in defs if d[1] is None and d[0] <= line]
        return before[-1][2] if before else None

    existing = {frozenset((e.get("source"), e.get("target"))) for e in graph.links(g)}
    added = 0
    for doc, f, defs in spans:
        if f is None:
            continue
        for occ in doc["occurrences"]:
            if occ["roles"] & DEFINITION or occ["symbol"] not in sym_node:
                continue
            line = _line(occ["range"])
            src_sym = caller(defs, line)
            src, tgt = sym_node.get(src_sym or ""), sym_node[occ["symbol"]]
            if not src or src == tgt or frozenset((src, tgt)) in existing:
                continue
            existing.add(frozenset((src, tgt)))
            graph.links(g).append({"relation": "calls", "confidence": graph.Confidence.EXTRACTED,
                                   "confidence_score": 1.0, "weight": 1.0, "_origin": "kb-scip",
                                   "source": src, "target": tgt, "source_file": f,
                                   "source_location": f"L{line + 1}"})
            added += 1
    return added


# --- getting an index -------------------------------------------------------------------------
def _php_indexer() -> list[str] | None:
    cmd = os.environ.get("KB_SCIP_PHP", "").strip()
    if cmd:
        return shlex.split(cmd)
    found = shutil.which("scip-php")
    return [found] if found else None


def autoload_paths(composer_json: str) -> tuple[list[str], list[str]]:
    """The folders and the files that the `autoload` and `autoload-dev` sections of composer.json name."""
    try:
        with open(composer_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return [], []
    dirs, files = [], []
    for section in ("autoload", "autoload-dev"):
        a = data.get(section) or {}
        for kind in ("psr-4", "psr-0"):
            for v in (a.get(kind) or {}).values():
                dirs += [v] if isinstance(v, str) else [p for p in v if isinstance(p, str)]
        dirs += [p for p in a.get("classmap") or [] if isinstance(p, str)]
        files += [p for p in a.get("files") or [] if isinstance(p, str)]
    return [d for d in dirs if d], [f for f in files if f]


def _placeholders(staged: str, composer_json: str) -> list[str]:
    """scip-php stops when an autoload path of composer.json is missing, and the code-only staging
    leaves out database/, tests/ and the like. Create each missing path empty: a folder, or a file
    with `<?php`. Returns what was created (deepest first), for removal after the run."""
    created: list[str] = []
    dirs, files = autoload_paths(composer_json)
    base = os.path.realpath(staged)
    for rel in dirs + files:
        path = os.path.realpath(os.path.join(staged, rel))
        if not path.startswith(base + os.sep) or os.path.lexists(path):
            continue
        parent = os.path.dirname(path) if rel in files else path
        todo = []
        while not os.path.exists(parent):
            todo.append(parent)
            parent = os.path.dirname(parent)
        for d in reversed(todo):
            os.mkdir(d)
            created.append(d)
        if rel in files:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("<?php\n")
            created.append(path)
    return created[::-1]


def run_php_indexer(staged: str, root: str, out: str) -> bool:
    """Run the PHP indexer in the staged copy. scip-php reads composer.json and vendor/, which the
    staging leaves out, so they are linked in from the project root for the run and removed after.
    Autoload paths that the staging leaves out get empty placeholders for the run."""
    cmd = _php_indexer()
    if not cmd or not os.path.isfile(os.path.join(root, "composer.json")):
        return False
    linked = []
    for name in ("composer.json", "composer.lock", "vendor"):
        src, dst = os.path.join(root, name), os.path.join(staged, name)
        if os.path.exists(src) and not os.path.lexists(dst):
            os.symlink(src, dst)
            linked.append(dst)
    linked += _placeholders(staged, os.path.join(root, "composer.json"))
    produced = os.path.join(staged, "index.scip")
    try:
        timeout = int(os.environ.get("KB_SCIP_TIMEOUT", "600"))
        p = subprocess.run(cmd, cwd=staged, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout, check=False)
        if p.returncode != 0 or not os.path.isfile(produced):
            tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no index.scip"]
            print(f"  scip: indexer failed in {staged}: {tail[0]}", file=sys.stderr)
            return False
        os.makedirs(os.path.dirname(out), exist_ok=True)
        os.replace(produced, out)
        return True
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        print(f"  scip: indexer failed in {staged}: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    finally:
        for dst in linked:
            try:
                if os.path.isdir(dst) and not os.path.islink(dst):
                    os.rmdir(dst)
                else:
                    os.unlink(dst)
            except OSError:
                pass
        if os.path.isfile(produced):
            os.unlink(produced)


def _stat_line(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size} {int(st.st_mtime)}"


def _index_key(staged: str) -> str | None:
    """What a fresh index depends on: the project's staged-code manifest (bin/kb writes it after a
    good graphify run) and the indexer command. None when there is no manifest or no indexer."""
    cmd = _php_indexer()
    try:
        with open(os.path.join(staged, "graphify-out", ".kb-manifest"), encoding="utf-8") as fh:
            manifest = fh.read().strip()
    except OSError:
        return None
    return f"{manifest} {' '.join(cmd)}" if manifest and cmd else None


def store_is_derived(store: str) -> bool:
    """True when the store file is the one this step wrote for the key in `<store>.key`.

    The key file has two lines: the index key and `<size> <mtime seconds>` of the store. bin/kb
    leaves a derived store out of its build signature: the staged code already covers it, and the
    file is rewritten in the same build. A store the user put there has no matching key file."""
    try:
        with open(store + ".key", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        return len(lines) == 2 and lines[1] == _stat_line(store)
    except OSError:
        return False


def find_index(name: str, root: str, repos_dir: str) -> tuple[str | None, str]:
    """The index file for a project and where it came from: the last run, a fresh run, the store or
    the root. A project whose staged code did not change since the last run is not indexed again."""
    store = os.path.join(os.path.dirname(os.path.abspath(repos_dir)), "scip", f"{name}.scip")
    staged = os.path.join(repos_dir, name)
    key = _index_key(staged) if os.path.isdir(staged) and root else None
    if key and store_is_derived(store):
        with open(store + ".key", encoding="utf-8") as fh:
            if fh.read().splitlines()[0] == key:
                return store, "cached"
    if os.path.isdir(staged) and root and run_php_indexer(staged, root, store):
        if key:
            with open(store + ".key", "w", encoding="utf-8") as fh:
                fh.write(f"{key}\n{_stat_line(store)}\n")
        elif os.path.exists(store + ".key"):
            os.unlink(store + ".key")
        return store, "indexed"
    if os.path.isfile(store):
        return store, "ready"
    if root and os.path.isfile(os.path.join(root, "index.scip")):
        return os.path.join(root, "index.scip"), "ready"
    return None, ""


def main(argv: list[str]) -> int:
    import addons
    if not addons.on(ADDON):
        return 0
    graph_path, repos_dir, specs = argv[0], argv[1], argv[2:]
    g = graph.load(graph_path)
    total, skipped, report = 0, [], []
    for spec in specs:
        name, _, root = spec.partition("=")
        path, how = find_index(name, root, repos_dir)
        if not path:
            skipped.append(name)
            continue
        try:
            with open(path, "rb") as fh:
                docs = decode(fh.read())
        except (OSError, ValueError) as e:
            print(f"  scip: {name}: cannot read {path}: {e}", file=sys.stderr)
            continue
        n = link(g, name, docs)
        total += n
        report.append(f"{name} {n} ({how})")
    if report:
        graph.save(g, graph_path)
        print(f"  SCIP calls: {total} edges — {', '.join(report)}")
    if skipped:
        print(f"  scip: no indexer and no index.scip for {', '.join(skipped)} — skipped (docs/tools.md, SCIP)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
