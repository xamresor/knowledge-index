"""The kb-first guard hook (skills/kb/install/kb-guard.py): what it refuses and what it lets through."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "skills", "kb", "install", "kb-guard.py")

# What Claude Code writes at the start of a session when the kb tools are behind the tool search.
DEFERRED = {"type": "attachment", "attachment": {"type": "deferred_tools_delta",
                                                 "addedLines": ["Monitor", "mcp__kb__batch", "mcp__kb__code_grep"]}}
# A prompt that only talks about the tools. The old guard read it as "kb was used".
TALK = {"type": "user", "message": {"role": "user", "content": 'the guard looks for "mcp__kb__search" in the text'}}
KB_CALL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "I ask the knowledge base."},
    {"type": "tool_use", "id": "t1", "name": "mcp__kb__code_grep", "input": {"pattern": "x"}}]}}
OTHER_CALL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "grep -r mcp__kb__ ."}}]}}


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.join(self.tmp.name, "repo")
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.cwd)
        os.makedirs(self.home)
        with open(os.path.join(self.cwd, "one.php"), "w") as f:
            f.write("<?php\n")

    def transcript(self, *rows) -> str:
        path = os.path.join(self.tmp.name, "session.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.write("not json at all, with mcp__kb__ inside\n")       # a broken line never stops the guard
        return path

    def run_guard(self, tool: str, tool_input: dict, transcript: str | None, env: dict | None = None,
                  **extra) -> dict | None:
        payload = {"hook_event_name": "PreToolUse", "cwd": self.cwd, "transcript_path": transcript,
                   "tool_name": tool, "tool_input": tool_input, **extra}
        full_env = {"PATH": os.environ.get("PATH", ""), "HOME": self.home, **(env or {})}
        out = subprocess.run([sys.executable, GUARD], input=json.dumps(payload), capture_output=True, text=True,
                             env=full_env, check=True).stdout
        return json.loads(out) if out.strip() else None

    def assertDenied(self, answer):
        self.assertIsNotNone(answer, "the guard let the call through")
        out = answer["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("kb", out["permissionDecisionReason"])

    # --- refused: a search with no kb call before it -----------------------------------------------------
    def test_a_sweep_is_refused_when_kb_is_only_listed_or_mentioned(self):
        t = self.transcript(DEFERRED, TALK, OTHER_CALL)
        self.assertDenied(self.run_guard("Grep", {"pattern": "Order"}, t))
        self.assertDenied(self.run_guard("Grep", {"pattern": "Order", "path": self.cwd}, t))
        self.assertDenied(self.run_guard("Glob", {"pattern": "**/*Order*.php"}, t))
        self.assertDenied(self.run_guard("Agent", {"subagent_type": "Explore", "prompt": "find it"}, t))
        self.assertDenied(self.run_guard("Task", {"prompt": "find it"}, t))            # no type = general-purpose

    def test_it_keeps_refusing_until_kb_is_called(self):
        t = self.transcript(DEFERRED)
        for _ in range(3):
            self.assertDenied(self.run_guard("Grep", {"pattern": "Order"}, t))

    def test_a_server_in_mcp_json_counts_as_connected_without_the_deferred_list(self):
        with open(os.path.join(self.cwd, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"kb": {"type": "http", "url": "http://kb.example/mcp"}}}, f)
        self.assertDenied(self.run_guard("Grep", {"pattern": "Order"}, self.transcript(TALK)))

    def test_a_server_in_the_user_config_counts_as_connected(self):
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"projects": {self.cwd: {"mcpServers": {"kb": {"command": "kb-mcp"}}}}}, f)
        self.assertDenied(self.run_guard("Grep", {"pattern": "Order"}, self.transcript(TALK)))

    def test_another_server_name(self):
        row = {"type": "attachment", "attachment": {"type": "deferred_tools_delta", "addedLines": ["mcp__code__search"]}}
        t = self.transcript(row)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, t))
        self.assertDenied(self.run_guard("Grep", {"pattern": "Order"}, t, env={"KB_GUARD_SERVER": "code"}))

    # --- let through --------------------------------------------------------------------------------------
    def test_after_one_kb_call_everything_passes(self):
        t = self.transcript(DEFERRED, KB_CALL)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, t))
        self.assertIsNone(self.run_guard("Glob", {"pattern": "**/*.php"}, t))
        self.assertIsNone(self.run_guard("Agent", {"subagent_type": "Explore", "prompt": "find it"}, t))

    def test_a_known_file_is_not_a_search(self):
        t = self.transcript(DEFERRED)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order", "path": os.path.join(self.cwd, "one.php")}, t))
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order", "path": "one.php"}, t))    # relative to cwd
        self.assertIsNone(self.run_guard("Glob", {"pattern": "app/Models/Order.php"}, t))       # no wildcard
        self.assertIsNone(self.run_guard("Agent", {"subagent_type": "statusline-setup", "prompt": "x"}, t))
        self.assertIsNone(self.run_guard("Read", {"file_path": "one.php"}, t))

    def test_kb_not_connected_blocks_nothing(self):
        t = self.transcript(TALK, OTHER_CALL)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, t))
        self.assertIsNone(self.run_guard("Agent", {"subagent_type": "Explore", "prompt": "find it"}, t))

    def test_inside_a_sub_agent_nothing_is_blocked(self):
        t = self.transcript(DEFERRED)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, t, agent_id="a1", agent_type="Explore"))

    def test_outside_the_guarded_folders_nothing_is_blocked(self):
        t = self.transcript(DEFERRED)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, t, env={"KB_GUARD_PATHS": "/srv/elsewhere"}))
        paths = os.pathsep.join(["/srv/elsewhere", self.tmp.name])
        self.assertDenied(self.run_guard("Grep", {"pattern": "Order"}, t, env={"KB_GUARD_PATHS": paths}))

    def test_switched_off_or_broken_input_blocks_nothing(self):
        t = self.transcript(DEFERRED)
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, t, env={"KB_GUARD": "off"}))
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, os.path.join(self.tmp.name, "missing.jsonl")))
        self.assertIsNone(self.run_guard("Grep", {"pattern": "Order"}, None))
        out = subprocess.run([sys.executable, GUARD], input="not json", capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
