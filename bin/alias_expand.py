#!/usr/bin/env python3
"""Directional alias expansion for search queries.

Why this exists
---------------
A living corpus renames things: a project, a repo, a person's name you first wrote down wrong.
The human remembers the OLD name; the page that owns the subject only knows the NEW one. Lexical
search then honestly finds nothing — for it those are two unrelated words.

This module rewrites the QUERY (never the corpus): when an old name appears, the canonical name is
appended. Measured on a 27-question golden set, this is the single cheapest recall win after fixing
AND→OR in the lexical leg.

Two rules learned the hard way (both from measurements, see the maintainer's notes):

1. **Directional, old → canonical.** Do NOT dump every spelling of a thing into the query. Pages
   that *discuss* renames (changelogs, planning notes, logs) contain all spellings at once, so a
   symmetric expansion pulls that meta-layer to rank 1 and pushes the owner page down.
2. **Match on raw text, before stemming.** The index is stemmed (FTS5 `porter unicode61`), and the
   stemmer can collapse a brand into an ordinary word — e.g. a product called `Bookinger` stems to `booking`. Matching the raw
   query string is what keeps an alias an alias instead of a stopword.

The table is deployment data, not code: it names private projects, products and people. It lives in
a gitignored `aliases.toml` (see `aliases.toml.example`), exactly like `kb.projects.toml`.

Usage:
    from alias_expand import load_aliases, expand
    expand("where does /srv/oldname live", load_aliases())
    # -> 'where does /srv/oldname live newname'

CLI (for measurement and debugging):
    bin/alias_expand.py "some query with an old name"
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path


import paths

DEFAULT_TABLE = paths.config_file("aliases.toml")


def load_aliases(path: Path | str | None = None) -> list[tuple[str, str]]:
    """Read the alias table. Missing file = feature off (returns []), never an error.

    Format (aliases.toml):
        [[alias]]
        from = "old-name"          # what a human might type
        to   = "new-name newer"    # canonical term(s) appended to the query
    """
    p = Path(path or os.environ.get("KB_ALIASES", DEFAULT_TABLE))
    if not p.is_file():
        return []
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for entry in data.get("alias", []):
        src, dst = entry.get("from"), entry.get("to")
        if isinstance(src, str) and isinstance(dst, str) and src.strip() and dst.strip():
            out.append((src.strip(), dst.strip()))
    return out


def _boundary_pattern(term: str) -> re.Pattern[str]:
    """Whole-token match, tolerant of path separators: `/srv/oldname` must match `oldname`.

    Word boundaries in `re` do not fire between `/` and a letter the way we need for path segments,
    so the token is bounded by anything that is not a letter/digit/underscore/hyphen.
    """
    edge = r"[^\w\-]"
    return re.compile(rf"(?:(?<=^)|(?<={edge}))" + re.escape(term) + rf"(?=$|{edge})", re.IGNORECASE)


def expand(query: str, aliases: list[tuple[str, str]] | None = None) -> str:
    """Append canonical terms for every old name present in `query`.

    - The original query is preserved verbatim — expansion only adds.
    - A canonical term already present in the query is not repeated.
    - Order of appended terms follows the table, so output is deterministic.
    """
    table = load_aliases() if aliases is None else aliases
    if not table or not query.strip():
        return query
    additions: list[str] = []
    for src, dst in table:
        if not _boundary_pattern(src).search(query):
            continue
        for term in dst.split():
            if _boundary_pattern(term).search(query) or term in additions:
                continue
            additions.append(term)
    return f"{query} {' '.join(additions)}" if additions else query


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: alias_expand.py <query>", file=sys.stderr)
        return 2
    table = load_aliases()
    query = " ".join(argv[1:])
    expanded = expand(query, table)
    print(expanded)
    if expanded == query:
        print(f"(no alias matched; table has {len(table)} entries)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
