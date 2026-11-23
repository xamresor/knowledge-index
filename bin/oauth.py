#!/usr/bin/env python3
"""MCP OAuth for bin/kb-http: an MCP client (Claude Code) signs in through the browser by itself.

Without it a developer runs bin/kb-login in a terminal and a client that tries OAuth after a 401
fails with "could not connect". With it, `/mcp` -> `kb` -> Authenticate opens a login page; the
person types the user name, password and code; the login webhook (access protocol v1) checks them and
gives the token, which the client keeps as its access token. kb-http checks it with the permission
webhook, so access rules do not change.

What it implements (MCP authorization spec 2025-06-18):

  GET  /.well-known/oauth-protected-resource      RFC 9728: which authorization server to ask
  GET  /.well-known/oauth-authorization-server    RFC 8414: the endpoints below
  POST /register                                  RFC 7591: dynamic client registration
  GET  /authorize, POST /authorize                the login page (password, then the 2FA code)
  POST /token                                     authorization_code (PKCE S256) and refresh_token

Deliberate limits:
  * Public clients only, PKCE S256 required, redirect URIs on loopback only (a desktop client's
    local callback). A page elsewhere cannot get a code sent to it.
  * The access token *is* the API token. A refresh returns it again while the API still accepts it;
    once it expired, the client sends the person through the browser again.
  * Codes, login transactions and refresh tokens live in memory (a restart means one more browser
    sign-in). Registered clients are kept in a file: they hold no secret.
  * Enrolling a new authenticator is not done here (it shows a secret): the page says to use the app
    or bin/kb-login once.
"""
from __future__ import annotations

import base64
import hashlib
import html
import importlib.machinery
import importlib.util
import json
import os
import secrets
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_TTL = 300          # authorization codes and login transactions, seconds
TOKEN_TTL = int(os.environ.get("KB_OAUTH_TOKEN_TTL", str(8 * 3600)))  # what expires_in reports
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")


def _load_kb_login():
    loader = importlib.machinery.SourceFileLoader("kb_login_for_oauth", os.path.join(HERE, "kb-login"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


kb_login = _load_kb_login()


class OAuthError(Exception):
    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status, self.error, self.description = status, error, description


def b64url_sha256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def is_loopback_redirect(uri: str) -> bool:
    try:
        u = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    return u.scheme == "http" and (u.hostname or "") in LOOPBACK and not u.fragment


class Server:
    """State and logic; the HTTP adapter in kb-http turns requests into calls to `handle`."""

    def __init__(self, login_env: dict, check_token, clients_file: str = "", now=time.time):
        self.env = login_env                 # KB_LOGIN_URL, KB_HOOK_HEADERS (docs/access-protocol.md)
        self.check_token = check_token       # token -> raises when the API no longer accepts it
        self.clients_file = clients_file
        self.now = now
        self.on_event = None                 # (user, action, detail) -> None: the audit log, set by kb-http
        self.lock = threading.Lock()
        self.clients: dict[str, dict] = self._load_clients()
        self.txns: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}

    # ------------------------------------------------------------------ storage
    def _load_clients(self) -> dict:
        try:
            with open(self.clients_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_clients(self) -> None:
        if not self.clients_file:
            return
        os.makedirs(os.path.dirname(self.clients_file) or ".", exist_ok=True)
        tmp = f"{self.clients_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.clients, f)
        os.replace(tmp, self.clients_file)

    def _expire(self) -> None:
        t = self.now()
        for table in (self.txns, self.codes):
            for k in [k for k, v in table.items() if v["exp"] < t]:
                del table[k]

    # ------------------------------------------------------------------ metadata
    @staticmethod
    def resource_metadata(base: str) -> dict:
        return {"resource": f"{base}/mcp", "authorization_servers": [base],
                "bearer_methods_supported": ["header"]}

    @staticmethod
    def server_metadata(base: str) -> dict:
        return {"issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"]}

    # ------------------------------------------------------------------ registration
    def register(self, body: dict) -> dict:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise OAuthError(400, "invalid_redirect_uri", "redirect_uris is required")
        if not all(is_loopback_redirect(u) for u in uris):
            raise OAuthError(400, "invalid_redirect_uri", "only http loopback redirect URIs are allowed")
        client_id = "kb-" + secrets.token_urlsafe(16)
        client = {"client_id": client_id, "redirect_uris": uris,
                  "client_name": str(body.get("client_name") or "")[:100],
                  "client_id_issued_at": int(self.now()),
                  "token_endpoint_auth_method": "none",
                  "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"]}
        with self.lock:
            self.clients[client_id] = client
            self._save_clients()
        return client

    # ------------------------------------------------------------------ authorize
    def start(self, q: dict) -> str:
        """Check the authorize request; returns a transaction id for the login page."""
        client = self.clients.get(q.get("client_id", ""))
        if not client:
            raise OAuthError(400, "invalid_client", "unknown client_id: register first")
        redirect = q.get("redirect_uri", "")
        if redirect not in client["redirect_uris"] and not (
                is_loopback_redirect(redirect) and _same_app(redirect, client["redirect_uris"])):
            raise OAuthError(400, "invalid_request", "redirect_uri is not registered for this client")
        if q.get("response_type") != "code":
            raise OAuthError(400, "unsupported_response_type", "response_type must be code")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            raise OAuthError(400, "invalid_request", "PKCE with S256 is required")
        txn = secrets.token_urlsafe(24)
        with self.lock:
            self._expire()
            self.txns[txn] = {"client_id": client["client_id"], "redirect_uri": redirect,
                              "state": q.get("state", ""), "challenge": q["code_challenge"],
                              "exp": self.now() + CODE_TTL, "api_challenge": None}
        return txn

    def password_step(self, txn_id: str, username: str, password: str) -> tuple[str, str | None]:
        """-> ("code", None) when a code is needed next, ("done", redirect URL) when signed in."""
        txn = self._txn(txn_id)
        txn["user"] = username
        try:
            data = kb_login.post(self.env["KB_LOGIN_URL"], {"username": username, "password": password},
                                 headers=kb_login.hook_headers(self.env))
        except kb_login.LoginError as e:
            self._event(username, "sign-in refused", str(e)[:200])
            raise
        challenge = (data.get("two_factor") or {}).get("challenge")
        if challenge:
            txn["api_challenge"] = challenge
            return "code", None
        return "done", self._finish(txn_id, data.get("token"))

    def code_step(self, txn_id: str, code: str) -> str:
        txn = self._txn(txn_id)
        if not txn.get("api_challenge"):
            raise OAuthError(400, "invalid_request", "no second step pending")
        try:
            data = kb_login.post(self.env["KB_LOGIN_URL"], {"challenge": txn["api_challenge"], "code": code.strip()},
                                 headers=kb_login.hook_headers(self.env))
        except kb_login.LoginError as e:
            self._event(txn.get("user", "?"), "sign-in refused", f"code: {str(e)[:180]}")
            raise
        return self._finish(txn_id, data.get("token"))

    def _txn(self, txn_id: str) -> dict:
        with self.lock:
            self._expire()
            txn = self.txns.get(txn_id)
        if not txn:
            raise OAuthError(400, "invalid_request", "the sign-in took too long: start it again")
        return txn

    def _finish(self, txn_id: str, token: str | None) -> str:
        if not token:
            raise kb_login.LoginError("the API answered without a token")
        with self.lock:
            txn = self.txns.pop(txn_id, None)
            if not txn:
                raise OAuthError(400, "invalid_request", "the sign-in took too long: start it again")
            code = secrets.token_urlsafe(32)
            self.codes[code] = {"token": token, "client_id": txn["client_id"],
                                "redirect_uri": txn["redirect_uri"], "challenge": txn["challenge"],
                                "exp": self.now() + CODE_TTL, "user": txn.get("user", "")}
        self._seen(txn["client_id"], txn.get("user", ""))
        client = self.clients.get(txn["client_id"], {})
        self._event(txn.get("user", "?"), "browser sign-in", str(client.get("client_name") or txn["client_id"])[:80])
        query = {"code": code}
        if txn["state"]:
            query["state"] = txn["state"]
        sep = "&" if "?" in txn["redirect_uri"] else "?"
        return txn["redirect_uri"] + sep + urllib.parse.urlencode(query)

    def _seen(self, client_id: str, user: str) -> None:
        """Note who signed in through a client and when (shown on the admin clients page). A client
        registers before anyone signs in, so its record has no user until this."""
        if not user:
            return
        with self.lock:
            client = self.clients.get(client_id)
            if client is None:
                return
            client.setdefault("users", {})[user] = int(self.now())
            try:
                self._save_clients()
            except OSError:
                pass

    def _event(self, user: str, action: str, detail: str = "") -> None:
        if self.on_event:
            try:
                self.on_event(user, action, detail)
            except Exception:  # noqa: BLE001 — the audit log never breaks a sign-in
                pass

    def revoke(self, client_id: str) -> bool:
        """Forget a client and its refresh tokens (admin page). False when there is no such client."""
        with self.lock:
            if client_id not in self.clients:
                return False
            del self.clients[client_id]
            for rt in [rt for rt, v in self.refresh.items() if v["client_id"] == client_id]:
                del self.refresh[rt]
            for code in [c for c, v in self.codes.items() if v["client_id"] == client_id]:
                del self.codes[code]
            self._save_clients()
        return True

    # ------------------------------------------------------------------ token
    def token(self, form: dict) -> dict:
        grant = form.get("grant_type")
        if grant == "authorization_code":
            with self.lock:
                self._expire()
                entry = self.codes.pop(form.get("code", ""), None)  # single use, even on failure
            if not entry:
                raise OAuthError(400, "invalid_grant", "unknown or expired code")
            if form.get("client_id") and form["client_id"] != entry["client_id"]:
                raise OAuthError(400, "invalid_grant", "code was issued to another client")
            if form.get("redirect_uri", entry["redirect_uri"]) != entry["redirect_uri"]:
                raise OAuthError(400, "invalid_grant", "redirect_uri does not match")
            if b64url_sha256(form.get("code_verifier", "")) != entry["challenge"]:
                raise OAuthError(400, "invalid_grant", "PKCE verification failed")
            return self._issue(entry["token"], entry["client_id"], entry.get("user", ""))
        if grant == "refresh_token":
            with self.lock:
                entry = self.refresh.pop(form.get("refresh_token", ""), None)  # rotated on use
            if not entry:
                raise OAuthError(400, "invalid_grant", "unknown refresh token")
            if form.get("client_id") and form["client_id"] != entry["client_id"]:
                raise OAuthError(400, "invalid_grant", "refresh token was issued to another client")
            try:
                self.check_token(entry["token"])
            except Exception:  # noqa: BLE001 — expired or revoked: sign in again
                raise OAuthError(400, "invalid_grant", "the API token expired: sign in again") from None
            self._seen(entry["client_id"], entry.get("user", ""))
            return self._issue(entry["token"], entry["client_id"], entry.get("user", ""))
        raise OAuthError(400, "unsupported_grant_type", "use authorization_code or refresh_token")

    def _issue(self, token: str, client_id: str, user: str = "") -> dict:
        rt = secrets.token_urlsafe(32)
        with self.lock:
            self.refresh[rt] = {"token": token, "client_id": client_id, "user": user}
        return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL,
                "refresh_token": rt}


def _same_app(redirect: str, registered: list[str]) -> bool:
    """A loopback client may pick a new port each run (RFC 8252 §7.3): same host and path count."""
    r = urllib.parse.urlsplit(redirect)
    for u in registered:
        g = urllib.parse.urlsplit(u)
        if (g.hostname, g.path) == (r.hostname, r.path):
            return True
    return False


# ---------------------------------------------------------------------- the login page
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in to the KB</title>
<style>body{{font:16px system-ui,sans-serif;background:#f5f5f4;color:#1c1917;margin:0}}
main{{max-width:22rem;margin:12vh auto;padding:2rem;background:#fff;border-radius:12px;
box-shadow:0 1px 3px #0002}}h1{{font-size:1.2rem;margin:0 0 .3rem}}p{{color:#57534e;margin:.2rem 0 1.2rem}}
label{{display:block;font-size:.9rem;margin:.8rem 0 .3rem}}input{{width:100%;box-sizing:border-box;
padding:.6rem;border:1px solid #d6d3d1;border-radius:8px;font-size:1rem}}button{{margin-top:1.2rem;
width:100%;padding:.7rem;border:0;border-radius:8px;background:#1c1917;color:#fff;font-size:1rem}}
.err{{color:#b91c1c;font-size:.9rem}}</style></head><body><main>
<h1>Code knowledge base</h1><p>{lead}</p>{error}
<form method="post" action="{action}"><input type="hidden" name="txn" value="{txn}">{fields}
<button type="submit">{button}</button></form></main></body></html>"""


def page(action: str, txn: str, step: str, error: str = "") -> bytes:
    if step == "code":
        lead = "Enter the code from your authenticator app (or a recovery code)."
        fields = ('<input type="hidden" name="step" value="code"><label for="c">Code</label>'
                  '<input id="c" name="code" autocomplete="one-time-code" inputmode="numeric" autofocus required>')
        button = "Confirm"
    else:
        lead = "Sign in with your user name and password of the API."
        fields = ('<input type="hidden" name="step" value="password"><label for="u">User name</label>'
                  '<input id="u" name="username" autocomplete="username" autofocus required>'
                  '<label for="p">Password</label>'
                  '<input id="p" name="password" type="password" autocomplete="current-password" required>')
        button = "Sign in"
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return PAGE.format(lead=lead, error=err, action=html.escape(action), txn=html.escape(txn),
                       fields=fields, button=button).encode()
