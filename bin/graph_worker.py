#!/usr/bin/env python3
"""A long-lived graphify: runs `graphify <query|path|explain|affected> …` with the graph kept in memory.

Why: every `graphify` call parses graph.json and builds a networkx graph before it answers. On a
23k-node graph that is ~3-4 s per call for a lookup that itself takes milliseconds. This worker runs
in graphify's own interpreter (bin/warm.py starts it), runs the real CLI code for each request, and
memoizes the expensive steps by the graph file's identity:

  * `Path.read_text` of a .json file  -> the same str object while the file is unchanged
  * `json.loads` of that str          -> the same parsed dict
  * `node_link_graph` of a dict       -> the same networkx graph (per directed/multigraph flavour)

**Views.** A request may name a view of the main graph instead of a filtered file: the projects a
caller may see (`allowed`, bin/access.py `filter_graph`) and/or the graph without domain anchors
(`routes`, `strip_domains`). The view is cut from the parsed main graph in memory — the node and edge
dicts are shared, only the lists are new — so a new project set costs a filter and one networkx
build, not a filtered file written by a subprocess and parsed again (3-5 s). The access rules are the
same: it is the same `filter_graph`.

Memory is bounded: at most KB_GRAPH_CACHE_FILES files and KB_GRAPH_VIEWS views are kept; the least
recently used goes first, together with the networkx graphs built from it.

The output is byte-for-byte what the CLI prints for the same graph — only the parse is skipped.

Protocol: one JSON object per line on stdin,
`{"argv": ["query", "…", "--graph", "/x.json"], "view": {"allowed": ["api"], "routes": true}}`
(`view` optional; `allowed` null = every project); one per line on stdout,
`{"out": "<stdout+stderr>", "code": <exit code>}`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

from networkx.readwrite import json_graph

from graphify.__main__ import _run_cli

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import access  # noqa: E402  (pure stdlib: filter_graph, strip_domains)

MAX_FILES = int(os.environ.get("KB_GRAPH_CACHE_FILES", "2"))
MAX_VIEWS = int(os.environ.get("KB_GRAPH_VIEWS", "8"))  # a project set takes 2: with and without domains

_texts: "OrderedDict[str, tuple[tuple[float, int], str]]" = OrderedDict()  # path -> (ident, text)
_parsed: dict[int, tuple[str, dict]] = {}                  # id(text) -> (text, parsed)
_views: "OrderedDict[tuple, tuple[dict, dict]]" = OrderedDict()  # key -> (base dict, view dict)
_graphs: dict[tuple, tuple[list, object]] = {}             # flavour key -> (nodes list, graph)
_view: dict | None = None                                  # the current request's view

_read_text = Path.read_text
_loads = json.loads
_node_link_graph = json_graph.node_link_graph


def _live_nodes() -> list:
    """Node lists a cached networkx graph may belong to: parsed files and views still kept."""
    return [p.get("nodes") for _, p in _parsed.values()] + [v.get("nodes") for _, v in _views.values()]


def _drop_dead_graphs() -> None:
    live = _live_nodes()
    for k in [k for k, (n, _) in _graphs.items() if not any(n is x for x in live)]:
        del _graphs[k]


def read_text(self: Path, *args, **kwargs):
    if self.suffix != ".json":
        return _read_text(self, *args, **kwargs)
    path = str(self.resolve())
    st = os.stat(path)
    ident = (st.st_mtime, st.st_size)
    hit = _texts.get(path)
    if hit and hit[0] == ident:
        _texts.move_to_end(path)
        return hit[1]
    text = _read_text(self, *args, **kwargs)
    if hit:  # the file changed: forget what was built from the old text
        _forget(path)
    while len(_texts) >= MAX_FILES:
        _forget(next(iter(_texts)))
    _texts[path] = (ident, text)
    return text


def _forget(path: str) -> None:
    _, text = _texts.pop(path)
    parsed = _parsed.pop(id(text), None)
    if parsed is not None:
        for k in [k for k, (base, _) in _views.items() if base is parsed[1]]:
            del _views[k]
    _drop_dead_graphs()


def _view_of(data: dict) -> dict:
    """The current request's view of a parsed graph file (cached)."""
    allowed = _view.get("allowed")
    routes = bool(_view.get("routes"))
    if allowed is None and not routes:
        return data
    key = (id(data), tuple(sorted(allowed)) if allowed is not None else None, routes)
    hit = _views.get(key)
    if hit and hit[0] is data:
        _views.move_to_end(key)
        return hit[1]
    view = data
    if allowed is not None:
        view = access.filter_graph(view, set(allowed))
    if routes:
        view = access.strip_domains(view)
    while len(_views) >= MAX_VIEWS:
        _views.popitem(last=False)
    _views[key] = (data, view)
    _drop_dead_graphs()
    return view


def loads(s, *args, **kwargs):
    if isinstance(s, str) and not args and not kwargs:
        hit = _parsed.get(id(s))
        if hit and hit[0] is s:
            data = hit[1]
        else:
            data = _loads(s)
            if not any(t is s for _, t in _texts.values()):
                return data
            _parsed[id(s)] = (s, data)
        return _view_of(data) if _view else data
    return _loads(s, *args, **kwargs)


def node_link_graph(data, *args, **kwargs):
    nodes = data.get("nodes") if isinstance(data, dict) else None
    links = (data.get("links") or data.get("edges") or []) if isinstance(data, dict) else []
    if nodes is None or not any(nodes is x for x in _live_nodes()):
        return _node_link_graph(data, *args, **kwargs)
    key = (id(nodes), data.get("directed"), data.get("multigraph"),
           bool(links) and "_src" in links[0], tuple(sorted(kwargs.items())))
    hit = _graphs.get(key)
    if hit and hit[0] is nodes:
        return hit[1]
    # Each networkx graph gets its own `graph` attribute dict. node_link_graph keeps the one from the
    # data, and views share it with the main graph; graphify caches its trigram index and IDF there,
    # so one view's index answered another view's lookup (KeyError on a node it does not have).
    graph = _node_link_graph({**data, "graph": dict(data.get("graph") or {})}, *args, **kwargs)
    _graphs[key] = (nodes, graph)
    return graph


Path.read_text = read_text
json.loads = loads
json_graph.node_link_graph = node_link_graph

ALLOWED = {"query", "path", "explain", "affected"}


def handle(argv: list[str], view: dict | None = None) -> dict:
    global _view
    if not argv or argv[0] not in ALLOWED:
        return {"out": f"(graph worker: command not allowed: {argv[:1]})", "code": 2}
    if view is not None and not isinstance(view, dict):
        return {"out": "(graph worker: bad view)", "code": 2}
    buf = io.StringIO()
    code = 0
    old_argv = sys.argv
    sys.argv = ["graphify", *argv]
    _view = view or None
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
        _view = None
    return {"out": buf.getvalue(), "code": code}


def main() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr  # nothing but replies may reach the protocol stream
    for line in sys.stdin:
        try:
            req = _loads(line)
            reply = handle(list(req.get("argv") or []), req.get("view"))
        except (ValueError, AttributeError) as e:
            reply = {"out": f"(graph worker: bad request: {e})", "code": 2}
        out.write(json.dumps(reply) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
