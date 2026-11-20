"""JavaScript / TypeScript knowledge: how a frontend calls a backend, and what is not our source.

The one thing an AST cannot see on this side of a codebase is the *call site*: `api.post('/v1/x')` is,
semantically, an edge to a controller in another repository. Recognising it means knowing the shapes
real clients take — a dotted client, an options object, a bare `fetch` on a base-URL template — which
is library knowledge, not syntax, so it lives here.

Like `php_laravel`, this is patterns and pure functions; `link_http.py` owns the pipeline. The route
matching itself deliberately stays there: a Laravel route table meeting a JS call site is a join
between two languages and belongs to neither plugin.
"""
from __future__ import annotations

import re

from . import FRONTEND

NAME = "js_ts"
EXTENSIONS = FRONTEND
COMMENT_PREFIXES = ("//", "/*", "*")
CONTRIBUTES = {
    "relations": ["http_request"],
    "patterns": ["api.<verb>() clients", "fetch/ofetch wrappers", "method: in an options object"],
}

#: Single-file components are templates, not classes — a distinct node type in the graph.
TEMPLATES = (".vue",)
#: Labels that name a script file rather than a symbol.
SCRIPTS = (".js", ".ts")
#: Dependencies and build output: present in a checkout, never our code, and slow to walk.
SKIP_DIRS = frozenset({"node_modules", ".next", ".nuxt", "dist", "build", "vendor"})

VERBS = ("get", "post", "put", "patch", "delete")
#: Dotted client: `api.post('/v1/x')` — the verb is the method name.
CALL_RE = re.compile(r"\bapi\.(" + "|".join(VERBS) + r")\(\s*[`'\"]([^`'\"]+)", re.IGNORECASE)
#: Options-object style, where the verb lives in an options object (GET when absent). Covers the
#: ofetch/fetch wrappers in use across frameworks:
#:   $api('v2/objects', {method:'POST'})        (Nuxt/ofetch)
#:   apiFetch('/auth/x', {method:'DELETE'})     (Next/React wrapper, incl. apiUpload/apiDownload)
#:   fetch(`${API_BASE}/categories`, {…})       (raw fetch to a base-URL template literal)
#: A TypeScript type argument may sit between the name and the call: $api<Page<Item>>('v2/items').
WRAPPER_RE = re.compile(
    r"(?<![\w.])(\$api|apiFetch|apiUpload|apiDownload|fetch)(?:<.*?>)?\(\s*[`'\"]([^`'\"]+)")
#: The same two shapes with the URL on the next line, as Prettier writes a long call:
#:   const data = await apiFetch<ReservationActionResponse>(
#:     `/auth/reservations/${id}/dispute`,
WRAPPER_OPEN_RE = re.compile(r"(?<![\w.])(\$api|apiFetch|apiUpload|apiDownload|fetch)(?:<.*?>)?\(\s*$")
CALL_OPEN_RE = re.compile(r"\bapi\.(" + "|".join(VERBS) + r")(?:<.*?>)?\(\s*$", re.IGNORECASE)
URL_ARG_RE = re.compile(r"^\s*[`'\"]([^`'\"]+)[`'\"]")
METHOD_RE = re.compile(r"\bmethod\s*:\s*['\"](" + "|".join(VERBS) + r")['\"]", re.IGNORECASE)
#: A versioned API path handed to any other function — a list or export composable, a store helper:
#:   usePaginatedList(() => `v2/customers/${id}/drivers`, {…})     exportTable(`v2/…/export`, {…})
#: The path may start the next line after `name(`. Only `v<digits>/…` counts: a bare string is too
#: often not a URL. The verb comes from a nearby `method:`, else GET.
VERSIONED_RE = re.compile(r"(?<![\w.$<])([A-Za-z_]\w*)(?:<.*?>)?\(\s*(?:\(\)\s*=>\s*)?[`'\"](/?v\d+/[^`'\"]*)")
OPEN_CALL_RE = re.compile(r"(?<![\w.$<])([A-Za-z_]\w*)(?:<.*?>)?\(\s*$")
NEXT_ARG_RE = re.compile(r"^\s*(?:\(\)\s*=>\s*)?[`'\"](/?v\d+/[^`'\"]*)")
#: Names the patterns above already cover, and language words that take a string.
NOT_A_CLIENT = frozenset({"api", "apiFetch", "apiUpload", "apiDownload", "fetch", "if", "for",
                          "while", "switch", "return", "t", "$t", "i18n", "console", "require",
                          "import", "includes", "startsWith", "endsWith", "indexOf", "replace",
                          "split", "match", "test"})
METHOD_WINDOW = 3          # lines after the call to look for `method:`


def iter_calls(lines: list[str]):
    """Yield (line number, verb, raw path) for every recognised API call site."""
    for i, line in enumerate(lines, 1):
        for verb, raw in CALL_RE.findall(line):
            yield i, verb.lower(), raw
        for match in WRAPPER_RE.finditer(line):
            name, raw = match.group(1), match.group(2)
            # A bare `fetch()` counts only when the URL is a base-URL template (`${…}/…`);
            # `fetch('/static/x')` or `fetch(url)` is not a backend API call.
            if name == "fetch" and not raw.startswith("${"):
                continue
            yield i, _verb(line[match.end():], lines, i), raw
        if i < len(lines):
            nxt = URL_ARG_RE.match(lines[i])
            opened = WRAPPER_OPEN_RE.search(line) if nxt else None
            dotted = CALL_OPEN_RE.search(line) if nxt else None
            if dotted:
                yield i, dotted.group(1).lower(), nxt.group(1)
            elif opened and not (opened.group(1) == "fetch" and not nxt.group(1).startswith("${")):
                yield i, _verb(lines[i][nxt.end():], lines, i + 1), nxt.group(1)
        for name, raw, end in _versioned(line, lines, i):
            if name not in NOT_A_CLIENT:
                yield i, _verb(line[end:], lines, i), raw


def _verb(rest: str, lines: list[str], i: int) -> str:
    """The `method:` of this call: on the rest of its line or the next few, but never past the
    start of the next call (a `method: 'POST'` two lines down may belong to it)."""
    window = [rest]
    for nxt in lines[i:i + METHOD_WINDOW]:
        if CALL_RE.search(nxt) or WRAPPER_RE.search(nxt) or VERSIONED_RE.search(nxt):
            break
        window.append(nxt)
    m = METHOD_RE.search("\n".join(window))
    return m.group(1).lower() if m else "get"


def _versioned(line: str, lines: list[str], i: int):
    """(function name, path, end offset in `line`) for a versioned path passed to a function."""
    for m in VERSIONED_RE.finditer(line):
        yield m.group(1), m.group(2), m.end()
    opened = OPEN_CALL_RE.search(line)
    if opened and i < len(lines):
        nxt = NEXT_ARG_RE.match(lines[i])
        if nxt:
            yield opened.group(1), nxt.group(1), len(line)


def prune(dirs: list[str]) -> list[str]:
    """In-place-friendly filter for `os.walk`'s dirs list: drop dependencies and build output."""
    return [d for d in dirs if d not in SKIP_DIRS]
