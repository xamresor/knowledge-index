"""Tests for bin/parsers/ — one extraction plugin per format.

The rules under test are the ones that keep this from rotting into a pile of half-working parsers:

  * a **missing external tool** makes a format unsupported and *reported*, never a crash;
  * **priority** decides who tries first — a text layer before OCR, always;
  * every output carries **provenance**, so an OCR'd scan can never pass for authored text;
  * the tool version in that provenance is **validated** (poppler's `pdftotext` treats `--version` as
    a filename and prints an I/O error — that error once ended up in a header).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import parsers
from parsers import pdf_ocr, pdf_text


class RegistryTest(unittest.TestCase):
    def test_pdf_prefers_the_text_layer_over_ocr(self):
        order = [m.__name__.rsplit(".", 1)[-1] for m in parsers.registry()[".pdf"]]
        self.assertEqual(order[0], "pdf_text", "OCR is a last resort, not a first choice")
        self.assertIn("pdf_ocr", order)

    def test_every_parser_declares_the_full_contract(self):
        for ext, modules in parsers.registry().items():
            for module in modules:
                with self.subTest(ext=ext, module=module.__name__):
                    self.assertTrue(module.EXTENSIONS)
                    self.assertIsInstance(module.REQUIRES, tuple)
                    self.assertIn(module.CONFIDENCE, (parsers.EXTRACTED, parsers.OCR))
                    self.assertTrue(callable(module.extract))

    def test_a_format_with_no_installed_tool_is_reported_not_crashed(self):
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertIsNone(parsers.for_path(Path("x.pdf")))
        self.assertIn("pdftotext", parsers.MISSING_TOOLS.get(".pdf", ()))

    def test_unknown_extension_has_no_parser_and_no_complaint(self):
        self.assertIsNone(parsers.for_path(Path("x.sqlite")))
        self.assertNotIn(".sqlite", parsers.MISSING_TOOLS)

    def test_available_lists_only_what_can_run_now(self):
        available = parsers.available()
        for ext, name in available.items():
            module = next(m for m in parsers.registry()[ext]
                          if m.__name__.rsplit(".", 1)[-1] == name)
            for tool in module.REQUIRES:
                self.assertIsNotNone(shutil.which(tool), f"{ext}: {tool}")


class ProvenanceTest(unittest.TestCase):
    def test_header_names_the_source_tool_and_confidence(self):
        out = parsers.with_provenance("body text", Path("/tmp/deck.pdf"), pdf_text)
        self.assertTrue(out.startswith("---\n"))
        self.assertIn('extracted_from: "/tmp/deck.pdf"', out)
        self.assertIn('extracted_by: "pdf_text"', out)
        self.assertIn(f"confidence: {parsers.EXTRACTED}", out)
        self.assertIn("body text", out)

    def test_ocr_output_is_labelled_as_ocr(self):
        out = parsers.with_provenance("nolsy tex7", Path("/tmp/scan.pdf"), pdf_ocr)
        self.assertIn(f"confidence: {parsers.OCR}", out)

    def test_the_generated_marker_warns_against_editing(self):
        out = parsers.with_provenance("x", Path("/tmp/a.pdf"), pdf_text)
        self.assertIn("Do not edit", out)

    def test_tool_version_rejects_an_error_message(self):
        """The bug this pins: `pdftotext --version` prints an I/O error, which landed in a header."""
        fake = mock.Mock()
        fake.stdout = "I/O Error: Couldn't open file '--version': No such file 2.0\n"
        fake.stderr = ""
        good = mock.Mock(stdout="tool version 24.02.0\n", stderr="")
        with mock.patch("subprocess.run", side_effect=[fake, good]):
            self.assertEqual(parsers._tool_version("tool"), "tool version 24.02.0")

    def test_tool_version_falls_back_to_the_name(self):
        with mock.patch("subprocess.run", side_effect=OSError):
            self.assertEqual(parsers._tool_version("nope"), "nope")


class PdfTextTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
    def test_a_scan_like_pdf_is_refused_with_an_explanation(self):
        """Near-empty output means no text layer: say so instead of indexing an empty document."""
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="  \f  ", stderr="")):
            with self.assertRaises(parsers.ExtractError) as ctx:
                pdf_text.extract(Path("scan.pdf"))
        self.assertIn("scan", str(ctx.exception).lower())

    def test_a_real_text_layer_comes_through(self):
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="A" * 200, stderr="")):
            self.assertEqual(len(pdf_text.extract(Path("deck.pdf"))), 200)

    def test_layout_is_preserved(self):
        """Pricing decks put figures in columns; without -layout a number loses its row."""
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="x" * 100, stderr="")) as run:
            pdf_text.extract(Path("deck.pdf"))
        self.assertIn("-layout", run.call_args[0][0])


class OcrTest(unittest.TestCase):
    def test_languages_come_from_the_environment_with_a_safe_default(self):
        """A wrong language model produces confident nonsense rather than an error, so default eng."""
        with mock.patch.object(pdf_ocr, "_ocr_image", return_value="text") as ocr, \
             mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("KB_OCR_LANGS", None)
            pdf_ocr.extract(Path("a.png"))
            self.assertEqual(ocr.call_args[0][1], "eng")
        with mock.patch.object(pdf_ocr, "_ocr_image", return_value="text") as ocr, \
             mock.patch.dict("os.environ", {"KB_OCR_LANGS": "eng+lav"}):
            pdf_ocr.extract(Path("a.png"))
            self.assertEqual(ocr.call_args[0][1], "eng+lav")

    def test_empty_ocr_is_an_error_not_an_empty_document(self):
        with mock.patch.object(pdf_ocr, "_ocr_image", return_value="   "):
            with self.assertRaises(parsers.ExtractError):
                pdf_ocr.extract(Path("a.png"))


class RealFileTest(unittest.TestCase):
    """End-to-end on a generated PDF, so the happy path is proven and not only mocked."""

    @unittest.skipUnless(shutil.which("pdftotext") and shutil.which("pdftoppm"),
                         "poppler not installed")
    def test_extraction_round_trip(self):
        # A minimal one-page PDF with a text layer, written by hand — no library needed.
        body = (b"%PDF-1.4\n"
                b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]"
                b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
                b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
                b"5 0 obj<</Length 78>>stream\n"
                b"BT /F1 12 Tf 20 100 Td (ConnectPay setup fee 9900 EUR monthly minimum) Tj ET\n"
                b"endstream endobj\n"
                b"trailer<</Root 1 0 R>>\n")
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "quote.pdf"
            pdf.write_bytes(body)
            out = parsers.extract(pdf)
        if out is None:                      # no parser could run in this environment
            self.skipTest("no pdf parser available")
        self.assertIn("9900", out, "the figure that matters must survive extraction")
        self.assertIn("extracted_by", out)


if __name__ == "__main__":
    unittest.main()
