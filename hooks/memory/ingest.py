"""Ingest session turns into the archive."""
import sys
import os

from . import db as archive_db
from .transcript_parser import parse_incremental, find_transcript


def archive_turns(session_id: str, cwd: str = "") -> int:
    """
    Archive new turns from session JSONL to SQLite.
    Returns count of new turns ingested.
    """
    transcript_path = find_transcript(session_id, cwd)
    if not transcript_path:
        return 0

    conn = archive_db.get_db(session_id)
    max_line = archive_db.get_max_line_number(conn, session_id)

    new_turns = parse_incremental(transcript_path, session_id, after_line=max_line)
    if not new_turns:
        conn.close()
        return 0

    conn.executemany("""
        INSERT OR IGNORE INTO turns
        (session_id, line_number, uuid, role, content, content_hash,
         token_estimate, tool_name, tool_input, files_touched)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (session_id, t.line_number, t.uuid, t.role, t.content,
         archive_db.content_hash(t.content),
         archive_db.estimate_tokens(t.content),
         t.tool_name, t.tool_input, t.files_touched)
        for t in new_turns
    ])

    # Update session metadata
    total = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
    ).fetchone()[0]

    conn.execute("""
        INSERT INTO sessions (session_id, project_dir, total_turns, total_compacts)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(session_id) DO UPDATE SET
            last_seen = datetime('now'),
            total_turns = ?,
            total_compacts = total_compacts + 1
    """, (session_id, cwd, total, total))

    conn.commit()
    conn.close()
    return len(new_turns)
