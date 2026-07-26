#!/usr/bin/env python3
"""Where things live — the one module that knows the difference between code and data.

Until packaging, everything was the repo root: the scripts, the config, the generated graph, the
staged copies of indexed repos, the web shell. A tool installed with `uv tool install` / `pipx` has no
repo root — its code sits in a read-only site-packages directory — so the two must be told apart:

    PACKAGE_ROOT   read-only, ships with the tool: bin/, web/, VERSION, *.example
    DATA_HOME      writable, per machine: graphify-out/, repos/, .docs/
    CONFIG_HOME    writable, per machine: kb.projects.toml, aliases.toml

**Checkout mode is preserved on purpose.** If the directory above `bin/` looks like a checkout (it has
a VERSION file and a bin/kb next to it), all three are that directory — exactly as before, so an
existing clone keeps working with no migration and no surprise about where its files went.

Installed mode follows the XDG spec, with `KB_HOME` as a single override for people who want
everything in one place (including on machines where XDG variables are not set):

    KB_HOME=/srv/kb            -> data and config both under /srv/kb
    XDG_DATA_HOME / XDG_CONFIG_HOME respected otherwise
    fallback: ~/.local/share/knowledge-index and ~/.config/knowledge-index
"""
from __future__ import annotations

import os
from pathlib import Path

APP = "knowledge-index"

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_checkout(path: Path) -> bool:
    """A source checkout, as opposed to an installed copy.

    The marker is `pyproject.toml`: a checkout always has it, and a wheel never installs it into
    site-packages. `VERSION` and `bin/` are **not** usable markers — the wheel ships both, which is
    exactly the bug this replaced: an installed copy called itself a checkout and pointed its data
    directory at site-packages.
    """
    return (path / "pyproject.toml").is_file() and (path / "bin" / "kb").is_file()


def _xdg(env: str, default: str) -> Path:
    raw = os.environ.get(env)
    return Path(raw).expanduser() if raw else Path.home() / default


def data_home() -> Path:
    """Writable root for generated artefacts. Created on demand by the caller that writes."""
    if override := os.environ.get("KB_HOME"):
        return Path(override).expanduser()
    if _is_checkout(PACKAGE_ROOT):
        return PACKAGE_ROOT
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def config_home() -> Path:
    """Writable root for hand-edited config (`kb.projects.toml`, `aliases.toml`)."""
    if override := os.environ.get("KB_HOME"):
        return Path(override).expanduser()
    if _is_checkout(PACKAGE_ROOT):
        return PACKAGE_ROOT
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def graph_path() -> Path:
    return data_home() / "graphify-out" / "graph.json"


def out_dir() -> Path:
    return data_home() / "graphify-out"


def config_file(name: str) -> Path:
    return config_home() / name


def web_source() -> Path:
    """The versioned shell that ships with the tool (read-only when installed)."""
    return PACKAGE_ROOT / "web"


def web_staged() -> Path:
    """Where the shell is copied so it sits **next to** the generated data.

    The page loads its data through relative `<script src>` (a `file://` page cannot fetch a sibling),
    so shell and data must share a parent. In a checkout that is already true and this is the same
    directory; installed, the shell is copied here at build time.
    """
    return data_home() / "web"


def describe() -> dict[str, str]:
    """For `--version` output and the installer: say plainly where everything is."""
    return {
        "mode": "checkout" if _is_checkout(PACKAGE_ROOT) else "installed",
        "package": str(PACKAGE_ROOT),
        "data": str(data_home()),
        "config": str(config_home()),
    }


def stage_web() -> Path:
    """Put the shell next to the data and return that directory.

    In a checkout this is a no-op (they are already siblings). Installed, the shipped `web/` lives in a
    read-only directory, so it is copied — and `vendor/` is left alone, because that is where the
    downloaded renderer lands and it must survive an upgrade of the tool.
    """
    import shutil

    src, dst = web_source(), web_staged()
    if src.resolve() == dst.resolve():
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == "vendor":
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    return dst


def _sh_exports() -> str:
    """Shell assignments for bin/kb, so bash and python agree on every path."""
    return "\n".join([
        f'KB_PACKAGE="{PACKAGE_ROOT}"',
        f'KB_DATA="{data_home()}"',
        f'KB_CONFIG="{config_home()}"',
        f'KB_OUT="{out_dir()}"',
        f'KB_WEB="{web_staged()}"',
    ])


if __name__ == "__main__":
    import sys
    if "--sh" in sys.argv:
        print(_sh_exports())
    else:
        for key, value in describe().items():
            print(f"{key:8} {value}")
