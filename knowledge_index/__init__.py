"""knowledge-index — a read-only knowledge layer over a set of related code projects.

This package exists so the tool can be installed with `uv tool install` / `pipx install` and get its
commands onto `PATH`. It deliberately stays a **thin launcher**: the implementation lives in the
shipped `bin/` scripts, which remain the source of truth and keep working from a plain checkout.

Why a launcher rather than a rewrite: `bin/kb` is bash (it drives rsync, graphify and qmd as
processes), and porting it to Python for the sake of packaging would be a large change with no user
benefit. Packaging solves distribution, not orchestration.
"""
from __future__ import annotations

__all__ = ["__version__"]


def _read_version() -> str:
    from pathlib import Path
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:  # pragma: no cover - only when the VERSION data file is missing
        return "0+unknown"


__version__ = _read_version()
