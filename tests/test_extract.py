#!/usr/bin/env python3
"""bin/extract --into: where extracted docs land, when they are extracted again, and what a write
may touch. A fake parser stands in for pdftotext, so no external tool is needed."""
from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import importlib.machinery
import importlib.util
import sys

from _kbtest import BIN

# bin/extract has no .py suffix, so name the loader explicitly.
_loader = importlib.machinery.SourceFileLoader("kb_extract", os.path.join(BIN, "extract"))
extract = importlib.util.module_from_spec(importlib.util.spec_from_loader("kb_extract", _loader))
sys.modules["kb_extract"] = extract
_loader.exec_module(extract)


class FakeParser:
    __name__ = "parsers.fake"
    REQUIRES: tuple = ()
    CONFIDENCE = "EXTRACTED"
    calls: list = []

    @classmethod
    def extract(cls, path: Path) -> str:
        cls.calls.append(str(path))
        return f"text of {path.name}"


class ExtractInto(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.root, self.stage = self.d / "docs", self.d / "stage"
        self.root.mkdir()
        self.stage.mkdir()
        FakeParser.calls = []
        patcher = mock.patch.object(extract.parsers, "for_path", return_value=FakeParser)
        patcher.start()
        self.addCleanup(patcher.stop)

    def src(self, rel: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF " + rel.encode())
        return p

    def run_extract(self, *files: Path, prune: bool = True) -> int:
        args = ["--into", str(self.stage), "--root", str(self.root)] + (["--prune"] if prune else [])
        stdin = io.StringIO("".join(f"{f}\0" for f in files))
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            return extract.main(args + ["--from0"])

    def test_relative_path_and_a_distinct_suffix(self):
        (self.stage / "guide.md").write_text("authored\n")
        a, b, g = self.src("a/x.pdf"), self.src("b/x.pdf"), self.src("guide.pdf")
        self.run_extract(a, b, g)
        self.assertEqual((self.stage / "guide.md").read_text(), "authored\n")
        self.assertIn("text of guide.pdf", (self.stage / "guide.pdf.md").read_text())
        self.assertTrue((self.stage / "a" / "x.pdf.md").is_file())
        self.assertTrue((self.stage / "b" / "x.pdf.md").is_file())

    def test_an_authored_file_of_the_same_name_wins(self):
        (self.root / "guide.pdf.md").write_text("authored\n")
        (self.stage / "guide.pdf.md").write_text("authored\n")
        self.run_extract(self.src("guide.pdf"))
        self.assertEqual((self.stage / "guide.pdf.md").read_text(), "authored\n")

    def test_unchanged_source_is_not_extracted_again(self):
        g = self.src("guide.pdf")
        self.run_extract(g)
        first = (self.stage / "guide.pdf.md").read_text()
        self.run_extract(g)
        self.assertEqual(len(FakeParser.calls), 1)
        self.assertEqual((self.stage / "guide.pdf.md").read_text(), first)
        os.utime(g, (time.time() + 5, time.time() + 5))
        self.run_extract(g)
        self.assertEqual(len(FakeParser.calls), 2)

    def test_timestamp_is_the_source_mtime(self):
        g = self.src("guide.pdf")
        os.utime(g, (1700000000, 1700000000))
        self.run_extract(g)
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(1700000000))
        self.assertIn(f"extracted_at: {stamp}\n", (self.stage / "guide.pdf.md").read_text())

    def test_removed_source_removes_its_extract(self):
        g = self.src("guide.pdf")
        self.run_extract(g)
        self.run_extract()
        self.assertFalse((self.stage / "guide.pdf.md").exists())

    def test_a_symlink_in_the_stage_is_replaced_not_followed(self):
        victim = self.d / "users.json"
        victim.write_text('{"keep": true}')
        (self.stage / "report.pdf.md").symlink_to(victim)
        self.run_extract(self.src("report.pdf"))
        self.assertEqual(victim.read_text(), '{"keep": true}')
        self.assertFalse((self.stage / "report.pdf.md").is_symlink())

    def test_a_symlinked_folder_out_of_the_stage_is_refused(self):
        outside = self.d / "outside"
        outside.mkdir()
        (self.stage / "sub").symlink_to(outside)
        self.run_extract(self.src("sub/r.pdf"))
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
