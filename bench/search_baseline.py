#!/usr/bin/env python3
"""Baseline for docs search: recall@5, MRR, and the share of confident wrong answers.

Retrieval only, no LLM expansion, no reranker (the kb default, KB_DOCS_MODE=light).
Backends:
  bm25    qmd search                       raw BM25 score, 0..1
  lex     the lex: lines of the kb query document only (OR of the content words), raw BM25 from --explain
  vec     qmd query "vec: <q>" --no-rerank raw cosine of the best chunk (from --explain)
  hybrid  qmd query <lex+vec document built by bin/query_build.py> --no-rerank
          = what the kb `search` tool sends to qmd; the printed score is rank-based (1, 0.5, 0.33, ...),
          so the raw best cosine and raw best BM25 score are read from --explain
  tool    bin/kb_core.search(domain=docs, project=all): the real tool, as the MCP server calls it, with
          its "is this enough?" check (bin/docs_grade.py): a refused question counts as "not found"
  tool-raw  the same tool with `refuse: false`: always the nearest hits (the behaviour before the check)

  python3 bench/search_baseline.py [out.json [backend ...]]
  python3 bench/search_baseline.py --summary [out.json]     print the table of an earlier run

Three numbers per backend:
  recall@5   share of the expected files in the top 5 files (refused = nothing found)
  MRR        mean of 1 / rank of the first expected file (0 if absent or refused)
  confident wrong  share of all questions that got a confident wrong answer: a no-answer question
             that got any confident hit, or an answerable question whose confident first file is not
             one of the expected files. A refusal is never confident. Backends without a refusal
             answer every question with a hit confidently.

The two question sets are your own and stay out of git: `bench/local/golden-kb.json` (questions with the
expected files) and `bench/local/no-answer-kb.json` (questions the corpus cannot answer), both
`{"queries": [{"id", "query", "type", "expected_files"}]}`.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KB, "bin"))
import query_build  # noqa: E402

N = 10


def qmd(args: list[str]) -> list[dict]:
    p = subprocess.run(["qmd", *args, "--format", "json", "-n", str(N)], cwd=KB, capture_output=True,
                       text=True, timeout=600)
    m = re.search(r"^\[", p.stdout, re.M)
    if not m:
        return []
    try:
        return json.loads(p.stdout[m.start():])
    except json.JSONDecodeError:
        return []


def path(h: dict) -> str:
    return h["file"].replace("qmd://", "")


def uniq(paths: list[str]) -> list[str]:
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def run_bm25(q: str):
    hits = qmd(["search", q])
    return uniq([path(h) for h in hits]), {"score": hits[0]["score"] if hits else 0.0}


def _raw(hits: list[dict]) -> dict:
    vec = [h.get("explain", {}).get("vectorScores") or [0] for h in hits]
    fts = [h.get("explain", {}).get("ftsScores") or [0] for h in hits]
    return {"cos": max((max(v) for v in vec), default=0.0), "fts": max((max(f) for f in fts), default=0.0)}


def run_vec(q: str):
    hits = qmd(["query", "vec: " + q, "--no-rerank", "--explain"])
    r = _raw(hits)
    return uniq([path(h) for h in hits]), {"score": r["cos"]}


def run_hybrid(q: str):
    hits = qmd(["query", query_build.build(q), "--no-rerank", "--explain"])
    r = _raw(hits)
    return uniq([path(h) for h in hits]), {"score": hits[0]["score"] if hits else 0.0,
                                           "cos": r["cos"], "fts": r["fts"]}


def run_lex(q: str):
    doc = "\n".join(l for l in query_build.build(q).split("\n") if l.startswith("lex:"))
    hits = qmd(["query", doc, "--no-rerank", "--explain"])
    r = _raw(hits)
    return uniq([path(h) for h in hits]), {"score": r["fts"]}


def _tool(q: str, refuse: bool):
    import docs_grade
    import kb_core
    out = kb_core.search({"q": q, "domain": "docs", "project": "all", "limit": N, "refuse": refuse})
    refused = docs_grade.REFUSED_MARK in out
    paths = [] if refused else re.findall(r"^qmd://([^\s:]+)", out, re.M)
    sc = re.findall(r"^Score:\s*(\d+)%", out, re.M)
    grade = re.search(r"(?:\[grade: |Grade: )([^\]]*)\]", out)
    return uniq(paths), {"score": int(sc[0]) / 100 if sc and not refused else 0.0, "refused": refused,
                         "grade": grade.group(1) if grade else None}


def run_tool(q: str):
    return _tool(q, True)


def run_tool_raw(q: str):
    return _tool(q, False)


BACKENDS = {"bm25": run_bm25, "lex": run_lex, "vec": run_vec, "hybrid": run_hybrid, "tool": run_tool,
            "tool-raw": run_tool_raw}


def metrics(rows: list[dict]) -> dict:
    """recall@5, MRR and the confident-wrong share of one backend's rows."""
    gold = [r for r in rows if r["kind"] == "gold"]
    none = [r for r in rows if r["kind"] == "none"]
    confident = lambda r: bool(r["top"]) and not r.get("refused")  # noqa: E731
    wrong_none = sum(1 for r in none if confident(r))
    wrong_gold = sum(1 for r in gold if confident(r) and r.get("first") != 1)
    refused_right = sum(1 for r in gold if r.get("refused") and r.get("first_raw") == 1)
    return {"recall5": statistics.mean(r["found5"] for r in gold) if gold else 0.0,
            "mrr": statistics.mean(1 / r["first"] if r["first"] else 0.0 for r in gold) if gold else 0.0,
            "wrong": (wrong_none + wrong_gold) / len(rows) if rows else 0.0,
            "wrong_none": wrong_none, "n_none": len(none), "wrong_gold": wrong_gold, "n_gold": len(gold),
            "refused_gold": sum(1 for r in gold if r.get("refused")), "refused_right": refused_right}


def summary(res: dict) -> str:
    lines = ["| Backend | recall@5 | MRR | confident wrong | no-answer answered | answerable: wrong first file "
             "| answerable refused (right without the check) |", "|---|---|---|---|---|---|---|"]
    for b, rows in res.items():
        m = metrics(rows)
        lines.append(f"| `{b}` | {m['recall5']:.2f} | {m['mrr']:.2f} | {m['wrong']:.2f} "
                     f"| {m['wrong_none']} of {m['n_none']} | {m['wrong_gold']} of {m['n_gold']} "
                     f"| {m['refused_gold']} ({m['refused_right']}) |")
    return "\n".join(lines)


def main() -> int:
    if sys.argv[1:2] == ["--summary"]:
        path_ = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "local", "baseline-raw.json")
        print(summary(json.load(open(path_))))
        return 0
    gold = json.load(open(os.path.join(HERE, "local", "golden-kb.json")))["queries"]
    noans = json.load(open(os.path.join(HERE, "local", "no-answer-kb.json")))["queries"]
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "local", "baseline-raw.json")
    only = sys.argv[2:] or list(BACKENDS)
    res = json.load(open(out_path)) if os.path.exists(out_path) and len(only) < len(BACKENDS) else {}
    for b, fn in BACKENDS.items():
        if b not in only:
            continue
        rows = []
        for kind, qs in (("gold", gold), ("none", noans)):
            for x in qs:
                paths, extra = fn(x["query"])
                row = {"id": x["id"], "kind": kind, "type": x["type"], "top": paths[:N], **extra}
                if kind == "gold":
                    exp = x["expected_files"]
                    ranks = [paths.index(e) + 1 for e in exp if e in paths]
                    row["first"] = min(ranks) if ranks else None
                    row["found5"] = sum(1 for e in exp if e in paths[:5]) / len(exp)
                    if extra.get("refused"):   # what the nearest hits would have been (for the cost)
                        raw, _ = run_tool_raw(x["query"])
                        row["first_raw"] = min((raw.index(e) + 1 for e in exp if e in raw), default=None)
                rows.append(row)
                print(b, x["id"], row.get("first"), row["score"], flush=True)
        res[b] = rows
    json.dump(res, open(out_path, "w"), indent=1)
    print(summary(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
