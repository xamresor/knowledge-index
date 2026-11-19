#!/usr/bin/env python3
"""Dry-run tests for bin/kb-login against a fake login API (no real credentials, no real API).

The fake API uses its own paths and a custom header, configured through env exactly as a real
deployment would, so the tests also prove that nothing in kb-login is tied to one API."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
loader = importlib.machinery.SourceFileLoader("kb_login", os.path.join(BIN, "kb-login"))
spec = importlib.util.spec_from_loader(loader.name, loader)
kb_login = importlib.util.module_from_spec(spec)
loader.exec_module(kb_login)

ENV = {"KB_LOGIN_PATH": "/v1/login", "KB_LOGIN_2FA_PATH": "/v1/login/two-factor",
       "KB_LOGIN_2FA_SETUP_PATH": "/v1/login/two-factor/setup",
       "KB_LOGIN_HEADERS": '{"X-Client": "dev"}'}

# username -> what the first step answers
USERS = {
    "plain": {"data": {"token": "1|plain", "user": {"uid": 1}}},
    "verify": {"data": {"two_factor": {"challenge": "ch-v", "type": "verify", "expires_in": 300}}},
    "enroll": {"data": {"two_factor": {"challenge": "ch-e", "type": "enroll", "expires_in": 300}}},
}


class FakeLogin(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict, str]] = []

    def log_message(self, *a):
        pass

    def reply(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLogin.seen.append((self.path, body, self.headers.get("X-Client", "")))
        if self.headers.get("X-Client") != "dev":
            return self.reply(400, {"message": "client header missing"})
        if self.path == "/v1/login":
            if body.get("username") == "locked":
                return self.reply(429, {"message": "Too many attempts."}, {"Retry-After": "60"})
            if body.get("password") != "secret" or body.get("username") not in USERS:
                return self.reply(401, {"message": "These credentials do not match our records."})
            return self.reply(200, USERS[body["username"]])
        if self.path == "/v1/login/two-factor/setup":
            return self.reply(200, {"data": {"secret": "ABC", "otpauth_uri": "otpauth://totp/x"}})
        if self.path == "/v1/login/two-factor":
            if body.get("code") == "123456" or body.get("recovery_code") == "rec-1":
                out = {"token": f"2|{body['challenge']}", "user": {"uid": 2}}
                if body["challenge"] == "ch-e":
                    out["recovery_codes"] = ["r1", "r2"]
                return self.reply(200, {"data": out})
            return self.reply(400, {"message": "The code is wrong.", "errors": {"code": ["wrong"]}})
        return self.reply(404, {"message": "not found"})


class KbLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLogin)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeLogin.seen.clear()
        self.said = []

    def login(self, user, password="secret", answer=""):
        return kb_login.login(self.api, user, password, ask=lambda _p: answer,
                              say=self.said.append, env=ENV)

    def test_plain_login(self):
        self.assertEqual(self.login("plain"), "1|plain")
        path, body, app = FakeLogin.seen[0]
        self.assertEqual((path, app), ("/v1/login", "dev"))  # paths and header came from env
        self.assertEqual(body, {"username": "plain", "password": "secret"})

    def test_two_factor_code(self):
        self.assertEqual(self.login("verify", answer="123456"), "2|ch-v")
        self.assertEqual(FakeLogin.seen[1][1], {"challenge": "ch-v", "code": "123456"})

    def test_two_factor_recovery_code(self):
        self.assertEqual(self.login("verify", answer="rec-1"), "2|ch-v")
        self.assertEqual(FakeLogin.seen[1][1], {"challenge": "ch-v", "recovery_code": "rec-1"})

    def test_enroll(self):
        self.assertEqual(self.login("enroll", answer="123456"), "2|ch-e")
        self.assertEqual([s[0] for s in FakeLogin.seen],
                         ["/v1/login", "/v1/login/two-factor/setup", "/v1/login/two-factor"])
        self.assertTrue(any("otpauth://totp/x" in s for s in self.said))
        self.assertIn("  r1", self.said)

    def test_errors(self):
        with self.assertRaisesRegex(kb_login.LoginError, "HTTP 401"):
            self.login("plain", password="wrong")
        with self.assertRaisesRegex(kb_login.LoginError, "retry after 60"):
            self.login("locked")
        with self.assertRaisesRegex(kb_login.LoginError, "HTTP 400.*wrong"):
            self.login("verify", answer="000000")
        with self.assertRaisesRegex(kb_login.LoginError, "cannot reach"):
            kb_login.login("http://127.0.0.1:9", "plain", "secret", env=ENV)

    def test_defaults_and_bad_headers(self):
        self.assertEqual(kb_login._paths({})["login"], "/login")
        self.assertEqual(kb_login._extra_headers({}), {})
        with self.assertRaises(kb_login.LoginError):
            kb_login._extra_headers({"KB_LOGIN_HEADERS": "[1]"})

    def test_write_mcp_json_keeps_other_servers(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".mcp.json")
            with open(p, "w") as f:
                json.dump({"mcpServers": {"other": {"command": "x"}}, "extra": 1}, f)
            kb_login.write_mcp_json(p, "1|abc", "https://kb.example.com/mcp")
            kb_login.write_mcp_json(p, "1|new", "https://kb.example.com/mcp")  # replaces the entry
            with open(p) as f:
                cfg = json.load(f)
            self.assertEqual(cfg["extra"], 1)
            self.assertEqual(cfg["mcpServers"]["other"], {"command": "x"})
            self.assertEqual(cfg["mcpServers"]["kb"], {
                "type": "http", "url": "https://kb.example.com/mcp",
                "headers": {"Authorization": "Bearer 1|new"}})

    def test_write_mcp_json_needs_a_kb_url(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(kb_login.LoginError):
                kb_login.write_mcp_json(os.path.join(d, ".mcp.json"), "1|abc", "")

    def test_write_mcp_json_new_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".mcp.json")
            kb_login.write_mcp_json(p, "1|abc", "https://kb.example.com/mcp")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_env_file_without_a_shell(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "kb-login.env")
            with open(p, "w") as f:
                f.write("# comment\nKB_LOGIN_API=https://api.example.com\n"
                        "KB_LOGIN_HEADERS='{\"app\":\"dev\"}'\nKB_URL=\"https://kb.example.com/mcp\"\n")
            env = {"KB_URL": "keep-me"}
            kb_login.load_env_file(p, env)
            self.assertEqual(env["KB_LOGIN_API"], "https://api.example.com")
            self.assertEqual(env["KB_LOGIN_HEADERS"], '{"app":"dev"}')
            self.assertEqual(env["KB_URL"], "keep-me")  # the environment wins over the file

    def test_token_file_is_private_and_feeds_headers_helper(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "kb", "token")
            kb_login.save_token(p, "1|abc")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(kb_login.headers_json(p)), {"Authorization": "Bearer 1|abc"})
            entry = kb_login.server_entry("https://kb.example.com/mcp", p)
            self.assertEqual(entry["type"], "http")
            self.assertNotIn("headers", entry)          # no token in the client config
            self.assertIn("--headers", entry["headersHelper"])
            self.assertIn(p, entry["headersHelper"])

    def test_headers_without_a_token_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kb_login.main(["--headers", "--token-file", os.path.join(d, "none")]), 1)

    def test_mcp_json_in_git_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            p = os.path.join(d, ".mcp.json")
            with mock.patch.object(kb_login, "login", return_value="1|abc"), \
                    mock.patch.object(kb_login.getpass, "getpass", return_value="pw"):
                rc = kb_login.main(["--api", "https://api.example.com", "--username", "u",
                                    "--kb-url", "https://kb.example.com/mcp", "--mcp-json", p])
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
