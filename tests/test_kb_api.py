"""Tests for bin/kb_api.py — the HTTP adapter.

Two things are worth locking. First, **routing is a pure function** (`dispatch`), so the surface can
be tested without opening a socket — that is why the logic lives outside the request handler.

Second, and more important, the **security policy**: this project's value is that the corpus never
leaves the machine, so binding off loopback without a token must be refused at startup rather than
warned about in a log. A future "just let me demo it quickly" change would have to delete a test.
"""
from __future__ import annotations

import unittest
from unittest import mock

from _kbtest import BIN  # noqa: F401  (puts bin/ on sys.path)

import kb_api
import kb_core


class BindPolicyTest(unittest.TestCase):
    def test_loopback_without_a_token_is_allowed(self):
        kb_api.check_bind("127.0.0.1", None)   # must not raise
        kb_api.check_bind("::1", None)
        kb_api.check_bind("localhost", None)

    def test_public_bind_without_a_token_is_refused(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.subTest(host=host), self.assertRaises(SystemExit) as ctx:
                kb_api.check_bind(host, None)
            self.assertIn("token", str(ctx.exception).lower())

    def test_public_bind_with_a_token_is_allowed(self):
        kb_api.check_bind("0.0.0.0", "secret")  # must not raise


class AuthTest(unittest.TestCase):
    def test_loopback_needs_no_header_when_no_token_is_configured(self):
        self.assertTrue(kb_api.authorized("127.0.0.1", None, None))

    def test_non_loopback_without_a_token_configured_is_never_authorized(self):
        """Defence in depth: even if check_bind were bypassed, requests still do not pass."""
        self.assertFalse(kb_api.authorized("0.0.0.0", None, None))

    def test_token_is_required_once_configured_even_on_loopback(self):
        self.assertFalse(kb_api.authorized("127.0.0.1", "secret", None))
        self.assertTrue(kb_api.authorized("127.0.0.1", "secret", "Bearer secret"))

    def test_bearer_prefix_is_optional_and_case_insensitive(self):
        self.assertTrue(kb_api.authorized("127.0.0.1", "secret", "secret"))
        self.assertTrue(kb_api.authorized("127.0.0.1", "secret", "bearer secret"))

    def test_wrong_token_fails(self):
        self.assertFalse(kb_api.authorized("127.0.0.1", "secret", "Bearer nope"))


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.call = mock.patch.object(kb_core, "call_tool", return_value="OUT").start()
        self.addCleanup(mock.patch.stopall)

    def test_health_and_version_need_no_core(self):
        self.assertEqual(kb_api.dispatch("GET", "/health", {}, {}), {"ok": True})
        info = kb_api.dispatch("GET", "/version", {}, {})
        self.assertIn("repo", info)
        self.assertIn("api_contract", info)

    def test_tools_publishes_the_same_list_mcp_does(self):
        payload = kb_api.dispatch("GET", "/tools", {}, {})
        self.assertIs(payload["tools"], kb_core.TOOLS,
                      "identity by construction: the same object, not a copy")

    def test_generic_call_is_the_mcp_shape(self):
        out = kb_api.dispatch("POST", "/call", {}, {"name": "search", "arguments": {"q": "x"}})
        self.assertEqual(out, {"text": "OUT"})
        self.call.assert_called_once_with("search", {"q": "x"})

    def test_ergonomic_aliases_map_to_tools(self):
        for path, tool in kb_api.ROUTES.items():
            with self.subTest(path=path):
                self.call.reset_mock()
                kb_api.dispatch("POST", path, {}, {"q": "x"})
                self.assertEqual(self.call.call_args[0][0], tool)

    def test_doc_takes_its_path_from_the_query_string(self):
        kb_api.dispatch("GET", "/doc", {"path": "a/b.md"}, {})
        self.call.assert_called_once_with("docs_get", {"path": "a/b.md"})

    def test_unknown_route_is_404(self):
        with self.assertRaises(kb_api.ApiError) as ctx:
            kb_api.dispatch("GET", "/nope", {}, {})
        self.assertEqual(ctx.exception.status, 404)

    def test_method_matters(self):
        with self.assertRaises(kb_api.ApiError):
            kb_api.dispatch("GET", "/search", {}, {})

    def test_tool_errors_propagate_for_the_handler_to_turn_into_400(self):
        self.call.side_effect = kb_core.ToolError("search: 'q' is required")
        with self.assertRaises(kb_core.ToolError):
            kb_api.dispatch("POST", "/search", {}, {})


class BodyParsingTest(unittest.TestCase):
    def test_empty_body_is_an_empty_dict(self):
        self.assertEqual(kb_api._json_body(b""), {})

    def test_invalid_json_is_a_400_not_a_500(self):
        with self.assertRaises(kb_api.ApiError) as ctx:
            kb_api._json_body(b"{nope")
        self.assertEqual(ctx.exception.status, 400)

    def test_a_json_array_is_rejected(self):
        with self.assertRaises(kb_api.ApiError):
            kb_api._json_body(b"[1,2]")


if __name__ == "__main__":
    unittest.main()
