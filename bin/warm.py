#!/usr/bin/env python3
"""Long-lived helper processes, so a tool call does not pay for a cold start.

Measured on a 23k-node graph and ~400 docs (2026-11-19):

  | call                      | one process per call | warm process |
  |---------------------------|----------------------|--------------|
  | graph explain / affected  | 1.2-5.6 s            | 0.003-0.02 s |
  | graph query (code search) | 6 s                  | 0.1 s        |
  | docs query (hybrid)       | 4.4-6.5 s            | 0.1 s        |

Two kinds:

* **GraphWorker** — bin/graph_worker.py in graphify's own interpreter, with graph.json kept parsed.
* **QmdWorker** — `qmd mcp` (stdio JSON-RPC), with the embedding model kept loaded.

Both are optional. Any failure (not installed, crashed, timed out) returns None and the caller runs
the old one-process-per-call path, so a broken worker never breaks a tool. A crashed worker is
started again on the next call. One request at a time per worker (a lock); a call takes milliseconds.

Only the servers turn this on (`enable()` in bin/kb-mcp and bin/kb-http). Tests and the build scripts
import kb_core without it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
ENABLED = False


def enable() -> None:
    global ENABLED
    ENABLED = os.environ.get("KB_WARM", "1") != "0"


class _Proc:
    """A child process that answers one JSON line with one JSON line."""

    def __init__(self, argv: list[str], cwd: str | None = None, timeout: float = 120.0):
        self.argv, self.cwd, self.timeout = argv, cwd, timeout
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        import addons
        env = {**os.environ, "KB_FAST_ROUTES": "1" if addons.on("fast-routes") else "0"}
        self.proc = subprocess.Popen(
            self.argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True, env=env)

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass

    def exchange(self, message: dict, want=lambda reply: True) -> dict | None:
        """Send one message, return the first reply `want` accepts; None on any failure."""
        with self.lock:
            for attempt in (1, 2):  # a dead process gets one restart
                try:
                    if not self._alive():
                        self._start()
                        self.on_start()
                    assert self.proc and self.proc.stdin and self.proc.stdout
                    self.proc.stdin.write(json.dumps(message) + "\n")
                    self.proc.stdin.flush()
                    return self._read(want)
                except (OSError, ValueError, AssertionError, TimeoutError):
                    self.kill()
                    if attempt == 2:
                        return None
        return None

    def _read(self, want) -> dict:
        assert self.proc and self.proc.stdout
        timer = threading.Timer(self.timeout, self.kill)  # a hung child is killed, the read ends
        timer.start()
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise TimeoutError("worker closed its output")
                reply = json.loads(line)
                if want(reply):
                    return reply
        finally:
            timer.cancel()

    def on_start(self) -> None:
        """Handshake after a (re)start; nothing by default."""


# --------------------------------------------------------------------------- graph
def graphify_python() -> str | None:
    """graphify's own interpreter, from its launcher's shebang (uv tool and pipx both write one)."""
    exe = shutil.which("graphify")
    if not exe:
        return None
    try:
        with open(os.path.realpath(exe), encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    py = first[2:].strip().split()[0]
    return py if os.path.exists(py) else None


class GraphWorker(_Proc):
    """graph_worker.py kept running. It also re-warms itself after a rebuild: the first call on a new
    graph.json parses it again and cuts each caller's view (2-9 s on a 23k-node graph, measured
    2026-11-20). A server that rebuilds after every commit (kb-refresh) paid that on the first call
    of every project set, many times a day. So the views used lately are cut again in the background
    as soon as a build has finished, before anyone asks."""

    RECENT_VIEWS = 8
    POLL = 5.0

    def __init__(self):
        py = graphify_python()
        super().__init__([py, os.path.join(HERE, "graph_worker.py")] if py else [], timeout=60.0)
        self.recent: "OrderedDict[str, tuple[dict | None, bool]]" = OrderedDict()
        self.graph_path: str | None = None
        self.rewarmed = 0
        self._watcher: threading.Thread | None = None

    def run(self, argv: list[str], view: dict | None = None, _remember: bool = True) -> str | None:
        """graphify's output for `argv`; `view` = {"allowed": [...] | None, "routes": bool} cuts the
        main graph in memory (see graph_worker.py). None when the worker cannot answer."""
        if not self.argv:
            return None
        if _remember and "--graph" in argv:
            self._remember(argv[argv.index("--graph") + 1], view, argv[0] == "routes")
        message: dict = {"argv": argv}
        if view:
            message["view"] = view
        reply = self.exchange(message)
        if reply is None:
            return None
        return str(reply.get("out", ""))

    def _remember(self, graph: str, view: dict | None, routes: bool) -> None:
        key = json.dumps(view or {}, sort_keys=True)
        old = self.recent.pop(key, (view, False))
        self.recent[key] = (view, old[1] or routes)
        while len(self.recent) > self.RECENT_VIEWS:
            self.recent.popitem(last=False)
        if self.graph_path != graph:
            self.graph_path = graph
        if self._watcher is None:
            self._watcher = threading.Thread(target=self._watch, name="kb-rewarm", daemon=True)
            self._watcher.start()

    @staticmethod
    def build_done(graph: str) -> float | None:
        """The finished build's time: graph.json's mtime once it has stopped changing and the build's
        end mark (.kb-post-manifest, written last by `kb update`) is not older than it."""
        try:
            g = os.path.getmtime(graph)
        except OSError:
            return None
        mark = os.path.join(os.path.dirname(graph), ".kb-post-manifest")
        try:
            if os.path.getmtime(mark) + 1 < g:
                return None        # graph.json is newer than the end mark: a build is still running
        except OSError:
            pass                   # an older bin/kb without the mark: fall back to "stopped changing"
        return g

    def rewarm(self) -> int:
        """Cut every recent view of the current graph again; returns how many."""
        graph = self.graph_path
        if not graph:
            return 0
        n = 0
        for view, routes in list(self.recent.values()):
            self.run(["explain", "warmup", "--graph", graph], view, _remember=False)
            if routes:
                self.run(["routes", json.dumps({"from": "warmup", "to": "warmup", "k": 1}), "--graph", graph],
                         view, _remember=False)
            n += 1
        self.rewarmed += 1
        return n

    def _watch(self) -> None:
        seen = self.build_done(self.graph_path) if self.graph_path else None
        pending = None
        while True:
            time.sleep(self.POLL)
            done = self.build_done(self.graph_path) if self.graph_path else None
            if done is None or done == seen:
                pending = None
                continue
            if pending != done:        # changed: wait one more poll to be sure it has settled
                pending = done
                continue
            seen, pending = done, None
            try:
                self.rewarm()
            except Exception:  # noqa: BLE001 — warming is a favour; a failure must not kill the thread
                pass


def graph_once(argv: list[str], view: dict | None = None, timeout: float = 120.0) -> str | None:
    """One request to a fresh graph worker that exits after it: the same answers without the warm
    process (KB_WARM=0, tests, scripts). None when graphify's interpreter is not found."""
    py = graphify_python()
    if not py:
        return None
    message: dict = {"argv": argv}
    if view:
        message["view"] = view
    import addons
    env = {**os.environ, "KB_FAST_ROUTES": "1" if addons.on("fast-routes") else "0"}
    try:
        p = subprocess.run([py, os.path.join(HERE, "graph_worker.py")], input=json.dumps(message) + "\n",
                           capture_output=True, text=True, timeout=timeout, env=env)
        return str(json.loads(p.stdout.strip().splitlines()[-1]).get("out", ""))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- docs
class QmdWorker(_Proc):
    def __init__(self, cwd: str | None = None):
        exe = shutil.which("qmd")
        super().__init__([exe, "mcp"] if exe else [], cwd=cwd, timeout=150.0)
        self._id = 0

    def on_start(self) -> None:
        assert self.proc and self.proc.stdin
        self._id += 1
        mid = self._id
        self.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "kb", "version": "1"}}}) + "\n")
        self.proc.stdin.flush()
        self._read(lambda r: r.get("id") == mid)
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                          "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def query(self, searches: list[dict], collections: list[str], limit: int | None,
              min_score: float | None, rerank: bool) -> list[dict] | None:
        """qmd's own hits (`structuredContent.results`), or None when the worker cannot answer."""
        if not self.argv:
            return None
        self._id += 1
        mid = self._id
        args: dict = {"searches": searches, "rerank": rerank}
        if collections:
            args["collections"] = collections
        if limit:
            args["limit"] = limit
        if min_score is not None:
            args["minScore"] = min_score
        reply = self.exchange({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                               "params": {"name": "query", "arguments": args}},
                              want=lambda r: r.get("id") == mid)
        if reply is None or "error" in reply:
            return None
        result = reply.get("result") or {}
        if result.get("isError"):
            return None
        return list((result.get("structuredContent") or {}).get("results") or [])


def format_hits(hits: list[dict]) -> str:
    """qmd's hits in the CLI's text layout, which the rest of kb_core already parses."""
    blocks = []
    for h in hits:
        snippet = "\n".join(_strip_number(line) for line in str(h.get("snippet", "")).split("\n"))
        blocks.append(f"qmd://{h.get('file', '')}:{h.get('line', 1)} {h.get('docid', '')}\n"
                      f"Title: {h.get('title', '')}\n"
                      f"Score:  {round(float(h.get('score') or 0) * 100)}%\n\n{snippet}\n")
    return "\n".join(blocks).strip() or "No results found."


def _strip_number(line: str) -> str:
    """The MCP snippet numbers its lines (`46: text`); the CLI does not."""
    head, sep, rest = line.partition(": ")
    return rest if sep and head.isdigit() else line


_graph: GraphWorker | None = None
_qmd: QmdWorker | None = None
_init = threading.Lock()


def graph() -> GraphWorker | None:
    global _graph
    if not ENABLED:
        return None
    with _init:
        if _graph is None:
            _graph = GraphWorker()
    return _graph


def qmd(cwd: str | None = None) -> QmdWorker | None:
    global _qmd
    if not ENABLED:
        return None
    with _init:
        if _qmd is None:
            _qmd = QmdWorker(cwd)
    return _qmd
