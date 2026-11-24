#!/usr/bin/env python3
"""run_code: an agent's JavaScript in the sandbox (QuickJS in WebAssembly inside a locked-down Node)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401

import kb_core

READY = shutil.which("node") and os.path.isdir(os.path.join(kb_core.SANDBOX, "node_modules", "quickjs-emscripten"))


@unittest.skipUnless(READY, "node or sandbox/node_modules is missing (npm ci in sandbox/)")
class RunCode(unittest.TestCase):
    def setUp(self):
        self.seen = []
        real = kb_core.call_tool

        def spy(name, args, allowed=None, caller=None, **kw):   # `nested=True` from run_code
            self.seen.append((name, allowed, caller))
            return real(name, args, allowed, caller, **kw)
        for target, name, value in ((kb_core, "call_tool", spy),
                                    (kb_core, "_index", lambda fn, *a: "GET /v2/a\nPOST /v2/b\nGET /v2/c")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_js(self, code, allowed=None, caller=None):
        return kb_core.call_tool("run_code", {"code": code}, allowed, caller)

    def test_program_calls_tools_and_returns_only_what_it_wants(self):
        out = self.run_js("const r = kb('contracts_search', {query: 'x'});"
                          "return r.split('\\n').filter(l => l.startsWith('GET')).join('; ')", {"api"}, "ann")
        self.assertIn("GET /v2/a; GET /v2/c", out)
        self.assertNotIn("POST", out)
        self.assertIn(("contracts_search", {"api"}, "ann"), self.seen)  # the caller's own rights

    def test_typescript_is_accepted(self):
        out = self.run_js("type Row = { m: string };\n"
                          "const rows: Row[] = kb('contracts_search', { query: 'x' }).split('\\n')"
                          ".map((l: string): Row => ({ m: l.split(' ')[0] }));\n"
                          "return rows.filter((r: Row) => r.m === 'GET').length as number")
        self.assertIn("2", out.split("[run_code")[0])
        self.assertIn("TypeScript", self.run_js("enum E { A }\nreturn E.A"))

    def test_no_host_objects_in_the_guest(self):
        out = self.run_js("return [typeof require, typeof process, typeof fetch, typeof setTimeout,"
                          " typeof globalThis.std, typeof globalThis.os].join(',')")
        self.assertIn("undefined,undefined,undefined,undefined,undefined,undefined", out)

    def test_no_run_code_or_batch_from_inside(self):
        for tool in ("run_code", "batch"):
            self.assertIn("cannot be called from run_code", self.run_js(f"return kb('{tool}', {{}})"))

    def test_limits(self):
        with mock.patch.object(kb_core, "RUN_SECONDS", 1):
            self.assertIn("interrupted", self.run_js("while (true) {}"))
        self.assertIn("string too long", self.run_js("let s = 'x'; while (true) s += s;"))
        self.assertIn("kb() calls", self.run_js("for (let i = 0; i < 99; i++) kb('version', {}); return 1"))
        with self.assertRaises(kb_core.ToolError):
            self.run_js("x" * 20_001)

    def test_errors_come_back_as_text(self):
        self.assertIn("(error: ReferenceError", self.run_js("return nope"))
        self.assertIn("graph_explain: 'symbol' is required", self.run_js("return kb('graph_explain', {})"))

    def test_the_node_wall_holds_without_quickjs(self):
        """The second wall: even code running in Node itself, as the runner is started, cannot reach the
        network, start a process or write a file."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "x.txt")
            script = ("const fs = require('node:fs'); const cp = require('node:child_process'); const r = [];"
                      f"try {{ fs.writeFileSync({target!r}, 'x'); r.push('write'); }} catch (e) {{ r.push(e.code); }}"
                      "try { cp.execSync('true'); r.push('exec'); } catch (e) { r.push(e.code); }"
                      "require('node:net').connect(80, '127.0.0.1').on('error', e => { r.push(e.code);"
                      " console.log(r.join(',')); });")
            p = subprocess.run(["node", "--permission", f"--allow-fs-read={kb_core.SANDBOX}", "-e", script],
                               capture_output=True, text=True, timeout=30)
            out = p.stdout + p.stderr
            self.assertFalse(os.path.exists(target))
            self.assertNotIn("write", out.split("\n")[0])
            self.assertIn("ERR_ACCESS_DENIED", out)


if __name__ == "__main__":
    unittest.main()
