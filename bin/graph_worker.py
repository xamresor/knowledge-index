#!/usr/bin/env python3
"""A long-lived graphify: runs `graphify <query|path|explain|affected> …` with the graph kept in memory.

Why: every `graphify` call parses graph.json and builds a networkx graph before it answers. On a
23k-node graph that is ~3-4 s per call for a lookup that itself takes milliseconds. This worker runs
in graphify's own interpreter (bin/warm.py starts it), runs the real CLI code for each request, and
memoizes the three expensive steps by the graph file's identity:

  * `Path.read_text` of a .json file  -> the same str object while the file is unchanged
  * `json.loads` of that str          -> the same parsed dict
  * `node_link_graph` of that dict    -> the same networkx graph (per directed/multigraph flavour)

So the output is byte-for-byte what the CLI prints — only the parse is skipped. A changed file
(new mtime or size) is parsed again; old entries are dropped.

Protocol: one JSON object per line on stdin, `{"argv": ["query", "…", "--graph", "/x.json"]}`;
one per line on stdout, `{"out": "<stdout+stderr>", "code": <exit code>}`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

from networkx.readwrite import json_graph

from graphify.__main__ import _run_cli

_texts: dict[str, tuple[tuple[float, int], str]] = {}     # path -> ((mtime, size), text)
_parsed: dict[int, tuple[str, dict]] = {}                  # id(text) -> (text, parsed)
_graphs: dict[tuple, tuple[list, object]] = {}             # flavour key -> (nodes list, graph)

# Graph files kept at once. Each caller's project set has its own filtered copy, and one parsed
# 23k-node graph costs ~150 MB per flavour: without a cap a shared server would grow without end.
MAX_FILES = int(os.environ.get("KB_GRAPH_CACHE_FILES", "4"))

_read_text = Path.read_text
_loads = json.loads
_node_link_graph = json_graph.node_link_graph


def read_text(self: Path, *args, **kwargs):
    if self.suffix != ".json":
        return _read_text(self, *args, **kwargs)
    path = str(self.resolve())
    st = os.stat(path)
    ident = (st.st_mtime, st.st_size)
    hit = _texts.get(path)
    if hit and hit[0] == ident:
        _texts[path] = _texts.pop(path)  # most recently used goes last
        return hit[1]
    text = _read_text(self, *args, **kwargs)
    if hit:  # the file changed: forget what was built from the old text
        _forget(path)
    while len(_texts) >= MAX_FILES:  # least recently loaded first (dicts keep insertion order)
        _forget(next(iter(_texts)))
    _texts[path] = (ident, text)
    return text


def _forget(path: str) -> None:
    _, text = _texts.pop(path)
    parsed = _parsed.pop(id(text), None)
    if parsed is not None:
        nodes = parsed[1].get("nodes")
        for k in [k for k, (n, _) in _graphs.items() if n is nodes]:
            del _graphs[k]


def loads(s, *args, **kwargs):
    if isinstance(s, str) and not args and not kwargs:
        hit = _parsed.get(id(s))
        if hit and hit[0] is s:
            return hit[1]
        data = _loads(s)
        if any(t is s for _, t in _texts.values()):
            _parsed[id(s)] = (s, data)
        return data
    return _loads(s, *args, **kwargs)


def node_link_graph(data, *args, **kwargs):
    nodes = data.get("nodes") if isinstance(data, dict) else None
    links = data.get("links") or data.get("edges") or [] if isinstance(data, dict) else []
    cached = nodes is not None and any(nodes is p.get("nodes") for _, p in _parsed.values())
    if not cached:
        return _node_link_graph(data, *args, **kwargs)
    key = (id(nodes), data.get("directed"), data.get("multigraph"),
           bool(links) and "_src" in links[0], tuple(sorted(kwargs.items())))
    hit = _graphs.get(key)
    if hit and hit[0] is nodes:
        return hit[1]
    graph = _node_link_graph(data, *args, **kwargs)
    for k in [k for k, (n, _) in _graphs.items() if not any(n is p.get("nodes") for _, p in _parsed.values())]:
        del _graphs[k]  # built from a file version that is gone
    _graphs[key] = (nodes, graph)
    return graph


Path.read_text = read_text
json.loads = loads
json_graph.node_link_graph = node_link_graph

ALLOWED = {"query", "path", "explain", "affected"}


def handle(argv: list[str]) -> dict:
    if not argv or argv[0] not in ALLOWED:
        return {"out": f"(graph worker: command not allowed: {argv[:1]})", "code": 2}
    buf = io.StringIO()
    code = 0
    old_argv = sys.argv
    sys.argv = ["graphify", *argv]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            _run_cli()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:  # noqa: BLE001 — one bad request must not kill the worker
        buf.write(f"\n(graph worker error: {type(e).__name__}: {e})")
        code = 1
    finally:
        sys.argv = old_argv
    return {"out": buf.getvalue(), "code": code}


def main() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr  # nothing but replies may reach the protocol stream
    for line in sys.stdin:
        try:
            req = json.loads(line, strict=True)
            reply = handle(list(req.get("argv") or []))
        except ValueError as e:
            reply = {"out": f"(graph worker: bad request: {e})", "code": 2}
        out.write(json.dumps(reply) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
