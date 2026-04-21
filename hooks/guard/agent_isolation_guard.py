#!/usr/bin/env python3
"""
redmem guard — agent isolation guard (optional, opt-in).

Prevents the most common pitfall in Claude Code parallel agent workflows:
the parent spawns multiple Agent tool calls that touch the same git repo
without `isolation: "worktree"`, causing them to stomp on each other's
uncommitted changes.

This hook is OPT-IN (installed only via `./install.sh --with-guard`) and is
NOT routed through redmem_dispatcher.py — it is a standalone hook wired
directly into PreToolUse / PostToolUse for the `Agent` tool.

Semantics:
- PreToolUse(Agent): if another Agent is already active in the same repo
  root without `isolation: "worktree"`, and the incoming call also lacks
  isolation, deny with a friendly message explaining how to fix.
- PostToolUse(Agent): remove the fingerprint from state so subsequent
  calls are unblocked.

Bypass:
- `touch ~/.claude/vault/.guard_bypass` grants a one-shot override. The
  file is deleted the first time a PreToolUse(Agent) conflict would
  otherwise deny.

Fail mode: FAIL-OPEN. This is ergonomics, not safety. Any exception is
logged to stderr (single line, `[redmem-guard]` prefix) and the hook exits
0 without emitting any JSON response.

Hook input (stdin JSON):
  - hook_event_name: "PreToolUse" | "PostToolUse"
  - tool_name: "Agent" | ...
  - tool_input: { isolation?: "worktree", ... }
  - session_id: string
  - cwd: string (absolute path, used to resolve repo root)

Hook output (PreToolUse deny only):
  {
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "..."
    }
  }
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys

# ── Constants ───────────────────────────────────────────────────────────

STALE_AFTER_SECONDS = 45 * 60  # 45 minutes
LOG_PREFIX = "[redmem-guard]"


def _vault_dir() -> str:
    """State dir — overridable via env var for tests."""
    override = os.environ.get("REDMEM_GUARD_STATE_DIR")
    if override:
        return override
    return os.path.expanduser("~/.claude/vault")


def _state_path() -> str:
    return os.path.join(_vault_dir(), "active_agents.json")


def _bypass_path() -> str:
    return os.path.join(_vault_dir(), ".guard_bypass")


def _log(msg: str) -> None:
    """Single-line stderr log, prefixed."""
    try:
        sys.stderr.write(f"{LOG_PREFIX} {msg}\n")
    except Exception:
        pass


# ── Repo root resolution ────────────────────────────────────────────────

def _resolve_repo_root(cwd: str) -> str:
    """Resolve the git repo root for cwd, or fall back to cwd itself.

    Any git failure (not a repo, command missing, timeout) → cwd.
    """
    if not cwd:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            if root:
                return root
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # Normalize to an absolute path so equality comparisons work.
    try:
        return os.path.abspath(cwd)
    except Exception:
        return cwd


# ── Fingerprint ─────────────────────────────────────────────────────────

def _fingerprint(tool_input) -> str:
    """16-char sha256 of canonical JSON of tool_input."""
    try:
        canonical = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        canonical = repr(tool_input)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]


# ── State file IO with flock ────────────────────────────────────────────

def _open_state_fd():
    """Open the state file (create if missing) and take an exclusive lock.

    Returns a file descriptor positioned at 0 with LOCK_EX held. Caller must
    close it (which releases the lock).
    """
    vault = _vault_dir()
    os.makedirs(vault, exist_ok=True)
    path = _state_path()
    # Use 'a+' semantics via os.open so we don't truncate on create.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd


def _read_state(fd: int) -> dict:
    """Read JSON from an open, locked fd. Corrupt/empty → fresh state."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
        if not raw.strip():
            return {"agents": []}
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
            _log("state file malformed (missing agents list); resetting")
            return {"agents": []}
        return data
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _log(f"state file corrupt ({e.__class__.__name__}); resetting")
        return {"agents": []}


def _write_state(fd: int, state: dict) -> None:
    """Overwrite the state file on an open, locked fd."""
    payload = json.dumps(state, sort_keys=True).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    # Loop because os.write may write fewer bytes than requested.
    to_write = payload
    while to_write:
        n = os.write(fd, to_write)
        if n <= 0:
            break
        to_write = to_write[n:]


# ── Stale purge ─────────────────────────────────────────────────────────

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(s: str):
    """Parse ISO-8601 UTC. Returns None on failure."""
    if not isinstance(s, str):
        return None
    try:
        # Tolerate both 'Z' and '+00:00' tails
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _purge_stale(agents: list) -> list:
    now = _utcnow()
    fresh = []
    for a in agents:
        ts = _parse_ts(a.get("started_at", ""))
        if ts is None:
            # Unparseable timestamp: treat as stale.
            continue
        # Ensure timezone-aware comparison
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        age = (now - ts).total_seconds()
        if age < STALE_AFTER_SECONDS:
            fresh.append(a)
    return fresh


# ── Deny response ──────────────────────────────────────────────────────

def _emit_deny(active_in_repo: list, repo_root: str) -> None:
    """Write a deny-shaped JSON response to stdout."""
    timestamps = ", ".join(a.get("started_at", "?") for a in active_in_repo)
    count = len(active_in_repo)
    reason = (
        f"redmem guard: detected {count} active agent(s) in this repo "
        f"({repo_root}) without isolation. Pass `isolation: \"worktree\"` "
        f"to this Agent call, or wait for the prior agent(s) to finish. "
        f"Active since: {timestamps}. Override: "
        f"`touch ~/.claude/vault/.guard_bypass` (expires after one call)."
    )
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(response))


# ── Bypass ──────────────────────────────────────────────────────────────

def _consume_bypass() -> bool:
    """If bypass file exists, delete it and return True (one-shot)."""
    path = _bypass_path()
    try:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                # Lost the race to another caller; treat as not-consumed.
                return False
            _log("bypass consumed")
            return True
    except OSError as e:
        _log(f"bypass check failed: {e.__class__.__name__}: {e}")
    return False


# ── Event handlers ─────────────────────────────────────────────────────

def _handle_pre(data: dict) -> None:
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    isolation = tool_input.get("isolation")
    this_isolated = (isolation == "worktree")

    cwd = data.get("cwd") or ""
    repo_root = _resolve_repo_root(cwd) if cwd else ""
    session_id = data.get("session_id", "")
    fp = _fingerprint(tool_input)

    fd = _open_state_fd()
    try:
        state = _read_state(fd)
        agents = _purge_stale(state.get("agents", []))

        # Count active non-isolated agents in the same repo.
        conflicts = [
            a for a in agents
            if a.get("repo_root") == repo_root and a.get("isolation") != "worktree"
        ]

        # If the current call is NOT isolated and there are existing
        # non-isolated agents in the same repo, we would deny — unless
        # bypass is active.
        if (not this_isolated) and repo_root and conflicts:
            if _consume_bypass():
                # Allow + record this agent.
                agents.append({
                    "session_id": session_id,
                    "repo_root": repo_root,
                    "fingerprint": fp,
                    "started_at": _utcnow().isoformat(),
                    "isolation": None,
                })
                _write_state(fd, {"agents": agents})
                return
            # Deny: do NOT record this agent (since it won't run).
            # Still persist the stale-purged state so we don't leak.
            _write_state(fd, {"agents": agents})
            _emit_deny(conflicts, repo_root)
            return

        # No conflict (or this call is isolated). Record and allow.
        agents.append({
            "session_id": session_id,
            "repo_root": repo_root,
            "fingerprint": fp,
            "started_at": _utcnow().isoformat(),
            "isolation": "worktree" if this_isolated else None,
        })
        _write_state(fd, {"agents": agents})
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _handle_post(data: dict) -> None:
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = data.get("cwd") or ""
    repo_root = _resolve_repo_root(cwd) if cwd else ""
    fp = _fingerprint(tool_input)

    fd = _open_state_fd()
    try:
        state = _read_state(fd)
        agents = _purge_stale(state.get("agents", []))
        # Remove first match on (repo_root, fingerprint).
        removed = False
        kept = []
        for a in agents:
            if (not removed
                    and a.get("fingerprint") == fp
                    and a.get("repo_root") == repo_root):
                removed = True
                continue
            kept.append(a)
        _write_state(fd, {"agents": kept})
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ── Main ────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            _log(f"bad stdin json: {e.__class__.__name__}: {e}")
            return
        if not isinstance(data, dict):
            return

        event = data.get("hook_event_name", "")
        tool_name = data.get("tool_name", "")

        # Only act on Agent tool events.
        if tool_name != "Agent":
            return

        if event == "PreToolUse":
            _handle_pre(data)
        elif event == "PostToolUse":
            _handle_post(data)
        # Other events: silent noop.
    except Exception as e:  # fail-OPEN
        _log(f"unexpected error: {e.__class__.__name__}: {e}")
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
