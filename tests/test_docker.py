#!/usr/bin/env python3
"""The Docker image for the shared server (Dockerfile, docker/compose.example.yml, docs/docker.md).

Checks the files, not a built image (no Docker needed): pinned versions, a non-root user, a build
context without private files, a sandbox container without network. Also the two code paths the
image depends on: run_code over a Unix socket (KB_SANDBOX_SOCKET) and a ready route table instead of
`php artisan route:list` (KB_ROUTES_DIR, or `routes = "<file>.json"`).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from _kbtest import BIN

import kb_core

ROOT = os.path.dirname(BIN)


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def services(compose: str) -> dict[str, str]:
    """The text of each service block under `services:` (a stdlib reader for one known file)."""
    body = compose.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    out, name = {}, None
    for line in body.splitlines():
        m = re.match(r"^  ([a-z][\w-]*):\s*$", line)
        if m:
            name = m.group(1)
            out[name] = ""
        elif name:
            out[name] += line + "\n"
    return out


class Dockerfile(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile")

    def test_every_base_image_has_an_exact_version(self):
        froms = re.findall(r"(?m)^FROM\s+(\S+)", self.text)
        self.assertGreaterEqual(len(froms), 2)
        for image in froms:
            self.assertRegex(image, r":\d+\.\d+\.\d+-[\w.-]+$", f"base image without an exact version: {image}")
            self.assertNotIn("latest", image)

    def test_tools_are_pinned(self):
        args = dict(re.findall(r"(?m)^ARG\s+(\w+)=(\S*)", self.text))
        for key in ("QMD_VERSION", "GRAPHIFY_VERSION", "RUSTWORKX_VERSION", "HATCHLING_VERSION"):
            self.assertRegex(args.get(key, ""), r"^\d+\.\d+\.\d+$", key)
        self.assertIn('"graphifyy==${GRAPHIFY_VERSION}"', self.text)
        self.assertIn('"rustworkx==${RUSTWORKX_VERSION}"', self.text)
        self.assertIn('"@tobilu/qmd@${QMD_VERSION}"', self.text)
        # The floor that bin/install checks (fields the enrichers expect).
        graphify = tuple(map(int, args["GRAPHIFY_VERSION"].split(".")))
        self.assertGreaterEqual(graphify, (0, 9, 25))

    def test_runtime_is_not_root_and_serves_kb_http(self):
        final = self.text.split("\nFROM ")[-1]
        users = re.findall(r"(?m)^USER\s+(\S+)", final)
        self.assertTrue(users, "the runtime stage sets no USER")
        self.assertNotIn(users[-1], ("root", "0", "0:0"))
        self.assertRegex(final, r'(?m)^CMD \["kb-http"\]')
        self.assertIn("/var/lib/knowledge-index", final)

    def test_context_files_the_dockerfile_copies_pass_the_dockerignore(self):
        ignore = read(".dockerignore")
        allowed = [p[1:].rstrip("/") for p in ignore.splitlines() if p.startswith("!")]
        for src in re.findall(r"(?m)^COPY\s+(?!--from)(.+)\s+\S+$", self.text):
            for path in src.split():
                if path == ".":
                    continue
                self.assertTrue(any(path == a or path.startswith(a + "/") for a in allowed),
                                f"{path} is copied but .dockerignore keeps it out")


class DockerIgnore(unittest.TestCase):
    def test_an_allow_list_that_keeps_private_files_out(self):
        lines = [x.strip() for x in read(".dockerignore").splitlines() if x.strip() and not x.startswith("#")]
        self.assertEqual(lines[0], "*", "start with '*' and allow what the image needs")
        allowed = [x[1:].rstrip("/") for x in lines if x.startswith("!")]
        for private in ("kb.projects.toml", "aliases.toml", "users.json", "smtp.env", "admins.txt",
                        "graphify-out", "repos", ".qmd", ".docs", "addons.json", "plugins", ".git"):
            self.assertFalse(any(fnmatch.fnmatch(private, a) for a in allowed), private)

    def test_every_file_the_wheel_force_includes_passes_the_allow_list(self):
        # pip builds the wheel from the build context: a forced include that .dockerignore keeps out
        # stops `docker build` (FileNotFoundError: Forced include not found).
        allowed = [x[1:].rstrip("/") for x in read(".dockerignore").splitlines() if x.startswith("!")]
        block = read("pyproject.toml").split("[tool.hatch.build.targets.wheel.force-include]", 1)[1]
        block = block.split("\n[", 1)[0]
        forced = re.findall(r'(?m)^"?([^"=\s]+)"?\s*=', block)
        self.assertIn("bin", forced)
        for path in forced:
            self.assertTrue(any(path == a or path.startswith(a + "/") for a in allowed),
                            f"pyproject.toml force-includes {path}, but .dockerignore keeps it out")

    def test_the_wheel_ships_the_socket_server(self):
        self.assertGreaterEqual(read("pyproject.toml").count("sandbox/socket-server.mjs"), 3)


class Compose(unittest.TestCase):
    def setUp(self):
        self.svc = services(read("docker/compose.example.yml"))

    def test_the_sandbox_has_no_network_and_no_kb_data(self):
        sb = self.svc["sandbox"]
        self.assertRegex(sb, r"(?m)^\s+network_mode:\s*none\s*$")
        self.assertRegex(sb, r"(?m)^\s+read_only:\s*true\s*$")
        self.assertRegex(sb, r"cap_drop:\s*\[ALL\]")
        self.assertNotIn("ports:", sb)
        self.assertNotIn("kb-data", sb)
        self.assertNotIn("/code", sb)
        self.assertIn("socket-server.mjs", sb)

    def test_the_two_share_only_the_socket(self):
        kb, sb = self.svc["kb"], self.svc["sandbox"]
        sock = re.search(r"KB_SANDBOX_SOCKET:\s*(\S+)", kb).group(1)
        self.assertIn(f"KB_SANDBOX_SOCKET: {sock}", sb)
        volume = re.search(r"-\s*([\w-]+):" + re.escape(os.path.dirname(sock)) + r"\s*$", kb, re.M).group(1)
        self.assertIn(f"- {volume}:{os.path.dirname(sock)}", sb)

    def test_code_and_config_are_read_only(self):
        kb = self.svc["kb"]
        for line in re.findall(r"(?m)^\s+- (/\S+:\S+)$", kb):
            self.assertTrue(line.endswith(":ro"), f"host folder mounted writable: {line}")
        self.assertIn("KB_SERVER", kb)
        self.assertIn("127.0.0.1:8790:8790", kb)


class RunCodeOverSocket(unittest.TestCase):
    """run_code with KB_SANDBOX_SOCKET: the same line protocol, spoken to a service instead of a child."""

    def serve(self, script):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: os.path.isdir(d) and __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.seen: list = []

        def run():
            conn, _ = srv.accept()
            with conn, conn.makefile("r") as rd, conn.makefile("w") as wr:
                self.seen.append(json.loads(rd.readline()))
                script(rd, wr, self.seen)
            srv.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        p = mock.patch.object(kb_core, "SANDBOX_SOCKET", path)
        p.start()
        self.addCleanup(p.stop)
        return path

    def test_tool_calls_go_through_call_tool_with_the_callers_rights(self):
        def script(rd, wr, seen):
            wr.write(json.dumps({"call": "version", "args": {}}) + "\n")
            wr.flush()
            seen.append(json.loads(rd.readline()))
            wr.write(json.dumps({"done": True, "output": "hi", "value": {"n": 1}, "error": None, "calls": 1}) + "\n")
            wr.flush()
        self.serve(script)
        calls = []
        with mock.patch.object(kb_core, "call_tool", lambda n, a, al=None, c=None, nested=False: calls.append((n, al, c)) or "v1"):
            out = kb_core.run_code("return 1", {"api"}, "ann")
        self.assertIn("hi", out)
        self.assertIn('"n": 1', out)
        self.assertIn("[run_code: 1 kb() call(s)]", out)
        self.assertEqual(self.seen[0]["code"], "return 1")
        self.assertEqual(self.seen[1], {"result": "v1"})
        self.assertEqual(calls, [("version", {"api"}, "ann")])

    def test_a_service_that_hangs_up_is_an_error(self):
        self.serve(lambda rd, wr, seen: None)
        with self.assertRaisesRegex(kb_core.ToolError, "sandbox stopped"):
            kb_core.run_code("return 1", None, None)

    def test_no_service_is_an_error_not_a_hang(self):
        with mock.patch.object(kb_core, "SANDBOX_SOCKET", "/nonexistent/kb-sandbox.sock"):
            with self.assertRaisesRegex(kb_core.ToolError, "does not answer"):
                kb_core.run_code("return 1", None, None)


FAKE_GRAPHIFY = r"""#!/bin/sh
case "$1" in
  update) mkdir -p "$2/graphify-out"
          echo '{"directed": true, "multigraph": false, "graph": {}, "nodes": [{"id": "n", "label": "n"}], "links": []}' > "$2/graphify-out/graph.json" ;;
esac
exit 0
"""
FAKE_PHP = "#!/bin/sh\necho called >> \"$FAKE_PHP_LOG\"\nexit 1\n"


class RouteTableFromAFile(unittest.TestCase):
    """bin/kb takes a ready route table when PHP is not there (the image has none)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.d = tmp.name
        self.home, fake, self.src = (os.path.join(d, x) for x in ("kb", "fakebin", "src"))
        for p in (self.home, fake, self.src):
            os.makedirs(p)
        with open(os.path.join(self.src, "a.py"), "w") as f:
            f.write("x = 1\n")
        for name, body in (("graphify", FAKE_GRAPHIFY), ("php", FAKE_PHP)):
            path = os.path.join(fake, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.php_log = os.path.join(d, "php.log")
        self.env = {**os.environ, "KB_HOME": self.home, "FAKE_PHP_LOG": self.php_log,
                    "PATH": fake + os.pathsep + os.environ["PATH"]}
        self.env.pop("KB_ROUTES_DIR", None)
        self.rows = [{"method": "GET|HEAD", "uri": "api/orders", "action": "App\\OrderController@index"}]

    def config(self, routes: str):
        with open(os.path.join(self.home, "kb.projects.toml"), "w") as f:
            f.write(f'[[project]]\nname = "one"\ncode = ["{self.src}"]\nroutes = "{routes}"\n')

    def write(self, path: str, data) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def build(self, **env) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", os.path.join(BIN, "kb"), "graph"], env={**self.env, **env},
                              capture_output=True, text=True, timeout=120)

    def table(self):
        with open(os.path.join(self.home, "routes.one.json")) as f:
            return json.load(f)

    def test_kb_routes_dir(self):
        self.config(self.src)
        rdir = os.path.join(self.d, "routes")
        self.write(os.path.join(rdir, "one.json"), self.rows)
        self.build(KB_ROUTES_DIR=rdir)
        self.assertEqual(self.table(), self.rows)
        self.assertFalse(os.path.exists(self.php_log), "php ran although a route table was given")

    def test_routes_names_a_json_file(self):
        self.config(self.write(os.path.join(self.d, "r", "api.json"), self.rows))
        self.build()
        self.assertEqual(self.table(), self.rows)
        self.assertFalse(os.path.exists(self.php_log))

    def test_a_broken_file_keeps_the_previous_table(self):
        self.config(self.write(os.path.join(self.d, "r", "api.json"), "{not json"))
        self.write(os.path.join(self.home, "routes.one.json"), self.rows)
        p = self.build()
        self.assertIn("using the previous route table", p.stderr)
        self.assertEqual(self.table(), self.rows)

    def test_without_a_file_artisan_still_runs(self):
        self.config(self.src)
        p = self.build()
        self.assertTrue(os.path.exists(self.php_log))
        self.assertIn("php artisan route:list failed", p.stderr)
        self.assertEqual(self.table(), [])


if __name__ == "__main__":
    unittest.main()
