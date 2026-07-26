"""PDF with a real text layer -> text, via `pdftotext -layout` (poppler).

`-layout` keeps the visual arrangement, which matters for the documents this actually meets: pricing
decks and proposals put numbers in columns, and without it a table becomes an unreadable stream where
a figure loses the row it belonged to.

This parser deliberately does **not** try to detect a scan. If a PDF has no text layer the output is
near-empty and the registry's caller sees that; a scan is the OCR parser's job.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import EXTRACTED, ExtractError

EXTENSIONS = (".pdf",)
REQUIRES = ("pdftotext",)
CONFIDENCE = EXTRACTED
PRIORITY = 10          # try the text layer before OCR


def extract(path: Path) -> str:
    try:
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                             capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractError(f"pdftotext failed on {path.name}: {exc}") from exc
    text = out.stdout.strip()
    if len(text) < 40:
        # Almost nothing came out: most likely a scan. Say so rather than indexing an empty document.
        raise ExtractError(
            f"{path.name}: no usable text layer ({len(text)} chars) — this looks like a scan, "
            "which needs the OCR parser (tesseract)")
    return text
