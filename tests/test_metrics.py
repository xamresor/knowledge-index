#!/usr/bin/env python3
"""bin/metrics.py: the Prometheus page, its format and who may read it."""
from __future__ import annotations

import base64
import os
import re
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core
import metrics
from usage import Usage


class Metrics(unittest.TestCase):
    def test_format_is_valid_text_exposition(self):
        with tempfile.TemporaryDirectory() as d:
            u = Usage(os.path.join(d, "u.json"), flush_every=3600)
            u.record("search", "ann", {"project": "api"}, True, 0.2)
            u.record("search", "ann", {}, False, 0.4)
            with mock.patch.object(kb_core, "KB", d), mock.patch.object(kb_core, "GRAPH", os.path.join(d, "g.json")):
                text = metrics.render(u)
        for line in text.splitlines():
            self.assertRegex(line, metrics.VALID, line)
        self.assertIn('kb_tool_calls_total{tool="search"} 2', text)
        self.assertIn('kb_tool_errors_total{tool="search"} 1', text)
        self.assertRegex(text, r'kb_info\{version="[^"]+",contract="\d+"\} 1')
        self.assertEqual(len(re.findall(r"^# TYPE kb_tool_calls_total ", text, re.M)), 1)

    def test_access(self):
        self.assertTrue(metrics.allowed("127.0.0.1", {}))
        self.assertFalse(metrics.allowed("127.0.0.1", {"X-Forwarded-For": "10.0.0.5"}))  # behind a proxy
        self.assertFalse(metrics.allowed("10.0.0.5", {}))
        with mock.patch.object(metrics, "TOKEN", "s3cret"):
            basic = "Basic " + base64.b64encode(b"kb:s3cret").decode()
            self.assertTrue(metrics.allowed("10.0.0.5", {"Authorization": basic}))
            self.assertTrue(metrics.allowed("10.0.0.5", {"Authorization": "Bearer s3cret"}))
            self.assertFalse(metrics.allowed("10.0.0.5", {"Authorization": "Bearer nope"}))

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_METRICS", None)
            self.assertNotEqual(os.environ.get("KB_METRICS", "off"), "on")


if __name__ == "__main__":
    unittest.main()
