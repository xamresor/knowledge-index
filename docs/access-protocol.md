# Access protocol v1

The shared server (`kb-http`) keeps no users and no passwords. It asks your system two questions over
HTTP, through two webhooks you provide:

1. **Login** — "here are a user name and a password (and later a code): give me a token".
2. **Permissions** — "here is a token and the list of my projects: who is this, and which of these may
   they see?"

Everything else (who has which role, how the second factor works, how long a token lives) stays in your
system. Your system needs to know nothing about the KB's configuration: it gets the project names in
the question and answers with a subset of them, or `"*"` for all.

Both webhooks take and return JSON. Every request carries `"protocol": 1` and the headers in
`KB_HOOK_HEADERS` (a JSON object, for example `{"X-Client": "kb"}`), so your system can tell the KB's
calls apart.

## 1. Login webhook — `KB_LOGIN_URL`

Used by the browser sign-in of MCP clients, by the admin pages and by `kb-login`.

**Step 1: the password**

```http
POST {KB_LOGIN_URL}
Content-Type: application/json

{"protocol": 1, "username": "ann", "password": "…"}
```

| Answer | Meaning |
|---|---|
| `200 {"token": "…"}` | signed in; the token is what the KB sends to the permission webhook |
| `200 {"two_factor": {"challenge": "…"}}` | a second step is needed: ask for the code |
| `401` or `422` `{"message": "…"}` | refused; the message is shown to the person |
| `429` with `Retry-After` | too many attempts |

**Step 2: the code** (only after a `two_factor` answer)

```http
POST {KB_LOGIN_URL}

{"protocol": 1, "challenge": "…", "code": "123456"}
```

`code` is whatever the person typed: an authenticator code or a recovery code; your system decides.
Answers as in step 1, without another `two_factor`.

A person who first has to set up an authenticator should do it in your own app: answer
`401 {"message": "set up the authenticator in the app first"}`.

## 2. Permission webhook — `KB_PERMISSION_URL`

Asked for every request with an unknown token, then cached for `KB_AUTH_CACHE_SECONDS` (60) per
SHA-256 of the token; a refusal is cached too. The token itself is never logged. A token that cannot be a
token (over 4096 characters, a space, a character that is no printable ASCII) gets `401` without a call.
At most `KB_AUTH_MAX_CHECKS` (16) calls run at once; one more waits `KB_AUTH_CHECK_WAIT` (2) seconds,
then gets `503`. An access token of the browser sign-in (`kbo_…`) is never sent: the KB sends the token of
the login webhook that stands behind it.

```http
POST {KB_PERMISSION_URL}
Authorization: Bearer <token>
Content-Type: application/json

{"protocol": 1, "projects": ["api", "web", "admin"]}
```

| Answer | Meaning |
|---|---|
| `200 {"user": {"name": "ann", …}, "projects": ["api", "web"]}` | these projects; names not in the question are ignored |
| `200 {"user": {…}, "projects": "*"}` | every project of the question; on a server without projects yet, the person still signs in to the admin pages |
| `401` | the token is unknown or expired: the client signs in again |
| `403`, another `4xx`, or `200` with an empty list | the person may not use the KB (`403`) |
| unreachable, a `5xx` answer, or a body of another shape | an outage: `503`, not cached. The admin pages show "not available", and a refresh of the browser sign-in keeps its refresh token for the next try |

`user` may hold any fields; the KB shows the first of `name`, `uname`, `username`, `id`, `uid` in its
logs and admin pages. It matches `KB_ADMINS` only against `uname`, `username`, `id` and `uid`: stable,
unique identifiers. The display `name` never makes an admin.

## The smallest implementation

A team without an API of its own can answer both webhooks from a short script. An example in Python
with one shared password and a fixed project list per user:

```python
# hooks.py — run behind your TLS proxy; tokens live in memory for the demo
import json, secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

USERS = {"ann": ("password-of-ann", "*"), "bob": ("password-of-bob", ["web"])}
TOKENS = {}

class Hooks(BaseHTTPRequestHandler):
    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/login":
            user = USERS.get(body.get("username"))
            if not user or not secrets.compare_digest(user[0], body.get("password", "")):
                return self.answer(401, {"message": "wrong user name or password"})
            token = secrets.token_urlsafe(32)
            TOKENS[token] = body["username"]
            return self.answer(200, {"token": token})
        if self.path == "/permissions":
            name = TOKENS.get(self.headers.get("Authorization", "")[7:])
            if not name:
                return self.answer(401, {"message": "unknown token"})
            return self.answer(200, {"user": {"name": name}, "projects": USERS[name][1]})
        self.answer(404, {})

HTTPServer(("127.0.0.1", 8791), Hooks).serve_forever()
```

```bash
KB_SERVER=on KB_LOGIN_URL=http://127.0.0.1:8791/login \
  KB_PERMISSION_URL=http://127.0.0.1:8791/permissions kb-http
```

## Settings

| Variable | Meaning |
|---|---|
| `KB_PERMISSION_URL` | the permission webhook (required for `kb-http`) |
| `KB_LOGIN_URL` | the login webhook (browser sign-in, admin pages, `kb-login`); without it only ready tokens work |
| `KB_HOOK_HEADERS` | extra headers for both webhooks, a JSON object |
| `KB_AUTH_CACHE_SECONDS` | how long an answer of the permission webhook is kept (60) |
| `KB_AUTH_TIMEOUT` | the timeout of a permission webhook call in seconds (10) |
| `KB_AUTH_MAX_CHECKS` / `KB_AUTH_CHECK_WAIT` | permission webhook calls at once (16), and the seconds one more waits for a slot (2) |
| `KB_LOGIN_TIMEOUT` | the timeout of a login webhook call in seconds (30): a person waits for it, and the second factor can be slow |
