"""Tests for redmem memory module."""
import json
import os
import sqlite3
import tempfile
import pytest

# Patch VAULT_DIR before importing
_tmpdir = tempfile.mkdtemp()
os.environ["REDMEM_VAULT_DIR"] = _tmpdir

# Patch the module
import hooks.memory.db as archive_db
archive_db.VAULT_DIR = _tmpdir

from hooks.memory.db import get_db, get_max_line_number, content_hash
from hooks.memory.transcript_parser import parse_incremental, ParsedTurn
from hooks.memory.search import sanitize_fts5_query, search
from hooks.memory.ingest import archive_turns
from hooks.memory.summarize import build_resume_context


class TestDB:
    def test_get_db_creates_tables(self):
        db = get_db("test-session-1")
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "turns" in table_names
        assert "milestones" in table_names
        assert "sessions" in table_names
        assert "state_events" in table_names
        db.close()

    def test_get_max_line_number_empty(self):
        db = get_db("test-empty")
        assert get_max_line_number(db, "test-empty") == 0
        db.close()

    def test_get_max_line_number_after_insert(self):
        db = get_db("test-insert")
        db.execute("""
            INSERT INTO turns (session_id, line_number, uuid, role, content, content_hash, token_estimate)
            VALUES ('test-insert', 42, 'uuid-1', 'user', 'hello', 'hash1', 2)
        """)
        db.execute("""
            INSERT INTO turns (session_id, line_number, uuid, role, content, content_hash, token_estimate)
            VALUES ('test-insert', 100, 'uuid-2', 'assistant', 'world', 'hash2', 2)
        """)
        db.commit()
        assert get_max_line_number(db, "test-insert") == 100
        db.close()

    def test_content_hash_deterministic(self):
        assert content_hash("hello") == content_hash("hello")
        assert content_hash("hello") != content_hash("world")


class TestFTS5:
    def test_sanitize_fts5_basic(self):
        assert sanitize_fts5_query("migration 076") == '"migration" "076"'
        assert sanitize_fts5_query("hello") == '"hello"'
        assert sanitize_fts5_query("") == '""'

    def test_sanitize_fts5_special_chars(self):
        # These would crash raw FTS5
        assert "?" not in sanitize_fts5_query("what about migration 076?")
        assert ":" not in sanitize_fts5_query("file_path: src/main.py")
        assert sanitize_fts5_query("before - migration") == '"before" "migration"'

    def test_search_returns_results(self):
        db = get_db("test-search")
        db.execute("""
            INSERT INTO turns (session_id, line_number, uuid, role, content, content_hash, token_estimate)
            VALUES ('test-search', 1, 'u1', 'user', 'discuss migration 076 changes', 'h1', 5)
        """)
        db.execute("""
            INSERT INTO turns (session_id, line_number, uuid, role, content, content_hash, token_estimate)
            VALUES ('test-search', 2, 'u2', 'assistant', 'the migration adds columns', 'h2', 5)
        """)
        db.commit()
        db.close()

        results = search("test-search", "migration 076")
        assert len(results) >= 1
        assert any("migration" in r[2].lower() for r in results)

    def test_search_empty_query(self):
        results = search("test-search", "")
        assert results == []

    def test_search_no_match(self):
        results = search("test-search", "xyznonexistent")
        assert results == []


class TestTranscriptParser:
    def _write_jsonl(self, path, entries):
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_parse_skips_compact_boundary(self):
        path = os.path.join(_tmpdir, "test-compact.jsonl")
        self._write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}, "uuid": "u1"},
            {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "manual"}},
            {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": [{"type": "text", "text": "summary"}]}, "uuid": "u2"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "new turn"}]}, "uuid": "u3"},
        ])
        turns = parse_incremental(path, "test")
        assert len(turns) == 2  # hello + new turn (skips boundary + summary)
        assert turns[0].content == "hello"
        assert turns[1].content == "new turn"

    def test_parse_incremental_skips_old(self):
        path = os.path.join(_tmpdir, "test-incr.jsonl")
        self._write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "old"}]}, "uuid": "u1"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "new"}]}, "uuid": "u2"},
        ])
        turns = parse_incremental(path, "test", after_line=1)
        assert len(turns) == 1
        assert turns[0].content == "new"
        assert turns[0].line_number == 2

    def test_parse_extracts_tool_info(self):
        path = os.path.join(_tmpdir, "test-tool.jsonl")
        self._write_jsonl(path, [
            {"type": "assistant", "uuid": "u1", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "reading file"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.rs"}}
            ]}},
        ])
        turns = parse_incremental(path, "test")
        assert len(turns) == 1
        assert turns[0].tool_name == "Read"
        assert "/src/main.rs" in turns[0].files_touched


class TestResume:
    def test_build_resume_empty_session(self):
        context = build_resume_context("nonexistent-session")
        # Should not crash, returns empty or minimal context
        assert isinstance(context, str)

    def test_build_resume_with_turns(self):
        db = get_db("test-resume")
        db.execute("""
            INSERT INTO turns (session_id, line_number, uuid, role, content, content_hash, token_estimate)
            VALUES ('test-resume', 1, 'u1', 'user', 'working on migration 076', 'h1', 5)
        """)
        db.execute("""
            INSERT INTO turns (session_id, line_number, uuid, role, content, content_hash, token_estimate)
            VALUES ('test-resume', 2, 'u2', 'assistant', 'I will update the schema', 'h2', 5)
        """)
        db.commit()
        db.close()

        context = build_resume_context("test-resume")
        assert "migration" in context.lower() or "schema" in context.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
