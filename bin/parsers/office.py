"""Office documents -> markdown, via `pandoc`.

Kept separate from the PDF parsers on purpose: a `.docx` is structured markup, so pandoc produces real
markdown (headings, lists, **tables**) rather than a flat text dump. Nothing here tries to be clever —
if pandoc is absent, the format is simply unsupported and the build says so.
"""
from __future__ import annotations

from pathlib import Path

from . import EXTRACTED, ExtractError, run_tool

EXTENSIONS = (".docx", ".odt", ".rtf", ".epub")
REQUIRES = ("pandoc",)
CONFIDENCE = EXTRACTED
PRIORITY = 20


def extract(path: Path) -> str:
    # Capped output and memory (run_tool): a small .docx is a zip and can expand to gigabytes.
    # A timeout or an OS error says nothing about the file: run_tool raises TransientError.
    out, err = run_tool(["pandoc", "--from", path.suffix.lstrip(".").lower(), "--to", "gfm",
                         "--wrap=none", str(path)], timeout=300)
    text = out.strip()
    if not text:
        raise ExtractError(f"{path.name}: pandoc produced nothing ({err.strip()[:120]})")
    return text
