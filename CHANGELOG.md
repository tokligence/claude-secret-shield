# Changelog

All notable changes to redmem will be documented in this file.

## [Unreleased]

### Added
- **Guard module** (optional, opt-in via `./install.sh --with-guard`): agent isolation guard that denies concurrent non-isolated `Agent` tool calls targeting the same git repo, preventing parallel subagents from stomping on each other's uncommitted changes. Standalone hook (not routed through the dispatcher); fail-open on any internal error. Includes `.guard_bypass` one-shot override and 11 hermetic tests (`test_guard.py`).
- **Memory module** (Phase 0-2): persistent session archive for Claude Code
  - `hooks/memory/db.py`: SQLite FTS5 schema + connection management
  - `hooks/memory/transcript_parser.py`: Claude Code JSONL parser (verified append-only format, skips compact_boundary/isCompactSummary markers)
  - `hooks/memory/ingest.py`: incremental turn archival (line_number based dedup)
  - `hooks/memory/search.py`: FTS5 full-text search with query sanitization (handles FTS5 syntax chars in natural language)
  - `hooks/memory/summarize.py`: resume context builder (session_state > milestone > recent turns, token-budgeted)
  - `hooks/memory/session_state.py`: heuristic session state generation (Goal/Plan/Done/Blocked/Decisions) + task/plan event tracking
  - `hooks/redmem_dispatcher.py`: single entry point routing to shield or memory based on hook event
  - `test_memory.py`: 21 tests covering DB, FTS5, parser, ingest, search, resume, session state
- **Pluggable backend architecture**: ArchiveBackend + VectorBackend abstract interfaces; SQLite (default) and PostgreSQL (enterprise) implementations documented
- **Architecture document** (`docs/design/architecture.md`): full technical specification, 13 rounds of Codex adversarial review

### Fixed
- **Write tool silent failure**: PreToolUse(Write) no longer causes data loss when PostToolUse falls back to backup restore. Content-based crash recovery (hash + placeholder detection) replaces flag-based approach. (Codex R5-R10, 6 rounds)
- **Crash recovery safety**: Write backups distinguished from Read backups; restore_pending_backups uses content comparison to detect completed writes
- **Placeholder regex**: crash recovery now matches pattern names containing digits (e.g. AWS_S3_ARN, DB2_URL) with `[A-Z0-9_]+`

### Changed
- Renamed from claude-secret-shield to redmem (redact + memory)
- Repository: github.com/tokligence/redmem

## [Pre-rename] claude-secret-shield

All prior history preserved in git log. Key features:
- 4-layer secret protection (prompt scan, file block, pattern redact, write restore)
- 205 regex patterns + 42 blocked file patterns
- HMAC-based deterministic placeholders
- Crash recovery with pending backup restoration
- 185 tests
