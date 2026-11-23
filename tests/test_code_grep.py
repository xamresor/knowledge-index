#!/usr/bin/env python3
"""code_grep: exact text over the staged code, only the caller's projects, bad patterns refused."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core


class CodeGrep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for proj, rel, text in (("api", "config/billing.php", "'key' => env('BILLING_SECRET'),\n"),
                                ("api", "graphify-out/cache.php", "BILLING_SECRET in a cache\n"),
                                ("web", "src/pay.ts", "const k = 'BILLING_SECRET';\n"),
                                ("web", "src/stores/SOSAlarm.js", "export const useSOSAlarmStore = 1;\n"),
                                ("api", "bootstrap/cache/routes-v7.php", "BILLING_SECRET route cache\n")):
            p = os.path.join(self.tmp.name, "repos", proj, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        for patch in (mock.patch.object(kb_core, "KB", self.tmp.name),
                      mock.patch.object(kb_core, "PROJECTS", {"api": [], "web": []})):
            patch.start()
            self.addCleanup(patch.stop)

    def grep(self, allowed=None, **args):
        return kb_core.call_tool("code_grep", args, allowed)

    def test_a_path_glob_works_with_and_without_ripgrep(self):
        for rg in ("/usr/bin/rg", None):
            if rg and not shutil.which("rg"):
                continue
            with mock.patch.object(kb_core.shutil, "which", lambda name, _r=rg: _r):
                out = self.grep(pattern="SOS|sos_alarm", glob="src/stores/*")
                self.assertIn("web:src/stores/SOSAlarm.js:1:", out, rg)
                self.assertIn("(no matches)", self.grep(pattern="SOS", glob="lib/*"))

    def test_generated_laravel_caches_are_skipped(self):
        for rg in (shutil.which("rg"), None):
            with mock.patch.object(kb_core.shutil, "which", lambda name, _r=rg: _r):
                self.assertNotIn("bootstrap/cache", self.grep(pattern="BILLING_SECRET"))

    def test_finds_text_in_every_visible_project(self):
        out = self.grep(pattern="BILLING_[A-Z]+")
        self.assertIn("api:config/billing.php:1:", out)
        self.assertIn("web:src/pay.ts:1:", out)
        self.assertNotIn("graphify-out", out)

    def test_only_the_callers_projects(self):
        out = self.grep({"api"}, pattern="BILLING_SECRET")
        self.assertIn("api:config/billing.php", out)
        self.assertNotIn("web:", out)
        with self.assertRaises(kb_core.ToolError):
            self.grep({"api"}, pattern="x", project="web")

    def test_glob_case_limit(self):
        self.assertNotIn("api:", self.grep(pattern="billing_secret", ignore_case=True, glob="*.ts"))
        self.assertIn("stopped at 1 lines", self.grep(pattern="BILLING", limit=1))
        self.assertIn("(no matches)", self.grep(pattern="NOTHING_LIKE_THIS"))

    def test_bad_pattern_and_option_injection(self):
        with self.assertRaises(kb_core.ToolError):
            self.grep(pattern="(unclosed")
        self.assertIn("(no matches)", self.grep(pattern="--version"))   # a pattern, never an option

    def test_without_ripgrep(self):
        with mock.patch.object(kb_core.shutil, "which", lambda name: None):
            self.assertIn("api:config/billing.php", self.grep(pattern="BILLING_SECRET"))


if __name__ == "__main__":
    unittest.main()
