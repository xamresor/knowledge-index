#!/usr/bin/env python3
"""Reusable `UserPromptSubmit` hook for the `kb` Code Knowledge Base.

Copy this into a consuming project's `.claude/hooks/` and wire it in
`.claude/settings.json` (see the kb README "Install into a project", step 3).

Why it exists: the `kb` skill only fires on the agent's own judgement, and the
`kb` MCP tools may be *deferred* behind a tool-search index in projects with many
MCP servers — so an agent can silently default to grep/Explore on a connection /
architecture / impact question and never reach the graph. This hook scans the
submitted prompt for trigger phrases and injects a one-line reminder to use the
`kb` graph tools first.

It reads the hook payload as JSON on stdin and, on a trigger-word match, prints a
JSON object whose hookSpecificOutput.additionalContext is injected into context.
It stays silent (and exits 0) on no match or any error, so it can never block a
prompt.

EXTEND `TRIGGERS` with the working languages of your team. The defaults cover
English + Russian; add your own phrasings rather than removing existing ones.
"""

import json
import sys

# Lower-cased substrings. "how does it work", "what connects to", "impact /
# blast radius", "trace", "shortest path", "where did we document".
TRIGGERS = [
    # English
    "how does",
    "how do ",
    "what connects",
    "what depends",
    "blast radius",
    "impact analysis",
    "shortest path",
    "where did we document",
    "which controller serves",
    "trace the",
    "trace this",
    "what breaks if",
    # Russian
    "как работает",
    "как устроен",
    "что связано",
    "что сломается",
    "на что влияет",
    "зависит",
    "кто вызывает",
    "кто использует",
    "архитектур",
    "трасс",
    "где задокументирован",
    "где мы писали",
]

REMINDER = (
    "This prompt looks like a cross-codebase connection/architecture/impact "
    "question. Reach for the `kb` Code Knowledge Base FIRST, before grep/Explore: "
    "invoke the `kb` skill and use its graph MCP tools (graph_query, "
    "graph_explain, graph_affected, graph_path, docs_search). They follow real "
    "edges (calls, eloquent, fk, sql, cross-repo http_request frontend->controller) "
    "and are more precise than text search. If the kb tools are deferred behind a "
    "tool-search index, load their schemas first, then call them. If you delegate "
    "to a sub-agent, tell it to use the kb graph tools explicitly. Only fall back "
    "to grep/Explore if the graph does not answer the question."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = str(payload.get("prompt", "")).lower()
    if not prompt:
        return 0

    if not any(trigger in prompt for trigger in TRIGGERS):
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": REMINDER,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
