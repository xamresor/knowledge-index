#!/usr/bin/env python3
"""Optional add-ons, named by what they do. The core works without any of them.

  code-mode       the `run_code` tool: agent programs in a sandbox (QuickJS in WebAssembly under Node)
  tool-multicall  the `batch` tool: several tool calls in one request
  fast-routes     `graph_routes` on rustworkx (Rust core) instead of networkx, 2-3x faster
  metrics         `GET /metrics` in the Prometheus format, for Prometheus, Grafana or PMM
  admin           the `/admin` pages (on the shared server also needs KB_ADMINS or admins.txt)

Which are on: `<data>/addons.json` (written by `kb addon install|remove`), or KB_ADDONS (comma-separated,
overrides the file; `KB_ADDONS=none` switches all off). An add-on that is off adds no tool, no page and
no route, and `kb addon install` is the only thing that downloads its pieces.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

ADDONS = {
    "code-mode": "run_code: agent programs that call the tools, in a sandbox (needs Node 22+)",
    "tool-multicall": "batch: several tool calls in one request",
    "fast-routes": "graph_routes on rustworkx, 2-3x faster (needs graphify via uv or pipx)",
    "metrics": "GET /metrics for Prometheus, Grafana or PMM",
    "admin": "/admin pages with status, charts, usage and sign-in clients",
}
TOOLS = {"run_code": "code-mode", "batch": "tool-multicall"}  # tool -> the add-on that brings it

_cache: dict = {}


def state_file() -> str:
    return os.path.join(str(paths.data_home()), "addons.json")


def enabled() -> set[str]:
    env = os.environ.get("KB_ADDONS")
    if env is not None:
        names = {n.strip() for n in env.split(",") if n.strip()}
        return set() if names == {"none"} else names & set(ADDONS)
    path = state_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return set()
    if _cache.get("mtime") != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _cache.update(mtime=mtime, names=set(json.load(f)) & set(ADDONS))
        except (OSError, ValueError):
            _cache.update(mtime=mtime, names=set())
    return set(_cache["names"])


def on(name: str) -> bool:
    return name in enabled()


def tool_on(tool: str) -> bool:
    need = TOOLS.get(tool)
    return need is None or on(need)


def set_enabled(name: str, value: bool) -> set[str]:
    if name not in ADDONS:
        raise SystemExit(f"unknown add-on: {name} (one of: {', '.join(ADDONS)})")
    names = enabled() if os.environ.get("KB_ADDONS") is None else set()
    names = (names | {name}) if value else (names - {name})
    path = state_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(names), f)
    _cache.clear()
    return names


if __name__ == "__main__":  # bin/kb calls this: addons.py list | on <name> | off <name>
    cmd = sys.argv[1:] or ["list"]
    if cmd[0] == "list":
        names = enabled()
        for n, what in ADDONS.items():
            print(f"{n:<15} {'on ' if n in names else 'off'}  {what}")
    elif cmd[0] in ("on", "off") and len(cmd) == 2:
        set_enabled(cmd[1], cmd[0] == "on")
    else:
        raise SystemExit("usage: addons.py list | on <name> | off <name>")
