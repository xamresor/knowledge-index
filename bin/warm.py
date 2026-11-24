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


class Overflow(Exception):
    """A child wrote more than its byte limit; it was stopped."""


def run_capped(argv: list[str], max_bytes: int, timeout: float, cwd: str | None = None,
               input_text: str | None = None, env: dict | None = None) -> tuple[int, str, str, bool]:
    """Run a process and read its output as it comes, at most `max_bytes` of stdout (and 64 KiB of
    stderr). Past the limit the process is killed: a producer cannot fill the memory of the server
    before the text is cut. -> (exit code, stdout, stderr, stdout was cut). Raises
    subprocess.TimeoutExpired after `timeout` seconds (the process is killed)."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None
                            else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err, cut = bytearray(), bytearray(), [False]

    def read_out() -> None:
        assert proc.stdout
        while chunk := proc.stdout.read1(65536):
            room = max_bytes - len(out)
            out.extend(chunk[:room])
            if len(chunk) > room:
                cut[0] = True
                proc.kill()
                return

    def read_err() -> None:
        assert proc.stderr
        while chunk := proc.stderr.read1(65536):
            err.extend(chunk[:max(0, 65536 - len(err))])   # keep draining, keep the first 64 KiB

    readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in readers:
        t.start()
    try:
        if input_text is not None:
            assert proc.stdin
            try:
                proc.stdin.write(input_text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for t in readers:
            t.join(timeout=5)
        for f in (proc.stdout, proc.stderr):
            if f:
                f.close()
    return (code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), cut[0])


class _Hung(Exception):
    """The child did not answer within its timeout."""


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
        """Send one message, return the first reply `want` accepts; None on any failure.

        A dead process gets one restart. A hung one (no answer in `timeout`) does not: the same
        request would hang again. A caller that waits longer than `timeout` for another request
        gets None too, and runs its own process instead."""
        if not self.lock.acquire(timeout=self.timeout):
            return None
        try:
            for attempt in (1, 2):
                try:
                    if not self._alive():
                        self._start()
                        self.on_start()
                    proc = self.proc
                    assert proc and proc.stdin and proc.stdout
                    proc.stdin.write(json.dumps(message) + "\n")
                    proc.stdin.flush()
                    return self._read(want, proc)
                except _Hung:
                    self.kill()
                    return None
                except (OSError, ValueError, AssertionError, EOFError):
                    self.kill()
                    if attempt == 2:
                        return None
            return None
        finally:
            self.lock.release()

    def _read(self, want, proc: subprocess.Popen | None = None) -> dict:
        proc = proc or self.proc
        assert proc and proc.stdout
        stdout = proc.stdout      # a local reference: the timer sets self.proc to None
        hung = threading.Event()

        def stop() -> None:       # a hung child is killed, the read ends
            hung.set()
            self.kill()
        timer = threading.Timer(self.timeout, stop)
        timer.start()
        try:
            while True:
                try:
                    line = stdout.readline()
                except (OSError, ValueError):
                    if hung.is_set():
                        raise _Hung() from None
                    raise
                if not line:
                    if hung.is_set():
                        raise _Hung()
                    raise EOFError("worker closed its output")
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


def _filtered(graph: str) -> bool:
    """A filtered copy of the main graph (bin/access.py writes them under FILTERED_DIR)."""
    import access
    base = os.path.abspath(access.FILTERED_DIR) + os.sep
    return os.path.abspath(graph).startswith(base)


class GraphWorker(_Proc):
    """graph_worker.py kept running. It also re-warms itself after a rebuild: the first call on a new
    graph.json parses it again and cuts each caller's view (2-9 s on a 23k-node graph, measured
    2026-11-20). A server that rebuilds after every commit (kb-refresh) paid that on the first call
    of every project set, many times a day. So the views used lately are cut again in the background
    as soon as a build has finished, before anyone asks."""

    RECENT_VIEWS = 8
    POLL = 5.0
    # request threads add views while the watcher reads them; there is one worker per process
    _recent_lock = threading.Lock()

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
        if _filtered(graph):
            return    # a filtered copy (the old per-file path): the watcher follows the main graph
        key = json.dumps(view or {}, sort_keys=True)
        with self._recent_lock:
            old = self.recent.pop(key, (view, False))
            self.recent[key] = (view, old[1] or routes)
            while len(self.recent) > self.RECENT_VIEWS:
                self.recent.popitem(last=False)
            if self.graph_path != graph:
                self.graph_path = graph
            if self._watcher is None:   # check and start under one lock: one watcher, ever
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
        with self._recent_lock:
            views = list(self.recent.values())
        for view, routes in views:
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
        _code, stdout, _err, cut = run_capped([py, os.path.join(HERE, "graph_worker.py")], ONCE_MAX_BYTES,
                                              timeout, input_text=json.dumps(message) + "\n", env=env)
        if cut:
            return f"(the answer is larger than {ONCE_MAX_BYTES // 1_000_000} MB: narrow the call)"
        return str(json.loads(stdout.strip().splitlines()[-1]).get("out", ""))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


ONCE_MAX_BYTES = 32_000_000   # one graph_once answer; the worker reads it all before it is cut


# --------------------------------------------------------------------------- docs
class QmdWorker(_Proc):
    def __init__(self, cwd: str | None = None):
        exe = shutil.which("qmd")
        super().__init__([exe, "mcp"] if exe else [], cwd=cwd, timeout=150.0)
        self._id = 0
        self._id_lock = threading.Lock()

    def _next_id(self) -> int:
        """A fresh JSON-RPC id; request threads ask at once, so the increment is under a lock."""
        with self._id_lock:
            self._id += 1
            return self._id

    def on_start(self) -> None:
        assert self.proc and self.proc.stdin
        mid = self._next_id()
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
        mid = self._next_id()
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


def hit_path(hit: dict) -> str:
    """The `qmd://collection/path` of a hit: the worker gives `collection/path`, the CLI the full URI."""
    f = str(hit.get("file", ""))
    return f if f.startswith("qmd://") else f"qmd://{f}"


def format_hits(hits: list[dict]) -> str:
    """qmd's hits in the CLI's text layout, which the rest of kb_core parses. Format only hits that
    are already authorized: the text is never filtered again. A snippet line that starts with
    `qmd://` is indented, so it never reads as the start of a hit."""
    blocks = []
    for h in hits:
        lines = (_strip_number(line) for line in str(h.get("snippet", "")).split("\n"))
        snippet = "\n".join(f" {line}" if line.startswith("qmd://") else line for line in lines)
        explain = _explain_line(h.get("explain"))
        title = " ".join(str(h.get("title", "")).split())
        blocks.append(f"{hit_path(h)}:{h.get('line', 1)} {h.get('docid', '')}\n"
                      f"Title: {title}\n"
                      f"Score:  {round(_num(h.get('score')) * 100)}%\n"
                      + (f"{explain}\n" if explain else "") + f"\n{snippet}\n")
    return "\n".join(blocks).strip() or "No results found."


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _explain_line(ex) -> str:
    """qmd's score trace (`--explain --format json`) on one line, like the CLI's `Explain:`."""
    if not isinstance(ex, dict):
        return ""

    def scores(key: str) -> str:
        vals = ex.get(key) or []
        return ", ".join(f"{_num(v):.4f}" for v in vals) if isinstance(vals, list) and vals else "none"
    rrf = ex.get("rrf") if isinstance(ex.get("rrf"), dict) else {}
    return (f"Explain: fts=[{scores('ftsScores')}] vec=[{scores('vectorScores')}] "
            f"rrf rank={rrf.get('rank', '?')} total={_num(rrf.get('totalScore')):.4f} "
            f"rerank={_num(ex.get('rerankScore')):.4f} blended={_num(ex.get('blendedScore')):.4f}")


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
    # `qmd mcp` picks its index once, at start: `<cwd>/.qmd` when it exists, else the global one.
    # A server that starts before the first build (the Docker order: `up`, then `kb build`) must not
    # start the worker yet, or the worker keeps the empty global index until a restart.
    if _qmd is None and cwd and not os.environ.get("INDEX_PATH") \
            and not os.path.isfile(os.path.join(cwd, ".qmd", "index.yml")):
        return None
    with _init:
        if _qmd is None:
            _qmd = QmdWorker(cwd)
    return _qmd
