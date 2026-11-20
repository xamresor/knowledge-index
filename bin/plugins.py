#!/usr/bin/env python3
"""External add-ons (plugins): tools that live outside this repository, under their own licence.

The core ships its own add-ons (bin/addons.py). A plugin is a folder with a `plugin.py`, installed
with `kb addon install <path to the folder>` (a link under `<data>/plugins/<name>`) and switched on
like any add-on. The core only loads it; what the plugin's code may be used for is its own licence's
business, which is why paid add-ons can live in a private repository next to this AGPL core.

A `plugin.py` defines:

  NAME = "co-change"                        # the add-on name (kb addon list/remove)
  DESCRIPTION = "…"                         # one line for `kb addon list`
  TOOLS = [{"name": …, "description": …, "inputSchema": {…}}]   # MCP tool schemas, as in kb_core
  def call(name, args, allowed, core) -> str     # answer one of its tools; `core` is kb_core
  def build(core) -> None                        # optional: run at the end of `kb update`

`allowed` is the caller's project set (None = everything): a plugin must answer only from those
projects, like every core tool. Paging, cursors, usage counting and access checks of arguments are the
core's, so a plugin returns plain text.
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

_loaded: dict[str, object] = {}


def folder() -> str:
    return os.path.join(str(paths.data_home()), "plugins")


def installed() -> dict[str, str]:
    """name -> folder of every plugin under <data>/plugins (links included)."""
    out = {}
    root = folder()
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if os.path.isfile(os.path.join(path, "plugin.py")):
                out[name] = path
    return out


def load(name: str, path: str):
    if name in _loaded:
        return _loaded[name]
    spec = importlib.util.spec_from_file_location(f"kb_plugin_{name.replace('-', '_')}",
                                                  os.path.join(path, "plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(path)
    _loaded[name] = mod
    return mod


def active(enabled: set[str]) -> list:
    """The loaded modules of the installed plugins that are switched on. A broken plugin is reported
    on stderr and skipped: it must never take the core down."""
    mods = []
    for name, path in installed().items():
        if name not in enabled:
            continue
        try:
            mods.append(load(name, path))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"kb: plugin {name} failed to load: {type(e).__name__}: {e}\n")
    return mods


def build_all() -> None:
    """`kb update` calls this after the graph is built: each active plugin's build(core)."""
    import addons
    import kb_core
    for mod in active(addons.enabled()):
        fn = getattr(mod, "build", None)
        if fn:
            try:
                fn(kb_core)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"kb: plugin {getattr(mod, 'NAME', '?')} build failed: {e}\n")


if __name__ == "__main__":  # bin/kb: plugins.py build
    if sys.argv[1:] == ["build"]:
        build_all()
