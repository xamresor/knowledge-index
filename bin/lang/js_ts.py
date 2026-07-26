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
WRAPPER_RE = re.compile(r"(?<![\w.])(\$api|apiFetch|apiUpload|apiDownload|fetch)\(\s*[`'\"]([^`'\"]+)")
METHOD_RE = re.compile(r"\bmethod\s*:\s*['\"](" + "|".join(VERBS) + r")['\"]", re.IGNORECASE)
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
            window = line[match.end():] + "\n" + "\n".join(lines[i:i + METHOD_WINDOW])
            verb_match = METHOD_RE.search(window)
            yield i, (verb_match.group(1).lower() if verb_match else "get"), raw


def prune(dirs: list[str]) -> list[str]:
    """In-place-friendly filter for `os.walk`'s dirs list: drop dependencies and build output."""
    return [d for d in dirs if d not in SKIP_DIRS]
