#!/usr/bin/env python3
"""Mutation pass for the HTTP-edge matcher (bin/link_http.py and its call-site patterns).

Unit tests can pass while being blind to a real regression. This harness breaks the matcher on
purpose, one small change at a time (a "mutant"), and runs the link_http tests against each copy.
A mutant that makes the tests fail is killed. A mutant the tests survive is a test gap.

Each mutant is an exact (old, new) text replacement. `old` must occur exactly once in its file, so
a refactor that moves the code fails loudly instead of silently testing nothing. The real files are
never touched: every mutant runs in a temporary copy of `bin/` and `tests/`.

Usage: python3 bench/mutation.py [-v] [--timeout SEC] [NAME ...]
Exit code: 0 when every mutant is killed, 1 when one survives or cannot be applied.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = "test_link_http.py"

LH = "bin/link_http.py"
JS = "bin/lang/js_ts.py"

# (name, file, old, new)
MUTANTS: list[tuple[str, str, str, str]] = [
    ("verb-default-post", JS,
     'return m.group(1).lower() if m else "get"',
     'return m.group(1).lower() if m else "post"'),
    ("verb-not-lowercased", JS,
     'return m.group(1).lower() if m else "get"',
     'return m.group(1) if m else "get"'),
    ("method-window-zero", JS,
     "METHOD_WINDOW = 3 ",
     "METHOD_WINDOW = 0 "),
    ("method-window-wide", JS,
     "METHOD_WINDOW = 3 ",
     "METHOD_WINDOW = 9 "),
    ("verb-ignores-next-call-boundary", JS,
     "if CALL_RE.search(nxt) or CLIENT_RE.search(nxt) or WRAPPER_RE.search(nxt) or VERSIONED_RE.search(nxt):\n            break",
     "if False:\n            break"),
    ("bare-fetch-any-url", JS,
     'if name == "fetch" and not raw.startswith("${"):',
     'if False:'),
    ("regex-lookbehind-dropped", JS,
     'r"(?<![\\w.])(\\$api|apiFetch|apiUpload|apiDownload|fetch)(?:<.*?>)?\\(\\s*[`\'\\"]([^`\'\\"]+)")',
     'r"(\\$api|apiFetch|apiUpload|apiDownload|fetch)(?:<.*?>)?\\(\\s*[`\'\\"]([^`\'\\"]+)")'),
    ("regex-versioned-any-path", JS,
     r"\(\s*(?:\(\)\s*=>\s*)?[`'\"](/?v\d+/",
     r"\(\s*(?:\(\)\s*=>\s*)?[`'\"](/?"),
    ("norm-base-url-not-stripped", LH,
     '    p = re.sub(r"^\\$\\{[^}]*\\}", "", p)',
     '    p = re.sub(r"^\\$\\{[^}]*\\}", "{}", p)'),
    ("norm-interpolation-dropped", LH,
     '    p = re.sub(r"\\$\\{[^}]*\\}", "{}", p)',
     '    p = re.sub(r"\\$\\{[^}]*\\}", "", p)'),
    ("norm-brace-param-kept", LH,
     '    p = re.sub(r"\\{[^}]*\\}", "{}", p)',
     '    p = re.sub(r"\\{[^}]*\\}", "{x}", p)'),
    ("norm-colon-param-kept", LH,
     '    p = re.sub(r":\\w+", "{}", p)',
     '    p = re.sub(r":\\w+", ":p", p)'),
    ("norm-query-kept", LH,
     '    p = p.split("?", 1)[0]',
     '    p = p'),
    ("norm-query-before-params", LH,
     '    p = re.sub(r"\\{[^}]*\\}", "{}", p)     # `{id}` / `{id?}` -> {}\n',
     '    p = p.split("?", 1)[0]\n    p = re.sub(r"\\{[^}]*\\}", "{}", p)\n'),
    ("norm-slashes-kept", LH,
     '    p = p.strip().strip("/")',
     '    p = p.strip()'),
    ("norm-double-slash-kept", LH,
     '    p = re.sub(r"//+", "/", p)\n',
     ''),
    ("norm-case-kept", LH,
     "    return p.lower()",
     "    return p"),
    ("action-non-controller-accepted", LH,
     'if "Controller" not in cls:',
     'if False:'),
    ("controller-bare-file-name-match", LH,
     'return len(parts) - parts.index("Http")',
     'return 1'),
    ("action-method-lost", LH,
     'return "/".join(cls.split("\\\\")) + ".php", (method or None)',
     'return "/".join(cls.split("\\\\")) + ".php", None'),
    ("routes-verb-not-lowercased", LH,
     "verb = verb.strip().lower()",
     "verb = verb.strip()"),
    ("routes-unknown-verb-kept", LH,
     "if verb in VERBS:",
     "if verb:"),
    ("match-exact-hit-skipped", LH,
     "    hit = routes.get((verb, path))\n    if hit:\n        return hit\n",
     ""),
    ("match-empty-path-allowed", LH,
     "    if not path:\n        return None\n",
     ""),
    ("match-suffix-without-slash", LH,
     'u.endswith("/" + path)',
     'u.endswith(path)'),
    ("match-endswith-inverted", LH,
     'u.endswith("/" + path)',
     'not u.endswith("/" + path)'),
    ("match-verb-ignored", LH,
     "for (v, u) in routes if v == verb and u.endswith",
     "for (v, u) in routes if u.endswith"),
    ("match-sort-dropped", LH,
     'cands = sorted((u for (v, u) in routes if v == verb and u.endswith("/" + path)), key=len)',
     'cands = [u for (v, u) in routes if v == verb and u.endswith("/" + path)]'),
    ("match-sort-reversed", LH,
     'u.endswith("/" + path)), key=len)',
     'u.endswith("/" + path)), key=lambda u: -len(u))'),
    ("match-sort-by-name", LH,
     'u.endswith("/" + path)), key=len)',
     'u.endswith("/" + path)))'),
    ("match-base-segment-any", LH,
     "if all(php_laravel.BASE_SEGMENT.match(seg) for seg in u[:-len(path) - 1].split(\"/\"))]",
     "if any(php_laravel.BASE_SEGMENT.match(seg) for seg in u[:-len(path) - 1].split(\"/\"))]"),
    ("match-based-not-shortest", LH,
     "return routes[(verb, based[0])]",
     "return routes[(verb, based[-1])]"),
    ("match-ambiguous-picks-one", LH,
     "return targets.pop() if len(targets) == 1 else None",
     "return targets.pop() if targets else None"),
    ("match-no-single-candidate-fallback", LH,
     "return targets.pop() if len(targets) == 1 else None",
     "return None"),
    ("scan-tests-dir-not-skipped", LH,
     "if backend and re.search(",
     "if False and re.search("),
    ("edge-signature-dedupe-off", LH,
     "if sig in seen:",
     "if False:"),
]


def apply(text: str, old: str, new: str, where: str) -> str:
    n = text.count(old)
    if n != 1:
        raise ValueError(f"{where}: `old` found {n} times, expected exactly 1")
    return text.replace(old, new)


def run_mutant(name: str, rel: str, old: str, new: str, timeout: int) -> tuple[str, str]:
    """Return (status, detail): killed | survived | error."""
    with tempfile.TemporaryDirectory(prefix="kb-mutant-") as tmp:
        shutil.copytree(os.path.join(ROOT, "bin"), os.path.join(tmp, "bin"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(tmp, "tests"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        target = os.path.join(tmp, rel)
        try:
            with open(target, encoding="utf-8") as fh:
                text = fh.read()
            mutated = apply(text, old, new, name)
        except (OSError, ValueError) as exc:
            return "error", str(exc)
        if mutated == text:
            return "error", f"{name}: the mutant changes nothing"
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        try:
            p = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", TEST_FILE],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "killed", "timeout"  # an endless loop is a visible failure too
        if p.returncode == 0:
            return "survived", ""
        tail = [ln for ln in (p.stderr or p.stdout).splitlines() if ln.startswith(("FAIL:", "ERROR:"))]
        return "killed", tail[0].split(" (")[0] if tail else "failed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="run only these mutants")
    ap.add_argument("--timeout", type=int, default=60, help="seconds per mutant (default 60)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    names = [m[0] for m in MUTANTS]
    if len(set(names)) != len(names):
        print("duplicate mutant names", file=sys.stderr)
        return 1
    unknown = [n for n in args.names if n not in names]
    if unknown:
        print(f"unknown mutant: {', '.join(unknown)}", file=sys.stderr)
        return 1
    chosen = [m for m in MUTANTS if not args.names or m[0] in args.names]

    # The suite must pass on the untouched code, or "killed" means nothing.
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", TEST_FILE],
                       cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("the link_http tests fail without any mutant:\n" + p.stderr, file=sys.stderr)
        return 1

    rows, bad = [], 0
    width = max(len(m[0]) for m in chosen)
    for name, rel, old, new in chosen:
        t0 = time.time()
        status, detail = run_mutant(name, rel, old, new, args.timeout)
        if status != "killed":
            bad += 1
        rows.append((name, status, detail))
        print(f"{name:<{width}}  {status:<8}  {time.time() - t0:5.1f}s  {detail if args.verbose or status != 'killed' else ''}",
              flush=True)
    killed = sum(1 for r in rows if r[1] == "killed")
    print(f"\n{killed}/{len(rows)} mutants killed" + ("" if not bad else f", {bad} NOT killed"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
