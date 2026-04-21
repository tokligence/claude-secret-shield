#!/usr/bin/env python3
"""
E2E tests for the redmem guard hook (hooks/guard/agent_isolation_guard.py).

Style mirrors test_hook.py: subprocess-invoke the script with JSON stdin,
parse stdout, assert. Hermetic — uses REDMEM_GUARD_STATE_DIR so no files
touch ~/.claude/vault/.

Run:
    python3 test_guard.py
    # or
    python3 -m pytest test_guard.py -v
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "hooks", "guard", "agent_isolation_guard.py",
)


def _run(payload: dict, state_dir: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["REDMEM_GUARD_STATE_DIR"] = state_dir
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    parsed = None
    if r.stdout.strip():
        try:
            parsed = json.loads(r.stdout)
        except json.JSONDecodeError:
            parsed = None
    return parsed, r.returncode, r.stderr


def _state_path(state_dir: str) -> str:
    return os.path.join(state_dir, "active_agents.json")


def _read_state(state_dir: str) -> dict:
    p = _state_path(state_dir)
    if not os.path.exists(p):
        return {"agents": []}
    with open(p) as f:
        raw = f.read().strip()
    if not raw:
        return {"agents": []}
    return json.loads(raw)


def _write_state(state_dir: str, state: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    p = _state_path(state_dir)
    with open(p, "w") as f:
        json.dump(state, f)


def _pre(tool_input: dict, cwd: str, session_id: str = "sess1") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "session_id": session_id,
        "cwd": cwd,
    }


def _post(tool_input: dict, cwd: str, session_id: str = "sess1") -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "session_id": session_id,
        "cwd": cwd,
        "tool_result": "(ok)",
    }


class GuardTests(unittest.TestCase):
    """All tests are hermetic: each sets up its own TemporaryDirectory."""

    # ── Helpers ─────────────────────────────────────────────────────────

    def _mk_state_dir(self) -> str:
        d = tempfile.mkdtemp(prefix="redmem-guard-state-")
        self.addCleanup(lambda: self._rmtree(d))
        return d

    def _mk_repo_dir(self, init_git: bool = False) -> str:
        """Make a fake repo-root directory. If init_git, actually run `git init`
        so `git rev-parse --show-toplevel` resolves it. Otherwise the guard
        falls back to cwd."""
        d = tempfile.mkdtemp(prefix="redmem-guard-repo-")
        self.addCleanup(lambda: self._rmtree(d))
        if init_git:
            r = subprocess.run(
                ["git", "init", "-q", d], capture_output=True, text=True, timeout=10
            )
            # If git isn't available, the test still works (falls back to cwd).
            if r.returncode != 0:
                pass
        # Resolve symlinks (e.g. /var → /private/var on macOS) so equality
        # checks against `git rev-parse` output succeed.
        return os.path.realpath(d)

    def _rmtree(self, path: str) -> None:
        import shutil
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    # ── guard_01 ────────────────────────────────────────────────────────

    def test_guard_01_no_active_agents_allows(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        parsed, rc, err = _run(_pre({"description": "task A"}, repo), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed, f"expected no stdout; got {parsed}")
        # State should now have exactly one agent.
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 1)
        self.assertIsNone(state["agents"][0]["isolation"])

    # ── guard_02 ────────────────────────────────────────────────────────

    def test_guard_02_second_non_isolated_in_same_repo_denies(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        # First agent.
        _run(_pre({"description": "task A"}, repo), state_dir)
        # Second agent, same repo, no isolation → deny.
        parsed, rc, err = _run(_pre({"description": "task B"}, repo), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed, f"expected deny stdout; got stderr={err}")
        hso = parsed["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("redmem guard", hso["permissionDecisionReason"])
        # State must still only contain the first agent — denied call not recorded.
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 1)

    # ── guard_03 ────────────────────────────────────────────────────────

    def test_guard_03_isolated_call_bypasses_conflict(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        _run(_pre({"description": "task A"}, repo), state_dir)
        parsed, rc, err = _run(
            _pre({"description": "task B", "isolation": "worktree"}, repo),
            state_dir,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed, f"expected allow; got {parsed}")
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 2)
        # The second entry should be marked isolated.
        isolated_count = sum(1 for a in state["agents"] if a["isolation"] == "worktree")
        self.assertEqual(isolated_count, 1)

    # ── guard_04 ────────────────────────────────────────────────────────

    def test_guard_04_isolated_agent_does_not_block(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        # First agent is isolated.
        _run(
            _pre({"description": "task A", "isolation": "worktree"}, repo),
            state_dir,
        )
        # Second agent is non-isolated — should still allow because the
        # first didn't count as a conflict.
        parsed, rc, err = _run(_pre({"description": "task B"}, repo), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed)
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 2)

    # ── guard_05 ────────────────────────────────────────────────────────

    def test_guard_05_different_repo_does_not_block(self):
        state_dir = self._mk_state_dir()
        repo_a = self._mk_repo_dir()
        repo_b = self._mk_repo_dir()
        _run(_pre({"description": "task A"}, repo_a), state_dir)
        parsed, rc, err = _run(_pre({"description": "task B"}, repo_b), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed)
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 2)
        roots = {a["repo_root"] for a in state["agents"]}
        self.assertEqual(len(roots), 2)

    # ── guard_06 ────────────────────────────────────────────────────────

    def test_guard_06_stale_entry_purged(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        # Pre-seed a stale entry (50 min ago) in the same repo.
        stale_ts = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=50)
        ).isoformat()
        _write_state(state_dir, {
            "agents": [{
                "session_id": "old",
                "repo_root": repo,
                "fingerprint": "deadbeefcafebabe",
                "started_at": stale_ts,
                "isolation": None,
            }],
        })
        # New non-isolated call in same repo should succeed because stale entry gets purged.
        parsed, rc, err = _run(_pre({"description": "fresh"}, repo), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed, f"expected allow; got {parsed}, err={err}")
        state = _read_state(state_dir)
        # Old entry gone, only new fresh entry present.
        self.assertEqual(len(state["agents"]), 1)
        self.assertEqual(state["agents"][0]["session_id"], "sess1")

    # ── guard_07 ────────────────────────────────────────────────────────

    def test_guard_07_post_tool_use_removes_fingerprint(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        tool_input = {"description": "task X"}
        _run(_pre(tool_input, repo), state_dir)
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 1)
        # PostToolUse with the same input → should clear the entry.
        parsed, rc, err = _run(_post(tool_input, repo), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed)
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 0)

    # ── guard_08 ────────────────────────────────────────────────────────

    def test_guard_08_non_agent_tool_is_noop(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "sess",
            "cwd": repo,
        }
        parsed, rc, err = _run(payload, state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed)
        # No state file or empty agents list.
        if os.path.exists(_state_path(state_dir)):
            state = _read_state(state_dir)
            self.assertEqual(len(state["agents"]), 0)

    # ── guard_09 ────────────────────────────────────────────────────────

    def test_guard_09_corrupt_state_fails_open(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        os.makedirs(state_dir, exist_ok=True)
        with open(_state_path(state_dir), "w") as f:
            f.write("this is not JSON {{{")
        parsed, rc, err = _run(_pre({"description": "task"}, repo), state_dir)
        self.assertEqual(rc, 0, f"expected exit 0, got {rc}; stderr={err}")
        # Must not deny: corrupt state should fail open.
        self.assertIsNone(parsed, f"expected no stdout; got {parsed}")
        # Script should have logged something to stderr about the corruption.
        self.assertIn("[redmem-guard]", err)

    # ── guard_10 ────────────────────────────────────────────────────────

    def test_guard_10_bypass_file_allows_and_is_consumed(self):
        state_dir = self._mk_state_dir()
        repo = self._mk_repo_dir()
        # Seed an existing non-isolated agent so the next call would be blocked.
        _run(_pre({"description": "task A"}, repo), state_dir)
        # Drop the bypass marker.
        bypass_path = os.path.join(state_dir, ".guard_bypass")
        with open(bypass_path, "w") as f:
            f.write("")
        # Second call that would normally be denied — bypass allows it.
        parsed, rc, err = _run(_pre({"description": "task B"}, repo), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed, f"bypass should have allowed; got {parsed}")
        self.assertFalse(
            os.path.exists(bypass_path),
            "bypass file should have been deleted after one use",
        )
        # State should now contain both agents.
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 2)
        # Third call should deny again (bypass is one-shot).
        parsed2, rc2, _ = _run(_pre({"description": "task C"}, repo), state_dir)
        self.assertEqual(rc2, 0)
        self.assertIsNotNone(parsed2)
        self.assertEqual(
            parsed2["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    # ── guard_11 ────────────────────────────────────────────────────────

    def test_guard_11_non_git_cwd_uses_cwd_as_root(self):
        state_dir = self._mk_state_dir()
        # /tmp is not a git repo, so guard falls back to cwd.
        repo_like = tempfile.mkdtemp(prefix="redmem-guard-nogit-")
        self.addCleanup(lambda: self._rmtree(repo_like))
        repo_like = os.path.realpath(repo_like)
        parsed, rc, err = _run(_pre({"description": "task"}, repo_like), state_dir)
        self.assertEqual(rc, 0)
        self.assertIsNone(parsed, f"unexpected stdout: {parsed}, err={err}")
        state = _read_state(state_dir)
        self.assertEqual(len(state["agents"]), 1)
        self.assertEqual(state["agents"][0]["repo_root"], repo_like)


if __name__ == "__main__":
    unittest.main(verbosity=2)
