"""Language knowledge for the graph — one plugin per language, one place for the shared facts.

What this is *not*: the AST parser. `graphify` does that. These plugins add what an AST cannot see —
framework relations (an Eloquent `belongsTo`, a `fetch()` call site hitting a route), schema facts,
design rationale in comments, documents as graph nodes.

Why it exists: the four enrichers each carried **their own copy** of the file-type list, and framework
knowledge sat in "concern" files rather than language ones. Adding a language meant editing four
places, and forgetting one produced a **silently partial graph** — the worst failure mode this project
has, because nothing looks broken.

Two things matter about the sets below: they are **named by role, not merged**. `FRONTEND` and
`ALL_CODE` are different questions, and a single `CODE_EXTENSIONS` would quietly change behaviour —
`link_http` deliberately looks at frontend files only, while `enrich` looks at everything.

Plugin contract (see markdown.py for a worked example):

    NAME              str                      short identifier, used in logs
    EXTENSIONS        tuple[str, ...]          files this plugin claims
    COMMENT_PREFIXES  tuple[str, ...]          comment openers, for rationale extraction
    CONTRIBUTES       dict[str, list[str]]     {"nodes": [...], "relations": [...]} — for status output
    enrich(graph, roots) -> tuple[int, int]    (nodes added, edges added); mutates the graph in place
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

# --- shared file-type facts ----------------------------------------------------------------------
PHP = (".php",)
FRONTEND = (".js", ".ts", ".tsx", ".vue")        # files that make HTTP calls to a backend
PYTHON = (".py",)
DOCS = (".md",)

#: Everything the enrichers treat as "our code" — the union that used to be copy-pasted around.
ALL_CODE = PHP + FRONTEND + PYTHON

#: Suffixes that mark a *label* as a file rather than a symbol (labels keep their extension).
FILE_LABEL_SUFFIXES = ALL_CODE + (".vue",)

#: Comment openers per language family. Rationale extraction needs this, and it is language
#: knowledge — keeping it here is what lets a new language arrive without touching link_rationale.
COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    ".php": ("//", "#", "/*", "*"),
    ".js": ("//", "/*", "*"),
    ".ts": ("//", "/*", "*"),
    ".tsx": ("//", "/*", "*"),
    ".vue": ("//", "/*", "*", "<!--"),
    ".py": ("#",),
    ".md": ("<!--",),
}


def comment_prefixes_for(path: str | Path) -> tuple[str, ...]:
    return COMMENT_PREFIXES.get(Path(path).suffix.lower(), ("//", "#"))


# --- plugin registry -----------------------------------------------------------------------------
def _modules():
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if not info.name.startswith("_"):
            yield importlib.import_module(f"{__package__}.{info.name}")


def plugins() -> list:
    """Language plugins, in a stable order (declaration priority, then name)."""
    return sorted(_modules(), key=lambda m: (getattr(m, "PRIORITY", 50), m.NAME))


def describe() -> dict[str, dict]:
    """What each plugin claims and contributes — for `kb status` and for the dashboard."""
    return {m.NAME: {"extensions": list(m.EXTENSIONS), **getattr(m, "CONTRIBUTES", {})}
            for m in plugins()}
