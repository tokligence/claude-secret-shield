#!/usr/bin/env python3
"""
redmem catchup — archive running sessions without restarting them.

Usage:
    python3 redmem_catchup.py                    # archive all discovered sessions
    python3 redmem_catchup.py --session <id>     # archive specific session
    python3 redmem_catchup.py --watch            # continuously archive every 60s
    python3 redmem_catchup.py --watch --interval 120  # custom interval

How it works:
    Directly reads Claude Code session JSONL files from ~/.claude/projects/
    and runs the same incremental ingest logic as the PreCompact hook.
    Works on running sessions — the JSONL is written in real-time.
"""
import argparse
import os
import sys
import time
import re

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

from memory.ingest import archive_turns
from memory.session_state import generate_session_state
from memory.knowledge import update_session_knowledge


PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def discover_sessions(max_age_days: int = 30):
    """
    Find all session JSONL files modified within max_age_days.
    Returns list of (session_id, cwd, jsonl_path, mtime).
    """
    if not os.path.isdir(PROJECTS_DIR):
        return []

    cutoff = time.time() - (max_age_days * 86400)
    sessions = []

    for project_dir_name in os.listdir(PROJECTS_DIR):
        project_path = os.path.join(PROJECTS_DIR, project_dir_name)
        if not os.path.isdir(project_path):
            continue

        # Reverse the encoding: "-Users-tonyseah-klee" -> "/Users/tonyseah/klee"
        if project_dir_name.startswith("-"):
            cwd = "/" + project_dir_name[1:].replace("-", "/")
        else:
            cwd = "/" + project_dir_name.replace("-", "/")

        for fname in os.listdir(project_path):
            if not fname.endswith(".jsonl"):
                continue
            session_id = fname[:-6]
            # UUID format check (loose)
            if not re.match(r"^[0-9a-f-]{36}$", session_id):
                continue

            jsonl_path = os.path.join(project_path, fname)
            mtime = os.path.getmtime(jsonl_path)
            if mtime < cutoff:
                continue

            sessions.append((session_id, cwd, jsonl_path, mtime))

    # Sort by mtime descending (most recent first)
    sessions.sort(key=lambda s: -s[3])
    return sessions


def archive_one(session_id: str, cwd: str, verbose: bool = True) -> int:
    """Archive a single session. Returns turn count."""
    try:
        count = archive_turns(session_id, cwd)
        if count > 0:
            if verbose:
                print(f"  [{session_id[:8]}] archived {count} new turns")
            # Also generate session_state + knowledge
            generate_session_state(session_id, cwd)
            update_session_knowledge(session_id, cwd)
        return count
    except Exception as e:
        if verbose:
            print(f"  [{session_id[:8]}] ERROR: {e}", file=sys.stderr)
        return 0


def run_once(args):
    """Archive all discovered sessions once."""
    if args.session:
        # Specific session
        sessions = [s for s in discover_sessions(args.max_age_days)
                    if s[0] == args.session]
        if not sessions:
            print(f"Session {args.session} not found in {PROJECTS_DIR}")
            return 1
    else:
        sessions = discover_sessions(args.max_age_days)

    if not sessions:
        print(f"No sessions found in {PROJECTS_DIR} (within {args.max_age_days} days)")
        return 0

    print(f"Archiving {len(sessions)} sessions...")
    total_turns = 0
    start = time.time()
    for session_id, cwd, jsonl_path, mtime in sessions:
        size_mb = os.path.getsize(jsonl_path) / 1024 / 1024
        age_hours = (time.time() - mtime) / 3600
        print(f"  [{session_id[:8]}] {size_mb:.1f}MB, {age_hours:.1f}h ago, cwd={cwd}")
        total_turns += archive_one(session_id, cwd, verbose=True)

    elapsed = time.time() - start
    print(f"\nDone: {total_turns} new turns in {elapsed:.1f}s")
    return 0


def run_watch(args):
    """Continuously archive every N seconds."""
    print(f"Watching {PROJECTS_DIR} (interval={args.interval}s, max_age={args.max_age_days}d)")
    print("Press Ctrl+C to stop")
    try:
        while True:
            sessions = discover_sessions(args.max_age_days)
            if sessions:
                batch_start = time.time()
                total = 0
                for session_id, cwd, jsonl_path, mtime in sessions:
                    total += archive_one(session_id, cwd, verbose=False)
                elapsed = time.time() - batch_start
                ts = time.strftime("%H:%M:%S")
                if total > 0:
                    print(f"[{ts}] +{total} turns across {len(sessions)} sessions ({elapsed:.1f}s)")
                else:
                    print(f"[{ts}] no new turns ({len(sessions)} sessions checked, {elapsed:.1f}s)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped")
        return 0


def main():
    ap = argparse.ArgumentParser(description="Archive Claude Code sessions now (no hook needed)")
    ap.add_argument("--session", help="Archive specific session ID (default: all)")
    ap.add_argument("--max-age-days", type=int, default=60,
                    help="Only archive sessions modified within N days (default: 60)")
    ap.add_argument("--watch", action="store_true",
                    help="Continuous mode: archive every --interval seconds")
    ap.add_argument("--interval", type=int, default=60,
                    help="Watch mode interval in seconds (default: 60)")
    args = ap.parse_args()

    if args.watch:
        return run_watch(args)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
