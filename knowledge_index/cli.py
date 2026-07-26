"""Console entry points for an installed copy.

Each function locates the shipped script and hands the process over to it with `os.execv`, so:

  * there is **one implementation** (the script) rather than a packaged fork of it;
  * signals, exit codes, stdin/stdout stay exactly as they are — which matters for `kb-mcp`, whose
    whole protocol is a stdio pipe, and for `kb-api`, which must die on Ctrl-C.

`bin/` is added to `PYTHONPATH` before handing over, because the scripts import their siblings
(`kb_core`, `graph`, `paths`) the way a directly executed script does.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _exec(script: str) -> None:
    target = BIN / script
    if not target.is_file():
        sys.exit(f"knowledge-index: missing shipped script {target} (broken installation?)")
    env_path = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = f"{BIN}{os.pathsep}{env_path}" if env_path else str(BIN)
    argv = [str(target), *sys.argv[1:]]
    try:
        os.execv(str(target), argv)
    except OSError as exc:  # not executable (e.g. a wheel stripped the bit) — run it explicitly
        interpreter = sys.executable if script.endswith(".py") or _is_python(target) else "/bin/bash"
        os.execv(interpreter, [interpreter, str(target), *sys.argv[1:]])
        raise SystemExit(f"knowledge-index: cannot run {target}: {exc}")


def _is_python(path: Path) -> bool:
    try:
        return path.read_bytes()[:30].startswith(b"#!") and b"python" in path.read_bytes()[:30]
    except OSError:
        return False


def kb() -> None:
    """`kb` — build and inspect the index (bash: drives rsync, graphify and qmd)."""
    _exec("kb")


def kb_mcp() -> None:
    """`kb-mcp` — MCP server over stdio, for agents."""
    _exec("kb-mcp")


def kb_api() -> None:
    """`kb-api` — HTTP API, for scripts."""
    _exec("kb-api")


def kb_install() -> None:
    """`kb-install` — first-run setup: requirements, config, the MCP snippet."""
    _exec("install")


def kb_extract() -> None:
    """`kb-extract` — turn a non-markdown document into markdown (one plugin per format)."""
    _exec("extract")
