#!/usr/bin/env python3
"""
redmem autopilot — let Claude keep working overnight on a spec.

Flow
────
1. User types `/autopilot [max_loop=150] <full-spec-path>` before going to bed.
2. UserPromptSubmit hook spots the expanded slash-command template (carries an
   `<!-- autopilot-init: ... -->` marker), parses args, creates a state file
   under ~/.claude/vault/autopilot/<session_id>.json.
3. Claude works a turn and stops.
4. Stop hook (this module) reads the transcript, decides:
   - Last user message carries `<!-- autopilot-continuation -->` ?
       no  -> human broke in, pause autopilot, allow stop.
       yes -> we're in autopilot rhythm:
           a. last assistant message contains `[[AUTOPILOT_DONE]]`
              -> allow stop, write AUTOPILOT_DONE.md.
           b. hit any halt condition (max loops / no-change streak / wall
              clock) -> allow stop, write AUTOPILOT_HALTED.md.
           c. otherwise -> emit `{"decision":"block","reason":"<continuation>"}`
              which Claude Code treats as a new user turn; the continuation
              text itself carries the marker so the next stop-hook tick
              recognises the loop.

Design notes
────────────
- stop_hook_active=true -> always allow. This is Claude Code's own recursion
  guard; we respect it.
- We never "kill" Claude mid-turn. The loop only advances between turns.
- All disk state lives under ~/.claude/vault/autopilot/. Status/progress
  markers land in the repo root where the user will look in the morning.
- Fail-open: any unexpected error logs to stderr and allows stop. We never
  want a hook bug to lock a session in autopilot forever.

CLI subcommands (called from dispatcher / slash commands)
─────────────────────────────────────────────────────────
  autopilot.py stop-hook     - read Stop event JSON on stdin, emit decision
  autopilot.py init          - read UserPromptSubmit JSON on stdin
                               (dispatched by redmem_dispatcher)
  autopilot.py stop          - disengage active autopilot(s) on this host
  autopilot.py status        - print summary of active autopilot(s)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys

# ── Constants ───────────────────────────────────────────────────────────

DEFAULT_MAX_LOOP = 150
NO_CHANGE_HALT_STREAK = 5
WALL_CLOCK_HALT_SECONDS = 10 * 3600  # 10h

CONTINUATION_MARKER = "<!-- autopilot-continuation -->"
INIT_MARKER_RE = re.compile(r"<!--\s*autopilot-init:\s*(.*?)\s*-->")
DONE_SENTINEL = "[[AUTOPILOT_DONE]]"

LOG_PREFIX = "[redmem-autopilot]"

# ── Bash guard patterns (denied when autopilot is active) ──────────────
#
# Each entry is (regex, reason). The reason is returned to Claude via
# permissionDecisionReason so it can pick a safer alternative next turn.
# Patterns deliberately err on the side of over-blocking; autopilot is an
# unattended mode — a false-positive costs one extra Claude turn, a
# false-negative can cost your night's work.

DANGEROUS_BASH_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*[rR]|--recursive\b|--force\b.*-r|-r\b.*--force)"),
        "`rm -rf` blocked. Move the target to `~/.autopilot-trash/<YYYYMMDD-HHMMSS>/` "
        "with `mv` instead — it's reversible; user will sweep the trash later.",
    ),
    (
        re.compile(r"\bfind\b[^\|;&]*\s-exec\s+rm\b"),
        "`find -exec rm` blocked. Collect paths first, then `mv` them into "
        "`~/.autopilot-trash/<timestamp>/`.",
    ),
    (
        re.compile(r"\bgit\s+reset\s+(?:[^|;&]*\s)?--hard\b"),
        "`git reset --hard` blocked. Use `git stash push -u` to shelve work, "
        "or `git revert <sha>` to undo a commit safely.",
    ),
    (
        re.compile(r"\bgit\s+checkout\s+(?:-f\b|--force\b|\.|--\s*\.)"),
        "`git checkout -f` / `git checkout .` blocked (throws away uncommitted "
        "work). Use `git stash` if you need a clean tree temporarily.",
    ),
    (
        re.compile(r"\bgit\s+clean\s+(?:-[a-zA-Z]*[fdx]|--force)"),
        "`git clean -fdx` blocked (destroys untracked files). Inspect "
        "untracked files first; move or stash them explicitly.",
    ),
    (
        re.compile(r"\bgit\s+branch\s+(?:-D\b|-d\b[^|;&]*--force|--delete\b[^|;&]*--force)"),
        "Branch deletion blocked. Log the request in `QUESTIONS.md` for the "
        "user to handle in the morning; branches are cheap, leave them.",
    ),
    (
        re.compile(r"\bgit\s+push\s+(?:[^|;&]*\s)?(?:-f\b|--force\b|--force-with-lease\b)"),
        "`git push --force` blocked. Do a regular push. On non-fast-forward, "
        "log the situation in `QUESTIONS.md`.",
    ),
    (
        re.compile(r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA|INDEX)\b", re.IGNORECASE),
        "DROP statement blocked. Record the schema change in `QUESTIONS.md` "
        "for explicit approval.",
    ),
    (
        re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
        "TRUNCATE TABLE blocked. Record in `QUESTIONS.md`.",
    ),
]


def _vault_dir() -> str:
    override = os.environ.get("REDMEM_AUTOPILOT_STATE_DIR")
    if override:
        return override
    return os.path.expanduser("~/.claude/vault/autopilot")


def _state_path(session_id: str) -> str:
    return os.path.join(_vault_dir(), f"{session_id}.json")


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"{LOG_PREFIX} {msg}\n")
    except Exception:
        pass


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _parse_ts(s: str) -> _dt.datetime | None:
    if not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


# ── State file ─────────────────────────────────────────────────────────

def load_state(session_id: str) -> dict | None:
    path = _state_path(session_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log(f"load_state failed ({e.__class__.__name__}); ignoring state")
        return None


def save_state(session_id: str, state: dict) -> None:
    os.makedirs(_vault_dir(), exist_ok=True)
    path = _state_path(session_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def delete_state(session_id: str) -> None:
    try:
        os.unlink(_state_path(session_id))
    except FileNotFoundError:
        pass


def list_active_states() -> list[tuple[str, dict]]:
    """Return (session_id, state) for every state file with active=true."""
    out: list[tuple[str, dict]] = []
    vault = _vault_dir()
    if not os.path.isdir(vault):
        return out
    for name in os.listdir(vault):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        st = load_state(sid)
        if st and st.get("active"):
            out.append((sid, st))
    return out


# ── Arg parsing ────────────────────────────────────────────────────────

def parse_args(arg_string: str) -> tuple[int, str] | None:
    """
    Parse `/autopilot` arguments.

    Accepts:
        "150 /path/to/spec.md"
        "/path/to/spec.md"                 -> defaults to DEFAULT_MAX_LOOP
        "  /path with spaces/spec.md  "    -> spec path may contain spaces

    Returns (max_loop, spec_path), or None if unparseable.
    """
    s = (arg_string or "").strip()
    if not s:
        return None

    first, _, rest = s.partition(" ")
    rest = rest.strip()
    if first.isdigit() and rest:
        try:
            return int(first), rest
        except ValueError:
            return None
    return DEFAULT_MAX_LOOP, s


# ── Transcript reading ─────────────────────────────────────────────────

def _iter_transcript(path: str):
    """Yield parsed JSON objects from a JSONL transcript, skipping bad lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        _log(f"cannot read transcript {path}: {e.__class__.__name__}")
        return


def _extract_text(msg_obj: dict) -> str:
    """Extract concatenated text content from a transcript entry."""
    if not isinstance(msg_obj, dict):
        return ""
    # Claude Code transcript entries usually look like:
    #   {"type":"user","message":{"role":"user","content":"..."}}
    # or {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."}]}}
    msg = msg_obj.get("message")
    if not isinstance(msg, dict):
        msg = msg_obj  # some shapes may be flat
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(item.get("text", "") or "")
            elif t == "tool_result":
                c = item.get("content", "")
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    for sub in c:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text", "") or "")
        return "\n".join(parts)
    return ""


def last_user_text(transcript_path: str) -> str:
    """Return the text of the most recent 'user' entry (or '' if none)."""
    last = ""
    for obj in _iter_transcript(transcript_path):
        if obj.get("type") == "user":
            t = _extract_text(obj)
            if t:
                last = t
    return last


def last_assistant_text(transcript_path: str) -> str:
    """Return the text of the most recent 'assistant' entry (or '' if none)."""
    last = ""
    for obj in _iter_transcript(transcript_path):
        if obj.get("type") == "assistant":
            t = _extract_text(obj)
            if t:
                last = t
    return last


# ── Git helpers (repo root, branch, dirty, worktree) ───────────────────

PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "dev"})


def _git(repo_root: str, *args: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""


def resolve_repo_root(cwd: str) -> str:
    if not cwd:
        return ""
    rc, out = _git(cwd, "rev-parse", "--show-toplevel")
    if rc == 0 and out.strip():
        return out.strip()
    return os.path.abspath(cwd)


def ensure_git_exclude(repo_root: str, entries: list) -> bool:
    """
    Idempotently append `entries` (each a line like '/.autopilot/') to
    `<repo>/.git/info/exclude` — or, for a linked worktree, whatever
    `git rev-parse --git-path info/exclude` resolves to.

    Why info/exclude, not .gitignore:
      - .gitignore is a tracked, shared file. Adding redmem's housekeeping
        rules there would pollute PRs and could conflict with team style.
      - info/exclude is per-clone-local; nothing leaks to the remote.

    Returns True if anything was written, False otherwise (or on any
    failure — fail-open so a hook bug can't disrupt the session).
    """
    if not repo_root:
        return False
    rc, out = _git(repo_root, "rev-parse", "--git-path", "info/exclude")
    if rc != 0:
        return False
    exclude_path = out.strip()
    if not exclude_path:
        return False
    if not os.path.isabs(exclude_path):
        exclude_path = os.path.join(repo_root, exclude_path)
    try:
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        existing_lines = set()
        if os.path.isfile(exclude_path):
            with open(exclude_path, "r", encoding="utf-8") as f:
                existing_lines = set(f.read().splitlines())
        to_add = [e for e in entries if e not in existing_lines]
        if not to_add:
            return False
        with open(exclude_path, "a", encoding="utf-8") as f:
            # Make the block visually separated if the file isn't empty.
            if existing_lines:
                f.write("\n")
            f.write("# Added by redmem\n")
            for e in to_add:
                f.write(e + "\n")
        return True
    except OSError as e:
        _log(f"ensure_git_exclude failed: {e.__class__.__name__}: {e}")
        return False


def preflight_health_check(cwd: str, repo_root: str) -> dict:
    """
    Refuse-or-warn checks run at /autopilot init:

    Hard refusal (ok=False):
      - cwd is not a git repo
      - current branch is in PROTECTED_BRANCHES
      - working tree has uncommitted changes

    Soft warning (ok=True, warnings non-empty):
      - not inside a git worktree (strongly recommend, not required)

    Returns: {"ok": bool, "errors": [str], "warnings": [str], "is_worktree": bool, "branch": str}
    """
    result = {"ok": True, "errors": [], "warnings": [],
              "is_worktree": False, "branch": ""}
    if not cwd or not os.path.isdir(cwd):
        result["ok"] = False
        result["errors"].append("cwd is not a directory")
        return result

    rc, _ = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        result["ok"] = False
        result["errors"].append(
            "cwd is not inside a git repo. autopilot needs git for safe "
            "rollback; run `git init` or cd into a repo."
        )
        return result

    rc, branch_out = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_out.strip() if rc == 0 else ""
    result["branch"] = branch
    if branch in PROTECTED_BRANCHES:
        result["ok"] = False
        result["errors"].append(
            f"refusing to run on protected branch `{branch}`. "
            f"Switch to a feature branch (`git switch -c autopilot/<name>`) "
            f"or, better, create a worktree:\n"
            f"  git worktree add ../$(basename $PWD)-autopilot-$(date +%s) "
            f"-b autopilot/<name>\n"
            f"  cd ../*-autopilot-*  &&  claude"
        )

    rc, status_out = _git(repo_root, "status", "--porcelain", timeout=5.0)
    if rc == 0 and status_out.strip():
        n = len(status_out.strip().splitlines())
        result["ok"] = False
        result["errors"].append(
            f"working tree has {n} uncommitted/untracked path(s). "
            f"autopilot needs a clean baseline so you can review diffs in "
            f"the morning. Either `git add -A && git commit -m '<wip>'` "
            f"or `git stash push -u` first."
        )

    rc, gitdir_out = _git(cwd, "rev-parse", "--git-dir")
    if rc == 0:
        gitdir = gitdir_out.strip()
        # A worktree's git-dir is like `/path/to/main/.git/worktrees/<name>`.
        result["is_worktree"] = "/worktrees/" in gitdir or gitdir.endswith("/worktrees")

    if result["ok"] and not result["is_worktree"]:
        result["warnings"].append(
            "not running inside a git worktree. Strongly recommended for "
            "overnight runs — it contains blast radius of accidental deletions.\n"
            "  git worktree add ../$(basename $PWD)-autopilot-$(date +%s) "
            "-b autopilot/<name>\n"
            "  cd ../*-autopilot-*  &&  claude"
        )

    return result


_FP_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".mypy_cache", ".pytest_cache",
})


def repo_fingerprint(repo_root: str) -> str:
    """Hash of (file count + max mtime) over the working tree.

    Any Write/Edit by Claude advances an mtime; any create/delete changes
    the count. Fast because it's just stat() calls — no git subprocesses
    that could blow the Stop-hook timeout on large repos.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return ""
    max_mt = 0.0
    count = 0
    try:
        for root, dirs, files in os.walk(repo_root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in _FP_SKIP_DIRS]
            for f in files:
                try:
                    mt = os.path.getmtime(os.path.join(root, f))
                    if mt > max_mt:
                        max_mt = mt
                    count += 1
                except OSError:
                    pass
    except OSError:
        return ""
    return f"{count}:{max_mt:.3f}"


# ── Continuation text ──────────────────────────────────────────────────

def build_continuation(state: dict) -> str:
    """Text injected into the reason field of a block decision."""
    spec = state.get("spec", "")
    it = state.get("iter_count", 0) + 1  # the upcoming turn
    mx = state.get("max_loop", DEFAULT_MAX_LOOP)
    return (
        f"{CONTINUATION_MARKER}\n\n"
        f"Autopilot 第 {it}/{mx} 轮。这是自动触发的消息，不必寒暄。\n\n"
        f"规则：\n"
        f"1. 每轮开头重读 spec 文件: {spec}\n"
        f"2. 做完所有你能做的之后，在最后一条消息里原样输出 {DONE_SENTINEL} 然后停下。\n"
        f"3. 禁止反问。需要用户决定的事追加到 `.autopilot/QUESTIONS.md`，跳过它继续做下一件。\n"
        f"4. 发现 spec 本身需要改进或澄清的地方：写建议到 `.autopilot/IMPROVE.md`（不要改 spec 本身），按当前理解继续。\n"
        f"5. 维护 `.autopilot/TASKS.md` 勾选框。所有运行产物都写在 `.autopilot/` 目录下（已加入 `.git/info/exclude`，不进 git）。\n"
        f"6. 继续干活。"
    )


# ── Initial prompt (used by /autopilot.md slash template) ──────────────

def build_init_prompt(spec: str, max_loop: int) -> str:
    return (
        f"{CONTINUATION_MARKER}\n\n"
        f"Autopilot 启动：按 {spec} 干活，共 {max_loop} 轮预算。本消息是自动模板，不必寒暄。\n\n"
        f"规则：\n"
        f"1. 每轮开头重读 spec 文件: {spec}\n"
        f"2. 做完所有你能做的之后，在最后一条消息里原样输出 {DONE_SENTINEL} 然后停下。\n"
        f"3. 禁止反问。需要用户决定的事追加到 `.autopilot/QUESTIONS.md`，跳过它继续做下一件。\n"
        f"4. 发现 spec 本身需要改进或澄清的地方：写建议到 `.autopilot/IMPROVE.md`（不要改 spec 本身），按当前理解继续。\n"
        f"5. 维护 `.autopilot/TASKS.md` 勾选框。所有运行产物都写在 `.autopilot/` 目录下（已加入 `.git/info/exclude`，不进 git）。\n"
        f"6. 现在开始第 1 轮。"
    )


# ── Halt-reason file writers ──────────────────────────────────────────

ARTIFACTS_DIR = ".autopilot"

ARTIFACTS_README = """# .autopilot/ — runtime artifacts

This directory is managed by the redmem autopilot plugin. Everything in
here is **local to your clone / worktree** — the plugin auto-registers
`/.autopilot/` into `.git/info/exclude` so git never tracks it. Nothing
here will leak into a PR.

| File | Who writes it | What it is |
|------|---------------|-----------|
| `TASKS.md` | Claude | Live checklist; unchecked items = left to do. |
| `QUESTIONS.md` | Claude | Decisions Claude deferred to you (it was told not to ask). |
| `IMPROVE.md` | Claude | Claude's suggestions for improving the spec itself. |
| `DONE.md` | plugin | Written when Claude emits `[[AUTOPILOT_DONE]]`. |
| `HALTED.md` | plugin | Written when a halt condition trips (max_loop / stuck / wall-clock). |
| `README.md` | plugin | This file. |

Safe to delete the whole directory any time.
"""


def _write_artifact(repo_root: str, name: str, body: str) -> None:
    """Write a file under <repo>/.autopilot/."""
    if not repo_root:
        return
    try:
        target_dir = os.path.join(repo_root, ARTIFACTS_DIR)
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, name), "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        _log(f"cannot write {ARTIFACTS_DIR}/{name}: {e.__class__.__name__}")


def _halt_summary(state: dict, reason: str) -> str:
    started = state.get("started_at", "")
    spec = state.get("spec", "")
    it = state.get("iter_count", 0)
    mx = state.get("max_loop", DEFAULT_MAX_LOOP)
    return (
        f"# Autopilot halted\n\n"
        f"- reason: {reason}\n"
        f"- started_at: {started}\n"
        f"- ended_at: {_utcnow_iso()}\n"
        f"- iterations: {it} / {mx}\n"
        f"- spec: {spec}\n"
        f"- session_id: {state.get('session_id', '')}\n"
    )


def _done_summary(state: dict) -> str:
    return (
        f"# Autopilot done\n\n"
        f"- started_at: {state.get('started_at', '')}\n"
        f"- ended_at: {_utcnow_iso()}\n"
        f"- iterations: {state.get('iter_count', 0)} / {state.get('max_loop', DEFAULT_MAX_LOOP)}\n"
        f"- spec: {state.get('spec', '')}\n"
        f"- session_id: {state.get('session_id', '')}\n\n"
        f"Claude reported {DONE_SENTINEL}. Check QUESTIONS.md and IMPROVE.md before next steps.\n"
    )


# ── init: handle UserPromptSubmit carrying <!-- autopilot-init: ... --> ─

def handle_init(data: dict) -> str | None:
    """
    Called by redmem_dispatcher when a UserPromptSubmit event carries an
    `<!-- autopilot-init: ARGS -->` marker (i.e. the user just ran /autopilot).

    Runs the preflight health check. On refusal, DOES NOT create state and
    returns a message for the dispatcher to inject into `additionalContext`
    so Claude (and the user) see why autopilot didn't start. On success,
    creates the state file and returns a short "autopilot armed" message
    plus any soft warnings.

    Return:
        - str: message to surface to Claude/user
        - None: nothing to say (marker absent, can't parse, no session_id)
    """
    prompt = data.get("prompt", "") or ""
    m = INIT_MARKER_RE.search(prompt)
    if not m:
        return None
    args = m.group(1).strip()
    parsed = parse_args(args)
    if not parsed:
        _log(f"init: could not parse args: {args!r}")
        return (
            "[autopilot] Could not parse arguments. Usage: "
            "`/autopilot [max_loop=150] <full-spec-path>`. Autopilot NOT armed."
        )
    max_loop, spec = parsed

    session_id = data.get("session_id") or ""
    if not session_id:
        _log("init: no session_id; skipping")
        return None
    cwd = data.get("cwd") or os.getcwd()
    repo_root = resolve_repo_root(cwd)

    # Preflight health check
    health = preflight_health_check(cwd, repo_root)
    if not health["ok"]:
        _log(f"init refused: {'; '.join(health['errors'])}")
        bullets = "\n".join(f"  - {e}" for e in health["errors"])
        return (
            f"[autopilot] **Refused to arm** — preflight failed:\n"
            f"{bullets}\n\n"
            f"Fix the above and run `/autopilot` again. No state was created, "
            f"Stop-hook loop is NOT active."
        )

    # Armed: persist state
    state = {
        "session_id": session_id,
        "active": True,
        "spec": spec,
        "max_loop": max_loop,
        "iter_count": 0,
        "no_change_streak": 0,
        "last_fingerprint": repo_fingerprint(repo_root),
        "started_at": _utcnow_iso(),
        "cwd": cwd,
        "repo_root": repo_root,
        "branch": health.get("branch", ""),
        "is_worktree": health.get("is_worktree", False),
    }
    save_state(session_id, state)

    # Register /.autopilot/ in git's local exclude list; plant the README.
    ensure_git_exclude(repo_root, ["/.autopilot/"])
    _write_artifact(repo_root, "README.md", ARTIFACTS_README)
    _log(f"init armed: session={session_id[:8]} spec={spec} "
         f"max_loop={max_loop} branch={health.get('branch')} "
         f"worktree={health.get('is_worktree')}")

    # Surface the armed status + any warnings
    lines = [
        f"[autopilot] **Armed** — branch=`{health.get('branch')}`, "
        f"worktree={health.get('is_worktree')}, max_loop={max_loop}."
    ]
    for w in health["warnings"]:
        lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


# ── Bash guard ─────────────────────────────────────────────────────────

def check_bash_command(data: dict) -> dict | None:
    """
    PreToolUse hook helper for the Bash tool.

    Returns a deny response dict if the session is in autopilot mode and
    the command matches a dangerous pattern. Returns None otherwise (pass
    through). Fail-open: any error → None, so a hook bug cannot deny
    legitimate commands.
    """
    try:
        session_id = data.get("session_id") or ""
        if not session_id:
            return None
        state = load_state(session_id)
        if not state or not state.get("active"):
            return None
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        command = tool_input.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return None
        for regex, reason in DANGEROUS_BASH_PATTERNS:
            if regex.search(command):
                _log(f"bash-guard deny session={session_id[:8]} "
                     f"cmd={command[:80]!r}")
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"autopilot guard: {reason}",
                    }
                }
    except Exception as e:
        _log(f"bash-guard error: {e.__class__.__name__}: {e}")
    return None


# ── Stop hook core ─────────────────────────────────────────────────────

def handle_stop_hook(data: dict) -> dict | None:
    """
    Return a hook response dict to emit, or None to exit 0 (allow stop).
    """
    session_id = data.get("session_id") or ""
    if not session_id:
        return None

    # Respect Claude Code's own recursion guard.
    if data.get("stop_hook_active"):
        return None

    state = load_state(session_id)
    if not state or not state.get("active"):
        return None

    transcript_path = data.get("transcript_path") or ""
    last_user = last_user_text(transcript_path) if transcript_path else ""

    # --- Human broke in? ---
    # The initial /autopilot expansion and every continuation carry
    # CONTINUATION_MARKER. If the last user message doesn't, a human typed
    # something and we disengage gracefully.
    if CONTINUATION_MARKER not in last_user:
        state["active"] = False
        state["paused_at"] = _utcnow_iso()
        state["paused_reason"] = "human intervention"
        save_state(session_id, state)
        _log(f"pausing {session_id[:8]}: human intervention detected")
        return None

    # --- Claude signalled done? ---
    last_asst = last_assistant_text(transcript_path) if transcript_path else ""
    if DONE_SENTINEL in last_asst:
        state["active"] = False
        state["ended_at"] = _utcnow_iso()
        state["end_reason"] = "done"
        save_state(session_id, state)
        _write_artifact(state.get("repo_root", ""),
                    "DONE.md", _done_summary(state))
        _log(f"done {session_id[:8]} after {state.get('iter_count', 0)} iters")
        return None

    # --- Halt conditions ---
    iter_count = int(state.get("iter_count", 0))
    max_loop = int(state.get("max_loop", DEFAULT_MAX_LOOP))

    # Wall clock
    started = _parse_ts(state.get("started_at", ""))
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=_dt.timezone.utc)
        elapsed = (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()
        if elapsed >= WALL_CLOCK_HALT_SECONDS:
            return _halt(state, session_id,
                         f"wall-clock {int(elapsed)}s >= {WALL_CLOCK_HALT_SECONDS}s")

    # Max iterations
    if iter_count >= max_loop:
        return _halt(state, session_id, f"max_loop reached ({iter_count}/{max_loop})")

    # No-change streak — compare repo fingerprint to last iter's.
    repo_root = state.get("repo_root", "")
    fp_now = repo_fingerprint(repo_root)
    fp_prev = state.get("last_fingerprint", "")
    if fp_now and fp_prev and fp_now == fp_prev:
        streak = int(state.get("no_change_streak", 0)) + 1
    else:
        streak = 0
    state["no_change_streak"] = streak
    state["last_fingerprint"] = fp_now
    if streak >= NO_CHANGE_HALT_STREAK:
        return _halt(state, session_id,
                     f"no repo change for {streak} consecutive turns")

    # --- Continue the loop ---
    state["iter_count"] = iter_count + 1
    state["last_continuation_at"] = _utcnow_iso()
    save_state(session_id, state)
    return {"decision": "block", "reason": build_continuation(state)}


def _halt(state: dict, session_id: str, reason: str) -> None:
    state["active"] = False
    state["ended_at"] = _utcnow_iso()
    state["end_reason"] = reason
    save_state(session_id, state)
    _write_artifact(state.get("repo_root", ""),
                    "HALTED.md", _halt_summary(state, reason))
    _log(f"halt {session_id[:8]}: {reason}")
    return None


# ── CLI: stop / status (called from slash-command `!python3 ...`) ──────

def cli_stop() -> int:
    actives = list_active_states()
    if not actives:
        print("autopilot: no active sessions.")
        return 0
    for sid, st in actives:
        st["active"] = False
        st["ended_at"] = _utcnow_iso()
        st["end_reason"] = "user /autopilot-stop"
        save_state(sid, st)
        print(f"autopilot: stopped session {sid[:8]} "
              f"(iter {st.get('iter_count', 0)}/{st.get('max_loop', '?')}).")
    return 0


def cli_status() -> int:
    actives = list_active_states()
    if not actives:
        # Also show recently halted/done for context, up to 3.
        vault = _vault_dir()
        recent: list[tuple[float, str, dict]] = []
        if os.path.isdir(vault):
            for name in os.listdir(vault):
                if not name.endswith(".json"):
                    continue
                sid = name[:-5]
                st = load_state(sid)
                if not st:
                    continue
                try:
                    mt = os.path.getmtime(os.path.join(vault, name))
                except OSError:
                    mt = 0.0
                recent.append((mt, sid, st))
        recent.sort(reverse=True)
        print("autopilot: no active sessions.")
        for _, sid, st in recent[:3]:
            print(f"  last: {sid[:8]} "
                  f"ended_at={st.get('ended_at', '?')} "
                  f"reason={st.get('end_reason', '?')} "
                  f"iter={st.get('iter_count', 0)}/{st.get('max_loop', '?')}")
        return 0

    for sid, st in actives:
        started = st.get("started_at", "?")
        spec = st.get("spec", "?")
        it = st.get("iter_count", 0)
        mx = st.get("max_loop", "?")
        streak = st.get("no_change_streak", 0)
        print(f"autopilot: ACTIVE session {sid[:8]}")
        print(f"  spec           : {spec}")
        print(f"  iterations     : {it}/{mx}")
        print(f"  no-change streak: {streak}/{NO_CHANGE_HALT_STREAK}")
        print(f"  started_at     : {started}")
        print(f"  repo_root      : {st.get('repo_root', '?')}")
    return 0


# ── Main dispatch ──────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    sub = sys.argv[1]

    if sub == "stop-hook":
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            _log(f"stop-hook: bad stdin json: {e.__class__.__name__}")
            return 0
        try:
            resp = handle_stop_hook(data)
        except Exception as e:  # fail-OPEN
            _log(f"stop-hook error: {e.__class__.__name__}: {e}")
            return 0
        if resp:
            sys.stdout.write(json.dumps(resp))
        return 0

    if sub == "init":
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            _log(f"init: bad stdin json: {e.__class__.__name__}")
            return 0
        try:
            handle_init(data)
        except Exception as e:  # fail-OPEN
            _log(f"init error: {e.__class__.__name__}: {e}")
        return 0

    if sub == "stop":
        return cli_stop()
    if sub == "status":
        return cli_status()

    _log(f"unknown subcommand: {sub}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
