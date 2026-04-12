"""Parse Claude Code session JSONL files."""
import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ParsedTurn:
    line_number: int
    uuid: str
    role: str          # 'user' or 'assistant'
    content: str
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    files_touched: Optional[str] = None


def extract_text(message: dict) -> str:
    """Extract text content from a Claude Code message."""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "tool_result":
                parts.append(str(item.get("content", "")))
    return "\n".join(parts)


def extract_tool_info(message: dict) -> tuple:
    """Extract tool_name and tool_input from assistant message."""
    content = message.get("content", [])
    if not isinstance(content, list):
        return None, None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = item.get("name", "")
            inp = item.get("input", {})
            # Summarize input (avoid storing huge file contents)
            input_summary = {}
            for k, v in inp.items():
                if isinstance(v, str) and len(v) > 200:
                    input_summary[k] = v[:200] + "..."
                else:
                    input_summary[k] = v
            return name, json.dumps(input_summary, ensure_ascii=False)
    return None, None


def extract_files(message: dict) -> Optional[str]:
    """Extract file paths from tool_use inputs."""
    content = message.get("content", [])
    if not isinstance(content, list):
        return None
    files = set()
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            inp = item.get("input", {})
            for key in ("file_path", "path"):
                if key in inp and isinstance(inp[key], str):
                    files.add(inp[key])
            # Grep/Glob path
            if "pattern" in inp and "path" in inp:
                files.add(inp["path"])
    return json.dumps(sorted(files), ensure_ascii=False) if files else None


def parse_incremental(transcript_path: str, session_id: str,
                      after_line: int = 0) -> List[ParsedTurn]:
    """
    Parse JSONL starting from after_line.
    Skips compact_boundary, isCompactSummary, and non-conversation entries.
    """
    turns = []
    with open(transcript_path) as f:
        for line_num, line in enumerate(f, 1):
            if line_num <= after_line:
                continue
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            # Skip compact markers
            if obj.get("subtype") == "compact_boundary":
                continue
            if obj.get("isCompactSummary"):
                continue

            entry_type = obj.get("type", "")
            if entry_type not in ("user", "assistant"):
                continue

            message = obj.get("message", {})
            if not isinstance(message, dict):
                continue

            text = extract_text(message)
            if not text.strip():
                continue

            uuid = obj.get("uuid", "")
            role = message.get("role", entry_type)
            tool_name, tool_input = extract_tool_info(message)
            files = extract_files(message)

            turns.append(ParsedTurn(
                line_number=line_num,
                uuid=uuid,
                role=role,
                content=text,
                tool_name=tool_name,
                tool_input=tool_input,
                files_touched=files,
            ))
    return turns


def find_transcript(session_id: str, cwd: str = "") -> Optional[str]:
    """Locate the session JSONL file."""
    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_dir):
        return None

    # Try direct path construction
    if cwd:
        project_hash = "-" + cwd.replace("/", "-")
        candidate = os.path.join(projects_dir, project_hash, f"{session_id}.jsonl")
        if os.path.isfile(candidate):
            return candidate

    # Fallback: search all project directories
    for dirpath, _, filenames in os.walk(projects_dir):
        for fname in filenames:
            if fname == f"{session_id}.jsonl":
                return os.path.join(dirpath, fname)
    return None
