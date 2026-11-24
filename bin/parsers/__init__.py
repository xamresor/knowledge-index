"""Extraction plugins: one file per format, so parsers never turn into one pile.

Contract — a parser module declares four things and nothing else:

    EXTENSIONS  tuple[str, ...]   the suffixes it handles, lowercase, with the dot
    REQUIRES    tuple[str, ...]   external CLIs it shells out to (checked, never installed for you)
    CONFIDENCE  str              how much to trust the output: EXTRACTED | OCR
    extract(path: Path) -> str   the document as text/markdown, or raise ExtractError

Three rules the registry enforces so this stays honest:

1. **A missing tool means the format is unsupported, not a crash.** `pdftotext` is present on this
   machine, `tesseract` is not; a scan is then skipped with a loud line, and the build continues.
2. **Every extracted document carries its provenance** (source file, tool and version, timestamp,
   confidence) as a front-matter block. OCR output must never look like text somebody wrote.
3. **Zero Python dependencies.** Every parser is a thin wrapper over an external CLI, the same way
   the rest of this project wraps `graphify` and `qmd`. No PDF libraries, no OCR bindings.
"""
from __future__ import annotations

import datetime
import importlib
import os
import pkgutil
import re
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path

__all__ = ["ExtractError", "TransientError", "for_path", "runnable", "available", "extract", "with_provenance",
           "run_tool", "MISSING_TOOLS"]

EXTRACTED = "EXTRACTED"   # a real text layer: the characters came from the file
OCR = "OCR"               # recognised from pixels: expect noise, never quote it as authoritative

MISSING_TOOLS: dict[str, tuple[str, ...]] = {}   # extension -> tools that were absent


class ExtractError(RuntimeError):
    """The format is known and the tool is present, but this file could not be read."""


class TransientError(ExtractError):
    """The tool did not finish this time (a timeout, an OS error): try again on the next build."""


DEFAULT_MAX_OUTPUT_MB = 20   # KB_EXTRACT_MAX_OUTPUT_MB: text that one tool run may produce
DEFAULT_MAX_MEM_MB = 2048     # KB_EXTRACT_MAX_MEM_MB: memory (data segment) of one tool run


def _mb(name: str, default: float) -> int:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(1, int(value * 2**20))


def run_tool(cmd: list[str], timeout: float) -> tuple[str, str]:
    """Run an extraction tool with caps: (stdout, stderr) as text.

    A small file can expand without limit: a .docx is a zip, a PNG can have 50 000 x 50 000 pixels.
    So the tool runs with a memory cap (RLIMIT_DATA, KB_EXTRACT_MAX_MEM_MB) and a cap on every file it
    writes (RLIMIT_FSIZE, KB_EXTRACT_MAX_OUTPUT_MB). Its stdout goes to a temp file, not to memory.
    Output over the cap is cut, and the text says so; stderr is kept up to 4 KB. A tool that runs out of memory fails, and the
    caller reports the file. A timeout or an OS error is transient: the next build tries again.
    """
    out_cap = _mb("KB_EXTRACT_MAX_OUTPUT_MB", DEFAULT_MAX_OUTPUT_MB)
    mem_cap = _mb("KB_EXTRACT_MAX_MEM_MB", DEFAULT_MAX_MEM_MB)

    def limits() -> None:  # runs in the child, before the tool starts
        resource.setrlimit(resource.RLIMIT_DATA, (mem_cap, mem_cap))
        resource.setrlimit(resource.RLIMIT_FSIZE, (out_cap + 1, out_cap + 1))

    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout,
                           preexec_fn=limits)
            out.seek(0)
            data = out.read(out_cap + 1)
            err.seek(0)
            errors = err.read(4096)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransientError(f"{cmd[0]} failed: {exc}") from exc
    text = data[:out_cap].decode("utf-8", errors="replace")
    if len(data) > out_cap:
        text += f"\n\n<!-- output cut at {out_cap / 2**20:g} MB (KB_EXTRACT_MAX_OUTPUT_MB) -->"
    return text, errors.decode("utf-8", errors="replace")


def _modules():
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if not info.name.startswith("_"):
            yield importlib.import_module(f"{__package__}.{info.name}")


def registry() -> dict[str, list]:
    """extension -> [parser modules], best first. Order is declaration order within a module list."""
    table: dict[str, list] = {}
    for module in sorted(_modules(), key=lambda m: getattr(m, "PRIORITY", 50)):
        for ext in module.EXTENSIONS:
            table.setdefault(ext.lower(), []).append(module)
    return table


def _tools_present(module) -> bool:
    return all(shutil.which(tool) for tool in module.REQUIRES)


def runnable(path: Path | str) -> list:
    """Every parser that can actually run for this file, best first. The caller tries the next one
    when a parser raises ExtractError (a PDF without a text layer goes on to OCR).

    A format with parsers but no installed tool records itself in MISSING_TOOLS, so the caller can
    report *why* a file was skipped instead of leaving a silent hole in the index.
    """
    ext = Path(path).suffix.lower()
    candidates = registry().get(ext, [])
    ready = [m for m in candidates if _tools_present(m)]
    if candidates and not ready:
        missing = tuple(sorted({t for m in candidates for t in m.REQUIRES if not shutil.which(t)}))
        MISSING_TOOLS[ext] = missing
    return ready


def for_path(path: Path | str):
    """The best parser that can actually run for this file, or None."""
    ready = runnable(path)
    return ready[0] if ready else None


def available() -> dict[str, str]:
    """Formats that can be extracted right now: extension -> parser name. For status output."""
    return {ext: mods[0].__name__.rsplit(".", 1)[-1]
            for ext, mods in registry().items()
            if any(_tools_present(m) for m in mods)}


_VERSIONISH = re.compile(r"\d+\.\d+")
_NOT_A_VERSION = re.compile(r"error|usage:|unknown option", re.IGNORECASE)


def _tool_version(tool: str) -> str:
    """Best-effort version string, **validated**.

    Version flags are not standard: poppler's `pdftotext` answers `-v` and treats `--version` as a
    filename, printing an I/O error. Taking the first output blindly put that error message into a
    provenance header — so an answer only counts if it looks like a version and not like a complaint.
    """
    for flag in ("--version", "-v", "-version"):
        try:
            out = subprocess.run([tool, flag], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in ((out.stdout or "") + "\n" + (out.stderr or "")).splitlines():
            line = line.strip()
            if line and _VERSIONISH.search(line) and not _NOT_A_VERSION.search(line):
                return line[:80]
    return tool


def with_provenance(text: str, source: Path, module) -> str:
    """Prepend the block that keeps an extracted document distinguishable from an authored one."""
    tools = ", ".join(_tool_version(t) for t in module.REQUIRES) or "none"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header = "\n".join([
        "---",
        f'extracted_from: "{source}"',
        f'extracted_by: "{module.__name__.rsplit(".", 1)[-1]}"',
        f'extracted_tool: "{tools}"',
        f"extracted_at: {stamp}",
        f"confidence: {module.CONFIDENCE}",
        "---",
        "",
        f"<!-- Generated from {source.name}. Do not edit: rebuild instead. -->",
        "",
    ])
    return header + text.strip() + "\n"


def extract(path: Path | str) -> str | None:
    """Extract one file with provenance, or None when no usable parser exists."""
    path = Path(path)
    module = for_path(path)
    if module is None:
        return None
    return with_provenance(module.extract(path), path, module)
