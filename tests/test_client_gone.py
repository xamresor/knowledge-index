#!/usr/bin/env python3
"""A client that closes its connection before the answer is written is not a fault of the server:
both servers log one line for it, and keep the traceback for every other exception."""
from __future__ import annotations

import contextlib
import io
import os
import unittest

from _kbtest import BIN

import mcp_rpc


def handle(exc: Exception) -> str:
    """The stderr text of QuietServer.handle_error while `exc` is the exception in flight."""
    srv = mcp_rpc.QuietServer.__new__(mcp_rpc.QuietServer)  # no socket: handle_error needs none
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            raise exc
        except Exception:  # noqa: BLE001
            srv.handle_error(None, ("127.0.0.1", 40000))
    return err.getvalue()


class ClientGone(unittest.TestCase):
    def test_a_closed_connection_is_one_line(self):
        for exc in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset"),
                    ConnectionAbortedError(103, "aborted")):
            out = handle(exc)
            self.assertNotIn("Traceback", out, exc)
            self.assertEqual(out.count("\n"), 1, out)
            self.assertIn("127.0.0.1", out)
            self.assertIn("closed the connection", out)

    def test_another_exception_keeps_its_traceback(self):
        out = handle(ValueError("boom"))
        self.assertIn("Traceback", out)
        self.assertIn("ValueError: boom", out)

    def test_both_servers_use_it(self):
        for name in ("kb-http", "kb_api.py"):
            with open(os.path.join(BIN, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("mcp_rpc.QuietServer", text, name)
            self.assertNotIn("ThreadingHTTPServer((", text, name)


if __name__ == "__main__":
    unittest.main()
