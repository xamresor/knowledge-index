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

Two kinds of module live here, and the difference is deliberate:

* **plugins** define `enrich(graph, roots)` and run as a pipeline step of their own (`markdown`);
* **knowledge modules** carry only patterns and pure functions over text, called by the enrichers
  (`php_laravel`, `js_ts`). Their pipelines are older and heavier than one function signature, so
  the regexes moved first and the pipeline stayed put — the cheap half of the split, done separately
  from the risky half.

Shared contract:

    NAME              str                      short identifier, used in logs
    EXTENSIONS        tuple[str, ...]          files this module claims
    COMMENT_PREFIXES  tuple[str, ...]          comment openers, for rationale extraction
    CONTRIBUTES       dict[str, list[str]]     what it adds to the graph — for status output
    enrich(graph, roots) -> tuple[int, int]    plugins only: (nodes added, edges added), in place
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
#: `.vue` is already in ALL_CODE via FRONTEND — this is the same set, named for the question it answers.
FILE_LABEL_SUFFIXES = ALL_CODE

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


def _ordered(modules) -> list:
    return sorted(modules, key=lambda m: (getattr(m, "PRIORITY", 50), m.NAME))


def plugins() -> list:
    """Modules that can enrich a graph themselves, in pipeline order (priority, then name)."""
    return _ordered(m for m in _modules() if hasattr(m, "enrich"))


def patterns() -> list:
    """Modules that only carry language patterns; the enrichers call into them."""
    return _ordered(m for m in _modules() if not hasattr(m, "enrich"))


def describe() -> dict[str, dict]:
    """What each module claims and contributes — for `kb status` and for the dashboard."""
    return {m.NAME: {"extensions": list(m.EXTENSIONS),
                     "role": "plugin" if hasattr(m, "enrich") else "patterns",
                     **getattr(m, "CONTRIBUTES", {})}
            for m in _ordered(_modules())}
