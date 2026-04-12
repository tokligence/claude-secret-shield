"""SQLite FTS5 archive backend for redmem."""
import os
import sqlite3
import hashlib

VAULT_DIR = os.path.expanduser("~/.claude/vault/sessions")

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    line_number   INTEGER NOT NULL,
    uuid          TEXT,
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    content_hash  TEXT    NOT NULL,
    token_estimate INTEGER,
    tool_name     TEXT,
    tool_input    TEXT,
    files_touched TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, line_number)
);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    content, tool_name, files_touched,
    content='turns', content_rowid='id',
    tokenize='porter unicode61'
);

-- FTS sync triggers (only create if not exist)
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, content, tool_name, files_touched)
    VALUES (new.id, new.content, new.tool_name, new.files_touched);
END;

CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content, tool_name, files_touched)
    VALUES ('delete', old.id, old.content, old.tool_name, old.files_touched);
END;

CREATE TABLE IF NOT EXISTS milestones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    turn_start    INTEGER NOT NULL,
    turn_end      INTEGER NOT NULL,
    summary       TEXT    NOT NULL,
    key_facts     TEXT,
    files_changed TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    project_dir    TEXT NOT NULL,
    first_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    total_turns    INTEGER NOT NULL DEFAULT 0,
    total_compacts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    line_number INTEGER,
    event_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_turns_session     ON turns(session_id, line_number);
CREATE INDEX IF NOT EXISTS idx_turns_role        ON turns(session_id, role);
CREATE INDEX IF NOT EXISTS idx_milestones_sess   ON milestones(session_id, turn_start);
CREATE INDEX IF NOT EXISTS idx_state_events_sess ON state_events(session_id, created_at);
"""


def get_db(session_id: str) -> sqlite3.Connection:
    """Get or create archive DB for a session."""
    os.makedirs(VAULT_DIR, mode=0o700, exist_ok=True)
    db_path = os.path.join(VAULT_DIR, f"{session_id}.db")
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    return db


def get_max_line_number(db: sqlite3.Connection, session_id: str) -> int:
    """Return highest archived line_number, or 0 if empty."""
    row = db.execute(
        "SELECT COALESCE(MAX(line_number), 0) FROM turns WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    return row[0]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English, ~2 for CJK."""
    return max(1, len(text) // 3)
