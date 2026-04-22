# Changelog

All notable changes to redmem will be documented in this file.

## [Unreleased]

### Added
- **Autopilot module** (`hooks/autopilot/`): overnight spec-driven loop that keeps Claude Code working unattended.
  - Three slash commands: `/autopilot [max_loop=150] <full-spec-path>`, `/autopilot-stop`, `/autopilot-status`.
  - Stop-hook re-injects a continuation message every turn until Claude emits `[[AUTOPILOT_DONE]]` or a halt condition trips (max_loop, 5-turn no-repo-change streak, 10h wall clock).
  - Graceful human takeover: typing any non-continuation message auto-disengages the loop (hook detects absence of the continuation marker and pauses state — no command needed).
  - **Preflight health check**: `/autopilot` refuses to arm on protected branches (main/master/trunk/develop/dev), refuses on a dirty working tree, and warns when not in a git worktree.
  - **Bash guard** (only active during autopilot): `PreToolUse(Bash)` denies `rm -rf`, `find -exec rm`, `git reset --hard`, `git checkout -f`, `git clean -fdx`, `git branch -D`, `git push --force`, `DROP TABLE`/`DATABASE`/`SCHEMA`/`INDEX`, and `TRUNCATE TABLE`. Claude is told why and picks a safer alternative.
  - **Artifacts under `.autopilot/`**: `TASKS.md`, `QUESTIONS.md`, `IMPROVE.md`, `DONE.md`, `HALTED.md`, auto-generated `README.md`. Plugin auto-registers `/.autopilot/` into `.git/info/exclude` so nothing leaks into git.
  - Fail-open: any hook error allows stop, preventing a plugin bug from trapping a session in an unstoppable loop.
  - `test_autopilot.py`: 58 tests (arg parsing, state round-trip, all stop-hook decision branches, 21 parameterized bash-guard samples, preflight refusal/warn cases, idempotent git-exclude register).
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
- **Shield housekeeping moved from `.gitignore` to `.git/info/exclude`**: `.tmp_secrets.*` temp files are now registered in git's *local* exclude list at file-create time (previously appended to the user's shared `.gitignore` on first Read, which leaked into PRs and never fired if Claude didn't Read the file). Existing stray `.tmp_secrets.*` files in users' working trees can be safely deleted; the new behavior prevents recurrence.
- **PreToolUse now routes through the dispatcher** instead of calling shield directly, so the autopilot bash guard can intercept destructive commands. Shield is still invoked as the first step inside the dispatcher (same semantics, one extra subprocess hop).
- **install.sh prefers local files over curl** when run from a checked-out repo (`SCRIPT_DIR/hooks/...` / `SCRIPT_DIR/commands/...`). Earlier versions always fetched from GitHub main, which prevented installing a WIP branch locally.

## [Pre-rename] claude-secret-shield

All prior history preserved in git log. Key features:
- 4-layer secret protection (prompt scan, file block, pattern redact, write restore)
- 205 regex patterns + 42 blocked file patterns
- HMAC-based deterministic placeholders
- Crash recovery with pending backup restoration
- 185 tests
