# redmem: Industrial-Grade Claude Code Infrastructure

> **red**act + **mem**ory — secret protection + persistent session memory in one plugin.

## Why One Plugin, Not Two

redmem combines secret redaction (formerly claude-secret-shield) and session
memory into a single dispatcher. Running them as independent plugins creates
three unsolvable problems:

1. **Hook ordering is unreliable** — two independent UserPromptSubmit hooks
   execute independently; shield blocking a prompt doesn't prevent the memory
   hook from seeing the raw secret.
2. **Hooks can't pass data** — each hook is a separate process; no shared state.
3. **Two installs, two configs** — users must manually align settings.json and
   debug interaction failures.

One dispatcher eliminates all three: secret redaction runs first as a function
call, memory operations run second in the same process, data flows through
function arguments — not hook ordering bets.

---

## Problem Statement

### Secrets: Claude Sees Too Much

Claude Code's tools (Read, Edit, Bash) read files that contain API keys,
database passwords, private keys. Without protection, these secrets flow to
the API in plaintext.

### Memory: Claude Forgets Too Much

Claude Code's context window is finite. When `/compact` triggers, the system
generates a lossy summary and discards original turns. This creates:

1. **State amnesia** — after compaction, Claude reports stale progress.
   Real example: reporting "migration 063" when the repo is at migration 076.
2. **Detail loss** — decisions, error messages, workarounds discussed early
   in the session are gone.
3. **Session ceiling** — sessions spanning days degrade as compactions
   compound information loss.

---

## What redmem Does

### Layer A: Secret Protection

Prevents Claude from seeing real secrets. 4-layer defense:

| Layer | Hook Event | Action |
|-------|-----------|--------|
| 0 | UserPromptSubmit | Block prompts containing secrets |
| 1 | PreToolUse(Read) | Block reads of `.env`, `credentials.json`, etc. (42 file patterns) |
| 2 | PreToolUse(Read) | Pattern-scan files (205 regex), replace with `{{NAME_hash}}` placeholders |
| 3 | PreToolUse(Write/Edit/Bash) | Restore `{{NAME_hash}}` to real values before disk write |

Placeholders are deterministic (HMAC from `~/.claude/.redact-hmac-key`).
Same secret = same placeholder across sessions.

### Layer B: Session Memory

Archives full conversation before compaction, restores context on resume:

| Hook Event | Action |
|-----------|--------|
| PreCompact | Archive turns to SQLite FTS5 + generate session_state.md |
| SessionStart(resume) | Inject session state + recent context as additionalContext |
| UserPromptSubmit | Auto-search archive on recall-intent keywords |
| PostToolUse(Task/Plan) | Track task/plan changes to state_events.jsonl |

### Layer C: Guard (optional, opt-in)

Standalone hook (NOT routed through `redmem_dispatcher.py`) that prevents
concurrent `Agent` tool calls from stomping on the same git repo when the
parent forgets to pass `isolation: "worktree"`.

| Hook Event | Action |
|-----------|--------|
| PreToolUse(Agent) | Deny if another non-isolated Agent is active in same repo root; record entry otherwise |
| PostToolUse(Agent) | Remove matching entry from `~/.claude/vault/active_agents.json` |

Fingerprint = 16-char sha256 of canonical `tool_input` JSON. Repo root =
`git rev-parse --show-toplevel` (falls back to `cwd` for non-git dirs).
Stale entries (>45min) are purged on every call. All errors fail **open**
— this is an ergonomics layer, not a security boundary. Bypass one call
via `touch ~/.claude/vault/.guard_bypass`. Installed only via
`./install.sh --with-guard`.

---

## Single Dispatcher Architecture

```python
# redmem_dispatcher.py — single entry point for ALL hook events

def main():
    data = json.load(sys.stdin)
    event = data['hook_event_name']

    if event == "UserPromptSubmit":
        # 1. Secret scan FIRST (Layer 0)
        shield_result = shield_scan_prompt(data)
        if shield_result.get('blocked'):
            output(shield_result)
            return  # blocked — memory search never runs

        # 2. Memory search SECOND (only on safe prompts)
        memory_context = memory_search_archive(data)
        if memory_context:
            shield_result = merge_additional_context(shield_result, memory_context)
        output(shield_result)

    elif event == "PreToolUse":
        # Secret redaction (Layers 1-3)
        output(shield_handle_pre_tool(data))

    elif event == "PostToolUse":
        # Secret restoration + task/plan tracking
        result = shield_handle_post_tool(data)
        if data.get('tool_name') in TASK_PLAN_TOOLS:
            memory_track_state_event(data)
        output(result)

    elif event == "PreCompact":
        # Archive turns + generate session state
        memory_archive_turns(data)
        memory_generate_session_state(data)

    elif event == "SessionStart":
        if is_resume(data):
            context = memory_build_resume_context(data)
            output(context)

    elif event == "SessionEnd":
        shield_cleanup_backups(data)
```

**Key safety guarantee**: `memory_search_archive` only runs AFTER
`shield_scan_prompt` passes. This is a code-level if/return, not a hook bet.

---

## Architecture Diagram

```
~/.claude/hooks/
  redmem_dispatcher.py  <-- single entry point
  shield/               <-- secret protection (existing)
  |  |-- redact.py
  |  |-- restore.py
  |  |-- prompt_scan.py
  |  +-- patterns.py        (205 patterns + 42 blocked files)
  memory/               <-- session archive (new)
  |  |-- db.py               SQLite FTS5 schema
  |  |-- ingest.py            JSONL parser + archival
  |  |-- search.py            FTS5 search + sanitize
  |  |-- summarize.py         resume context builder
  |  |-- session_state.py     heuristic state generation
  |  +-- transcript_parser.py
  custom-patterns.py    <-- user patterns (never overwritten)
  mask-output.py        <-- AWS secret manager masking

~/.claude/vault/sessions/
  {session_id}.db        <-- SQLite FTS5 archive
  {session_id}_state.md  <-- structured session state
  state_events.jsonl     <-- task/plan change log
```

---

## Database Schema

```sql
-- One file per session: ~/.claude/vault/sessions/{session_id}.db

CREATE TABLE turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    line_number   INTEGER NOT NULL,  -- JSONL line number (natural order)
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL,  -- pre-redacted by shield
    content_hash  TEXT    NOT NULL,  -- SHA-256 dedup guard
    token_estimate INTEGER,
    tool_name     TEXT,
    tool_input    TEXT,             -- JSON: summarized parameters
    files_touched TEXT,             -- JSON array
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, line_number)
);

CREATE VIRTUAL TABLE turns_fts USING fts5(
    content, tool_name, files_touched,
    content='turns', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, content, tool_name, files_touched)
    VALUES (new.id, new.content, new.tool_name, new.files_touched);
END;

CREATE TRIGGER turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content, tool_name, files_touched)
    VALUES ('delete', old.id, old.content, old.tool_name, old.files_touched);
END;

CREATE TABLE milestones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    turn_start    INTEGER NOT NULL,
    turn_end      INTEGER NOT NULL,
    summary       TEXT    NOT NULL,
    key_facts     TEXT,
    files_changed TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    session_id     TEXT PRIMARY KEY,
    project_dir    TEXT NOT NULL,
    first_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    total_turns    INTEGER NOT NULL DEFAULT 0,
    total_compacts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE state_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    line_number INTEGER,
    event_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_turns_session     ON turns(session_id, line_number);
CREATE INDEX idx_turns_role        ON turns(session_id, role);
CREATE INDEX idx_milestones_sess   ON milestones(session_id, turn_start);
CREATE INDEX idx_state_events_sess ON state_events(session_id, created_at);
```

---

## Session State

Structured orientation file generated by heuristic extraction on each compact.

```markdown
# Session State
<!-- Auto-generated by redmem. Last updated: turn 847, 2026-04-12T15:30Z -->

## Goal
Multi-currency architecture -- store deposits in original currency

## Plan
1. ~~Foundation: exchange_rate_configs + exchange_service~~ DONE
2. **Deposit in original currency** <- CURRENT
3. Allocate/Recall parameterized currency

## Done (this session)
- exchange_rate_configs migration (platform_db/005)
- exchange_service.rs: get_usd_rate, get_quote

## Blocked / Open
- secret-shield blocks Edit tool on redacted files

## Key Decisions
- allocate/recall is same-currency, no exchange involved
```

### Generation: Heuristic (Zero LLM Cost)

```python
def memory_generate_session_state(data):
    session_id = data.get('session_id', '')
    db = get_archive_db(session_id)
    state_path = get_state_path(session_id)

    prev_state = read_file_or_empty(state_path)
    sections = parse_existing_state(prev_state)

    # From state_events (structured, high signal)
    events = db.execute("""
        SELECT event_type, title, detail FROM state_events
        WHERE session_id = ? ORDER BY created_at
    """, (session_id,)).fetchall()

    for etype, title, detail in events:
        if etype == 'task_completed':
            sections['done'].append(f"- {title}")
        elif etype == 'task_created':
            sections['plan'].append(title)
        elif etype == 'plan_updated' and detail:
            sections['goal'] = detail

    # From recent turns (keyword extraction, lower signal)
    recent = db.execute("""
        SELECT content FROM turns
        WHERE session_id = ? ORDER BY line_number DESC LIMIT 80
    """, (session_id,)).fetchall()

    for (content,) in recent:
        if re.search(r'(blocked|workaround|can.t|failed|error)', content, re.I):
            blocker = extract_first_sentence(content)
            if blocker and len(blocker) < 200:
                sections['blocked'].add(blocker)
        if re.search(r'(decided|decision|choosing|approach)', content, re.I):
            decision = extract_first_sentence(content)
            if decision and len(decision) < 200:
                sections['decisions'].add(decision)

    write_atomic(state_path, render_state_md(sections))
```

---

## Resume Context Injection

```python
def memory_build_resume_context(data):
    session_id = data['session_id']
    db = get_archive_db(session_id)
    state_path = get_state_path(session_id)
    budget = 4000
    state_section = ""
    milestone_section = ""

    # 1. Session state (top priority)
    state = read_file_or_empty(state_path)
    if state:
        state_section = f"## Current Session State\n{state}"
        budget -= estimate_tokens(state_section)

    # 2. Latest milestone
    milestone = db.execute("""
        SELECT turn_end, summary, key_facts FROM milestones
        WHERE session_id = ? ORDER BY turn_end DESC LIMIT 1
    """, (session_id,)).fetchone()
    if milestone and budget > 500:
        milestone_section = f"## Previous Milestone\n{milestone[1]}"
        budget -= estimate_tokens(milestone_section)

    # 3. Recent turns fill remaining budget
    recent = get_recent_turns_condensed(db, session_id, max_tokens=budget)

    context = combine_sections(state_section, milestone_section, recent)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }
```

---

## FTS5 Search

### Query Sanitization

```python
import re

def sanitize_fts5_query(raw: str) -> str:
    """
    Natural language -> safe FTS5 query.
    Strips operators, quotes tokens, joins with implicit AND.
    """
    tokens = re.findall(r'[\w]+', raw, re.UNICODE)
    if not tokens:
        return '""'
    return ' '.join(f'"{t}"' for t in tokens)
```

### Auto-Recall on UserPromptSubmit

```python
RECALL_PATTERN = re.compile(
    r'(before|earlier|remember|recall|之前|上次|记得|还记得|migration\s+\d+)',
    re.IGNORECASE
)

def memory_search_archive(data):
    """Called AFTER shield_scan_prompt passes. Safe -- no raw secrets."""
    prompt = data.get('prompt', '')  # UserPromptSubmit uses 'prompt', not 'message'
    if not RECALL_PATTERN.search(prompt):
        return None

    session_id = data.get('session_id', '')
    db = get_archive_db(session_id)
    if db is None:
        return None

    safe_query = sanitize_fts5_query(prompt)
    results = db.execute("""
        SELECT t.line_number, t.role, t.content, t.files_touched, rank
        FROM turns_fts f JOIN turns t ON t.id = f.rowid
        WHERE turns_fts MATCH ? AND t.session_id = ?
        ORDER BY rank LIMIT 5
    """, (safe_query, session_id)).fetchall()

    if not results:
        return None
    return format_search_results(results)
```

---

## JSONL Format (Verified)

Claude Code session JSONL is **append-only**. Compact does NOT delete turns.

```
Before compact:  [turn 1] [turn 2] ... [turn 2000]
After compact:   [turn 1] [turn 2] ... [turn 2000] [compact_boundary] [summary] [turn 2001] ...
After 2nd:       [...all above...] [compact_boundary] [summary] [turn 3001] ...
```

Key findings from real session analysis (36,850 lines, 37 compacts):

| Field | Value |
|-------|-------|
| Compact boundary | `type=system, subtype=compact_boundary, compactMetadata={trigger, preTokens}` |
| Summary entry | `type=user, isCompactSummary=true` (immediately after boundary) |
| Turn ordering | No `line_number` field. UUID + parentUuid chain. Line number = natural order. |
| Deletion | **None.** All original turns preserved. Compact only appends markers. |

**Implication for ingest:** Track last archived line number (not turn_index — that field doesn't exist).
Skip `compact_boundary` and `isCompactSummary` entries. Append everything else.

## Ingest: Archiving Turns

```python
def memory_archive_turns(data):
    session_id = data.get('session_id', '')
    if not session_id:
        return

    transcript_path = find_transcript(session_id, data.get('cwd', ''))
    if not transcript_path:
        return

    backend = get_archive_backend(session_id)  # SQLite or PostgreSQL

    max_line = backend.get_max_line_number(session_id)

    # Parse JSONL from last archived line onward (incremental)
    new_turns = []
    with open(transcript_path) as f:
        for line_num, line in enumerate(f, 1):
            if line_num <= max_line:
                continue
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            # Skip compact markers and non-conversation entries
            if obj.get('subtype') == 'compact_boundary':
                continue
            if obj.get('isCompactSummary'):
                continue
            if obj.get('type') not in ('user', 'assistant'):
                continue

            turn = parse_turn(obj, line_num, session_id)
            turn = sanitize_turn(turn)  # secondary secret defense
            new_turns.append(turn)

    if new_turns:
        backend.ingest(session_id, new_turns)
```

---

## Security Model

### Archived Content is Pre-Redacted

redmem archives the session JSONL -- which records what Claude saw, not what's
on disk. Since shield redacts before Claude reads, the archive contains
`{{NAME_hash}}` placeholders, never real secrets.

```
File on disk:     DATABASE_URL=postgres://user:realpass@host/db
  -> shield PreToolUse(Read): redacts
Claude sees:      DATABASE_URL={{DATABASE_URL_a1b2c3d4}}
  -> recorded in session JSONL
Archive stores:   DATABASE_URL={{DATABASE_URL_a1b2c3d4}}       <- safe
  -> injected on resume
Claude sees:      DATABASE_URL={{DATABASE_URL_a1b2c3d4}}       <- still safe
```

### Secondary Scan (Edge Cases)

For Bash output / curl responses that bypass shield:

```python
def sanitize_for_archive(content: str) -> str:
    patterns_path = os.path.expanduser("~/.claude/hooks/shield/patterns.py")
    if os.path.exists(patterns_path):
        return apply_shield_patterns(content, patterns_path)
    return re.sub(
        r'(?i)(password|secret|token|key)\s*[=:]\s*\S+',
        r'\1=[REDACTED]', content
    )
```

### Other Guarantees

- **File permissions**: `~/.claude/vault/sessions/` -- 0700 dir, 0600 files
- **No network**: all processing local, no data leaves machine
- **Placeholder stability**: HMAC deterministic -- FTS5 search matches consistently
- **Encryption**: `.redact-mapping.json` uses Fernet (if cryptography installed)

---

## Settings Configuration

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 5
        }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Read|Write|Edit|Bash",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 5
        }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Read|Write|Edit|Bash",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 5
        }]
      },
      {
        "matcher": "TodoWrite|TodoRead|EnterPlanMode|ExitPlanMode|TaskCreate|TaskUpdate",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 5
        }]
      }
    ],
    "PreCompact": [
      {
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 30,
          "statusMessage": "Archiving session..."
        }]
      }
    ],
    "SessionStart": [
      {
        "matcher": "resume",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 10,
          "statusMessage": "Loading session memory..."
        }]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/redmem_dispatcher.py",
          "timeout": 5
        }]
      }
    ]
  }
}
```

---

## Pluggable Backend Architecture

redmem supports two backend modes via `~/.claude/redmem.yaml`:

### Configuration

```yaml
# Local mode (default, zero config)
backend:
  archive: sqlite    # SQLite FTS5, one file per session
  vector: none       # Phase 3: sqlite-vec

# PostgreSQL mode (local or enterprise)
backend:
  archive: postgres
  archive_dsn: "postgresql://redmem:pass@localhost:5432/redmem"
  vector: pgvector   # Phase 3: co-located with archive
  # vector_dsn defaults to archive_dsn

# Enterprise mode (remote, multi-tenant)
backend:
  archive: postgres
  archive_dsn: "postgresql://redmem:pass@db.company.com:5432/redmem"
  vector: pgvector
  user_id: "tony@company.com"  # multi-tenant identity
```

### Abstract Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class Turn:
    session_id: str
    line_number: int      # JSONL line number (natural order, no line_number)
    uuid: str             # Claude Code UUID
    role: str
    content: str
    content_hash: str
    tool_name: Optional[str] = None
    files_touched: Optional[List[str]] = None
    user_id: Optional[str] = None  # multi-tenant (enterprise)

@dataclass
class SearchResult:
    turn: Turn
    relevance: float
    context_before: Optional[Turn] = None
    context_after: Optional[Turn] = None


class ArchiveBackend(ABC):
    @abstractmethod
    def ingest(self, session_id: str, turns: List[Turn]) -> int: ...

    @abstractmethod
    def search(self, session_id: str, query: str, limit: int = 10) -> List[SearchResult]: ...

    @abstractmethod
    def get_recent(self, session_id: str, limit: int = 50) -> List[Turn]: ...

    @abstractmethod
    def get_max_line_number(self, session_id: str) -> int: ...

    @abstractmethod
    def save_milestone(self, session_id: str, turn_start: int,
                       turn_end: int, summary: str): ...

    @abstractmethod
    def get_latest_milestone(self, session_id: str) -> Optional[dict]: ...


class VectorBackend(ABC):
    @abstractmethod
    def embed_and_store(self, session_id: str, turn_id: int, content: str): ...

    @abstractmethod
    def search_similar(self, session_id: str, query: str,
                       limit: int = 10) -> List[SearchResult]: ...
```

### SQLite Implementation (Default)

```python
class SQLiteArchive(ArchiveBackend):
    """Single-file SQLite FTS5. Zero dependencies."""
    def __init__(self, session_id: str):
        db_path = f"~/.claude/vault/sessions/{session_id}.db"
        self.db = sqlite3.connect(os.path.expanduser(db_path))
        self._ensure_schema()

    def ingest(self, session_id, turns):
        self.db.executemany("INSERT OR IGNORE INTO turns ...", ...)
        self.db.commit()
        return len(turns)

    def search(self, session_id, query, limit=10):
        safe = sanitize_fts5_query(query)
        return self.db.execute("SELECT ... FROM turns_fts ... MATCH ?", ...)
```

### PostgreSQL Implementation

```python
class PostgresArchive(ArchiveBackend):
    """PostgreSQL + tsvector. Multi-user, multi-tenant."""
    def __init__(self, dsn: str, user_id: str = None):
        self.pool = psycopg_pool.ConnectionPool(dsn)
        self.user_id = user_id

    def ingest(self, session_id, turns):
        with self.pool.connection() as conn:
            conn.executemany("""
                INSERT INTO redmem.turns (user_id, session_id, ...)
                VALUES (%s, %s, ...) ON CONFLICT DO NOTHING
            """, ...)

    def search(self, session_id, query, limit=10):
        with self.pool.connection() as conn:
            return conn.execute("""
                SELECT *, ts_rank(tsv, query) AS relevance
                FROM redmem.turns, plainto_tsquery('english', %s) query
                WHERE tsv @@ query AND session_id = %s
                ORDER BY relevance DESC LIMIT %s
            """, (query, session_id, limit))
```

### PostgreSQL Schema (Multi-Tenant)

```sql
CREATE SCHEMA IF NOT EXISTS redmem;

CREATE TABLE redmem.turns (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT    NOT NULL,
    session_id    TEXT    NOT NULL,
    project_dir   TEXT,
    line_number   INTEGER NOT NULL,
    uuid          TEXT    NOT NULL,
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    content_hash  TEXT    NOT NULL,
    token_estimate INTEGER,
    tool_name     TEXT,
    files_touched JSONB,
    tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding     vector(384),  -- pgvector (Phase 3)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, session_id, line_number)
);

CREATE INDEX idx_turns_fts ON redmem.turns USING GIN (tsv);
CREATE INDEX idx_turns_vec ON redmem.turns USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_turns_user_session ON redmem.turns(user_id, session_id, line_number);
CREATE INDEX idx_turns_project ON redmem.turns(project_dir, created_at);

-- Row-Level Security for multi-tenant
-- Row-Level Security: SECURITY DEFINER function for safe role->user_id lookup.
-- user_roles table is owned by redmem_admin; users cannot read/write it directly.
-- The lookup function runs with admin privileges but only returns the caller's user_id.

CREATE TABLE redmem.user_roles (
    db_role   TEXT PRIMARY KEY,  -- PostgreSQL role name (e.g. redmem_tony)
    user_id   TEXT NOT NULL      -- application identity (e.g. tony@company.com)
);
ALTER TABLE redmem.user_roles OWNER TO redmem_admin;
REVOKE ALL ON redmem.user_roles FROM PUBLIC;

-- Safe lookup: runs as admin, returns only the calling user's identity
CREATE OR REPLACE FUNCTION redmem.my_user_id()
RETURNS TEXT LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = redmem
AS $$ SELECT user_id FROM redmem.user_roles WHERE db_role = current_user $$;
ALTER FUNCTION redmem.my_user_id() OWNER TO redmem_admin;

ALTER TABLE redmem.turns ENABLE ROW LEVEL SECURITY;
CREATE POLICY user_own_data ON redmem.turns
    FOR ALL USING (user_id = redmem.my_user_id());
CREATE POLICY admin_all ON redmem.turns
    FOR SELECT TO redmem_admin USING (true);

-- Same pattern for milestones, sessions, state_events tables
```

### Backend Factory

```python
def get_archive_backend(session_id: str = None) -> ArchiveBackend:
    config = load_config()  # ~/.claude/redmem.yaml
    backend_type = config.get('backend', {}).get('archive', 'sqlite')

    if backend_type == 'sqlite':
        return SQLiteArchive(session_id)
    elif backend_type == 'postgres':
        dsn = config['backend']['archive_dsn']
        user_id = config['backend'].get('user_id')
        return PostgresArchive(dsn, user_id)
    else:
        raise ValueError(f"Unknown backend: {backend_type}")

def get_vector_backend(session_id: str = None) -> Optional[VectorBackend]:
    config = load_config()
    vector_type = config.get('backend', {}).get('vector', 'none')

    if vector_type == 'none':
        return None
    elif vector_type == 'sqlite-vec':
        return SQLiteVecVector(session_id)
    elif vector_type == 'pgvector':
        dsn = config['backend'].get('vector_dsn') or config['backend']['archive_dsn']
        return PgvectorVector(dsn)
    else:
        raise ValueError(f"Unknown vector backend: {vector_type}")
```

### Enterprise Value

```
50 engineers x Claude Code daily:
  -> ~10k turns/day, ~3.6M turns/year
  -> "Who solved Aurora failover before?" (cross-user search)
  -> "When was this API decision made?" (project + time search)
  -> "New hire: search all sessions on this repo" (knowledge transfer)
  -> "Most common prompt patterns" (usage analytics)
```


## CLI

```
redmem install                          # configure hooks in settings.json
redmem search  --query "migration 076"  # FTS5 search
redmem timeline [--since "2026-04-10"]  # file change history
redmem stats   [--session ID]           # archive statistics
redmem export  --session ID --format md # export session
redmem gc      [--older-than 90d]       # prune old archives
redmem check                            # verify hooks + shield + memory
```

---

## File Layout

```
redmem/
|-- hooks/
|   |-- redmem_dispatcher.py     <- single entry point
|   |-- shield/
|   |   |-- redact.py
|   |   |-- restore.py
|   |   |-- prompt_scan.py
|   |   +-- patterns.py          (205 patterns + 42 blocked files)
|   |-- memory/
|   |   |-- db.py
|   |   |-- ingest.py
|   |   |-- search.py
|   |   |-- summarize.py
|   |   |-- session_state.py
|   |   +-- transcript_parser.py
|   |-- custom-patterns.py       (user patterns, never overwritten)
|   |-- mask-output.py
|   +-- statusline.sh
|-- cli/
|   |-- __init__.py
|   +-- main.py
|-- tests/
|   |-- test_shield.py
|   |-- test_memory_ingest.py
|   |-- test_memory_search.py
|   |-- test_session_state.py
|   |-- test_dispatcher.py       (integration: shield + memory)
|   +-- fixtures/
|       |-- sample_session.jsonl
|       +-- sample_secrets.env
|-- install.sh
|-- pyproject.toml
|-- README.md
+-- docs/
    +-- design/
        +-- architecture.md      <- this file
```

---

## Implementation Phases

| Phase | Scope | Deliverable |
|-------|-------|-------------|
| **0. Restructure** | Refactor redact-restore.py into shield/ submodule, redmem_dispatcher.py as entry point. All existing shield tests pass. | Same shield functionality, new structure |
| **1. MVP Memory** | ingest + search + summarize + PreCompact/SessionStart hooks | Turns archived, context restored on resume |
| **1.5 Session State** | session_state.md + PostToolUse task tracking + heuristic | Structured orientation on resume |
| **2. Smart Recall** | UserPromptSubmit auto-search on recall keywords | Auto-retrieves from archive mid-conversation |
| **3. Cross-Session** | Project-level knowledge index across sessions | Cross-session queries |
| **4. Semantic Search** | On Hold. sqlite-vec embeddings + FTS5/vector hybrid (RRF) | Fuzzy recall without exact keywords. Optional: `pip install redmem[semantic]` |

### Phase 0: Migration from claude-secret-shield

```bash
# 1. Restructure
mkdir -p hooks/shield hooks/memory
# Split redact-restore.py -> shield/{redact,restore,prompt_scan}.py

# 2. New dispatcher
# hooks/redmem_dispatcher.py imports shield/* and memory/*

# 3. Update install.sh
# - Replace redact-restore.py with redmem_dispatcher.py
# - Add PreCompact, SessionStart, PostToolUse(Task) hooks
# - Migration: detect old settings.json, replace references

# 4. All existing tests pass
```

---

## Phase 4: Semantic Search (sqlite-vec) — On Hold

```sql
CREATE VIRTUAL TABLE turns_vec USING vec0(
    embedding float[384]
);
```

```python
def hybrid_search(query, session_id, db, k=10):
    fts_results = fts5_search(query, session_id, db, limit=k*2)
    vec_results = vector_search(embed(query), session_id, db, limit=k*2)
    return reciprocal_rank_fusion(fts_results, vec_results, k=k)
```

Local embedding models (no API): all-MiniLM-L6-v2 (384d, 80MB),
nomic-embed-text (768d, 274MB), bge-small-en-v1.5 (384d, 130MB).

---

## Phase 3: Cross-Session Knowledge (Done)

```
~/.claude/vault/
  |-- sessions/{session_1}.db, {session_2}.db, ...
  +-- projects/{project_hash}/
      |-- knowledge.db
      +-- entities.db
```

---

## Other TODO

- [ ] README.md: rewrite for redmem (currently still claude-secret-shield README)
- [ ] install.sh: adapt for redmem_dispatcher.py + memory hooks (PreCompact, SessionStart, PostToolUse Task)
- [ ] CLI tool: `redmem search/stats/gc/timeline/export/check` implementation
- [ ] Migration path: detect old claude-secret-shield settings.json, auto-upgrade to redmem_dispatcher.py
- [ ] uninstall.sh: update for new hook references
- [ ] test_hook.py: extend with memory ingest/search/resume tests
- [ ] pyproject.toml: package metadata for PyPI publication

## Open Questions

1. **Transcript path**: Verify how PreCompact hook locates session JSONL.
2. **Token budget**: Start at 4000 tokens for additionalContext, tune later.
3. **GC policy**: Auto-prune archives older than N days? Or keep forever?
4. **Migration**: Existing secret-shield users need seamless upgrade path.
5. **Multi-machine**: Archives are local. Defer sync to later.
