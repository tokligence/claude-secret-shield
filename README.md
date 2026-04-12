# redmem

**red**act + **mem**ory — industrial-grade Claude Code infrastructure.

Secret protection + persistent session memory in one plugin.

## The Problem

Claude Code has two fundamental gaps:

### Secrets Leak to the API

Claude Code's tools (Read, Edit, Bash) read files containing API keys, database
passwords, and private keys. Without protection, these flow to the API in plaintext.

### Context Gets Lost

When a long conversation triggers `/compact`, the system generates a lossy summary
and discards original turns. Three failure modes worsen over time:

1. **State amnesia** — Claude reports stale progress after compaction.
2. **Detail loss** — decisions, error messages, and workarounds discussed early
   in the session are permanently gone.
3. **Session ceiling** — sessions spanning days degrade as repeated compactions
   compound information loss.

## The Solution

One plugin, one dispatcher, two capabilities:

```
redmem_dispatcher.py
  |-- shield/  -- 205 regex patterns, 42 blocked files
  |             Redacts secrets before Claude sees them
  |             Restores real values when Claude writes
  |
  +-- memory/  -- SQLite FTS5, session state, auto-recall
                Archives full conversation before compact
                Restores context on resume
                Auto-searches history on recall keywords
```

**Why merged?** Shield and memory share hook events (UserPromptSubmit, PostToolUse).
As separate plugins, hook ordering is unreliable. One dispatcher guarantees shield
runs first (code-level if/return), memory runs second on safe data only.

## Features

### Secret Protection (Layer A)

- 205 regex patterns (AWS, OpenAI, Anthropic, GitHub, Stripe, database URLs,
  private keys, Web3 wallets, BIP39 mnemonics, ...)
- 42 blocked file patterns (.env, credentials.json, id_rsa, ...)
- Deterministic `{{NAME_hash}}` placeholders (HMAC-based, stable across sessions)
- Auto-restore on write/edit — Claude writes placeholders, disk gets real values
- User prompt scanning — blocks messages containing raw secrets

### Session Memory (Layer B)

- **PreCompact archive** — full conversation saved to SQLite FTS5 before compaction
- **Resume restore** — session state + recent context injected on `--resume`
- **Auto-recall** — "remember", "before", "earlier", "\u4e4b\u524d" triggers automatic archive search
- **Session state** — structured Goal/Plan/Done/Blocked/Decisions file, auto-generated
- **Task tracking** — captures TaskCreate/TaskUpdate/Plan changes via PostToolUse hook
- **Full-text search** — BM25 ranking with porter stemmer + unicode support

## Quick Start

```bash
pip install redmem
redmem install    # configures Claude Code hooks
# Done. Protection + memory are automatic from now on.
```

## How It Works

```
User message
  |
  v
redmem_dispatcher.py
  |
  +-- UserPromptSubmit
  |     1. Shield: scan for secrets, block if found
  |     2. Memory: search archive if recall keywords detected
  |
  +-- PreToolUse (Read/Write/Edit/Bash)
  |     Shield: redact secrets / restore placeholders
  |
  +-- PostToolUse (Read/Write/Edit/Bash)
  |     Shield: restore files / scan for residual placeholders
  |
  +-- PostToolUse (TodoWrite/TaskUpdate/PlanMode)
  |     Memory: track task/plan changes -> state_events
  |
  +-- PreCompact
  |     Memory: archive turns to SQLite FTS5 + generate session_state.md
  |
  +-- SessionStart (resume)
  |     Memory: inject session state + recent context as additionalContext
  |
  +-- SessionEnd
        Shield: cleanup backup directory
```

## CLI

```bash
redmem install                          # configure hooks in settings.json
redmem search --query "migration 076"   # search session history
redmem timeline                         # file change history
redmem stats                            # archive statistics
redmem check                            # verify installation health
redmem gc --older-than 90d              # prune old archives
```

## Backend Configuration

redmem supports pluggable backends via `~/.claude/redmem.yaml`:

```yaml
# Local mode (default, zero config)
backend:
  archive: sqlite    # SQLite FTS5, one file per session

# PostgreSQL mode (local or enterprise, multi-tenant)
backend:
  archive: postgres
  archive_dsn: "postgresql://redmem:pass@localhost:5432/redmem"
```

See [architecture.md](docs/design/architecture.md) for PostgreSQL schema with
row-level security, SECURITY DEFINER role mapping, and enterprise deployment.

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| **0. Restructure** | Done | Dispatcher wraps existing shield, memory module alongside |
| **1. MVP Memory** | Done | Ingest, search, summarize, PreCompact/SessionStart hooks |
| **1.5 Session State** | Done | session_state.md, task/plan tracking via PostToolUse |
| **2. Smart Recall** | Done | Auto-search archive on recall keywords in UserPromptSubmit |
| **3. Cross-Session Knowledge** | Done | Project-level `knowledge.db` indexing session summaries + key entities across all sessions. Enables "who solved this before?" and new-session onboarding from prior sessions. |
| **4. Semantic Search** | On Hold | sqlite-vec + embedding model for fuzzy recall. Requires external deps (~200MB fastembed or ~2GB torch). FTS5 keyword search is sufficient for most code conversations. Will implement as optional: `pip install redmem[semantic]` |

### Other TODO

- [ ] README.md: update installation instructions once PyPI package ready
- [ ] install.sh: adapt for redmem_dispatcher.py + memory hooks
- [ ] CLI tool: `redmem search/stats/gc/timeline/export/check` implementation
- [ ] Migration path: detect old claude-secret-shield settings.json, auto-upgrade
- [ ] pyproject.toml: package metadata for PyPI
- [ ] Latency analysis in architecture.md

## Tests

```bash
# Shield tests (185)
python3 test_hook.py

# Memory tests (21)
python3 -m pytest test_memory.py -v

# All tests
python3 test_hook.py && python3 -m pytest test_memory.py -v
```

## Documentation

- [Architecture & Technical Design](docs/design/architecture.md) — full specification
  including database schema, dispatcher design, pluggable backends, security model,
  JSONL format verification, and implementation phases.
- [Security Model](docs/SECURITY.md) — secret protection details
- [Pattern Reference](docs/PATTERNS.md) — all 205 regex patterns

## License

MIT
