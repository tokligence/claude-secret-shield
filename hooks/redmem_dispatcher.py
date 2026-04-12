#!/usr/bin/env python3
"""
redmem dispatcher — single entry point for all Claude Code hook events.

Routes to:
- shield (redact-restore.py) for secret protection
- memory module for session archival/search/resume
"""
import json
import os
import re
import subprocess
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))

# Task/plan tools that trigger state tracking
TASK_PLAN_TOOLS = {"TodoWrite", "TodoRead", "EnterPlanMode", "ExitPlanMode",
                   "TaskCreate", "TaskUpdate"}

# Recall-intent keywords
RECALL_RE = re.compile(
    r"(before|earlier|remember|recall|之前|上次|记得|还记得|migration\s+\d+)",
    re.IGNORECASE,
)


def run_shield(input_json: str) -> dict:
    """Run the existing redact-restore.py and capture its output."""
    shield_path = os.path.join(HOOKS_DIR, "redact-restore.py")
    try:
        result = subprocess.run(
            [sys.executable, shield_path],
            input=input_json,
            capture_output=True,
            text=True,
            timeout=25,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        return {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}


def handle_pre_compact(data: dict):
    """Archive turns to SQLite on compact."""
    session_id = data.get("session_id", "")
    cwd = data.get("cwd", "")
    if not session_id:
        return

    try:
        from memory.ingest import archive_turns
        count = archive_turns(session_id, cwd)
        if count > 0:
            sys.stderr.write(f"[redmem] Archived {count} turns\n")
        # Generate session_state.md (Phase 1.5)
        from memory.session_state import generate_session_state
        generate_session_state(session_id, cwd)
    except Exception as e:
        sys.stderr.write(f"[redmem] Archive error: {e}\n")


def handle_session_start(data: dict):
    """Inject resume context."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    try:
        from memory.summarize import build_resume_context
        context = build_resume_context(session_id)
        if context:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }))
            return
    except Exception as e:
        sys.stderr.write(f"[redmem] Resume error: {e}\n")


def handle_user_prompt_memory(data: dict, shield_result: dict) -> dict:
    """Search archive if recall keywords detected (Phase 2)."""
    prompt = data.get("prompt", "")
    if not RECALL_RE.search(prompt):
        return shield_result

    session_id = data.get("session_id", "")
    if not session_id:
        return shield_result

    try:
        from memory.search import search, format_results
        results = search(session_id, prompt, limit=5)
        if results:
            formatted = format_results(results)
            # Merge with shield's additionalContext if any
            hook_output = shield_result.get("hookSpecificOutput", {})
            existing_ctx = hook_output.get("additionalContext", "")
            if existing_ctx:
                formatted = existing_ctx + "\n\n" + formatted
            shield_result.setdefault("hookSpecificOutput", {})
            shield_result["hookSpecificOutput"]["additionalContext"] = formatted
    except Exception as e:
        sys.stderr.write(f"[redmem] Search error: {e}\n")

    return shield_result


def handle_task_event(data: dict):
    """Track task/plan changes for session state (Phase 1.5)."""
    session_id = data.get("session_id", "")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_result = data.get("tool_result", {})
    if not session_id:
        return
    try:
        from memory.session_state import track_state_event
        track_state_event(session_id, tool_name, tool_input, tool_result)
    except Exception as e:
        sys.stderr.write(f"[redmem] Task tracking error: {e}\n")


def main():
    raw_input = sys.stdin.read()
    try:
        data = json.loads(raw_input)
    except json.JSONDecodeError:
        sys.exit(0)

    event = data.get("hook_event_name", "")

    # ── PreCompact: archive turns (memory only, shield not involved) ──
    if event == "PreCompact":
        handle_pre_compact(data)
        sys.exit(0)

    # ── SessionStart: inject resume context (memory only) ──
    if event == "SessionStart":
        source = data.get("source", "")
        if source == "resume":
            handle_session_start(data)
        sys.exit(0)

    # ── UserPromptSubmit: shield first, then memory search ──
    if event == "UserPromptSubmit":
        shield_result = run_shield(raw_input)

        # If shield blocked, don't run memory search
        blocked = shield_result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        if shield_result.get("decision") == "block":
            blocked = True

        if not blocked:
            shield_result = handle_user_prompt_memory(data, shield_result)

        if shield_result:
            print(json.dumps(shield_result))
        sys.exit(0)

    # ── PostToolUse: shield handles restore, then track tasks ──
    if event == "PostToolUse":
        tool_name = data.get("tool_name", "")
        shield_result = run_shield(raw_input)

        if tool_name in TASK_PLAN_TOOLS:
            handle_task_event(data)

        if shield_result:
            print(json.dumps(shield_result))
        sys.exit(0)

    # ── All other events (PreToolUse, SessionEnd): pass to shield ──
    shield_result = run_shield(raw_input)
    if shield_result:
        print(json.dumps(shield_result))
    sys.exit(0)


if __name__ == "__main__":
    main()
