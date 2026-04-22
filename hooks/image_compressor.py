#!/usr/bin/env python3
"""
redmem image compressor — transparently downscale large images before they
reach Claude's vision API, saving tokens on every session (not just autopilot).

Mechanism
─────────
Claude Code's PreToolUse hook lets us rewrite tool_input via
`hookSpecificOutput.updatedInput`. When Claude tries to Read a large image
file, this module:

  1. Checks if the file is an image (.png/.jpg/.jpeg/.webp/.heic)
  2. Checks file size and dimensions (fast path: small → pass through)
  3. Invokes `sips -Z <max_dim>` (macOS built-in — no Python deps) to
     produce a downscaled copy in `/tmp/redmem-img-cache/`, keyed by
     path-hash + mtime so later edits invalidate the cache automatically
  4. Returns `updatedInput` pointing at the cached copy

Claude sees the smaller image; the original file on disk is untouched.

Opt-out
───────
Three layers, in order of scope:

  - `REDMEM_NO_IMAGE_COMPRESS=1` env var — whole-host kill switch
  - `<cwd>/.redmem-no-compress` file    — per-project kill switch
  - `.orig.` or `.nocompress.` in the filename — per-image escape hatch

Failure modes (all fail-open → original path unchanged)
──────────────────────────────────────────────────────
  - `sips` not on PATH (non-macOS) → log once, pass through
  - File can't be read / is a symlink loop → pass through
  - Cache dir can't be created → pass through
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"})

# Thresholds: only touch images that are BOTH bigger than `SIZE_THRESHOLD`
# AND longer than `DIM_THRESHOLD_PX` on their longest side.
SIZE_THRESHOLD_BYTES = 500 * 1024       # 500 KB
DIM_THRESHOLD_PX = 1920                 # longest side cap after compression

CACHE_DIR = os.environ.get(
    "REDMEM_IMG_CACHE_DIR", "/tmp/redmem-img-cache"
)
OPT_OUT_ENV = "REDMEM_NO_IMAGE_COMPRESS"
OPT_OUT_FILE = ".redmem-no-compress"
OPT_OUT_FILENAME_MARKERS = (".orig.", ".nocompress.")

# `redmem-original <path>` — a fake bash command CC can issue to request
# the next Read of <path> return the uncompressed original. The hook
# intercepts it at PreToolUse, sets a flag, and denies (so nothing
# actually runs in the shell — it's purely a signalling mechanism).
ORIGINAL_REQUEST_CMD_RE = re.compile(
    r"""^\s*redmem-original\s+['"]?([^'"]+?)['"]?\s*$"""
)

LOG_PREFIX = "[redmem-imgc]"


def _flag_dir() -> str:
    return os.path.join(CACHE_DIR, "original-requests")


def _flag_path(session_id: str, file_path: str) -> str:
    """Session-scoped flag file path. Using session_id in the key means
    one session's request doesn't leak to another concurrent session
    reading the same file."""
    norm = os.path.abspath(file_path)
    key = hashlib.sha1(f"{session_id}:{norm}".encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(_flag_dir(), f"{key}.req")


def request_original(session_id: str, file_path: str) -> bool:
    """Flag the next Read of file_path (for this session) to bypass compression."""
    if not session_id or not file_path:
        return False
    try:
        os.makedirs(_flag_dir(), exist_ok=True)
        with open(_flag_path(session_id, file_path), "w", encoding="utf-8") as f:
            f.write(os.path.abspath(file_path))
        return True
    except OSError as e:
        _log(f"request_original failed: {e.__class__.__name__}")
        return False


def _consume_original_request(session_id: str, file_path: str) -> bool:
    """One-shot: if flag exists, delete it and return True."""
    if not session_id or not file_path:
        return False
    p = _flag_path(session_id, file_path)
    if not os.path.isfile(p):
        return False
    try:
        os.unlink(p)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def _meta_path(compressed_path: str) -> str:
    return compressed_path + ".meta.json"


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"{LOG_PREFIX} {msg}\n")
    except Exception:
        pass


def is_image_path(file_path: str) -> bool:
    if not file_path:
        return False
    ext = os.path.splitext(file_path.lower())[1]
    return ext in IMAGE_EXTS


def opt_out_active(file_path: str, cwd: str) -> bool:
    """Any of the three opt-out layers active?"""
    if os.environ.get(OPT_OUT_ENV):
        return True
    if file_path:
        base = os.path.basename(file_path).lower()
        for marker in OPT_OUT_FILENAME_MARKERS:
            if marker in base:
                return True
    if cwd:
        try:
            if os.path.isfile(os.path.join(cwd, OPT_OUT_FILE)):
                return True
        except OSError:
            pass
    return False


def get_image_dims(file_path: str) -> tuple[int, int]:
    """Return (width, height) via sips. (0, 0) on any failure."""
    try:
        r = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", file_path],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return 0, 0
        w = h = 0
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("pixelWidth:"):
                try:
                    w = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("pixelHeight:"):
                try:
                    h = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return w, h
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0, 0


def cache_path_for(file_path: str) -> str:
    """Deterministic cache path that invalidates when source mtime changes.

    Using mtime in the filename means we never serve stale compressed
    images after the user edits / replaces the original.
    """
    try:
        mt = int(os.path.getmtime(file_path))
    except OSError:
        mt = 0
    key = hashlib.sha1(os.path.abspath(file_path).encode("utf-8", "replace"))
    h = key.hexdigest()[:12]
    ext = os.path.splitext(file_path)[1].lower() or ".png"
    return os.path.join(CACHE_DIR, f"{h}-{mt}{ext}")


def compress_to_cache(file_path: str, max_dim: int = DIM_THRESHOLD_PX) -> str | None:
    """Downscale longest side to `max_dim`. Returns the cache path on
    success, None on any failure. Writes a sidecar `<cache>.meta.json`
    pointing back at the original — so PostToolUse can show CC the
    path to request if detail is needed. Idempotent."""
    out = cache_path_for(file_path)
    if os.path.isfile(out):
        return out
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        r = subprocess.run(
            ["sips", "-Z", str(max_dim), file_path, "-o", out],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and os.path.isfile(out):
            try:
                w, h = get_image_dims(file_path)
                nw, nh = get_image_dims(out)
                with open(_meta_path(out), "w", encoding="utf-8") as f:
                    json.dump({
                        "original_path": os.path.abspath(file_path),
                        "original_dims": [w, h],
                        "compressed_dims": [nw, nh],
                    }, f)
            except OSError as e:
                _log(f"sidecar write failed: {e.__class__.__name__}")
            return out
        _log(f"sips rc={r.returncode}: {r.stderr.strip()[:120]}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(f"compress failed: {e.__class__.__name__}: {e}")
    return None


def maybe_compress_read(data: dict) -> dict | None:
    """
    PreToolUse helper. Returns a hook response dict that rewrites
    `tool_input.file_path` to a compressed version, or None to pass
    through unchanged. Fail-open on any error.
    """
    try:
        if data.get("tool_name") != "Read":
            return None
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        file_path = tool_input.get("file_path", "")
        if not file_path or not is_image_path(file_path):
            return None
        if not os.path.isfile(file_path):
            return None  # leave Read's own error handling in place

        # If CC asked for the original via `redmem-original`, honour it
        # ONCE and pass through without compression.
        session_id = data.get("session_id", "") or ""
        if _consume_original_request(session_id, file_path):
            _log(f"serving original (requested): {file_path}")
            return None

        cwd = data.get("cwd", "") or ""
        if opt_out_active(file_path, cwd):
            return None

        try:
            size = os.path.getsize(file_path)
        except OSError:
            return None
        if size < SIZE_THRESHOLD_BYTES:
            return None

        w, h = get_image_dims(file_path)
        if max(w, h) < DIM_THRESHOLD_PX:
            return None

        compressed = compress_to_cache(file_path)
        if not compressed:
            return None

        _log(
            f"compressed {file_path} ({w}x{h}, {size // 1024}KB) "
            f"-> {compressed}"
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {**tool_input, "file_path": compressed},
            }
        }
    except Exception as e:
        _log(f"unexpected error: {e.__class__.__name__}: {e}")
        return None


def maybe_handle_bash_original_request(data: dict) -> dict | None:
    """
    PreToolUse(Bash) helper. If CC's command is `redmem-original <path>`
    (our sentinel, not a real program), flag the path for this session
    and deny the command — so nothing actually runs in the shell. The
    next `Read <path>` will serve the uncompressed original.

    Returns a deny response dict, or None to pass through.
    """
    try:
        if data.get("tool_name") != "Bash":
            return None
        tool_input = data.get("tool_input") or {}
        command = (tool_input.get("command") or "").strip()
        if not command:
            return None
        m = ORIGINAL_REQUEST_CMD_RE.match(command)
        if not m:
            return None
        session_id = data.get("session_id", "") or ""
        if not session_id:
            return None
        path = m.group(1).strip()
        # Best effort — if the file doesn't exist, still flag; user may
        # intend a path that becomes available in a later turn.
        request_original(session_id, path)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[redmem-imgc] Flag set. The next `Read {path}` in this "
                    f"session will receive the uncompressed original. "
                    f"(redmem-original is a signalling sentinel — no shell "
                    f"command actually runs.)"
                ),
            }
        }
    except Exception as e:
        _log(f"bash-intercept error: {e.__class__.__name__}: {e}")
        return None


def maybe_notify_post_read(data: dict) -> dict | None:
    """
    PostToolUse(Read) helper. If the Read we just served came out of our
    cache, emit an `additionalContext` note telling CC it saw a
    compressed image and how to request the original if needed.
    Returns None if no notice is warranted.
    """
    try:
        if data.get("tool_name") != "Read":
            return None
        tool_input = data.get("tool_input") or {}
        file_path = tool_input.get("file_path", "")
        if not file_path or not os.path.abspath(file_path).startswith(
            os.path.abspath(CACHE_DIR)
        ):
            return None
        meta = _meta_path(file_path)
        if not os.path.isfile(meta):
            return None
        try:
            with open(meta, "r", encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        orig = info.get("original_path", "?")
        ow, oh = info.get("original_dims", [0, 0])
        nw, nh = info.get("compressed_dims", [0, 0])
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"[redmem-imgc] The image you just read was auto-downscaled "
                    f"from {ow}×{oh} to {nw}×{nh} to save vision tokens. "
                    f"If you cannot make out small text or fine UI details, "
                    f"run this Bash command to request the original:\n"
                    f"  redmem-original {orig}\n"
                    f"It's a signalling sentinel (the shell command will be "
                    f"denied — that's expected); the next `Read {orig}` will "
                    f"then serve the uncompressed image."
                ),
            }
        }
    except Exception as e:
        _log(f"post-notify error: {e.__class__.__name__}: {e}")
        return None


# Standalone diagnostics. Dispatches on hook_event_name + tool_name so
# the same script can answer all three event types we care about.
if __name__ == "__main__":
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.exit(0)
    event = data.get("hook_event_name", "")
    tool = data.get("tool_name", "")
    resp = None
    if event == "PreToolUse" and tool == "Read":
        resp = maybe_compress_read(data)
    elif event == "PreToolUse" and tool == "Bash":
        resp = maybe_handle_bash_original_request(data)
    elif event == "PostToolUse" and tool == "Read":
        resp = maybe_notify_post_read(data)
    if resp:
        sys.stdout.write(json.dumps(resp))
    sys.exit(0)
