"""Scanned PDF -> text, via `pdftoppm` + `tesseract`.

Used only when the text-layer parser found nothing, because OCR is slower by orders of magnitude and
its output is **noisy**: numbers become letters, table columns merge, hyphenation invents words. That
is why its documents are stamped `confidence: OCR` — a search hit from here is a lead, not a quote.

Languages are taken from `KB_OCR_LANGS` (tesseract syntax, e.g. `eng+lav+rus`); the default is `eng`
because a wrong language model produces confident nonsense rather than an error.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import OCR, ExtractError

EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
REQUIRES = ("tesseract",)
CONFIDENCE = OCR
PRIORITY = 90          # last resort


def _ocr_image(image: Path, langs: str) -> str:
    out = subprocess.run(["tesseract", str(image), "stdout", "-l", langs],
                         capture_output=True, text=True, timeout=600)
    return out.stdout


def extract(path: Path) -> str:
    langs = os.environ.get("KB_OCR_LANGS", "eng")
    if path.suffix.lower() != ".pdf":
        text = _ocr_image(path, langs).strip()
        if not text:
            raise ExtractError(f"{path.name}: OCR produced nothing")
        return text
    if not shutil_which("pdftoppm"):
        raise ExtractError(f"{path.name}: OCR of a PDF needs pdftoppm (poppler) as well as tesseract")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(["pdftoppm", "-r", "200", "-png", str(path), str(prefix)],
                       capture_output=True, timeout=900)
        pages = sorted(Path(tmp).glob("page-*.png"))
        if not pages:
            raise ExtractError(f"{path.name}: could not rasterise for OCR")
        chunks = []
        for page in pages:
            body = _ocr_image(page, langs).strip()
            if body:
                chunks.append(f"<!-- page {page.stem.split('-')[-1]} -->\n\n{body}")
    text = "\n\n".join(chunks).strip()
    if not text:
        raise ExtractError(f"{path.name}: OCR produced nothing across {len(pages)} page(s)")
    return text


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)
