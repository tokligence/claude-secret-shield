#!/bin/sh
# claude-secret-shield — One-line installer
# Usage: curl -sL https://raw.githubusercontent.com/tokligence/claude-secret-shield/main/install.sh | sh
#
# What this does:
#   1. Installs the redact-restore hook (Python) to ~/.claude/hooks/
#   2. Installs the patterns file to ~/.claude/hooks/
#   3. Merges hook config into ~/.claude/settings.json (preserves existing settings)
#   4. Done — next Claude Code session will redact secrets automatically.

set -e

HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS_FILE="$HOME/.claude/settings.json"
BASE_URL="https://raw.githubusercontent.com/tokligence/claude-secret-shield/main"

echo ""
echo "  claude-secret-shield"
echo "  ----------------------------"
echo "  Prevents Claude Code from seeing your secrets."
echo "  Secrets are replaced with placeholders and restored on write."
echo ""

# Check prerequisites
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

# Create hooks directory
mkdir -p "$HOOKS_DIR"

# Download hook files
echo "  -> Downloading hook script..."
curl -fsSL "$BASE_URL/hooks/redact-restore.py" -o "$HOOKS_DIR/redact-restore.py"
chmod +x "$HOOKS_DIR/redact-restore.py"
echo "  OK: Installed $HOOKS_DIR/redact-restore.py"

echo "  -> Downloading patterns..."
curl -fsSL "$BASE_URL/hooks/patterns.py" -o "$HOOKS_DIR/patterns.py"
echo "  OK: Installed $HOOKS_DIR/patterns.py"

# Install custom-patterns example (never overwrite user's custom file)
echo "  -> Downloading custom-patterns example..."
curl -fsSL "$BASE_URL/hooks/custom-patterns.example.py" -o "$HOOKS_DIR/custom-patterns.example.py"
echo "  OK: Installed $HOOKS_DIR/custom-patterns.example.py"

# Install status line script
echo "  -> Downloading status line..."
curl -fsSL "$BASE_URL/hooks/statusline.sh" -o "$HOOKS_DIR/statusline.sh"
chmod +x "$HOOKS_DIR/statusline.sh"
echo "  OK: Installed $HOOKS_DIR/statusline.sh"

if [ -f "$HOOKS_DIR/custom-patterns.py" ]; then
  echo "  OK: Existing custom-patterns.py preserved (not overwritten)"
fi

# Remove old bash hook if present
if [ -f "$HOOKS_DIR/redact-secrets.sh" ]; then
  rm "$HOOKS_DIR/redact-secrets.sh"
  echo "  OK: Removed old redact-secrets.sh hook"
fi

# Merge into settings.json
echo "  -> Configuring Claude Code settings..."

PRE_HOOK_CONFIG='{"matcher":"Read|Write|Edit|Bash","hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'

POST_HOOK_CONFIG='{"matcher":"Read|Write|Edit","hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'


SESSION_END_HOOK_CONFIG='{"hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'

PROMPT_HOOK_CONFIG='{"hooks":[{"type":"command","command":"python3 ~/.claude/hooks/redact-restore.py","timeout":5}]}'
if [ -f "$SETTINGS_FILE" ]; then
  EXISTING=$(cat "$SETTINGS_FILE")

  HAS_HOOKS=$(echo "$EXISTING" | jq 'has("hooks")' 2>/dev/null || echo "false")

  if [ "$HAS_HOOKS" = "true" ]; then
    # Remove any old hook entries, add PreToolUse, PostToolUse, UserPromptSubmit, SessionEnd
    UPDATED=$(echo "$EXISTING" | jq \
      --argjson pre_hook "$PRE_HOOK_CONFIG" \
      --argjson post_hook "$POST_HOOK_CONFIG" \
      --argjson stop_hook "$SESSION_END_HOOK_CONFIG" \
      --argjson prompt_hook "$PROMPT_HOOK_CONFIG" '
      .hooks.PreToolUse = (
        (.hooks.PreToolUse // [])
        | map(select(
            (.hooks[0].command != "~/.claude/hooks/redact-secrets.sh") and
            (.hooks[0].command != "python3 ~/.claude/hooks/redact-restore.py")
          ))
      ) + [$pre_hook]
      |
      .hooks.PostToolUse = (
        (.hooks.PostToolUse // [])
        | map(select(
            (.hooks[0].command != "python3 ~/.claude/hooks/redact-restore.py")
          ))
      ) + [$post_hook]
      |
      .hooks.SessionEnd = [$stop_hook]
      |
      .hooks.UserPromptSubmit = (
        (.hooks.UserPromptSubmit // [])
        | map(select(
            (.hooks[0].command != "python3 ~/.claude/hooks/redact-restore.py") and
            (.command != "python3 ~/.claude/hooks/redact-restore.py")
          ))
      ) + [$prompt_hook]
      |
      .statusLine = {"type": "command", "command": "~/.claude/hooks/statusline.sh"}
    ')
  else
    UPDATED=$(echo "$EXISTING" | jq \
      --argjson pre_hook "$PRE_HOOK_CONFIG" \
      --argjson post_hook "$POST_HOOK_CONFIG" \
      --argjson stop_hook "$SESSION_END_HOOK_CONFIG" \
      --argjson prompt_hook "$PROMPT_HOOK_CONFIG" '
      .hooks = { "PreToolUse": [$pre_hook], "PostToolUse": [$post_hook], "SessionEnd": [$stop_hook], "UserPromptSubmit": [$prompt_hook] } | .statusLine = {"type": "command", "command": "~/.claude/hooks/statusline.sh"}
    ')
  fi

  echo "$UPDATED" | jq '.' > "$SETTINGS_FILE"
else
  jq -n \
    --argjson pre_hook "$PRE_HOOK_CONFIG" \
    --argjson post_hook "$POST_HOOK_CONFIG" \
    --argjson stop_hook "$SESSION_END_HOOK_CONFIG" \
    --argjson prompt_hook "$PROMPT_HOOK_CONFIG" '{
    hooks: { PreToolUse: [$pre_hook], PostToolUse: [$post_hook], SessionEnd: [$stop_hook], UserPromptSubmit: [$prompt_hook] }
  }' > "$SETTINGS_FILE"
fi

echo "  OK: Updated $SETTINGS_FILE"

# ── Inject placeholder guidance into ~/.claude/CLAUDE.md ──────────────
CLAUDE_MD="$HOME/.claude/CLAUDE.md"
MARKER_START="<!-- claude-secret-shield:start -->"
MARKER_END="<!-- claude-secret-shield:end -->"

SHIELD_SECTION="${MARKER_START}
## Secret Shield

This environment uses **claude-secret-shield**. Values in \`{{NAME_hash}}\` format
(e.g. \`{{OPENAI_KEY_8f3a2b1c}}\`, \`{{WALLET_PRIVATE_KEY_d4e5f6a7}}\`) are
**redacted secret placeholders** — the real values have been replaced for safety.

Rules for working with placeholders:
- Treat placeholders as the actual secret values for all tasks.
- Use them as-is in code, file edits, and shell commands.
- The hook automatically restores real values before execution.
- **Never** ask the user to substitute, reveal, or re-enter the real values.
- **Never** attempt to guess, decode, or reconstruct the original secrets.
${MARKER_END}"

echo "  -> Configuring CLAUDE.md placeholder guidance..."

if [ -f "$CLAUDE_MD" ]; then
  if grep -qF "$MARKER_START" "$CLAUDE_MD"; then
    # Replace existing section (upgrade path)
    # Use Python for reliable multiline replacement between markers
    python3 -c "
import sys, re
with open(sys.argv[1], 'r') as f:
    content = f.read()
start_marker = sys.argv[2]
end_marker = sys.argv[3]
new_section = sys.argv[4]
pattern = re.escape(start_marker) + r'.*?' + re.escape(end_marker)
result = re.sub(pattern, new_section, content, count=1, flags=re.DOTALL)
with open(sys.argv[1], 'w') as f:
    f.write(result)
" "$CLAUDE_MD" "$MARKER_START" "$MARKER_END" "$SHIELD_SECTION"
    echo "  OK: Updated existing section in $CLAUDE_MD"
  else
    # Append to existing file (with blank line separator)
    printf '\n%s\n' "$SHIELD_SECTION" >> "$CLAUDE_MD"
    echo "  OK: Appended section to $CLAUDE_MD"
  fi
else
  # Create new file
  printf '%s\n' "$SHIELD_SECTION" > "$CLAUDE_MD"
  echo "  OK: Created $CLAUDE_MD"
fi

echo ""
echo "  Installation complete!"
echo ""
echo "  How it works:"
echo "    - Strategy 1: Blocked files (.env, credentials, etc.) are never read"
echo "    - Strategy 2: Secrets in any file are replaced with {{PLACEHOLDER}} tokens"
echo "    - Strategy 3: Placeholders are restored to real values when writing files"
echo "    - Strategy 4: User prompts are scanned — blocks if secrets are pasted"
echo ""
echo "  Upstream patterns:  ~/.claude/hooks/patterns.py (updated on each install)"
echo "  Custom patterns:    ~/.claude/hooks/custom-patterns.py (never overwritten)"
echo "  Session mappings:   /tmp/.claude-redact-{session_id}.json"
echo ""
echo "  To add your own patterns, copy the example file:"
echo "    cp ~/.claude/hooks/custom-patterns.example.py ~/.claude/hooks/custom-patterns.py"
echo "  Then edit custom-patterns.py to add your patterns."
echo ""
echo "  Re-running install.sh updates upstream patterns without affecting your custom patterns."
echo ""
echo "  Restart Claude Code for changes to take effect."
echo ""
