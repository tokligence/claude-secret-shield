"""Build context for resume injection."""
import os
from . import db as archive_db

VAULT_DIR = archive_db.VAULT_DIR


def build_resume_context(session_id: str, max_tokens: int = 4000) -> str:
    """
    Build context document for SessionStart(resume) injection.
    Priority: session_state.md > milestone > recent turns.
    """
    budget = max_tokens
    state_section = ""
    milestone_section = ""

    # 1. Session state (top priority)
    state_path = os.path.join(VAULT_DIR, f"{session_id}_state.md")
    if os.path.isfile(state_path):
        with open(state_path) as f:
            state = f.read()
        if state.strip():
            state_section = f"## Current Session State\n{state}"
            budget -= archive_db.estimate_tokens(state_section)

    # 2. Latest milestone
    conn = archive_db.get_db(session_id)
    milestone = conn.execute("""
        SELECT turn_end, summary, key_facts FROM milestones
        WHERE session_id = ? ORDER BY turn_end DESC LIMIT 1
    """, (session_id,)).fetchone()

    if milestone and budget > 500:
        milestone_section = f"## Previous Milestone\n{milestone[1]}"
        budget -= archive_db.estimate_tokens(milestone_section)

    # 3. Recent turns fill remaining budget
    recent_turns = conn.execute("""
        SELECT line_number, role, content FROM turns
        WHERE session_id = ?
        ORDER BY line_number DESC LIMIT 30
    """, (session_id,)).fetchall()
    conn.close()

    recent_section = ""
    if recent_turns and budget > 200:
        lines = ["## Recent Conversation"]
        for ln, role, content in reversed(recent_turns):
            preview = content[:200] + "..." if len(content) > 200 else content
            entry = f"[L{ln}] {role}: {preview}"
            entry_tokens = archive_db.estimate_tokens(entry)
            if budget - entry_tokens < 0:
                break
            lines.append(entry)
            budget -= entry_tokens
        recent_section = "\n".join(lines)

    parts = [p for p in [state_section, milestone_section, recent_section] if p]
    return "\n\n".join(parts)
