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
  * The access token is a token of this server (`kbo_…`) that stands for the token of the login webhook
    and ends after KB_OAUTH_TOKEN_TTL (8 hours), the `expires_in` the client is told. kb-http swaps it for
    the token behind it and checks that with the permission webhook. A refresh gives a new access token
    while the permission webhook still accepts the token behind it; once that ended, the client sends the
    person through the browser again. The access token a refresh replaces ends a minute later, a client
    keeps at most KB_OAUTH_TOKENS_PER_CLIENT (50) access and refresh tokens, and the tokens, codes and
    sign-ins of a revoked client stop at once.
  * Codes, login transactions, access and refresh tokens live in memory (a restart means one more
    browser sign-in). Registered clients are kept in a file: they hold no secret.
  * /register needs no sign-in, so everything a stranger can make has a limit (environment, defaults):
    KB_OAUTH_MAX_CLIENTS (1000) clients, KB_OAUTH_MAX_URIS (10) redirect URIs per client of at most
    KB_OAUTH_MAX_URI_LENGTH (512) characters, KB_OAUTH_MAX_TXNS (1000) sign-ins in progress, of them
    KB_OAUTH_TXNS_PER_ADDRESS (10) of one client address. A full list pushes out the oldest sign-in of the
    address that has the most, or one older than a minute, never a fresh sign-in of another person; a client
    that nobody signed in with goes after KB_OAUTH_CLIENT_DAYS (1), and first when the list is full; a refresh token ends after
    KB_OAUTH_REFRESH_DAYS (30). kb-http limits registrations per address (admin.register_throttle).
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
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_TTL = 300          # authorization codes and login transactions, seconds


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


TOKEN_TTL = _env_int("KB_OAUTH_TOKEN_TTL", 8 * 3600)    # an access token ends after this; expires_in says so
ACCESS_PREFIX = "kbo_"                                    # the access tokens of this server
# Limits: /register needs no sign-in, so nothing a stranger sends may grow without end.
MAX_CLIENTS = _env_int("KB_OAUTH_MAX_CLIENTS", 1000)     # registered clients in all
MAX_URIS = _env_int("KB_OAUTH_MAX_URIS", 10)             # redirect URIs of one client
MAX_URI_LENGTH = _env_int("KB_OAUTH_MAX_URI_LENGTH", 512)
MAX_TXNS = _env_int("KB_OAUTH_MAX_TXNS", 1000)           # sign-ins in progress (GET /authorize)
CLIENT_DAYS = _env_int("KB_OAUTH_CLIENT_DAYS", 1)        # a client nobody signed in with goes after this
TXNS_PER_CLIENT = _env_int("KB_OAUTH_TXNS_PER_CLIENT", 20)  # sign-ins in progress of one client
TXNS_PER_ADDRESS = _env_int("KB_OAUTH_TXNS_PER_ADDRESS", 10)  # sign-ins in progress of one client address
TXN_FRESH = 60       # a sign-in younger than this never gives way to another address
REFRESH_DAYS = _env_int("KB_OAUTH_REFRESH_DAYS", 30)     # a refresh token ends after this time
ROTATE_OVERLAP = 60  # seconds the access token a refresh replaced still works (requests in flight)
TOKENS_PER_CLIENT = _env_int("KB_OAUTH_TOKENS_PER_CLIENT", 50)  # live access and refresh tokens of one client
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
    """An http URI on this computer, in its plain form. A backslash, a user part, a control character or
    a space is refused: Python and a browser can read such a URI as two different hosts
    (`http://evil.example\\@localhost/` is localhost for Python and evil.example for a browser)."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH:
        return False
    if "\\" in uri or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in uri) or not uri.isascii():
        return False
    try:
        u = urllib.parse.urlsplit(uri)
        port = u.port
    except ValueError:
        return False
    if u.scheme != "http" or u.fragment or "@" in u.netloc or u.username is not None:
        return False
    host = (u.hostname or "").lower()
    if host not in LOOPBACK:
        return False
    # The authority must be exactly host[:port]: nothing else may hide in it.
    shown = f"[{host}]" if ":" in host else host
    return u.netloc.lower() == (shown if port is None and not u.netloc.endswith(":") else f"{shown}:{port}")


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
        self.access: dict[str, dict] = {}    # SHA-256 of an access token -> the token behind it, client, end
        self.headers: dict[str, str] = {}    # extra headers for the login webhook (kb-http: its self-call header)

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
        folder = os.path.dirname(self.clients_file) or "."
        os.makedirs(folder, exist_ok=True)
        # A temporary file of its own: a second process (or a test) on the same file must not rename ours away.
        fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(self.clients_file)}.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.clients, f)
            os.replace(tmp, self.clients_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _expire(self) -> None:
        t = self.now()
        for table in (self.txns, self.codes, self.refresh, self.access):
            for k in [k for k, v in table.items() if v.get("exp", float("inf")) < t]:
                del table[k]

    def _prune_clients(self) -> bool:
        """Drop the clients that registered more than CLIENT_DAYS ago and never had a sign-in. -> changed"""
        cut = self.now() - CLIENT_DAYS * 86400
        old = [cid for cid, c in self.clients.items()
               if not c.get("users") and (c.get("client_id_issued_at") or 0) < cut]
        for cid in old:
            del self.clients[cid]
        return bool(old)

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
        if len(uris) > MAX_URIS:
            raise OAuthError(400, "invalid_redirect_uri", f"{MAX_URIS} redirect URIs at most")
        if not all(is_loopback_redirect(u) for u in uris):
            raise OAuthError(400, "invalid_redirect_uri", "only plain http loopback redirect URIs are allowed "
                                                          f"(at most {MAX_URI_LENGTH} characters)")
        client_id = "kb-" + secrets.token_urlsafe(16)
        client = {"client_id": client_id, "redirect_uris": uris,
                  "client_name": str(body.get("client_name") or "")[:100],
                  "client_id_issued_at": int(self.now()),
                  "token_endpoint_auth_method": "none",
                  "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"]}
        with self.lock:
            changed = self._prune_clients()
            if len(self.clients) >= MAX_CLIENTS:   # full: the oldest client nobody signed in with goes
                unused = sorted((c.get("client_id_issued_at") or 0, cid) for cid, c in self.clients.items()
                                if not c.get("users"))
                if unused:
                    del self.clients[unused[0][1]]
                    changed = True
            if len(self.clients) >= MAX_CLIENTS:
                if changed:
                    self._save_clients()
                raise OAuthError(503, "temporarily_unavailable", "too many registered clients: ask an admin")
            self.clients[client_id] = client
            try:
                self._save_clients()
            except OSError:
                del self.clients[client_id]   # not on disk: not registered (kb-http answers 503)
                raise OAuthError(503, "temporarily_unavailable", "the client could not be saved: try again later") \
                    from None
        return client

    # ------------------------------------------------------------------ authorize
    def start(self, q: dict, ip: str = "") -> str:
        """Check the authorize request; returns a transaction id for the login page. `ip` is the client
        address: a flood from one address or many must not push out a person's sign-in."""
        client = self.clients.get(q.get("client_id", ""))
        if not client:
            raise OAuthError(400, "invalid_client", "unknown client_id: register first")
        redirect = q.get("redirect_uri", "")
        if not is_loopback_redirect(redirect) or not (
                redirect in client["redirect_uris"] or _same_app(redirect, client["redirect_uris"])):
            raise OAuthError(400, "invalid_request", "redirect_uri is not registered for this client")
        if q.get("response_type") != "code":
            raise OAuthError(400, "unsupported_response_type", "response_type must be code")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            raise OAuthError(400, "invalid_request", "PKCE with S256 is required")
        txn = secrets.token_urlsafe(24)
        with self.lock:
            self._expire()
            # Full: room is made only at the cost of the flood. An address over its own limit loses its own
            # oldest sign-in; a full client or a full list loses one of the busiest address, or one older
            # than TXN_FRESH. A fresh sign-in of another person never goes: then this request is refused.
            own = sorted((t["created"], k) for k, t in self.txns.items() if t.get("ip") == ip)
            if len(own) >= TXNS_PER_ADDRESS > 0:
                del self.txns[own[0][1]]
            same = [k for k, t in self.txns.items() if t["client_id"] == client["client_id"]]
            if (len(same) >= TXNS_PER_CLIENT and not self._make_room(same, ip)) or \
                    (len(self.txns) >= MAX_TXNS and not self._make_room(list(self.txns), ip)):
                raise OAuthError(503, "temporarily_unavailable", "too many sign-ins in progress: try again in a minute")
            now = self.now()
            self.txns[txn] = {"client_id": client["client_id"], "redirect_uri": redirect,
                              "state": q.get("state", ""), "challenge": q["code_challenge"], "ip": ip,
                              "created": now, "exp": now + CODE_TTL, "api_challenge": None}
        return txn

    def _make_room(self, keys: list[str], ip: str) -> bool:
        """Drop one sign-in of `keys`: the oldest of this address, else the oldest of the address with the
        most (two or more: more than one person makes), else the oldest one older than TXN_FRESH. False
        when every one is the fresh sign-in of another address. The caller holds the lock."""
        rows = sorted((self.txns[k]["created"], k, self.txns[k].get("ip", "")) for k in keys)
        if not rows:
            return False
        mine = [k for _, k, a in rows if a == ip]
        if not mine:
            count: dict[str, int] = {}
            for _, _, a in rows:
                count[a] = count.get(a, 0) + 1
            busiest = max(count, key=count.get)
            if count[busiest] >= 2:
                mine = [k for _, k, a in rows if a == busiest]
            else:
                mine = [k for created, k, _ in rows if created < self.now() - TXN_FRESH]
        if not mine:
            return False
        del self.txns[mine[0]]
        return True

    def password_step(self, txn_id: str, username: str, password: str) -> tuple[str, str | None]:
        """-> ("code", None) when a code is needed next, ("done", redirect URL) when signed in."""
        txn = self._txn(txn_id)
        txn["user"] = username
        try:
            data = kb_login.post(self.env["KB_LOGIN_URL"], {"username": username, "password": password},
                                 headers=self._hook_headers(txn))
        except kb_login.LoginError as e:
            self._event(username, "sign-in refused", str(e)[:200])
            raise
        challenge = kb_login.challenge_of(data)
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
                                 headers=self._hook_headers(txn))
        except kb_login.LoginError as e:
            self._event(txn.get("user", "?"), "sign-in refused", f"code: {str(e)[:180]}")
            raise
        return self._finish(txn_id, data.get("token"))

    def _hook_headers(self, txn: dict) -> dict:
        """KB_HOOK_HEADERS, and the headers kb-http adds when the login webhook is kb-http itself: its
        self-call header and the address of the person, so that the webhook counts that address."""
        out = {**kb_login.hook_headers(self.env), **self.headers}
        if self.headers and txn.get("ip"):
            out["X-Forwarded-For"] = txn["ip"]
        return out

    def _txn(self, txn_id: str) -> dict:
        with self.lock:
            self._expire()
            txn = self.txns.get(txn_id)
        if not txn:
            raise OAuthError(400, "invalid_request", "the sign-in took too long: start it again")
        return txn

    def _finish(self, txn_id: str, token: str | None) -> str:
        if not isinstance(token, str) or not token:
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
        """Forget a client, its sign-ins in progress, codes and tokens (admin page). False when there is no
        such client."""
        with self.lock:
            if client_id not in self.clients:
                return False
            del self.clients[client_id]
            for txn in [t for t, v in self.txns.items() if v["client_id"] == client_id]:
                del self.txns[txn]
            for rt in [rt for rt, v in self.refresh.items() if v["client_id"] == client_id]:
                del self.refresh[rt]
            for code in [c for c, v in self.codes.items() if v["client_id"] == client_id]:
                del self.codes[code]
            for at in [at for at, v in self.access.items() if v["client_id"] == client_id]:
                del self.access[at]
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
            rt = form.get("refresh_token", "")
            with self.lock:
                self._expire()
                entry = self.refresh.pop(rt, None)  # rotated on use
            if not entry:
                raise OAuthError(400, "invalid_grant", "unknown refresh token")
            if form.get("client_id") and form["client_id"] != entry["client_id"]:
                raise OAuthError(400, "invalid_grant", "refresh token was issued to another client")
            try:
                self.check_token(entry["token"])
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status", None) in (401, 403):  # expired or revoked: sign in again
                    raise OAuthError(400, "invalid_grant", "the API token expired: sign in again") from None
                # The permission webhook is down (503) or broke: the refresh token stays for the next try.
                with self.lock:
                    if entry["client_id"] in self.clients:
                        self.refresh[rt] = entry
                raise OAuthError(503, "temporarily_unavailable",
                                 "the sign-in service is not available: try again later") from None
            self._seen(entry["client_id"], entry.get("user", ""))
            return self._issue(entry["token"], entry["client_id"], entry.get("user", ""), replaces=entry.get("at"))
        raise OAuthError(400, "unsupported_grant_type", "use authorization_code or refresh_token")

    def _issue(self, token: str, client_id: str, user: str = "", replaces: str | None = None) -> dict:
        """A new access token that ends after TOKEN_TTL, and a refresh token. The map keeps only the
        SHA-256 of the access token. The client must still exist under the lock: one revoked while the
        refresh asked the permission webhook gets nothing. `replaces` is the SHA-256 of the access token
        a refresh supersedes: it ends after ROTATE_OVERLAP. A client keeps at most TOKENS_PER_CLIENT
        access and refresh tokens; the oldest go first."""
        rt = secrets.token_urlsafe(32)
        at = ACCESS_PREFIX + secrets.token_urlsafe(32)
        with self.lock:
            self._expire()
            if client_id not in self.clients:
                raise OAuthError(400, "invalid_grant", "the client was revoked: sign in again")
            now = self.now()
            old = self.access.get(replaces or "")
            if old is not None:
                old["exp"] = min(old["exp"], now + ROTATE_OVERLAP)
            for table in (self.access, self.refresh):
                mine = sorted((v["exp"], k) for k, v in table.items() if v["client_id"] == client_id)
                for _, k in mine[:max(0, len(mine) - TOKENS_PER_CLIENT + 1)]:
                    del table[k]
            self.refresh[rt] = {"token": token, "client_id": client_id, "user": user, "at": _sha(at),
                                "exp": now + REFRESH_DAYS * 86400}
            self.access[_sha(at)] = {"token": token, "client_id": client_id, "exp": now + TOKEN_TTL}
        return {"access_token": at, "token_type": "Bearer", "expires_in": TOKEN_TTL,
                "refresh_token": rt}

    def resolve(self, access_token: str) -> str | None:
        """The login webhook's token behind an access token of this server, or None when it is unknown,
        ended or its client was revoked."""
        with self.lock:
            entry = self.access.get(_sha(access_token))
            if entry is None or entry["exp"] < self.now() or entry["client_id"] not in self.clients:
                return None
            return entry["token"]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
        lead = "Sign in with your user name and password."
        fields = ('<input type="hidden" name="step" value="password"><label for="u">User name</label>'
                  '<input id="u" name="username" autocomplete="username" autofocus required>'
                  '<label for="p">Password</label>'
                  '<input id="p" name="password" type="password" autocomplete="current-password" required>')
        button = "Sign in"
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return PAGE.format(lead=lead, error=err, action=html.escape(action), txn=html.escape(txn),
                       fields=fields, button=button).encode()
