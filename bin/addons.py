#!/usr/bin/env python3
"""Optional add-ons, named by what they do. The core works without any of them.

  code-mode       the `run_code` tool: agent programs in a sandbox (QuickJS in WebAssembly under Node)
  tool-multicall  the `batch` tool: several tool calls in one request
  fast-routes     `graph_routes` on rustworkx (Rust core) instead of networkx, 2-3x faster
  metrics         `GET /metrics` in the Prometheus format, for Prometheus, Grafana or PMM
  audit           the Audit tab of the admin pages and its sign-in / admin-action log
  scip            `kb update` adds EXTRACTED `calls` edges from a SCIP index (scip-php), bin/link_scip.py
  datadict        the meaning of each table from a hand-written file per project (bin/datadict.py)

Which are on: `<data>/addons.json` (written by `kb addon install|remove` and the Settings page of /admin),
or KB_ADDONS (comma-separated, overrides the file; `KB_ADDONS=none` switches all off). An add-on that is
off adds no tool, no page and no route, and `kb addon install` is the only thing that downloads its pieces.

The admin pages are part of the core since 2026-11-23: an old addons.json that still names `admin` works,
the name is ignored (enabled() keeps only known names). set_enabled keeps every name of the file, also the
name of a plugin whose folder is missing for a moment.
"""
from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

ADDONS = {
    "code-mode": "run_code: agent programs that call the tools, in a sandbox (needs Node 22+)",
    "tool-multicall": "batch: several tool calls in one request",
    "fast-routes": "graph_routes on rustworkx, 2-3x faster (needs graphify via uv or pipx)",
    "metrics": "GET /metrics for Prometheus, Grafana or PMM",
    "audit": "/admin/audit: sign-ins, admin actions and every tool call with user and argument",
    "questions": "/admin/questions: the questions people asked, a label with the right answer for each, "
                 "and the labelled ones as a golden set",
    "scip": "kb update adds precise calls edges from a SCIP index (needs scip-php or a ready index.scip)",
    "datadict": "data dictionary: the meaning of each table (term, grain, summable columns) from "
                "datadict/<project>.toml, in search, graph_explain and problems",
}
TOOLS = {"run_code": "code-mode", "batch": "tool-multicall"}  # tool -> the add-on that brings it
# A switch of these takes effect after a restart of the server: the warm graph worker gets fast-routes in
# its environment when it starts. Plugins are loaded once per process (kb_core, the access plugin of kb-http).
RESTART = {"fast-routes"}
CORE = {"admin"}   # former add-ons that are part of the core now

_cache: dict = {}      # {"mtime": ..., "names": frozenset}: replaced in one assignment, never changed in place
_cache_lock = threading.Lock()
_write_lock = threading.Lock()   # set_enabled: read, change and write the file as one step


def known() -> dict[str, str]:
    """Every add-on name with its description: the core's own and the installed plugins."""
    import plugins
    out = dict(ADDONS)
    for name, path in plugins.installed().items():
        out.setdefault(name, f"plugin in {path}")
    return out


def state_file() -> str:
    return os.path.join(str(paths.data_home()), "addons.json")


def _from_file() -> frozenset:
    """Every name in addons.json, known or not. A plugin whose folder is missing for a moment (a deploy
    by copy) stays in the file: only `enabled()` drops the names that are not installed."""
    global _cache
    path = state_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return frozenset()
    cache = _cache   # one read of the reference: another thread may replace it
    if cache.get("mtime") == mtime:
        return cache["names"]
    with _cache_lock:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            names = frozenset(str(n) for n in data) if isinstance(data, list) else frozenset()
        except (OSError, ValueError):
            names = frozenset()
        _cache = {"mtime": mtime, "names": names}
    return names


def enabled() -> set[str]:
    env = os.environ.get("KB_ADDONS")
    if env is not None:
        names = {n.strip() for n in env.split(",") if n.strip()}
        return set() if names == {"none"} else names & set(known())
    return set(_from_file()) & set(known())


def on(name: str) -> bool:
    return name in enabled()


def tool_on(tool: str) -> bool:
    need = TOOLS.get(tool)
    return need is None or on(need)


def needs_restart(name: str) -> bool:
    """True when a switch of this add-on takes effect only after a restart of the server."""
    import plugins
    return name in RESTART or name in plugins.installed()


def _sandbox_dirs() -> list[str]:
    """Where run_code's sandbox may be installed (kb_core.SANDBOX picks one of these)."""
    return [os.path.join(str(paths.data_home()), "sandbox"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")]


def missing(name: str) -> str | None:
    """What a switched-on add-on still lacks, when a cheap check can tell; None = nothing known.
    fast-routes is not checked: rustworkx lives in graphify's interpreter, and asking it costs a process."""
    import shutil
    if name == "code-mode":
        if os.environ.get("KB_SANDBOX_SOCKET"):
            return None
        if not shutil.which("node"):
            return "no Node on the PATH"
        if not any(os.path.isdir(os.path.join(d, "node_modules", "quickjs-emscripten")) for d in _sandbox_dirs()):
            return "the sandbox packages are not installed (kb addon install code-mode)"
    if name == "scip" and not os.environ.get("KB_SCIP_PHP", "").strip() and not shutil.which("scip-php"):
        return "no scip-php: only ready index.scip files are read"
    return None


def set_enabled(name: str, value: bool) -> set[str]:
    if name not in known() and value:
        raise SystemExit(f"unknown add-on: {name} (one of: {', '.join(known())})")
    global _cache
    with _write_lock:
        names = set(_from_file())   # the file as it is: names of plugins missing right now stay
        names = (names | {name}) if value else (names - {name})
        path = state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(names), f)
        os.replace(tmp, path)   # a reader (another server process) sees the old list or the new one
        with _cache_lock:
            _cache = {}
    return names


if __name__ == "__main__":  # bin/kb calls this: addons.py list | on <name> | off <name>
    cmd = sys.argv[1:] or ["list"]
    if cmd[0] == "list":
        names = enabled()
        for n, what in known().items():
            print(f"{n:<15} {'on ' if n in names else 'off'}  {what}")
    elif cmd[0] in ("on", "off") and len(cmd) == 2 and cmd[1] in CORE:
        print(f"{cmd[1]}: part of the core now, always available; nothing to switch")
    elif cmd[0] in ("on", "off") and len(cmd) == 2:
        import plugins
        if cmd[0] == "on" and plugins.is_access(cmd[1]):
            others = sorted(n for n in enabled() if n != cmd[1] and plugins.is_access(n))
            if others:
                print(f"note: {', '.join(others)} answers the sign-in already. One server has one access plugin: "
                      f"the first by name. Switch the other one off (kb addon off {others[0]}), then restart.")
        set_enabled(cmd[1], cmd[0] == "on")
        if plugins.is_access(cmd[1]):
            print(f"{cmd[1]} is an access plugin: restart kb-http for the change to take effect.")
    else:
        raise SystemExit("usage: addons.py list | on <name> | off <name>")
