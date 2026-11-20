#!/usr/bin/env python3
"""bin/plugins.py: an external add-on folder is found, switched on like any add-on, its tools join the
core's list and answer through the core (paging, access, validation), and a broken one is skipped."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from _kbtest import BIN

PLUGIN = textwrap.dedent('''
    NAME = "hello"
    DESCRIPTION = "says hello"
    TOOLS = [{"name": "hello", "description": "greet",
              "inputSchema": {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}}]

    def call(name, args, allowed, core):
        return f"hello {args['who']} from {sorted(allowed) if allowed is not None else 'all'}"

    def build(core):
        open(core.paths.data_home() / "built.txt", "w").write("ok")
''')

PROBE = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import kb_core
    names = [t["name"] for t in kb_core.tools()]
    out = {"listed": "hello" in names}
    if out["listed"]:
        out["answer"] = kb_core.call_tool("hello", {"who": "ann"}, {"api"})
        try:
            kb_core.call_tool("hello", {})
        except kb_core.ToolError as e:
            out["error"] = str(e)
        out["paged"] = "page" in kb_core.TOOLS_BY_NAME["hello"]["inputSchema"]["properties"]
    print(json.dumps(out))
''')


class Plugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        src = os.path.join(self.home, "src", "hello")
        os.makedirs(src)
        with open(os.path.join(src, "plugin.py"), "w") as f:
            f.write(PLUGIN)
        os.makedirs(os.path.join(self.home, "plugins"))
        os.symlink(src, os.path.join(self.home, "plugins", "hello"))
        self.env = {**os.environ, "KB_HOME": self.home}
        self.env.pop("KB_ADDONS", None)

    def probe(self, addons: str | None):
        env = dict(self.env)
        if addons is not None:
            env["KB_ADDONS"] = addons
        p = subprocess.run([sys.executable, "-c", PROBE, BIN], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        import json
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_off_until_switched_on(self):
        self.assertFalse(self.probe("")["listed"])

    def test_on_answers_through_the_core(self):
        out = self.probe("hello")
        self.assertTrue(out["listed"])
        self.assertEqual(out["answer"], "hello ann from ['api']")
        self.assertIn("'who' is required", out["error"])
        self.assertTrue(out["paged"])

    def test_build_hook_and_a_broken_plugin(self):
        os.makedirs(os.path.join(self.home, "plugins", "broken"))
        with open(os.path.join(self.home, "plugins", "broken", "plugin.py"), "w") as f:
            f.write("raise RuntimeError('boom')\n")
        env = {**self.env, "KB_ADDONS": "hello,broken"}
        p = subprocess.run([sys.executable, os.path.join(BIN, "plugins.py"), "build"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("plugin broken failed to load", p.stderr)
        with open(os.path.join(self.home, "built.txt")) as f:
            self.assertEqual(f.read(), "ok")

    def test_addon_list_shows_plugins(self):
        p = subprocess.run([sys.executable, os.path.join(BIN, "addons.py"), "list"], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertIn("hello", p.stdout)


if __name__ == "__main__":
    unittest.main()
