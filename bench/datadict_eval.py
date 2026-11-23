#!/usr/bin/env python3
"""The "20 questions about data" check for the `datadict` add-on: recall@5 of tables, off and on.

Each question names the tables that answer it. The script asks the real `search` tool (through
kb_core.call_tool, page 1, as an agent sees it) twice: once with the add-on off, once with it on.
From each answer it takes the first 5 distinct table names, in the order they appear in the text.
A table name counts when it stands as a whole word and is a table the graph or a dictionary knows.
A model class counts as its table (the `defines_table` edges of the graph): without the add-on the
answer names models, not tables.

  recall@5 of a question = expected tables among those 5 / expected tables
  the score              = the mean over the questions

Keep the add-on only if "on" beats "off" (docs/design.md, roadmap).

  python3 bench/datadict_eval.py [questions.json] [--domain code|docs|both] [--project NAME] [--json out.json]

The question file is your own and stays out of git: `bench/local/datadict-questions.json`,
`{"queries": [{"id", "query", "expected_tables": [...]}]}`. An example with demo names:
`bench/datadict-questions.example.json`. The run needs the built graph, and for `docs`/`both` qmd.

Caveat: a table named like a common word (`users`, `settings`) also counts when a doc merely says
the word. Both runs share this bias, so the difference between them still holds.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KB, "bin"))

K = 5
DEFAULT_QUESTIONS = os.path.join(HERE, "local", "datadict-questions.json")


def known_tables(graph: str, extra: set[str]) -> set[str]:
    import datadict
    names = set(datadict.graph_tables(graph)) | extra
    for table in datadict.entries().values():
        names |= set(table)
    return {n for n in names if n}


def model_tables(graph: str) -> dict[str, str]:
    """Model class -> its table, from the graph's `defines_table` edges. Without the add-on, `search`
    names the model (`ObjectFuelCardTransaction`), not the table: count the model as its table."""
    try:
        with open(graph, encoding="utf-8") as f:
            g = json.load(f)
    except (OSError, ValueError):
        return {}
    labels = {n.get("id"): n.get("label") for n in g.get("nodes", [])}
    out = {}
    for e in g.get("links") or g.get("edges") or []:
        if e.get("relation") == "defines_table" and str(e.get("target", "")).startswith("db::"):
            if labels.get(e.get("source")):
                out[labels[e["source"]]] = e["target"][4:]
    return out


def top_tables(text: str, tables: set[str], k: int = K, models: dict[str, str] | None = None) -> list[str]:
    """The first k distinct tables in the text, by first position. A model name counts as its table."""
    models = models or {}
    first: dict[str, int] = {}
    for name in tables | set(models):
        m = re.search(rf"(?<![\w.\\/-]){re.escape(name)}(?![\w-])", text)
        if m:
            table = models.get(name, name)
            first[table] = min(first.get(table, m.start()), m.start())
    return [n for n, _ in sorted(first.items(), key=lambda x: x[1])][:k]


def recall(got: list[str], want: list[str]) -> float:
    return len(set(got) & set(want)) / len(set(want)) if want else 0.0


def set_addon(on: bool) -> None:
    import addons
    names = addons.enabled() - {"datadict"}
    if on:
        names.add("datadict")
    os.environ["KB_ADDONS"] = ",".join(sorted(names)) or "none"


def ask(query: str, domain: str, project: str) -> str:
    import kb_core
    try:
        return kb_core.call_tool("search", {"q": query, "domain": domain, "project": project})
    except Exception as e:  # noqa: BLE001 — one failed question must not stop the run
        return f"(error: {e})"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("questions", nargs="?", default=DEFAULT_QUESTIONS)
    ap.add_argument("--domain", default="both", choices=["code", "docs", "both"])
    ap.add_argument("--project", default="all")
    ap.add_argument("--json", dest="out", help="write every answer's tables and scores here")
    a = ap.parse_args(argv)
    try:
        with open(a.questions, encoding="utf-8") as f:
            queries = json.load(f)["queries"]
    except (OSError, ValueError, KeyError) as e:
        print(f"cannot read {a.questions}: {e}\n(copy bench/datadict-questions.example.json there and "
              "write your own questions)", file=sys.stderr)
        return 2

    import kb_core
    expected = {t for q in queries for t in q.get("expected_tables", [])}
    tables = known_tables(kb_core.GRAPH, expected)
    models = model_tables(kb_core.GRAPH)
    rows, sums = [], {"off": 0.0, "on": 0.0}
    for q in queries:
        row = {"id": q["id"], "query": q["query"], "expected": q.get("expected_tables", [])}
        for mode in ("off", "on"):
            set_addon(mode == "on")
            got = top_tables(ask(q["query"], a.domain, a.project), tables, models=models)
            row[mode] = {"tables": got, "recall": recall(got, row["expected"])}
            sums[mode] += row[mode]["recall"]
        rows.append(row)
        print(f"{q['id']:<10} off {row['off']['recall']:.2f}  on {row['on']['recall']:.2f}  "
              f"want {','.join(row['expected'])}  on-top {','.join(row['on']['tables']) or '-'}")
    n = len(queries) or 1
    better = sum(r["on"]["recall"] > r["off"]["recall"] for r in rows)
    worse = sum(r["on"]["recall"] < r["off"]["recall"] for r in rows)
    print(f"\nquestions {len(queries)}  domain {a.domain}  project {a.project}")
    print(f"recall@{K}  off {sums['off'] / n:.3f}  on {sums['on'] / n:.3f}  "
          f"(better {better}, worse {worse}, same {len(rows) - better - worse})")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"domain": a.domain, "project": a.project, "k": K,
                       "recall_off": sums["off"] / n, "recall_on": sums["on"] / n, "rows": rows}, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
