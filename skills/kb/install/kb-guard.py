#!/usr/bin/env python3
"""`PreToolUse` hook that makes the `kb` Code Knowledge Base the first search tool of a session.

Copy this into `.claude/hooks/` and wire it in `.claude/settings.json` with the matcher
`Grep|Glob|Agent|Task` (see docs/install.md, "Make kb the first search tool").

Why it exists: a rule in CLAUDE.md and a reminder are advice, and an agent that judges a search
"trivial" goes to Grep anyway. This hook refuses a search (a Grep or Glob sweep, a search sub-agent)
until the session calls a `kb` tool once. After that call, also a failed one, it refuses nothing.

It lets through, always:
  * a call that is not a search: Grep in one known file, Glob without a wildcard, an agent that is not
    a search agent (extend `SEARCH_AGENTS`);
  * every call inside a sub-agent (the Agent call that started it was already checked);
  * a session where the `kb` server is not connected, so the agent cannot get stuck;
  * a folder outside `KB_GUARD_PATHS`, when that variable is set;
  * any error of its own (it fails open and exits 0).

How it knows that kb was used: it reads the session transcript (JSON lines) and counts only an
assistant block `{"type": "tool_use", "name": "mcp__kb__..."}`. A plain text match is wrong: Claude Code
writes the names of the deferred tools into the transcript at the start of every session.

Not covered: `grep`, `rg` and `find` through the Bash tool.

Environment:
  KB_GUARD=off          switch the guard off
  KB_GUARD_SERVER=name  the name of the MCP server (default `kb`, tools `mcp__kb__*`)
  KB_GUARD_PATHS=a:b    guard only sessions that run under one of these folders (default: everywhere)
"""

import json
import os
import sys

SERVER = os.environ.get("KB_GUARD_SERVER") or "kb"
PREFIX = f"mcp__{SERVER}__"

# Sub-agent types that search the code. Lower case. No type means general-purpose.
SEARCH_AGENTS = {"", "explore", "general-purpose", "plan"}

REASON = (
    f"kb first: this session has not asked the `{SERVER}` Code Knowledge Base yet. Start the search with a "
    f"{PREFIX}* tool: code_grep for exact text, search for a question, contracts_search for an endpoint, "
    "graph_explain for a symbol. If these tools are deferred, load them with the tool search first. After one "
    "kb call, also a failed one, Grep, Glob and search sub-agents work again. If you already know the file, "
    "read it or grep that one file."
)


def is_search(tool: str, tool_input: dict, cwd: str) -> bool:
    """True when the call looks for something whose file is not known yet."""
    if tool == "Grep":
        path = str(tool_input.get("path") or "")
        return not (path and os.path.isfile(os.path.join(cwd, path)))
    if tool == "Glob":
        return any(ch in str(tool_input.get("pattern") or "") for ch in "*?[{")
    if tool in ("Agent", "Task"):
        return str(tool_input.get("subagent_type") or "").lower() in SEARCH_AGENTS
    return False


def transcript_state(path: str) -> tuple[bool, bool]:
    """(the kb tools are listed in the session, a kb tool was called)."""
    listed = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if PREFIX not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "assistant":
                content = (row.get("message") or {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and str(block.get("name") or "").startswith(PREFIX)):
                        return True, True
            elif row.get("type") == "attachment":
                attachment = row.get("attachment") or {}
                if attachment.get("type") == "deferred_tools_delta" and any(
                        str(name).startswith(PREFIX) for name in attachment.get("addedLines") or []):
                    listed = True
    return listed, False


def _servers(path: str) -> list[dict]:
    """Every `mcpServers` object of one Claude Code config file: its own and the ones per project."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    found = [data.get("mcpServers")]
    projects = data.get("projects")
    if isinstance(projects, dict):
        found += [p.get("mcpServers") for p in projects.values() if isinstance(p, dict)]
    return [s for s in found if isinstance(s, dict)]


def configured(cwd: str) -> bool:
    """True when a config file names the server: `.mcp.json` of the folder or a parent, or `~/.claude.json`.
    Covers a session that lists its tools in full, so the transcript has no deferred list."""
    files = [os.path.join(os.path.expanduser("~"), ".claude.json")]
    folder = os.path.abspath(cwd)
    while True:
        files.append(os.path.join(folder, ".mcp.json"))
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return any(SERVER in servers for path in files for servers in _servers(path))


def guarded(cwd: str) -> bool:
    paths = [p for p in os.environ.get("KB_GUARD_PATHS", "").split(os.pathsep) if p]
    if not paths:
        return True
    here = os.path.abspath(cwd)
    return any(here == os.path.abspath(p) or here.startswith(os.path.abspath(p).rstrip(os.sep) + os.sep)
               for p in paths)


def refuses(payload: dict) -> bool:
    if os.environ.get("KB_GUARD", "").lower() in ("off", "0", "false", "no"):
        return False
    if payload.get("agent_id"):
        return False
    cwd = str(payload.get("cwd") or os.getcwd())
    tool_input = payload.get("tool_input")
    if not is_search(str(payload.get("tool_name") or ""), tool_input if isinstance(tool_input, dict) else {}, cwd):
        return False
    if not guarded(cwd):
        return False
    listed, used = transcript_state(str(payload.get("transcript_path") or ""))
    if used:
        return False
    return listed or configured(cwd)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        refuse = isinstance(payload, dict) and refuses(payload)
    except Exception:
        return 0
    if refuse:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
