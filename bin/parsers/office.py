"""Office documents -> markdown, via `pandoc`.

Kept separate from the PDF parsers on purpose: a `.docx` is structured markup, so pandoc produces real
markdown (headings, lists, **tables**) rather than a flat text dump. Nothing here tries to be clever —
if pandoc is absent, the format is simply unsupported and the build says so.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import EXTRACTED, ExtractError

EXTENSIONS = (".docx", ".odt", ".rtf", ".epub")
REQUIRES = ("pandoc",)
CONFIDENCE = EXTRACTED
PRIORITY = 20


def extract(path: Path) -> str:
    try:
        out = subprocess.run(
            ["pandoc", "--from", path.suffix.lstrip(".").lower(), "--to", "gfm",
             "--wrap=none", str(path)],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractError(f"pandoc failed on {path.name}: {exc}") from exc
    text = out.stdout.strip()
    if not text:
        raise ExtractError(f"{path.name}: pandoc produced nothing ({out.stderr.strip()[:120]})")
    return text
