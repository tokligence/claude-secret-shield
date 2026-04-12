"""Session state generation and task/plan tracking."""
import json
import os
import re
from datetime import datetime, timezone
from . import db as archive_db

VAULT_DIR = archive_db.VAULT_DIR


def get_state_path(session_id: str) -> str:
    return os.path.join(VAULT_DIR, f"{session_id}_state.md")


def get_events_path(session_id: str) -> str:
    return os.path.join(VAULT_DIR, f"{session_id}_events.jsonl")


def track_state_event(session_id: str, tool_name: str, tool_input: dict,
                      tool_result: dict = None):
    """
    Append a state event from PostToolUse(Task/Plan).
    Writes to both state_events.jsonl (fast append) and SQLite (structured query).
    """
    if not session_id:
        return

    ts = datetime.now(timezone.utc).isoformat()

    # Determine event type and title from tool
    if tool_name in ("TaskCreate", "TodoWrite"):
        tasks = tool_input.get("todos", tool_input.get("tasks", []))
        if isinstance(tasks, list):
            for task in tasks:
                if isinstance(task, dict):
                    title = task.get("description", task.get("content", "unknown"))
                    status = task.get("status", "in_progress")
                    event_type = "task_completed" if status == "completed" else "task_created"
                    _write_event(session_id, ts, event_type, title[:200])
        # TodoWrite may also be a single task update
        content = tool_input.get("content", "")
        if content and not tasks:
            _write_event(session_id, ts, "task_created", content[:200])

    elif tool_name == "TaskUpdate":
        task_id = tool_input.get("id", "")
        status = tool_input.get("status", "")
        description = tool_input.get("description", task_id)
        if status == "completed":
            _write_event(session_id, ts, "task_completed", description[:200])
        elif status == "in_progress":
            _write_event(session_id, ts, "task_started", description[:200])

    elif tool_name in ("EnterPlanMode", "ExitPlanMode"):
        plan_text = tool_input.get("plan", tool_input.get("content", ""))
        _write_event(session_id, ts, "plan_updated",
                     tool_name, detail=plan_text[:500] if plan_text else None)


def _write_event(session_id: str, ts: str, event_type: str, title: str,
                 detail: str = None):
    """Write event to JSONL file and SQLite."""
    # JSONL append (fast, crash-safe)
    events_path = get_events_path(session_id)
    os.makedirs(os.path.dirname(events_path), mode=0o700, exist_ok=True)
    entry = {"ts": ts, "type": event_type, "title": title, "session_id": session_id}
    if detail:
        entry["detail"] = detail
    with open(events_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # SQLite (structured query)
    try:
        conn = archive_db.get_db(session_id)
        conn.execute("""
            INSERT INTO state_events (session_id, event_type, title, detail)
            VALUES (?, ?, ?, ?)
        """, (session_id, event_type, title, detail))
        conn.commit()
        conn.close()
    except Exception:
        pass  # JSONL is the fallback


def generate_session_state(session_id: str, cwd: str = ""):
    """
    Generate session_state.md from state_events + recent turns.
    Called during PreCompact after ingest.
    """
    state_path = get_state_path(session_id)

    # Parse existing state (if any) to preserve manually set goal
    prev_sections = _parse_existing_state(state_path)

    # Read state events from SQLite
    try:
        conn = archive_db.get_db(session_id)
        events = conn.execute("""
            SELECT event_type, title, detail FROM state_events
            WHERE session_id = ? ORDER BY created_at
        """, (session_id,)).fetchall()

        recent = conn.execute("""
            SELECT content FROM turns
            WHERE session_id = ? ORDER BY line_number DESC LIMIT 80
        """, (session_id,)).fetchall()
        conn.close()
    except Exception:
        events = []
        recent = []

    sections = {
        "goal": prev_sections.get("goal", ""),
        "plan": list(prev_sections.get("plan", [])),
        "done": list(prev_sections.get("done", [])),
        "blocked": set(prev_sections.get("blocked", [])),
        "decisions": set(prev_sections.get("decisions", [])),
    }

    # From events (high signal)
    for etype, title, detail in events:
        if etype == "task_completed":
            entry = f"- {title}"
            # Normalize: check both raw title and bulleted form to avoid duplicates
            if entry not in sections["done"] and title not in str(sections["done"]):
                sections["done"].append(entry)
        elif etype == "task_created" and title not in str(sections["plan"]):
            sections["plan"].append(title)
        elif etype == "plan_updated" and detail:
            sections["goal"] = detail

    # From recent turns (keyword extraction)
    blocker_re = re.compile(r"(blocked|workaround|can.t|failed|error|bug)", re.I)
    decision_re = re.compile(r"(decided|decision|choosing|approach|我们决定|确定)", re.I)

    for (content,) in recent:
        if blocker_re.search(content):
            sentence = _extract_first_sentence(content)
            if sentence and len(sentence) < 200:
                sections["blocked"].add(sentence)
        if decision_re.search(content):
            sentence = _extract_first_sentence(content)
            if sentence and len(sentence) < 200:
                sections["decisions"].add(sentence)

    # Render and write
    md = _render_state_md(sections)
    os.makedirs(os.path.dirname(state_path), mode=0o700, exist_ok=True)

    # Atomic write
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(md)
    os.rename(tmp_path, state_path)


def _parse_existing_state(state_path: str) -> dict:
    """Parse existing session_state.md into sections."""
    sections = {"goal": "", "plan": [], "done": [], "blocked": [], "decisions": []}
    if not os.path.isfile(state_path):
        return sections

    try:
        with open(state_path) as f:
            content = f.read()
    except OSError:
        return sections

    current_section = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Goal"):
            current_section = "goal"
        elif stripped.startswith("## Plan"):
            current_section = "plan"
        elif stripped.startswith("## Done"):
            current_section = "done"
        elif stripped.startswith("## Blocked"):
            current_section = "blocked"
        elif stripped.startswith("## Key Decision"):
            current_section = "decisions"
        elif stripped.startswith("## ") or stripped.startswith("# "):
            current_section = None
        elif current_section and stripped:
            if current_section == "goal":
                sections["goal"] = stripped
            else:
                sections[current_section].append(stripped)

    return sections


def _extract_first_sentence(text: str) -> str:
    """Extract first meaningful sentence from text."""
    # Skip common prefixes
    text = text.strip()
    for prefix in ("Let me ", "I will ", "I'll ", "Now ", "OK, "):
        if text.startswith(prefix):
            text = text[len(prefix):]

    # Take first sentence (up to period, newline, or 200 chars)
    match = re.match(r"^(.{10,200}?)[.\n]", text)
    if match:
        return match.group(1).strip()
    return text[:200].strip()


def _render_state_md(sections: dict) -> str:
    """Render sections dict to markdown."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    lines = [
        "# Session State",
        f"<!-- Auto-generated by redmem. Last updated: {ts} -->",
        "",
    ]

    if sections["goal"]:
        lines += ["## Goal", sections["goal"], ""]

    if sections["plan"]:
        lines += ["## Plan"]
        for i, item in enumerate(sections["plan"], 1):
            item = item.lstrip("0123456789. -")
            lines.append(f"{i}. {item}")
        lines.append("")

    if sections["done"]:
        lines += ["## Done (this session)"]
        for item in sections["done"]:
            if not item.startswith("- "):
                item = f"- {item}"
            lines.append(item)
        lines.append("")

    if sections["blocked"]:
        lines += ["## Blocked / Open"]
        for item in sorted(sections["blocked"]):
            if not item.startswith("- "):
                item = f"- {item}"
            lines.append(item)
        lines.append("")

    if sections["decisions"]:
        lines += ["## Key Decisions"]
        for item in sorted(sections["decisions"]):
            if not item.startswith("- "):
                item = f"- {item}"
            lines.append(item)
        lines.append("")

    return "\n".join(lines)
