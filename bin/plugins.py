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

An access plugin also defines both of these, and kb-http then answers the two webhooks of the access
protocol (docs/access-protocol.md) from it, at `/local/login` and `/local/permissions`. It also says
`ACCESS = True`: the Settings page reads that line without loading the plugin (without it, a plugin.py
that defines both functions itself counts, one that imports them does not):

  def login(body, core) -> (status, body) or (status, body, headers)   # body["ip"]: the client address
  def permissions(token, projects, core) -> (status, body)

It may bring pages to `/admin` as well (the plugin `contrib/users` does: users, rights, tokens):

  TABS = [("Users", "/users", "admin"), ("My tokens", "/me", "member")]   # label, path, who may open it
  TOKENS = True                              # people make tokens on its pages: the how-to says so
  def role(token, core) -> "admin" | "member" | None      # who may sign in to the pages with this token
  def page(req, core) -> (status, title, body) or ("redirect", "login")

  PUBLIC = ["/forgot", "/reset"]             # pages under /admin that need no session
  def public_page(req, core) -> (status, title, body)     # req: method, path, form, query, base, now, ip
  SIGN_IN_HINT = "your password"             # what the how-to tells a person to enter at the sign-in

The core checks the session, the csrf value of a form and the `who` of the tab before it calls `page`.
A public page has no session: the plugin owns its safety. A `"/forgot"` in PUBLIC puts a link on the
sign-in page.
`req` has `method`, `path` (under /admin), `form`, `base`, `csrf`, `user`, `admin`, `now` and three calls:
`forget()` drops the cached token verdicts, `drop_sessions(name)` ends the page sessions of a user,
`audit(action, detail)` writes the admin log. A "member" opens only the tabs marked `member`.

`allowed` is the caller's project set (None = everything): a plugin must answer only from those
projects, like every core tool. Paging, cursors, usage counting and access checks of arguments are the
core's, so a plugin returns plain text.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

_loaded: dict[str, object] = {}
_load_lock = threading.RLock()


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
    """Load a plugin once per process. The lock keeps concurrent first calls (request threads) from making
    two module objects, which would mean two copies of the plugin's state."""
    if name in _loaded:
        return _loaded[name]
    with _load_lock:
        if name in _loaded:
            return _loaded[name]
        return _load(name, path)


def _load(name: str, path: str):
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


_ACCESS_FLAG = re.compile(r"^ACCESS\s*=\s*(True|False)\s*(?:#.*)?$", re.M)


def is_access(name: str) -> bool:
    """An installed plugin that answers the access webhooks. Read from the source, not by loading it: a page
    must not run the code of a plugin that is off. A line `ACCESS = True` or `ACCESS = False` in plugin.py
    decides; without it, a plugin that defines both `login` and `permissions` in plugin.py counts. A plugin
    that imports them from another file must say `ACCESS = True`."""
    try:
        with open(os.path.join(installed()[name], "plugin.py"), encoding="utf-8") as f:
            src = f.read()
    except (OSError, KeyError):
        return False
    flag = _ACCESS_FLAG.search(src)
    if flag:
        return flag.group(1) == "True"
    return "\ndef login(" in src and "\ndef permissions(" in src


def name_of(mod) -> str | None:
    """The installed name of a loaded plugin module (its add-on name), or None."""
    for name, loaded in list(_loaded.items()):
        if loaded is mod:
            return name
    return None


def access(enabled: set[str]):
    """The active plugin that answers the two access webhooks (the first by name), or None."""
    for mod in active(enabled):
        if callable(getattr(mod, "login", None)) and callable(getattr(mod, "permissions", None)):
            return mod
    return None


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
