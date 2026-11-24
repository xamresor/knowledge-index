"""Scanned PDF -> text, via `pdftoppm` + `tesseract`.

Used only when the text-layer parser found nothing, because OCR is slower by orders of magnitude and
its output is **noisy**: numbers become letters, table columns merge, hyphenation invents words. That
is why its documents are stamped `confidence: OCR` — a search hit from here is a lead, not a quote.

Languages are taken from `KB_OCR_LANGS` (tesseract syntax, e.g. `eng+lav+rus`); the default is `eng`
because a wrong language model produces confident nonsense rather than an error.

Budgets: a PDF is rendered one page at a time, each page scaled to fit SCALE_TO pixels, and the page
image is deleted before the next one, so a compact scan of many pages never fills the temp disk.
At most `KB_OCR_MAX_PAGES` pages (default 200) are read; the text says when it stopped early.
An image file (PNG, JPEG, TIFF) goes to tesseract as it is: the memory cap of `run_tool`
(`KB_EXTRACT_MAX_MEM_MB`) makes a huge-pixel image fail instead of filling the memory.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import OCR, ExtractError, ToolFailed, run_tool

EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
REQUIRES = ("tesseract",)
CONFIDENCE = OCR
PRIORITY = 90          # last resort
#: The long side of a rendered page in pixels: about 200 dpi on A4, and a cap for an oversized page.
SCALE_TO = 2400
DEFAULT_MAX_PAGES = 200


def _max_pages() -> int:
    try:
        return max(1, int(os.environ.get("KB_OCR_MAX_PAGES", DEFAULT_MAX_PAGES)))
    except ValueError:
        return DEFAULT_MAX_PAGES


def _ocr_image(image: Path, langs: str) -> str:
    # Capped memory: an image file is not scaled, and a huge-pixel PNG must fail, not fill the memory.
    return run_tool(["tesseract", str(image), "stdout", "-l", langs], timeout=600)[0]


def extract(path: Path) -> str:
    langs = os.environ.get("KB_OCR_LANGS", "eng")
    if path.suffix.lower() != ".pdf":
        text = _ocr_image(path, langs).strip()
        if not text:
            raise ExtractError(f"{path.name}: OCR produced nothing")
        return text
    if not shutil_which("pdftoppm"):
        raise ExtractError(f"{path.name}: OCR of a PDF needs pdftoppm (poppler) as well as tesseract")
    limit = _max_pages()
    chunks, pages, more = [], 0, False
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        # One page at a time: pdftoppm stops with no image after the last page.
        for number in range(1, limit + 2):
            try:
                run_tool(["pdftoppm", "-f", str(number), "-l", str(number), "-scale-to", str(SCALE_TO),
                          "-png", str(path), str(prefix)], timeout=300)
            except ToolFailed as exc:
                # A page after the last one: pdftoppm says "Wrong page range" and stops. That is the
                # end of the document. Any other failure is an error, not a short document.
                if number == 1 or "page range" not in exc.stderr.lower():
                    raise
            images = sorted(Path(tmp).glob("page-*.png"))
            if not images:
                break
            if number > limit:            # one page more than the budget exists: say so, do not read it
                more = True
                for image in images:
                    image.unlink()
                break
            pages += 1
            for image in images:
                body = _ocr_image(image, langs).strip()
                image.unlink()
                if body:
                    chunks.append(f"<!-- page {number} -->\n\n{body}")
    if pages == 0:
        raise ExtractError(f"{path.name}: could not rasterise for OCR")
    text = "\n\n".join(chunks).strip()
    if not text:
        raise ExtractError(f"{path.name}: OCR produced nothing across {pages} page(s)")
    if more:
        text += f"\n\n<!-- OCR read only the first {limit} pages (KB_OCR_MAX_PAGES) -->"
    return text


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)
