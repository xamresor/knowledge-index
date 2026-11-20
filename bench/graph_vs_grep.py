#!/usr/bin/env python3
"""Graph vs grep: does the KB answer find the right files, and how much text does an agent read?

The Thoughtworks Technology Radar (Vol 35, 2026-11) keeps "graph-grounded codebase reasoning" in Trial
because graph context sometimes does worse than plain grep. This script measures it on your own code,
retrieval only (no LLM in the loop): for each question, one KB tool call and one grep with the obvious
keyword, scored by

  * recall — the share of the expected files the answer names;
  * chars  — the size of the answer (about 4 characters per token): what the agent has to read;
  * hops   — grep needs a second search when the answer is in another repository (a frontend call ->
             the route table -> the controller); counted from the question's `grep_hops`.

Questions live in a JSON file outside git (they name your code):
  [{"id": "...", "question": "...", "kb": {"tool": "contracts_search", "args": {...}},
    "grep": {"pattern": "...", "root": "/path/to/checkout", "hops": 1}, "gold": ["path/part", ...]}]
A chain question (A -> route -> controller -> service) lists one grep per step as "patterns"; the agent reads
all their outputs, and "hops" defaults to their number.

  python3 bench/graph_vs_grep.py bench/local/questions.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))


def kb_answer(tool: str, args: dict) -> str:
    import kb_core
    return kb_core.call_tool(tool, {**args, "page_length": 50_000})


def grep_answer(pattern: str, root: str) -> str:
    p = subprocess.run(["grep", "-rnIE", "--exclude-dir=node_modules", "--exclude-dir=vendor",
                        "--exclude-dir=.next", "--exclude-dir=.git", "--exclude-dir=.worktrees",
                        "--exclude-dir=storage", "--exclude-dir=dist", "-e", pattern, root],
                       capture_output=True, text=True, errors="replace")
    return p.stdout.replace(root.rstrip("/") + "/", "")


def recall(text: str, gold: list[str]) -> float:
    return sum(1 for g in gold if g in text) / len(gold) if gold else 1.0


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        questions = json.load(f)
    rows = []
    for q in questions:
        t = time.monotonic()
        k = kb_answer(q["kb"]["tool"], q["kb"].get("args", {}))
        kt = time.monotonic() - t
        # a chain question needs several greps, one per step: their outputs add up
        pats = q["grep"].get("patterns") or [q["grep"]["pattern"]]
        g = "\n".join(grep_answer(p, q["grep"]["root"]) for p in pats)
        rows.append((q["id"], recall(k, q["gold"]), len(k), kt, recall(g, q["gold"]), len(g),
                     q["grep"].get("hops", len(pats))))
    print(f"{'question':<28} {'kb recall':>9} {'kb chars':>9} {'kb s':>6}   {'grep recall':>11} {'grep chars':>10} {'hops':>4}")
    for r in rows:
        print(f"{r[0]:<28} {r[1]:>9.2f} {r[2]:>9} {r[3]:>6.2f}   {r[4]:>11.2f} {r[5]:>10} {r[6]:>4}")
    n = len(rows)
    print(f"\n{'mean':<28} {sum(r[1] for r in rows) / n:>9.2f} {sum(r[2] for r in rows) // n:>9} "
          f"{sum(r[3] for r in rows) / n:>6.2f}   {sum(r[4] for r in rows) / n:>11.2f} "
          f"{sum(r[5] for r in rows) // n:>10} {sum(r[6] for r in rows) / n:>4.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "local", "questions.json")))
