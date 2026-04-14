#!/bin/sh
# redmem — One-line installer
# Usage: curl -sL https://raw.githubusercontent.com/tokligence/redmem/main/install.sh | sh
#
# What this does:
#   1. Installs shield hooks (secret redaction) to ~/.claude/hooks/
#   2. Installs memory module (session archive) to ~/.claude/hooks/memory/
#   3. Installs the dispatcher (single entry point) to ~/.claude/hooks/
#   4. Merges hook config into ~/.claude/settings.json (preserves existing settings)
#   5. Creates vault directory for session archives
#   6. Migrates from old claude-secret-shield if detected

set -e

HOOKS_DIR="$HOME/.claude/hooks"
MEMORY_DIR="$HOOKS_DIR/memory"
VAULT_DIR="$HOME/.claude/vault/sessions"
SETTINGS_FILE="$HOME/.claude/settings.json"
BASE_URL="https://raw.githubusercontent.com/tokligence/redmem/main"

echo ""
echo "  redmem (redact + memory)"
echo "  ────────────────────────"
echo "  Secret protection + persistent session memory for Claude Code."
echo ""

# ── Prerequisites ───────────────────────────────────────────────────────
if ! command -v jq >/dev/null 2>&1; then
  echo "  ERROR: jq is required. Install it:"
  echo "    macOS: brew install jq"
  echo "    Ubuntu: sudo apt install jq"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "  ERROR: python3 is required."
  exit 1
fi

# ── Detect migration from claude-secret-shield ──────────────────────────
MIGRATING=false
if [ -f "$HOOKS_DIR/redact-restore.py" ] && ! [ -f "$HOOKS_DIR/redmem_dispatcher.py" ]; then
  MIGRATING=true
  echo "  -> Detected existing claude-secret-shield installation"
  echo "     Will migrate to redmem (preserving custom-patterns.py)"
fi

# ── Create directories ──────────────────────────────────────────────────
mkdir -p "$HOOKS_DIR"
mkdir -p "$MEMORY_DIR"
mkdir -p "$VAULT_DIR"
chmod 700 "$VAULT_DIR"

# ── Download shield files ───────────────────────────────────────────────
echo "  -> Downloading shield (secret protection)..."
curl -fsSL "$BASE_URL/hooks/redact-restore.py" -o "$HOOKS_DIR/redact-restore.py"
chmod +x "$HOOKS_DIR/redact-restore.py"

curl -fsSL "$BASE_URL/hooks/patterns.py" -o "$HOOKS_DIR/patterns.py"

curl -fsSL "$BASE_URL/hooks/custom-patterns.example.py" -o "$HOOKS_DIR/custom-patterns.example.py"

curl -fsSL "$BASE_URL/hooks/mask-output.py" -o "$HOOKS_DIR/mask-output.py"

curl -fsSL "$BASE_URL/hooks/statusline.sh" -o "$HOOKS_DIR/statusline.sh"
chmod +x "$HOOKS_DIR/statusline.sh"

# Preserve user custom patterns
if [ -f "$HOOKS_DIR/custom-patterns.py" ]; then
  echo "  OK: Existing custom-patterns.py preserved"
fi

echo "  OK: Shield installed"

# ── Download memory module ──────────────────────────────────────────────
echo "  -> Downloading memory (session archive)..."
for FILE in __init__.py db.py transcript_parser.py ingest.py search.py summarize.py session_state.py knowledge.py; do
  curl -fsSL "$BASE_URL/hooks/memory/$FILE" -o "$MEMORY_DIR/$FILE"
done
echo "  OK: Memory module installed"

# ── Download dispatcher ─────────────────────────────────────────────────
echo "  -> Downloading dispatcher..."
curl -fsSL "$BASE_URL/hooks/redmem_dispatcher.py" -o "$HOOKS_DIR/redmem_dispatcher.py"
chmod +x "$HOOKS_DIR/redmem_dispatcher.py"
curl -fsSL "$BASE_URL/hooks/redmem_catchup.py" -o "$HOOKS_DIR/redmem_catchup.py"
chmod +x "$HOOKS_DIR/redmem_catchup.py"
echo "  OK: Dispatcher + catchup installed"

# ── Remove legacy files ─────────────────────────────────────────────────
if [ -f "$HOOKS_DIR/redact-secrets.sh" ]; then
  rm "$HOOKS_DIR/redact-secrets.sh"
  echo "  OK: Removed legacy redact-secrets.sh"
fi

# ── Configure settings.json ─────────────────────────────────────────────
echo "  -> Configuring Claude Code settings..."

# Shield hooks (direct, for Read/Write/Edit/Bash — latency-critical)
SHIELD_PRE='{"matcher":"Read|Write|Edit|Bash","hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'
SHIELD_POST='{"matcher":"Read|Write|Edit|Bash","hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'
SHIELD_SESSION_END='{"hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'

# Dispatcher hooks (shield + memory combined)
DISPATCH_PROMPT='{"hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redmem_dispatcher.py","timeout":5}]}'
DISPATCH_COMPACT='{"hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redmem_dispatcher.py","timeout":30,"statusMessage":"Archiving session..."}]}'
DISPATCH_RESUME='{"matcher":"resume","hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redmem_dispatcher.py","timeout":10,"statusMessage":"Loading session memory..."}]}'
DISPATCH_TASK='{"matcher":"TodoWrite|TodoRead|EnterPlanMode|ExitPlanMode|TaskCreate|TaskUpdate","hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redmem_dispatcher.py","timeout":5}]}'

if [ -f "$SETTINGS_FILE" ]; then
  EXISTING=$(cat "$SETTINGS_FILE")
  HAS_HOOKS=$(echo "$EXISTING" | jq 'has("hooks")' 2>/dev/null || echo "false")
else
  EXISTING='{}'
  HAS_HOOKS="false"
fi

# Build the complete hooks config
# Strategy: remove all old redact-restore.py and redmem_dispatcher.py entries, then add fresh
if [ "$HAS_HOOKS" = "true" ]; then
  UPDATED=$(echo "$EXISTING" | jq     --argjson shield_pre "$SHIELD_PRE"     --argjson shield_post "$SHIELD_POST"     --argjson shield_end "$SHIELD_SESSION_END"     --argjson dispatch_prompt "$DISPATCH_PROMPT"     --argjson dispatch_compact "$DISPATCH_COMPACT"     --argjson dispatch_resume "$DISPATCH_RESUME"     --argjson dispatch_task "$DISPATCH_TASK" '
    # Clean old entries
    def remove_old:
      map(select(
        (.hooks[0].command != "python3 ~/.claude/hooks/redact-restore.py") and
        (.hooks[0].command != "python3 ~/.claude/hooks/redmem_dispatcher.py") and
        (.hooks[0].command != "~/.claude/hooks/redact-secrets.sh")
      ));

    .hooks.PreToolUse = ((.hooks.PreToolUse // []) | remove_old) + [$shield_pre]
    | .hooks.PostToolUse = ((.hooks.PostToolUse // []) | remove_old) + [$shield_post, $dispatch_task]
    | .hooks.SessionEnd = [$shield_end]
    | .hooks.UserPromptSubmit = ((.hooks.UserPromptSubmit // []) | remove_old) + [$dispatch_prompt]
    | .hooks.PreCompact = ((.hooks.PreCompact // []) | remove_old) + [$dispatch_compact]
    | .hooks.SessionStart = ((.hooks.SessionStart // []) | remove_old) + [$dispatch_resume]
    | .statusLine = {"type": "command", "command": "~/.claude/hooks/statusline.sh"}
  ')
else
  UPDATED=$(echo "$EXISTING" | jq     --argjson shield_pre "$SHIELD_PRE"     --argjson shield_post "$SHIELD_POST"     --argjson shield_end "$SHIELD_SESSION_END"     --argjson dispatch_prompt "$DISPATCH_PROMPT"     --argjson dispatch_compact "$DISPATCH_COMPACT"     --argjson dispatch_resume "$DISPATCH_RESUME"     --argjson dispatch_task "$DISPATCH_TASK" '
    .hooks = {
      PreToolUse: [$shield_pre],
      PostToolUse: [$shield_post, $dispatch_task],
      SessionEnd: [$shield_end],
      UserPromptSubmit: [$dispatch_prompt],
      PreCompact: [$dispatch_compact],
      SessionStart: [$dispatch_resume]
    }
    | .statusLine = {"type": "command", "command": "~/.claude/hooks/statusline.sh"}
  ')
fi

echo "$UPDATED" | jq '.' > "$SETTINGS_FILE"
echo "  OK: Updated $SETTINGS_FILE"

# ── CLAUDE.md guidance ──────────────────────────────────────────────────
CLAUDE_MD="$HOME/.claude/CLAUDE.md"
MARKER_START="<!-- claude-secret-shield:start -->"
MARKER_END="<!-- claude-secret-shield:end -->"

SHIELD_SECTION="${MARKER_START}
## Secret Shield

This environment uses **redmem**. Values in \`{{NAME_hash}}\` format
(e.g. \`{{OPENAI_KEY_8f3a2b1c}}\`, \`{{WALLET_PRIVATE_KEY_d4e5f6a7}}\`) are
**redacted secret placeholders** — the real values have been replaced for safety.

Rules for working with placeholders:
- Treat placeholders as the actual secret values for all tasks.
- Use them as-is in code, file edits, and shell commands.
- The hook automatically restores real values before execution.
- **Never** ask the user to substitute, reveal, or re-enter the real values.
- **Never** attempt to guess, decode, or reconstruct the original secrets.
${MARKER_END}"

echo "  -> Configuring CLAUDE.md..."

if [ -f "$CLAUDE_MD" ]; then
  if grep -qF "$MARKER_START" "$CLAUDE_MD"; then
    python3 -c "
import sys, re
with open(sys.argv[1], 'r') as f:
    content = f.read()
pattern = re.escape(sys.argv[2]) + r'.*?' + re.escape(sys.argv[3])
result = re.sub(pattern, sys.argv[4], content, count=1, flags=re.DOTALL)
with open(sys.argv[1], 'w') as f:
    f.write(result)
" "$CLAUDE_MD" "$MARKER_START" "$MARKER_END" "$SHIELD_SECTION"
    echo "  OK: Updated existing section in $CLAUDE_MD"
  else
    printf '\n%s\n' "$SHIELD_SECTION" >> "$CLAUDE_MD"
    echo "  OK: Appended section to $CLAUDE_MD"
  fi
else
  printf '%s\n' "$SHIELD_SECTION" > "$CLAUDE_MD"
  echo "  OK: Created $CLAUDE_MD"
fi

# ── Summary ─────────────────────────────────────────────────────────────
echo ""
if [ "$MIGRATING" = true ]; then
  echo "  Migration from claude-secret-shield complete!"
else
  echo "  Installation complete!"
fi
echo ""
echo "  What redmem does:"
echo "    Shield: Secrets in files are replaced with {{PLACEHOLDER}} tokens"
echo "    Shield: Blocked files (.env, credentials) are never read"
echo "    Shield: Placeholders restored to real values on write"
echo "    Shield: User prompts scanned for accidental secret paste"
echo "    Memory: Full conversation archived to SQLite before /compact"
echo "    Memory: Session state + context restored on --resume"
echo "    Memory: Auto-recall from archive when you say \"remember\"/\"before\""
echo "    Memory: Cross-session knowledge index for project continuity"
echo ""
echo "  Files:"
echo "    Shield hooks:   ~/.claude/hooks/redact-restore.py + patterns.py"
echo "    Memory module:  ~/.claude/hooks/memory/*.py"
echo "    Dispatcher:     ~/.claude/hooks/redmem_dispatcher.py"
echo "    Archives:       ~/.claude/vault/sessions/"
echo "    Custom patterns: ~/.claude/hooks/custom-patterns.py (never overwritten)"
echo ""
echo "  To add custom secret patterns:"
echo "    cp ~/.claude/hooks/custom-patterns.example.py ~/.claude/hooks/custom-patterns.py"
echo "    # Edit custom-patterns.py"
echo ""
echo "  Re-running install.sh upgrades redmem without affecting custom patterns."
echo ""
echo "  Restart Claude Code for changes to take effect."
echo ""

# ── One-time catchup: archive existing sessions ─────────────────────────
echo "  -> Archiving existing sessions (last 60 days)..."
if python3 "$HOOKS_DIR/redmem_catchup.py" --max-age-days 60 2>&1; then
  echo "  OK: Catchup complete"
else
  echo "  WARN: Catchup had errors (not fatal — run manually later)"
fi

echo ""
echo "  Tips:"
echo "    - For continuous archival of long-running sessions:"
echo "        python3 ~/.claude/hooks/redmem_catchup.py --watch"
echo "    - Or set up as a launchd/systemd daemon:"
echo "        https://github.com/tokligence/redmem/blob/main/docs/watch-daemon.md"
echo ""
