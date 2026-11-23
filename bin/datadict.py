#!/usr/bin/env python3
"""The data dictionary add-on (`datadict`): what a table means, not only where the code uses it.

The graph knows tables from the PHP source (a model's `$table`, `Schema::create`, `DB::table`), as
`db_table` nodes with the ID `db::<name>`. It does not know what one row stands for, which columns
add up, or what the business calls the table. A person writes that down in a small file per project:

    <config>/datadict/<project>.toml     (or KB_DATADICT_DIR/<project>.toml)

    [[table]]
    name         = "orders"                    # the table name as the code writes it (required)
    term         = "Customer order"            # the business term
    grain        = "one row per order a customer placed"
    summable     = ["total_amount"]            # columns that may be summed
    not_summable = ["id", "customer_id"]       # IDs, prices, rates, statuses: never sum them
    synonyms     = ["sale", "revenue"]         # other words people use for it
    notes        = "Cancelled orders stay in the table."

With the add-on on, the core uses this file in three places (bin/kb_core.py):
  * `search` (code half): a "Data dictionary" block lists the tables whose term, synonyms, name,
    grain, columns or notes match the question, before the code subgraph;
  * `graph_explain` of a table: the meaning under the node header; a business term that names no
    node is read as the table it stands for;
  * `problems`: entries for tables the graph does not know, tables of the project with no entry,
    and mistakes in the file.

The file is read on every call, cached by its mtime: an edit needs no rebuild. A caller limited to
some projects sees the files of those projects only.

CLI:  datadict.py check [graph.json]   — print the problems of every dictionary file
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

FIELDS = {"name": str, "term": str, "grain": str, "notes": str,
          "summable": list, "not_summable": list, "synonyms": list}
LIST_FIELDS = ("summable", "not_summable", "synonyms")
MAX_HITS = 5

_lock = threading.Lock()
_FILES: dict[str, tuple[float, dict, list]] = {}      # path -> (mtime, entries, errors)
_GRAPH: dict[str, tuple[float, dict]] = {}            # graph path -> (mtime, table -> projects)


def folder() -> str:
    return os.environ.get("KB_DATADICT_DIR") or str(paths.config_file("datadict"))


def files(allowed: set[str] | None = None) -> dict[str, str]:
    """project -> dictionary file, for the projects in `allowed` (None = every file)."""
    root = folder()
    out = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return {}
    for name in names:
        project, ext = os.path.splitext(name)
        if ext == ".toml" and (allowed is None or project in allowed):
            out[project] = os.path.join(root, name)
    return out


# --- parsing -------------------------------------------------------------------------------------
def parse(text: str) -> tuple[dict[str, dict], list[str]]:
    """TOML text -> (table name -> entry, errors). A bad entry is reported and skipped, never fatal."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {}, [f"not valid TOML: {e}"]
    rows = data.get("table", [])
    errors = [f"unknown top-level key: {k}" for k in data if k != "table"]
    if not isinstance(rows, list):
        return {}, errors + ["`table` must be a list: write each entry as [[table]]"]
    entries: dict[str, dict] = {}
    for i, row in enumerate(rows, 1):
        where = f"entry {i}"
        if not isinstance(row, dict):
            errors.append(f"{where}: not a table")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: `name` is required (the table name as the code writes it)")
            continue
        name = name.strip()
        where = f"entry {i} ({name})"
        entry: dict = {"name": name}
        for key, value in row.items():
            if key == "name":
                continue
            want = FIELDS.get(key)
            if want is None:
                errors.append(f"{where}: unknown key `{key}` (known: {', '.join(FIELDS)})")
            elif want is str:
                if isinstance(value, str):
                    entry[key] = value.strip()
                else:
                    errors.append(f"{where}: `{key}` must be a string")
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                entry[key] = [v.strip() for v in value if v.strip()]
            else:
                errors.append(f"{where}: `{key}` must be a list of strings")
        both = sorted(set(entry.get("summable", [])) & set(entry.get("not_summable", [])))
        if both:
            errors.append(f"{where}: in both `summable` and `not_summable`: {', '.join(both)}")
        if name in entries:
            errors.append(f"{where}: the table is listed twice; the later entry wins")
        entries[name] = entry
    return entries, errors


def load(path: str) -> tuple[dict[str, dict], list[str]]:
    """One dictionary file, cached by mtime. A missing file is an empty dictionary."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}, []
    with _lock:
        hit = _FILES.get(path)
        if hit and hit[0] == mtime:
            return hit[1], hit[2]
    try:
        with open(path, encoding="utf-8") as f:
            entries, errors = parse(f.read())
    except (OSError, UnicodeDecodeError) as e:
        entries, errors = {}, [f"cannot read: {e}"]
    with _lock:
        _FILES[path] = (mtime, entries, errors)
    return entries, errors


def entries(allowed: set[str] | None = None) -> dict[str, dict[str, dict]]:
    """project -> table name -> entry, for the projects the caller may see."""
    return {p: load(f)[0] for p, f in files(allowed).items()}


# --- the graph's tables --------------------------------------------------------------------------
def graph_tables(graph: str) -> dict[str, set[str]]:
    """table name -> the projects whose nodes link to it (not through `in_domain`). Cached by mtime."""
    try:
        mtime = os.path.getmtime(graph)
    except OSError:
        return {}
    with _lock:
        hit = _GRAPH.get(graph)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(graph, encoding="utf-8") as f:
            g = json.load(f)
    except (OSError, ValueError):
        return {}
    nodes = g.get("nodes", [])
    tables = {n["id"]: n.get("label", "") for n in nodes if n.get("type") == "db_table" and n.get("id")}
    repo = {n.get("id"): n.get("repo", "") for n in nodes}
    out: dict[str, set[str]] = {label: set() for label in tables.values() if label}
    for e in g.get("links", g.get("edges", [])):
        if e.get("relation") == "in_domain":
            continue
        s, t = e.get("source"), e.get("target")
        for table, other in ((s, t), (t, s)):
            if table in tables and other not in tables:
                r = repo.get(other, "")
                if r and r not in ("db", "domain"):
                    out.setdefault(tables[table], set()).add(r)
    with _lock:
        _GRAPH[graph] = (mtime, out)
    return out


# --- problems --------------------------------------------------------------------------------------
def problems(graph: str, allowed: set[str] | None = None, known_projects=None) -> list[dict]:
    """Rows in the shape of the `problems` index: {project, kind, where, message}.

      datadict-invalid        a mistake in the file (bad TOML, wrong type, unknown key, no such project)
      datadict-unknown-table  an entry names a table the graph does not have
      datadict-missing        a table the project's code uses has no entry (only for a project that
                              has a dictionary file: no file, no noise)
    """
    tables = graph_tables(graph)
    rows = []
    for project, path in files(allowed).items():
        where = os.path.basename(path)
        found, errors = load(path)
        if known_projects and project not in known_projects:
            rows.append({"project": project, "kind": "datadict-invalid", "where": where,
                         "message": f"no project named '{project}' in kb.projects.toml: name the file "
                                    "after the project"})
        for err in errors:
            rows.append({"project": project, "kind": "datadict-invalid", "where": where, "message": err})
        if not tables:
            continue   # no graph yet: nothing to check the names against
        for name in sorted(found):
            if name not in tables:
                rows.append({"project": project, "kind": "datadict-unknown-table", "where": name,
                             "message": f"{where} describes a table the graph does not know (a typo, a "
                                        "dropped table, or a table only .sql files create)"})
        for name in sorted(t for t, owners in tables.items() if project in owners and t not in found):
            rows.append({"project": project, "kind": "datadict-missing", "where": name,
                         "message": f"the code of {project} uses this table; {where} has no entry for it"})
    return rows


# --- matching a question ---------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9]+")
STOP = {
    "a", "an", "the", "of", "for", "per", "in", "on", "at", "by", "to", "and", "or", "with", "from",
    "which", "what", "where", "who", "how", "is", "are", "was", "be", "do", "does", "did", "it", "its",
    "this", "that", "these", "there", "we", "our", "i", "me", "my", "you", "can", "show", "find", "get",
    "list", "hold", "holds", "keep", "keeps", "kept", "store", "stores", "stored", "table", "tables",
    "data", "row", "rows", "column", "columns", "field", "fields", "each", "one", "all", "many", "much",
    "number", "total", "sum", "count",
}


def _norm(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def words(text: str) -> list[str]:
    """Lower-case content words, snake_case split, plural `s` dropped: `order_items` -> order, item."""
    return [_norm(w) for w in _WORD.findall(text.lower()) if w not in STOP]


def _phrase_in(phrase: list[str], seq: list[str]) -> bool:
    n = len(phrase)
    return bool(n) and any(seq[i:i + n] == phrase for i in range(len(seq) - n + 1))


def score(question: str, entry: dict) -> float:
    """How well an entry answers a question. A term, synonym or table name found whole in the
    question counts 3; each of their words counts 1; a word of the grain, a column or the notes 0.5."""
    q = words(question)
    if not q:
        return 0.0
    qs = set(q)
    strong_texts = [entry["name"], entry.get("term", ""), *entry.get("synonyms", [])]
    total = 0.0
    strong: set[str] = set()
    for text in strong_texts:
        w = words(text)
        strong |= set(w)
        if len(w) > 0 and _phrase_in(w, q):
            total += 3
    total += len(qs & strong)
    weak = set(words(entry.get("grain", "")) + words(entry.get("notes", "")))
    for col in entry.get("summable", []) + entry.get("not_summable", []):
        weak |= set(words(col))
    total += 0.5 * len((qs & weak) - strong)
    return total


def matches(question: str, by_project: dict[str, dict[str, dict]], limit: int = MAX_HITS) -> list[tuple]:
    """-> [(score, project, entry)], best first, at least one strong word or two weak ones."""
    hits = [(s, p, e) for p, table in by_project.items() for e in table.values()
            if (s := score(question, e)) >= 1]
    hits.sort(key=lambda h: (-h[0], h[2]["name"], h[1]))
    return hits[:limit]


def term_tables(symbol: str, by_project: dict[str, dict[str, dict]]) -> list[str]:
    """The tables whose term or synonym is exactly `symbol` (case and plural ignored)."""
    want = words(symbol)
    if not want:
        return []
    out = []
    for table in by_project.values():
        for e in table.values():
            if any(words(t) == want for t in [e.get("term", ""), *e.get("synonyms", [])] if t):
                if e["name"] not in out:
                    out.append(e["name"])
    return out


# --- text for the tools ------------------------------------------------------------------------------
def describe(entry: dict, indent: str = "    ") -> list[str]:
    lines = []
    for label, key in (("Term", "term"), ("Grain", "grain"), ("Sum", "summable"),
                       ("Do not sum", "not_summable"), ("Synonyms", "synonyms"), ("Notes", "notes")):
        value = entry.get(key)
        if value:
            text = ", ".join(value) if isinstance(value, list) else value
            lines.append(f"{indent}{label + ':':<12}{text}")
    return lines


def search_block(question: str, by_project: dict[str, dict[str, dict]]) -> str:
    """The block `search` puts before the code subgraph; empty when nothing matches."""
    hits = matches(question, by_project)
    if not hits:
        return ""
    lines = ["Data dictionary (tables whose meaning matches the question; graph_explain <table> for its code):"]
    for _s, project, e in hits:
        lines.append(f"  TABLE {e['name']}  [{project}]")
        lines += describe(e)
    return "\n".join(lines)


def with_search(out: str, question: str, by_project: dict[str, dict[str, dict]]) -> str:
    """Put the dictionary block after the `[projects: …]` line of a code answer."""
    block = search_block(question, by_project)
    if not block:
        return out
    head, sep, rest = out.partition("\n\n")
    if head.startswith("[projects:") and sep:
        return f"{head}\n\n{block}\n\n{rest}"
    return f"{block}\n\n{out}"


_HEADER = re.compile(r"(?m)^  ID:\s+db::(\S+)\s*$")


def with_explain(out: str, by_project: dict[str, dict[str, dict]]) -> str:
    """Under each table node header of a graph_explain answer, the table's meaning."""
    if not by_project or "db::" not in out:
        return out
    pieces, last = [], 0
    for m in _HEADER.finditer(out):
        name = m.group(1)
        found = [(p, t[name]) for p, t in sorted(by_project.items()) if name in t]
        if not found:
            continue
        end = out.find("\n\n", m.end())
        end = len(out) if end < 0 else end
        block = []
        for project, e in found:
            block.append(f"  Meaning:   (data dictionary of {project})")
            block += describe(e)
        pieces.append(out[last:end] + "\n" + "\n".join(block))
        last = end
    return "".join(pieces) + out[last:] if pieces else out


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "check":
        print("usage: datadict.py check [graph.json]", file=sys.stderr)
        return 2
    graph = argv[1] if len(argv) > 1 else str(paths.graph_path())
    found = files()
    if not found:
        print(f"no dictionary files in {folder()}")
        return 0
    rows = problems(graph)
    for project, path in found.items():
        print(f"{project}: {len(load(path)[0])} tables described ({path})")
    for r in rows:
        print(f"[{r['project']}] {r['kind']} {r['where']}\n    {r['message']}")
    print(f"{len(rows)} problem(s)")
    return 1 if any(r["kind"] != "datadict-missing" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
