#!/usr/bin/env python3
"""HTTP adapter — so **scripts** (bash, python, CI, cron) can run the same search as agents do.

Same core as the MCP adapter (`kb_core.call_tool`), same schemas (`kb_core.TOOLS`), so the two
surfaces are identical by construction rather than by discipline.

    bin/kb-api                      # 127.0.0.1:8899, no token needed (loopback only)
    bin/kb-api --port 9000
    bin/kb-api --bind 0.0.0.0 --token secret   # a token is MANDATORY off loopback

Endpoints — the MCP shape plus ergonomic aliases:

    GET  /health                     -> {"ok": true}
    GET  /version                    -> {"repo", "api_contract", "collection", "aliases"}
    GET  /tools                      -> the same schema list MCP publishes
    POST /call        {name, arguments}          -> {"text": …}   (the generic MCP-shaped call)
    POST /search      {q, domain, scope, …}     -> {"text": …}
    POST /graph/path  {from, to}                -> {"text": …}
    POST /graph/explain   {symbol}              -> {"text": …}
    POST /graph/affected  {symbol}              -> {"text": …}
    GET  /doc?path=…                            -> {"text": …}
    GET  /  and  /web/…, /graphify-out/…        -> the dashboard, so its docs search can call /search

Serving the dashboard from here is what lets the page search docs: the page and the API share one
origin, so no CORS header is ever sent. A CORS header would let any website open in the same browser
read the index from 127.0.0.1 — the opposite of the point.

Why the security defaults are strict: this project's whole value is that the corpus never leaves the
machine. A knowledge index bound to the world without a token would be exactly the leak it exists to
prevent — so binding off loopback without a token is refused at startup, not warned about in a log.
For the same reason a tokenless server answers only requests addressed to a loopback name (the `Host`
header): otherwise a website could point its own domain at 127.0.0.1 (DNS rebinding) and read it.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import kb_core
import paths

LOOPBACK = ("127.0.0.1", "::1", "localhost")

# path -> tool name, for the ergonomic aliases. `/call` covers everything else.
ROUTES = {
    "/search": "search",
    "/graph/path": "graph_path",
    "/graph/explain": "graph_explain",
    "/graph/affected": "graph_affected",
}


# What the dashboard needs, and nothing more: the shell and the split viz data. Not graph.json.
STATIC_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
DATA_FILES = ("kb-manifest.js", "kb-status.js")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def check_bind(host: str, token: str | None) -> None:
    """Refuse an unauthenticated non-loopback bind. Called before the socket is opened."""
    if host not in LOOPBACK and not token:
        raise SystemExit(
            f"refusing to bind {host} without a token: this exposes the whole index.\n"
            f"Pass --token (or set KB_API_TOKEN), or bind 127.0.0.1 and tunnel over SSH."
        )


def authorized(host: str, token: str | None, header: str | None) -> bool:
    """Loopback needs no token (the process boundary is the trust boundary); anything else does."""
    if not token:
        return host in LOOPBACK
    if not header:
        return False
    supplied = header[7:] if header.lower().startswith("bearer ") else header
    return supplied == token


def host_allowed(token: str | None, host_header: str | None) -> bool:
    """Without a token the trust boundary is "a request to this machine by a loopback name". A request
    whose Host is some other domain reached us through DNS rebinding, so it is refused."""
    if token:
        return True
    name = urllib.parse.urlsplit("//" + (host_header or "")).hostname or ""
    return name in LOOPBACK


def static_file(path: str):
    """URL path -> a file of the dashboard, or None. Only the shell and the split viz data are served;
    the resolved file must stay inside its root, so `..` cannot walk out of it."""
    roots = {"/web/": paths.web_staged(), "/graphify-out/": paths.out_dir()}
    for prefix, root in roots.items():
        if not path.startswith(prefix):
            continue
        rel = urllib.parse.unquote(path[len(prefix):])
        if prefix == "/graphify-out/" and not (rel in DATA_FILES or rel.startswith("kb-graph-data/")):
            return None
        target = (root / rel).resolve()
        if root.resolve() not in target.parents or target.suffix not in STATIC_TYPES:
            return None
        return target if target.is_file() else None
    return None


def dispatch(method: str, path: str, query: dict, body: dict) -> dict:
    """Pure routing: (request) -> payload. No sockets, so this is what the tests exercise."""
    if method == "GET" and path == "/health":
        return {"ok": True}
    if method == "GET" and path == "/version":
        return kb_core.version_info()
    if method == "GET" and path == "/tools":
        return {"tools": kb_core.TOOLS}
    if method == "GET" and path == "/doc":
        return {"text": kb_core.call_tool("docs_get", {"path": query.get("path", "")})}
    if method == "POST" and path == "/call":
        name = body.get("name", "")
        return {"text": kb_core.call_tool(name, body.get("arguments", {}) or {})}
    if method == "POST" and path in ROUTES:
        return {"text": kb_core.call_tool(ROUTES[path], body)}
    raise ApiError(404, f"no route for {method} {path}")


def _json_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError(400, f"invalid JSON body: {e}") from e
    if not isinstance(parsed, dict):
        raise ApiError(400, "body must be a JSON object")
    return parsed


def make_handler(host: str, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"kb-api/{kb_core.VERSION}"

        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> bool:
            """Serve a dashboard file if `path` names one. The page carries no secrets and loopback is
            the trust boundary, so it needs no token; its /search calls do, when one is set."""
            target = static_file(path)
            if target is None:
                return False
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES[target.suffix] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        def _handle(self, method: str) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                if not host_allowed(token, self.headers.get("Host")):
                    raise ApiError(403, "not addressed to a loopback name; pass --token to serve others")
                if method == "GET" and parsed.path in ("", "/"):
                    # a redirect, not the page itself: the shell's relative paths assume /web/
                    self.send_response(302)
                    self.send_header("Location", "/web/index.html")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "GET" and self._static(parsed.path):
                    return
                if not authorized(host, token, self.headers.get("Authorization")):
                    raise ApiError(401, "missing or invalid token")
                length = int(self.headers.get("Content-Length") or 0)
                body = _json_body(self.rfile.read(length) if length else b"")
                self._reply(200, dispatch(method, parsed.path.rstrip("/") or "/", query, body))
            except kb_core.ToolError as e:
                self._reply(400, {"error": str(e)})
            except ApiError as e:
                self._reply(e.status, {"error": e.message})
            except Exception as e:  # noqa: BLE001
                self._reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def log_message(self, fmt: str, *args) -> None:
            # One line per request on stderr; quiet enough for a foreground `make serve`.
            print(f"[kb-api] {self.address_string()} {fmt % args}", flush=True)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Knowledge-index HTTP API (same core as the MCP server)")
    ap.add_argument("--bind", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--token", default=os.environ.get("KB_API_TOKEN"),
                    help="bearer token; REQUIRED for any non-loopback bind (env: KB_API_TOKEN)")
    args = ap.parse_args(argv)

    check_bind(args.bind, args.token)
    kb_core.warm_qmd()
    httpd = ThreadingHTTPServer((args.bind, args.port), make_handler(args.bind, args.token))
    auth = "token required" if args.token else "loopback only, no token"
    paths.stage_web()   # installed mode: put the shell next to the data, as `kb open` does
    print(f"kb-api {kb_core.VERSION} (contract {kb_core.API_CONTRACT}) on "
          f"http://{args.bind}:{args.port} — {auth} · dashboard: http://{args.bind}:{args.port}/",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nkb-api stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
