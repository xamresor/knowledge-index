#!/usr/bin/env python3
"""Server settings that an admin may change at run time: the registry, the file, the env override.

Why this module exists
----------------------
Most settings are env variables read once at start. A few are product decisions an admin makes on the
running server (the Settings page of /admin): who sees which contracts. Each of those lives here once,
with its choices, its default, its description and the env variable that overrides it.

  * **The file.** `<config>/settings.json`, a JSON object `{"contracts": "own"}`. Only keys of the
    registry count; anything else in the file is ignored. A write goes to a temp file and then
    `os.replace`, so a reader sees the old file or the new one. Reads are cached by mtime: a change
    takes effect on the next call, without a restart (unless the setting says `restart`).
  * **The env variable.** When it is set, it wins, and the Settings page shows the value as locked
    ("set by KB_CONTRACTS").
  * **A bad value** (in the env or in the file) reads as the setting's `invalid` value. For a setting
    that guards access this is the closed choice, so a typo never opens more than intended.

CLI: `settings.py list` | `settings.py get <key>` | `settings.py set <key> <value>`.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402


@dataclass(frozen=True)
class Setting:
    key: str
    choices: tuple[str, ...]
    default: str
    description: str
    env: str
    restart: bool = False       # True: a change takes effect after a restart of the server
    invalid: str | None = None  # the value a bad value reads as (None: the default)


REGISTRY: dict[str, Setting] = {s.key: s for s in (
    Setting("contracts", ("all", "own"), "all",
            "Who sees the contracts between services (contracts_search, connections, contract docs, the "
            "http_request edges to other projects). all: every signed-in user sees every contract. own: a "
            "user sees only the contracts that touch an allowed project; the other side shows as a bare "
            "endpoint (method and URI).",
            "KB_CONTRACTS", invalid="own"),
)}


class Locked(PermissionError):
    """The env variable of the setting is set: the file value does not count."""


_cache: dict = {}
_lock = threading.Lock()


def state_file() -> str:
    return os.path.join(str(paths.config_home()), "settings.json")


def _file_values() -> dict:
    path = state_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        if _cache.get("path") != path or _cache.get("mtime") != mtime:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            _cache.update(path=path, mtime=mtime, values=data if isinstance(data, dict) else {})
        return dict(_cache["values"])


def _checked(s: Setting, raw) -> str:
    value = str(raw).strip().lower()
    if value in s.choices:
        return value
    return s.invalid if s.invalid is not None else s.default


def source(key: str) -> tuple[str, str | None]:
    """Where the value comes from: ("env", "KB_…"), ("file", None) or ("default", None)."""
    s = REGISTRY[key]
    if os.environ.get(s.env, "").strip():
        return "env", s.env
    if key in _file_values():
        return "file", None
    return "default", None


def get(key: str) -> str:
    s = REGISTRY[key]
    env = os.environ.get(s.env, "").strip()
    if env:
        return _checked(s, env)
    values = _file_values()
    if key in values:
        return _checked(s, values[key])
    return s.default


def set_value(key: str, value: str) -> None:
    """Write one setting to the file. Raises KeyError (unknown key), ValueError (not a choice) or
    Locked (the env variable is set)."""
    s = REGISTRY[key]
    if os.environ.get(s.env, "").strip():
        raise Locked(f"{key} is set by {s.env}")
    value = str(value).strip().lower()
    if value not in s.choices:
        raise ValueError(f"{key}: one of {', '.join(s.choices)}")
    path = state_file()
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        _cache.clear()


def rows() -> list[dict]:
    """Every setting with its value and where the value comes from (for the Settings page)."""
    out = []
    for key, s in REGISTRY.items():
        where, env = source(key)
        out.append({"key": key, "value": get(key), "default": s.default, "choices": list(s.choices),
                    "description": s.description, "env": s.env, "locked": where == "env",
                    "source": where, "restart": s.restart})
    return out


if __name__ == "__main__":
    cmd = sys.argv[1:] or ["list"]
    if cmd == ["list"]:
        for r in rows():
            lock = f"  (set by {r['env']})" if r["locked"] else ""
            print(f"{r['key']:<12} {r['value']:<6} default {r['default']}{lock}")
    elif len(cmd) == 2 and cmd[0] == "get":
        print(get(cmd[1]))
    elif len(cmd) == 3 and cmd[0] == "set":
        try:
            set_value(cmd[1], cmd[2])
        except (KeyError, ValueError, Locked) as e:
            raise SystemExit(str(e)) from None
    else:
        raise SystemExit("usage: settings.py list | get <key> | set <key> <value>")
