#!/usr/bin/env python3
"""Baseline for docs search: recall@5, MRR, and top scores of questions that have no answer.

Retrieval only, no LLM expansion, no reranker (the kb default, KB_DOCS_MODE=light).
Backends:
  bm25    qmd search                       raw BM25 score, 0..1
  lex     the lex: lines of the kb query document only (OR of the content words), raw BM25 from --explain
  vec     qmd query "vec: <q>" --no-rerank raw cosine of the best chunk (from --explain)
  hybrid  qmd query <lex+vec document built by bin/query_build.py> --no-rerank
          = what the kb `search` tool sends to qmd; the printed score is rank-based (1, 0.5, 0.33, ...),
          so the raw best cosine and raw best BM25 score are read from --explain
  tool    bin/kb_core.search(domain=docs, project=all): the real tool, as the MCP server calls it

  python3 bench/search_baseline.py [out.json [backend ...]]

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


def run_tool(q: str):
    import kb_core
    out = kb_core.search({"q": q, "domain": "docs", "project": "all", "limit": N})
    paths = re.findall(r"^qmd://([^\s:]+)", out, re.M)
    sc = re.findall(r"^Score:\s*(\d+)%", out, re.M)
    return uniq(paths), {"score": int(sc[0]) / 100 if sc else 0.0}


BACKENDS = {"bm25": run_bm25, "lex": run_lex, "vec": run_vec, "hybrid": run_hybrid, "tool": run_tool}


def main() -> int:
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
                rows.append(row)
                print(b, x["id"], row.get("first"), row["score"], flush=True)
        res[b] = rows
    json.dump(res, open(out_path, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
