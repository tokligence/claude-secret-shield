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
    turn_index    INTEGER NOT NULL,
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL,  -- pre-redacted by shield
    content_hash  TEXT    NOT NULL,  -- SHA-256 dedup guard
    token_estimate INTEGER,
    tool_name     TEXT,
    tool_input    TEXT,             -- JSON: summarized parameters
    files_touched TEXT,             -- JSON array
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, turn_index)
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
    turn_index  INTEGER,
    event_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_turns_session     ON turns(session_id, turn_index);
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
        WHERE session_id = ? ORDER BY turn_index DESC LIMIT 80
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
        SELECT t.turn_index, t.role, t.content, t.files_touched, rank
        FROM turns_fts f JOIN turns t ON t.id = f.rowid
        WHERE turns_fts MATCH ? AND t.session_id = ?
        ORDER BY rank LIMIT 5
    """, (safe_query, session_id)).fetchall()

    if not results:
        return None
    return format_search_results(results)
```

---

## Ingest: Archiving Turns

```python
def memory_archive_turns(data):
    session_id = data.get('session_id', '')
    if not session_id:
        return

    transcript_path = find_transcript(session_id, data.get('cwd', ''))
    if not transcript_path:
        return

    db = get_or_create_archive_db(session_id)

    max_index = db.execute(
        "SELECT COALESCE(MAX(turn_index), -1) FROM turns WHERE session_id = ?",
        (session_id,)
    ).fetchone()[0]

    turns = parse_transcript_jsonl(transcript_path)
    new_turns = [t for t in turns if t.index > max_index]
    if not new_turns:
        return

    # Content is pre-redacted -- Claude never saw real secrets
    db.executemany("""
        INSERT OR IGNORE INTO turns
        (session_id, turn_index, role, content, content_hash,
         token_estimate, tool_name, tool_input, files_touched)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [turn_to_row(session_id, t) for t in new_turns])

    db.execute("""
        INSERT INTO sessions (session_id, project_dir, total_turns, total_compacts)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(session_id) DO UPDATE SET
            last_seen = datetime('now'),
            total_turns = excluded.total_turns,
            total_compacts = total_compacts + 1
    """, (session_id, data.get('cwd', ''), len(turns)))

    db.commit()
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
        "matcher": "Read|Write|Edit",
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
| **3. Semantic Search** | sqlite-vec embeddings + FTS5/vector hybrid (RRF) | Fuzzy recall without exact keywords |
| **4. Cross-Session** | Project-level knowledge index across sessions | Cross-session queries |

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

## Phase 3: Semantic Search (sqlite-vec)

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

## Phase 4: Cross-Session Knowledge

```
~/.claude/vault/
  |-- sessions/{session_1}.db, {session_2}.db, ...
  +-- projects/{project_hash}/
      |-- knowledge.db
      +-- entities.db
```

---

## Open Questions

1. **Transcript path**: Verify how PreCompact hook locates session JSONL.
2. **Token budget**: Start at 4000 tokens for additionalContext, tune later.
3. **GC policy**: Auto-prune archives older than N days? Or keep forever?
4. **Migration**: Existing secret-shield users need seamless upgrade path.
5. **Multi-machine**: Archives are local. Defer sync to later.
