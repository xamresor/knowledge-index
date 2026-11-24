#!/usr/bin/env python3
"""Is this docs result enough? A grade for the first hit of a docs search, and an honest "not found".

The problem
-----------
`search` always returns the nearest documents, also when no document answers the question. qmd's
fused score cannot tell: it is rank-based (1, 0.5, 0.33, ...), so the first hit always prints 100%.
The baseline of 2026-11-23 (bench/search_baseline.py) also found that the raw scores do not separate
either: the best cosine and the best BM25 score of a no-answer question fall inside the range of the
answerable ones for 10 of 10 questions.

The grade
---------
One number, from the question's own words and qmd's keyword index (read-only, no model, a few ms):

    coverage = sum of idf(word) for the question's words that the first hit contains
               / sum of idf(word) for all the question's words

    idf(word) = ln((N + 1) / (df + 0.5))    N = documents the caller may see, df = those with the word

Plainly: how much of the question's *specific* vocabulary the first hit holds. A word that every
document has ("kb") weighs nearly nothing; a word that no document has ("terraform") weighs most. So
a question about a thing the docs never mention gets a low coverage on any hit, and the words that
no document holds are named in the answer.

Why not the raw cosine or BM25: they need `--explain`, which the long-lived qmd worker (bin/warm.py)
does not return, and they separate worse (see the baseline). Why not the gap between hit 1 and
hit 2: the fused scores are ranks, so the gap is always the same.

Words are the content words of bin/query_build.py, matched the way qmd's own `lex:` search matches
them (porter stems, prefix match for one word, a phrase for `a-b`, `a.b`, `a/b`).
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
from dataclasses import dataclass, field

import query_build

# Below this coverage the first hit is "weak". Read off bench/local (38 answerable and 10 no-answer
# questions, 2026-11-23; docs/design.md, "Docs search: an honest not found"): 8 of the 10 no-answer
# first hits had coverage 0.17-0.45, every right answerable first hit but one (0.42) had 0.47 or more.
# 0.46 sits in that gap. It was fitted on the same 48 questions, so it is optimistic: recheck it on
# questions written later.
COVERAGE_MIN = 0.46

# Questions with fewer content words than this are not graded: one or two words carry too little to
# judge (a bare symbol name, "pint") and a keyword search already answers them exactly.
MIN_TERMS = 3

REFUSED_MARK = "[not found:"   # the first line of a refusal; bench/search_baseline.py looks for it


@dataclass
class Grade:
    coverage: float | None              # None = not graded (index missing, too few words, ...)
    missing: list[str] = field(default_factory=list)   # words that no indexed document holds
    absent: list[str] = field(default_factory=list)    # words the first hit lacks (incl. missing)
    reason: str = ""

    @property
    def weak(self) -> bool:
        return self.coverage is not None and self.coverage < COVERAGE_MIN

    def label(self) -> str:
        if self.coverage is None:
            return f"not graded ({self.reason})"
        text = f"coverage {self.coverage:.2f} (min {COVERAGE_MIN:.2f})"
        if self.missing:
            text += "; in no document: " + ", ".join(self.missing)
        elif self.absent:
            text += "; not in the first hit: " + ", ".join(self.absent)
        return text


def index_path(kb: str) -> str | None:
    """The qmd index the `qmd` CLI uses when it runs in `kb` (same order as qmd: env, local, cache)."""
    for p in (os.environ.get("INDEX_PATH"), os.path.join(kb, ".qmd", "index.sqlite"),
              os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                           "qmd", "index.sqlite")):
        if p and os.path.isfile(p):
            return p
    return None


def fts_query(term: str) -> str | None:
    """`term` as qmd's own lex search would send it to FTS5 (store.js buildFTS5Query)."""
    parts = [p for p in re.split(r"[^\w']+|_", term.lower()) if p]
    if not parts:
        return None
    return f'"{parts[0]}"*' if len(parts) == 1 else '"' + " ".join(parts) + '"'


def first_hit(out: str) -> tuple[str, str] | None:
    """(collection, path) of the first `qmd://collection/path:line` hit in a docs answer."""
    m = re.search(r"(?m)^qmd://([^/\s]+)/([^\s:]+)", out)
    return (m.group(1), m.group(2)) if m else None


CONTRACTS_DIR = "contracts/"   # contract docs: open to every caller (bin/access.py)


class Index:
    """Read-only questions to qmd's keyword index: how many documents hold a word, does one doc.

    `collections` limits the counts to the documents a caller may see: those collections, plus the
    contract docs of every collection whose name starts with `contract_prefix`. None = the whole
    index (a local caller). Without the limit the counts would tell a limited caller which words
    the docs of hidden projects hold."""

    def __init__(self, path: str, collections: list[str] | None = None, contract_prefix: str | None = None):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        self.scope, self.params = "", ()
        if collections is not None:
            marks = ",".join("?" * len(collections)) or "NULL"
            self.scope = f" AND (d.collection IN ({marks})"
            self.params = tuple(collections)
            if contract_prefix:
                # the contract docs only (bin/access.py doc_allowed): a *.md file directly in
                # contracts/, not its README and not a sub-folder
                self.scope += (" OR (substr(d.path, 1, ?) = ? AND substr(d.collection, 1, ?) = ?"
                               " AND instr(substr(d.path, ?), '/') = 0 AND lower(d.path) LIKE '%.md'"
                               " AND lower(d.path) != ?)")
                self.params += (len(CONTRACTS_DIR), CONTRACTS_DIR, len(contract_prefix), contract_prefix,
                                len(CONTRACTS_DIR) + 1, CONTRACTS_DIR + "readme.md")
            self.scope += ")"

    def close(self) -> None:
        self.con.close()

    def size(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM documents d WHERE d.active = 1" + self.scope,
                                self.params).fetchone()[0]

    def df(self, match: str) -> int:
        return self.con.execute(
            "SELECT COUNT(*) FROM documents_fts f JOIN documents d ON d.id = f.rowid "
            "WHERE d.active = 1 AND documents_fts MATCH ?" + self.scope, (match, *self.params)).fetchone()[0]

    def doc_id(self, collection: str, path: str) -> int | None:
        row = self.con.execute("SELECT id FROM documents WHERE active = 1 AND collection = ? AND path = ?",
                               (collection, path)).fetchone()
        return row[0] if row else None

    def holds(self, doc: int, match: str) -> bool:
        return self.con.execute("SELECT 1 FROM documents_fts WHERE documents_fts MATCH ? AND rowid = ?",
                                (match, doc)).fetchone() is not None


def coverage(terms: list[str], df: dict[str, int], held: set[str], n: int) -> float:
    """Share of the question's idf weight that the first hit holds (see the module docstring)."""
    weight = {t: math.log((n + 1) / (df[t] + 0.5)) for t in terms}
    total = sum(max(w, 0.0) for w in weight.values())
    if total <= 0:
        return 1.0
    return sum(max(weight[t], 0.0) for t in terms if t in held) / total


def grade(query: str, out: str, index: Index | None) -> Grade:
    """Grade the first hit of a docs answer `out` for `query`."""
    if index is None:
        return Grade(None, reason="no qmd index")
    if query_build.is_structured(query):
        return Grade(None, reason="typed query")
    terms = [t for t in query_build.terms(query) if fts_query(t)]
    if len(terms) < MIN_TERMS:
        return Grade(None, reason="too few words")
    hit = first_hit(out)
    try:
        n = index.size()
        df = {t: index.df(fts_query(t)) for t in terms}
        doc = index.doc_id(*hit) if hit else None
        held = {t for t in terms if doc is not None and index.holds(doc, fts_query(t))}
    except sqlite3.Error as e:   # an index of another qmd version: do not judge, do not break search
        return Grade(None, reason=f"index error: {e}")
    if hit and doc is None:
        return Grade(None, reason="first hit not in the index")
    missing = [t for t in terms if df[t] == 0]
    absent = [t for t in terms if t not in held]
    return Grade(coverage(terms, df, held, n) if hit else 0.0, missing, absent)


def open_index(kb: str, collections: list[str] | None = None,
               contract_prefix: str | None = None) -> Index | None:
    """qmd's index, its counts limited to `collections` (+ contract docs) when given (see Index)."""
    path = index_path(kb)
    if not path:
        return None
    try:
        return Index(path, collections, contract_prefix)
    except sqlite3.Error:
        return None
