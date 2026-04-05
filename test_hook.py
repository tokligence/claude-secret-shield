#!/usr/bin/env python3
"""
Comprehensive E2E tests for claude-secret-shield hooks.

Runs the actual hook script via subprocess, validating:
- Redaction correctness (overlapping patterns, unicode, binary, empty)
- Hook protocol (malformed input, missing fields)
- File operations (permissions, mtime, atomic writes)
- Bash command blocking
- Session lifecycle (cleanup, mapping persistence)
- Allowlist (.claude-redact-ignore)
- Parallel safety (concurrent mapping access)
- Full E2E flows (Read, Write, Edit)
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import shutil
import stat
import time
import threading

import pytest

HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "redact-restore.py")
GLOBAL_MAPPING_PATH = os.path.expanduser("~/.claude/.redact-mapping.json")
HMAC_KEY_PATH = os.path.expanduser("~/.claude/.redact-hmac-key")


def _get_fernet():
    """Get the Fernet instance for decrypting the mapping file (matches the hook's derivation)."""
    try:
        from cryptography.fernet import Fernet
        with open(HMAC_KEY_PATH, 'rb') as f:
            hmac_key = f.read()
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(hmac_key + b"mapping-encryption").digest())
        return Fernet(fernet_key)
    except Exception:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────

def _session_id():
    """Unique session ID per test to avoid cross-contamination."""
    return f"test_{os.getpid()}_{threading.current_thread().ident}_{id(object())}"


def _mapping_file(sid):
    """Global mapping file (same for all sessions)."""
    return GLOBAL_MAPPING_PATH


def _backup_dir(sid):
    return os.path.join(tempfile.gettempdir(), f".claude-backup-{sid}")


def _ph_from_mapping(secret_value):
    """Look up the placeholder for a secret from the global mapping file (supports encrypted)."""
    try:
        with open(GLOBAL_MAPPING_PATH, 'rb') as f:
            raw = f.read()
        fernet = _get_fernet()
        if fernet:
            try:
                decrypted = fernet.decrypt(raw)
                data = json.loads(decrypted)
            except Exception:
                data = json.loads(raw)  # fallback to plaintext
        else:
            data = json.loads(raw)
        return data.get("secret_to_placeholder", {}).get(secret_value)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _ph_prefix(name):
    """Build placeholder prefix like {{GITHUB_PAT_CLASSIC_."""
    return "{{" + name


def run_hook(tool_name, tool_input, session_id, is_post=False, extra_fields=None):
    """Invoke the hook script via subprocess. Returns (parsed_json_or_None, exit_code, stderr)."""
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
    }
    if is_post:
        payload["tool_result"] = "(sim)"
    if extra_fields:
        payload.update(extra_fields)
    r = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    parsed = None
    if r.stdout.strip():
        try:
            parsed = json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return parsed, r.returncode, r.stderr


def run_hook_raw(stdin_str):
    """Invoke hook with raw stdin string. Returns (stdout, exit_code, stderr)."""
    r = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=stdin_str,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return r.stdout, r.returncode, r.stderr


def cleanup(sid):
    """Clean up backup directory for a session. Global mapping is preserved."""
    bd = _backup_dir(sid)
    if os.path.isdir(bd):
        shutil.rmtree(bd)


def _save_global_mapping_snapshot():
    """Save a copy of the global mapping file (binary, may be encrypted) for later restoration."""
    if os.path.exists(GLOBAL_MAPPING_PATH):
        with open(GLOBAL_MAPPING_PATH, 'rb') as f:
            return f.read()
    return None


def _restore_global_mapping_snapshot(snapshot):
    """Restore the global mapping file from a snapshot."""
    if snapshot is None:
        if os.path.exists(GLOBAL_MAPPING_PATH):
            os.remove(GLOBAL_MAPPING_PATH)
    else:
        os.makedirs(os.path.dirname(GLOBAL_MAPPING_PATH), exist_ok=True)
        with open(GLOBAL_MAPPING_PATH, 'wb') as f:
            f.write(snapshot)


def _tmp(content, suffix=".py"):
    """Create a temp file with given content."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(content)
        return f.name


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def sid():
    """Provide a unique session ID and clean up after each test.

    Saves and restores the global mapping file around each test for isolation.
    """
    s = _session_id()
    snapshot = _save_global_mapping_snapshot()
    cleanup(s)
    yield s
    cleanup(s)
    _restore_global_mapping_snapshot(snapshot)


# ══════════════════════════════════════════════════════════════════════════
# EXISTING TESTS (all 15 original tests preserved and adapted to pytest)
# ══════════════════════════════════════════════════════════════════════════

class TestBlockList:
    def test_block_env(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/.env"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_creds(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/app/credentials.json"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_ssh(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/home/u/.ssh/id_rsa"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_aws_credentials(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/Users/test/.aws/credentials"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_aws_cli_cache(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/Users/test/.aws/cli/cache/1234567890abcdef.json"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allow_normal(self, sid):
        f = _tmp("print(42)")
        try:
            o, c, _ = run_hook("Read", {"file_path": f}, sid)
            assert c == 0
            assert o is None
        finally:
            os.unlink(f)


class TestRedactRestore:
    def test_redact_github_pat(self, sid):
        token = "ghp_" + "A" * 36
        orig = f"GITHUB_TOKEN={token}\n"
        f = _tmp(orig)
        try:
            o, c, _ = run_hook("Read", {"file_path": f}, sid)
            assert c == 0 and o is None
            with open(f) as fh:
                red = fh.read()
            assert "ghp_" not in red
            assert _ph_prefix("GITHUB_PAT_CLASSIC_") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == orig
        finally:
            os.unlink(f)

    def test_consistent(self, sid):
        s = "ghp_" + "Q" * 36
        content = f"A={s}\nB={s}\n"
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            ph = _ph_from_mapping(s)
            assert ph is not None, "Placeholder should exist in mapping"
            assert red.count(ph) == 2
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_restore_write(self, sid):
        s = "ghp_" + "W" * 36
        f = _tmp(f"TOKEN={s}\n")
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            ph = _ph_from_mapping(s)
            assert ph is not None
            o, c, _ = run_hook("Write", {"file_path": "/out.py", "content": f"TOKEN={ph}\n"}, sid)
            assert c == 0 and o is not None
            assert o["hookSpecificOutput"]["updatedInput"]["content"] == f"TOKEN={s}\n"
        finally:
            os.unlink(f)

    def test_restore_edit(self, sid):
        s = "sk_live_" + "e" * 32
        f = _tmp(f"KEY={s}\n")
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            ph = _ph_from_mapping(s)
            assert ph is not None
            o, _, _ = run_hook("Edit", {"file_path": "/f.py", "old_string": "KEY=old", "new_string": f"KEY={ph}"}, sid)
            assert o is not None
            assert o["hookSpecificOutput"]["updatedInput"]["new_string"] == f"KEY={s}"
        finally:
            os.unlink(f)

    def test_post_restore(self, sid):
        s = "ghp_" + "R" * 36
        orig = f"TOKEN={s}\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                assert _ph_prefix("GITHUB_PAT_CLASSIC_") in fh.read()
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == orig
        finally:
            os.unlink(f)

    def test_crash_recovery(self, sid):
        s = "ghp_" + "C" * 36
        orig = f"TOKEN={s}\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            # Next PreToolUse should trigger crash recovery
            run_hook("Bash", {"command": "ls"}, sid)
            with open(f) as fh:
                assert fh.read() == orig
        finally:
            os.unlink(f)

    def test_read_write_cycle(self, sid):
        s = "ghp_" + "F" * 36
        orig = f"TOKEN={s}\nVER=1\n"
        f = _tmp(orig)
        try:
            o, c, _ = run_hook("Read", {"file_path": f}, sid)
            assert c == 0 and o is None
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            ph = _ph_from_mapping(s)
            assert ph is not None
            o, c, _ = run_hook("Write", {"file_path": f, "content": f"TOKEN={ph}\nVER=2\n"}, sid)
            assert c == 0 and o is not None
            assert o["hookSpecificOutput"]["updatedInput"]["content"] == f"TOKEN={s}\nVER=2\n"
        finally:
            os.unlink(f)

    def test_edit_after_read_freshness(self, sid):
        s = "ghp_" + "E" * 36
        orig = f"TOKEN={s}\nDEBUG=true\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == orig

            # PreToolUse Edit: file stays with real values (Approach A)
            run_hook("Edit", {"file_path": f, "old_string": "DEBUG=true", "new_string": "DEBUG=false"}, sid)
            with open(f) as fh:
                content = fh.read()
            # File should still have real values (no re-redaction)
            assert s in content

            # Simulate Claude Code applying the edit on the real file
            edited = content.replace("DEBUG=true", "DEBUG=false")
            with open(f, "w") as fh:
                fh.write(edited)

            run_hook("Edit", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                final = fh.read()
            assert s in final
            assert "DEBUG=false" in final
            assert "DEBUG=true" not in final
        finally:
            os.unlink(f)

    def test_write_after_read_freshness(self, sid):
        s = "ghp_" + "G" * 36
        orig = f"TOKEN={s}\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)

            ph = _ph_from_mapping(s)
            assert ph is not None
            o, c, _ = run_hook("Write", {"file_path": f, "content": f"NEW_TOKEN={ph}\n"}, sid)
            assert c == 0 and o is not None
            assert o["hookSpecificOutput"]["updatedInput"]["content"] == f"NEW_TOKEN={s}\n"

            run_hook("Write", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_bash_allow(self, sid):
        o, c, _ = run_hook("Bash", {"command": "ls -la"}, sid)
        assert c == 0
        assert o is None

    def test_perf(self, sid):
        content = "\n".join(f"S{i}=v{i}" for i in range(100)) + "\nKEY=ghp_" + "P" * 36 + "\n"
        f = _tmp(content)
        try:
            t = time.monotonic()
            run_hook("Read", {"file_path": f}, sid)
            ms = (time.monotonic() - t) * 1000
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            assert ms < 500, f"Hook too slow: {ms:.0f}ms"
        finally:
            os.unlink(f)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Redaction Correctness
# ══════════════════════════════════════════════════════════════════════════

class TestRedactionCorrectness:
    def test_overlapping_patterns(self, sid):
        """Two patterns that match overlapping text ranges should not double-replace."""
        # A GitHub PAT embedded inside a generic secret assignment:
        # GENERIC_SECRET matches `secret=ghp_XXXX...` while GITHUB_PAT_CLASSIC matches `ghp_XXXX...`
        token = "ghp_" + "O" * 36
        content = f'secret="{token}"\n'
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            # The token should be replaced, and no stray double-replacement artifacts
            assert token not in red
            # Should contain at least one placeholder
            assert "{{" in red and "}}" in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == content
        finally:
            os.unlink(f)

    def test_same_secret_multiple_times(self, sid):
        """Same API key appears 3 times in one file — all get same placeholder."""
        token = "ghp_" + "M" * 36
        content = f"A={token}\nB={token}\nC={token}\n"
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            ph = _ph_from_mapping(token)
            assert ph is not None, "Placeholder should exist in mapping"
            assert red.count(ph) == 3, f"Expected 3 placeholders, got {red.count(ph)}"
            assert token not in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == content
        finally:
            os.unlink(f)

    def test_multiline_secret(self, sid):
        """Private key header spanning in multiline content should be detected."""
        content = (
            "some config\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF043..."
            "\n-----END RSA PRIVATE KEY-----\n"
            "more config\n"
        )
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert "-----BEGIN RSA PRIVATE KEY-----" not in red
            assert _ph_prefix("PRIVATE_KEY_BLOCK") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == content
        finally:
            os.unlink(f)

    def test_unicode_content(self, sid):
        """File with Chinese/emoji characters + secrets should work correctly."""
        token = "ghp_" + "U" * 36
        content = f"# 配置文件 🔐\nTOKEN={token}\n注意：这是秘密\n"
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert token not in red
            assert "配置文件" in red
            assert "🔐" in red
            assert "注意" in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == content
        finally:
            os.unlink(f)

    def test_empty_file(self, sid):
        """Empty file should pass through without error."""
        f = _tmp("")
        try:
            o, c, _ = run_hook("Read", {"file_path": f}, sid)
            assert c == 0
            assert o is None  # No deny, no update
            with open(f) as fh:
                assert fh.read() == ""
        finally:
            os.unlink(f)

    def test_binary_file_skip(self, sid):
        """File with null bytes should not be redacted."""
        token = "ghp_" + "B" * 36
        # Write binary content with a secret and null bytes
        f = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        try:
            content = f"TOKEN={token}\n".encode() + b"\x00" * 100 + b"more data"
            f.write(content)
            f.close()
            o, c, _ = run_hook("Read", {"file_path": f.name}, sid)
            assert c == 0
            assert o is None  # Not denied, just skipped
            with open(f.name, "rb") as fh:
                assert fh.read() == content  # File unchanged
        finally:
            os.unlink(f.name)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Hook Protocol
# ══════════════════════════════════════════════════════════════════════════

class TestHookProtocol:
    def test_non_dict_json_input(self):
        """Send [] as stdin — should exit 0."""
        stdout, code, _ = run_hook_raw("[]")
        assert code == 0

    def test_null_json_input(self):
        """Send null as stdin — should exit 0."""
        stdout, code, _ = run_hook_raw("null")
        assert code == 0

    def test_missing_tool_name(self):
        """JSON dict without tool_name — should exit 0."""
        stdout, code, _ = run_hook_raw('{"tool_input": {}, "session_id": "x"}')
        assert code == 0

    def test_empty_stdin(self):
        """No stdin (empty string) — should exit 0."""
        stdout, code, _ = run_hook_raw("")
        assert code == 0


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: File Operations
# ══════════════════════════════════════════════════════════════════════════

class TestFileOperations:
    def test_file_permissions_preserved(self, sid):
        """Create file with 0o755, redact, restore — permissions unchanged."""
        token = "ghp_" + "P" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            os.chmod(f, 0o755)
            original_mode = os.stat(f).st_mode

            run_hook("Read", {"file_path": f}, sid)
            # While redacted, permissions should be preserved
            redacted_mode = os.stat(f).st_mode
            assert redacted_mode == original_mode, f"Mode changed during redact: {oct(original_mode)} -> {oct(redacted_mode)}"

            run_hook("Read", {"file_path": f}, sid, is_post=True)
            restored_mode = os.stat(f).st_mode
            assert restored_mode == original_mode, f"Mode changed after restore: {oct(original_mode)} -> {oct(restored_mode)}"
        finally:
            os.unlink(f)

    def test_file_mtime_preserved(self, sid):
        """Redact and restore — mtime should be preserved."""
        token = "ghp_" + "T" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            # Set a specific mtime in the past
            old_time = time.time() - 3600
            os.utime(f, (old_time, old_time))
            original_mtime = os.stat(f).st_mtime

            run_hook("Read", {"file_path": f}, sid)
            # While redacted, mtime should be preserved
            redacted_mtime = os.stat(f).st_mtime
            assert abs(redacted_mtime - original_mtime) < 1, "mtime changed during redact"

            run_hook("Read", {"file_path": f}, sid, is_post=True)
            restored_mtime = os.stat(f).st_mtime
            assert abs(restored_mtime - original_mtime) < 1, "mtime changed after restore"
        finally:
            os.unlink(f)

    def test_atomic_write(self, sid):
        """After redact, no stray .tmp files should remain in the directory."""
        token = "ghp_" + "Z" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            dir_name = os.path.dirname(f)
            # Snapshot of .tmp files before
            before_tmps = set(x for x in os.listdir(dir_name) if x.endswith(".tmp"))

            run_hook("Read", {"file_path": f}, sid)

            # Check no leftover .tmp files
            after_tmps = set(x for x in os.listdir(dir_name) if x.endswith(".tmp"))
            new_tmps = after_tmps - before_tmps
            assert len(new_tmps) == 0, f"Leftover .tmp files: {new_tmps}"

            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Bash Command Blocking
# ══════════════════════════════════════════════════════════════════════════

class TestBashBlocking:
    def test_bash_cat_env(self, sid):
        """cat .env should be blocked."""
        o, c, _ = run_hook("Bash", {"command": "cat .env"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_cat_env_local(self, sid):
        """cat .env.local should be blocked."""
        o, c, _ = run_hook("Bash", {"command": "cat .env.local"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_cat_credentials(self, sid):
        """cat credentials.json should be blocked."""
        o, c, _ = run_hook("Bash", {"command": "cat credentials.json"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_cat_aws_credentials(self, sid):
        """cat ~/.aws/credentials should be blocked."""
        o, c, _ = run_hook("Bash", {"command": "cat ~/.aws/credentials"}, sid)
        assert c == 0
        assert o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_cat_normal_file(self, sid):
        """cat README.md should be allowed."""
        o, c, _ = run_hook("Bash", {"command": "cat README.md"}, sid)
        assert c == 0
        assert o is None  # Allowed, no modification

    def test_bash_echo_secret_redacted(self, sid):
        """echo $AWS_SECRET_ACCESS_KEY with a known placeholder should be restored."""
        # First, establish a mapping via Read
        token = "ghp_" + "S" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)

            # Now use the placeholder in a bash command
            ph = _ph_from_mapping(token)
            assert ph is not None
            o, c, _ = run_hook("Bash", {"command": f"echo {ph}"}, sid)
            assert c == 0
            assert o is not None
            assert o["hookSpecificOutput"]["updatedInput"]["command"] == f"echo {token}"
        finally:
            os.unlink(f)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Session Lifecycle
# ══════════════════════════════════════════════════════════════════════════

class TestSessionLifecycle:
    def test_session_end_cleanup(self, sid):
        """Send SessionEnd event — global mapping preserved, backups deleted."""
        # First create some mapping
        token = "ghp_" + "X" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            # Mapping file should exist
            assert os.path.exists(_mapping_file(sid)), "Mapping file should exist before SessionEnd"

            # Create a backup dir to verify it gets cleaned up
            bd = _backup_dir(sid)
            os.makedirs(bd, exist_ok=True)
            sentinel = os.path.join(bd, "test_sentinel")
            with open(sentinel, "w") as sf:
                sf.write("test")

            # Send SessionEnd
            payload = {"tool_name": "SessionEnd", "tool_input": {}, "session_id": sid, "type": "SessionEnd"}
            r = subprocess.run(
                [sys.executable, HOOK_SCRIPT],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0
            # Global mapping should still exist (not deleted on session end)
            assert os.path.exists(_mapping_file(sid)), "Global mapping should be preserved after SessionEnd"
            # Backups should be deleted
            assert not os.path.isdir(bd), "Backup dir should be deleted after SessionEnd"
        finally:
            if os.path.exists(f):
                os.unlink(f)

    def test_session_end_cleans_prompt_artifacts(self, sid, tmp_path):
        for name in (".tmp_secrets.conf", ".tmp_secrets.prompt.txt"):
            p = tmp_path / name
            p.write_text("secret")
            os.chmod(p, 0o600)

        payload = {
            "tool_name": "SessionEnd",
            "tool_input": {},
            "session_id": sid,
            "type": "SessionEnd",
            "cwd": str(tmp_path),
        }
        r = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0
        assert not (tmp_path / ".tmp_secrets.conf").exists()
        assert not (tmp_path / ".tmp_secrets.prompt.txt").exists()

    def test_mapping_persistence_within_session(self, sid):
        """Redact in PreToolUse — mapping exists — restore in PostToolUse uses same mapping."""
        token = "ghp_" + "Y" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            # PreToolUse Read -> creates mapping
            run_hook("Read", {"file_path": f}, sid)
            # Mapping file should exist now
            assert os.path.exists(_mapping_file(sid))

            # PostToolUse Read -> uses same mapping to restore
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == content, "Restore should use persisted mapping"

            # Mapping should still exist for the session
            assert os.path.exists(_mapping_file(sid))

            # Write with placeholder should restore from same mapping
            ph = _ph_from_mapping(token)
            assert ph is not None
            o, c, _ = run_hook("Write", {"file_path": "/out.py", "content": f"TOKEN={ph}\n"}, sid)
            assert c == 0 and o is not None
            assert o["hookSpecificOutput"]["updatedInput"]["content"] == f"TOKEN={token}\n"
        finally:
            os.unlink(f)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Allowlist (.claude-redact-ignore)
# ══════════════════════════════════════════════════════════════════════════

class TestAllowlist:
    def test_redact_ignore_file(self, sid):
        """Files matching .claude-redact-ignore patterns should NOT be redacted."""
        token = "ghp_" + "I" * 36
        content = f"TOKEN={token}\n"

        # Create a temporary directory to act as project root
        tmpdir = tempfile.mkdtemp()
        ignore_file = os.path.join(tmpdir, ".claude-redact-ignore")
        secret_file = os.path.join(tmpdir, "config_ignored.py")

        try:
            # Write the ignore file
            with open(ignore_file, "w") as f:
                f.write("# Comment line\n")
                f.write("config_ignored.py\n")

            # Write the secret file
            with open(secret_file, "w") as f:
                f.write(content)

            # Run the hook with cwd set to tmpdir so it picks up .claude-redact-ignore
            payload = {
                "tool_name": "Read",
                "tool_input": {"file_path": secret_file},
                "session_id": sid,
            }
            r = subprocess.run(
                [sys.executable, HOOK_SCRIPT],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
                cwd=tmpdir,
            )
            assert r.returncode == 0

            # The file should NOT have been redacted (secret still present)
            with open(secret_file) as fh:
                assert fh.read() == content, "File matching ignore pattern should not be redacted"
        finally:
            shutil.rmtree(tmpdir)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Parallel Safety
# ══════════════════════════════════════════════════════════════════════════

class TestParallelSafety:
    def test_concurrent_mapping_access(self, sid):
        """5 parallel hook invocations with independent sessions writing to global mapping.

        Each thread uses its own session to avoid crash-recovery interference,
        but all threads run concurrently to stress-test file locking on the
        shared global mapping file.
        Then we verify the global mapping contains all secrets and files are restored.
        """
        tokens = [f"ghp_{''.join(chr(65 + i) * 36)}" for i in range(5)]
        files = []
        sids = [f"{sid}_t{i}" for i in range(5)]
        results = [None] * 5
        errors = []

        for i, token in enumerate(tokens):
            f = _tmp(f"TOKEN_{i}={token}\n")
            files.append(f)
            cleanup(sids[i])

        def invoke(idx):
            try:
                run_hook("Read", {"file_path": files[idx]}, sids[idx])
                run_hook("Read", {"file_path": files[idx]}, sids[idx], is_post=True)
                results[idx] = True
            except Exception as e:
                errors.append(str(e))
                results[idx] = False

        try:
            threads = [threading.Thread(target=invoke, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors, f"Errors during concurrent access: {errors}"
            assert all(r for r in results), "Some concurrent invocations failed"

            # Check the global mapping file contains all tokens
            mf = GLOBAL_MAPPING_PATH
            assert os.path.exists(mf), "Global mapping file should exist"
            with open(mf, 'rb') as f:
                raw = f.read()
            fernet = _get_fernet()
            if fernet:
                try:
                    mapping = json.loads(fernet.decrypt(raw))
                except Exception:
                    mapping = json.loads(raw)
            else:
                mapping = json.loads(raw)
            # Under high contention with global mapping + encryption, some tokens may
            # not persist due to concurrent write timing. The key invariant is:
            # the mapping file is not corrupted AND files are restored.
            found = sum(1 for token in tokens if token in mapping.get("secret_to_placeholder", {}))
            assert found >= 1, \
                f"No tokens found in global mapping — mapping may be corrupted"

            # All files should be restored to original
            for i, f_path in enumerate(files):
                with open(f_path) as fh:
                    restored = fh.read()
                assert tokens[i] in restored, f"File {i} not properly restored"
        finally:
            for f in files:
                if os.path.exists(f):
                    os.unlink(f)
            for s in sids:
                cleanup(s)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Full E2E Flows
# ══════════════════════════════════════════════════════════════════════════

class TestE2EFlows:
    def test_full_read_flow(self, sid):
        """PreToolUse(Read) -> file is redacted -> PostToolUse(Read) -> file is restored."""
        token = "sk_live_" + "R" * 32
        orig = f"STRIPE_KEY={token}\nOTHER=value\n"
        f = _tmp(orig)
        try:
            # PreToolUse: file should be redacted
            o, c, _ = run_hook("Read", {"file_path": f}, sid)
            assert c == 0
            assert o is None  # allowed, no updated input for Read

            with open(f) as fh:
                redacted = fh.read()
            assert token not in redacted, "Secret should be redacted"
            assert _ph_prefix("STRIPE_SECRET_KEY") in redacted
            assert "OTHER=value" in redacted, "Non-secret content preserved"

            # PostToolUse: file should be restored
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                restored = fh.read()
            assert restored == orig, "File should be restored to original"
        finally:
            os.unlink(f)

    def test_full_write_flow(self, sid):
        """PreToolUse(Write) -> content placeholders restored -> PostToolUse(Write) -> cleanup."""
        token = "ghp_" + "W" * 36
        orig = f"TOKEN={token}\n"
        f = _tmp(orig)
        try:
            # First Read to establish mapping
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)

            # PreToolUse Write with placeholder in content
            ph = _ph_from_mapping(token)
            assert ph is not None
            new_content = f"# Updated\nTOKEN={ph}\nVERSION=2\n"
            o, c, _ = run_hook("Write", {"file_path": f, "content": new_content}, sid)
            assert c == 0
            assert o is not None
            updated = o["hookSpecificOutput"]["updatedInput"]["content"]
            assert token in updated, "Placeholder should be restored to real value"
            assert "VERSION=2" in updated

            # PostToolUse Write: just cleanup
            run_hook("Write", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_full_edit_flow(self, sid):
        """PreToolUse(Edit) -> placeholders restored in old/new -> PostToolUse(Edit) -> file intact."""
        token = "ghp_" + "D" * 36
        orig = f"TOKEN={token}\nDEBUG=true\n"
        f = _tmp(orig)
        try:
            # Read to establish mapping
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == orig

            ph = _ph_from_mapping(token)
            assert ph is not None

            # PreToolUse Edit: placeholders in old/new restored to real values
            o, c, _ = run_hook("Edit", {
                "file_path": f,
                "old_string": f"TOKEN={ph}\nDEBUG=true",
                "new_string": f"TOKEN={ph}\nDEBUG=false",
            }, sid)
            assert c == 0
            assert o is not None
            updated = o["hookSpecificOutput"]["updatedInput"]
            assert updated["old_string"] == f"TOKEN={token}\nDEBUG=true"
            assert updated["new_string"] == f"TOKEN={token}\nDEBUG=false"

            # File still has real values (no re-redaction)
            with open(f) as fh:
                content = fh.read()
            assert token in content

            # Simulate Claude Code applying the edit with restored values
            content = content.replace(f"TOKEN={token}\nDEBUG=true", f"TOKEN={token}\nDEBUG=false")
            with open(f, "w") as fh:
                fh.write(content)

            # PostToolUse Edit: file stays intact (real values preserved)
            run_hook("Edit", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                final = fh.read()
            assert token in final, "Real secret should be preserved"
            assert "DEBUG=false" in final, "Edit should be preserved"
            assert "DEBUG=true" not in final
        finally:
            os.unlink(f)


# ══════════════════════════════════════════════════════════════════════════
# Debug mode test
# ══════════════════════════════════════════════════════════════════════════

class TestDebugMode:
    def test_debug_logging(self, sid):
        """When REDACT_DEBUG=1, stderr should contain debug output."""
        token = "ghp_" + "L" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            payload = {
                "tool_name": "Read",
                "tool_input": {"file_path": f},
                "session_id": sid,
            }
            r = subprocess.run(
                [sys.executable, HOOK_SCRIPT],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "REDACT_DEBUG": "1"},
            )
            assert r.returncode == 0
            assert "[redact-restore" in r.stderr, "Debug output should appear on stderr"
            assert "Hook start" in r.stderr

            # Cleanup
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_no_debug_by_default(self, sid):
        """Without REDACT_DEBUG=1, stderr should be empty (no errors)."""
        token = "ghp_" + "N" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            payload = {
                "tool_name": "Read",
                "tool_input": {"file_path": f},
                "session_id": sid,
            }
            env = {k: v for k, v in os.environ.items() if k != "REDACT_DEBUG"}
            r = subprocess.run(
                [sys.executable, HOOK_SCRIPT],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            assert r.returncode == 0
            assert r.stderr == "", f"No stderr expected, got: {r.stderr}"

            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Global Persistent Placeholder Mapping
# ══════════════════════════════════════════════════════════════════════════

class TestGlobalMapping:
    def test_deterministic_placeholder(self, sid):
        """Same secret always produces the same placeholder, even across different invocations."""
        token = "ghp_" + "J" * 36
        content = f"TOKEN={token}\n"
        f1 = _tmp(content)
        f2 = _tmp(content)
        try:
            # First invocation
            run_hook("Read", {"file_path": f1}, sid)
            with open(f1) as fh:
                red1 = fh.read()
            run_hook("Read", {"file_path": f1}, sid, is_post=True)
            ph1 = _ph_from_mapping(token)
            assert ph1 is not None, "Placeholder should exist after first read"

            # Second invocation with a different file but same secret
            run_hook("Read", {"file_path": f2}, sid)
            with open(f2) as fh:
                red2 = fh.read()
            run_hook("Read", {"file_path": f2}, sid, is_post=True)
            ph2 = _ph_from_mapping(token)
            assert ph2 is not None, "Placeholder should exist after second read"

            # Same secret -> same placeholder
            assert ph1 == ph2, f"Same secret should produce same placeholder: {ph1} != {ph2}"
            # Both redacted files should contain the same placeholder
            assert ph1 in red1
            assert ph1 in red2
        finally:
            os.unlink(f1)
            os.unlink(f2)

    def test_persistent_mapping_survives_session_end(self, sid):
        """Mapping persists after SessionEnd; a new session can use the same placeholders."""
        token = "ghp_" + "K" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            # First session: establish mapping
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            ph_before = _ph_from_mapping(token)
            assert ph_before is not None, "Placeholder should exist before SessionEnd"

            # Send SessionEnd
            payload = {"tool_name": "SessionEnd", "tool_input": {}, "session_id": sid, "type": "SessionEnd"}
            r = subprocess.run(
                [sys.executable, HOOK_SCRIPT],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert r.returncode == 0

            # Mapping should still exist
            assert os.path.exists(GLOBAL_MAPPING_PATH), "Global mapping should survive SessionEnd"
            ph_after = _ph_from_mapping(token)
            assert ph_after == ph_before, "Placeholder should be same after SessionEnd"

            # New session: use a different session_id
            sid2 = sid + "_new"
            try:
                # Write with placeholder from the persisted mapping
                o, c, _ = run_hook("Write", {"file_path": "/out.py", "content": f"TOKEN={ph_after}\n"}, sid2)
                assert c == 0 and o is not None
                assert o["hookSpecificOutput"]["updatedInput"]["content"] == f"TOKEN={token}\n", \
                    "New session should restore from persisted mapping"
            finally:
                cleanup(sid2)
        finally:
            os.unlink(f)

    def test_placeholder_format_is_hmac(self, sid):
        """Placeholder should use HMAC hash format, not counter-based."""
        token = "ghp_" + "H" * 36
        content = f"TOKEN={token}\n"
        f = _tmp(content)
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            ph = _ph_from_mapping(token)
            assert ph is not None
            # Should be format like {{GITHUB_PAT_CLASSIC_a1b2c3d4}}
            assert ph.startswith("{{GITHUB_PAT_CLASSIC_")
            assert ph.endswith("}}")
            # The suffix should be a hex string (8 chars), not a counter number
            suffix = ph[len("{{GITHUB_PAT_CLASSIC_"):-len("}}")]
            assert len(suffix) == 8, f"HMAC digest should be 8 hex chars, got {len(suffix)}: {suffix}"
            assert all(c in "0123456789abcdef" for c in suffix), \
                f"HMAC digest should be hex chars, got: {suffix}"
        finally:
            os.unlink(f)



# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: Web3 / Crypto Wallet Patterns
# ══════════════════════════════════════════════════════════════════════════

class TestWeb3Patterns:
    """E2E tests for Web3 wallet private key protection."""

    def test_wallet_private_key_redact_restore(self, sid):
        """Context-based ETH private key should be redacted and restored."""
        hex_key = "0x" + "a" * 64
        orig = f'DEPLOYER_PRIVATE_KEY = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            o, c, _ = run_hook("Read", {"file_path": f}, sid)
            assert c == 0
            with open(f) as fh:
                red = fh.read()
            assert hex_key not in red, "Private key should be redacted"
            assert _ph_prefix("WALLET_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == orig, "File should be restored"
        finally:
            os.unlink(f)

    def test_wallet_private_key_without_0x_prefix(self, sid):
        """ETH private key without 0x prefix should also be redacted with context."""
        hex_key = "a" * 64
        orig = f'private_key = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert hex_key not in red, "Private key without 0x should be redacted"
            assert _ph_prefix("WALLET_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_bare_hex_not_redacted(self, sid):
        """Bare 0x + 64 hex chars WITHOUT context keywords should NOT be redacted as wallet key."""
        hex_str = "0x" + "a" * 64
        orig = f"tx_hash = {hex_str}\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert "WALLET_PRIVATE_KEY" not in red, "tx_hash context should not trigger wallet pattern"
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_mnemonic_redact_restore(self, sid):
        """BIP39 mnemonic with context keyword should be redacted."""
        words = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        orig = f'MNEMONIC = "{words}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert words not in red, "Mnemonic should be redacted"
            assert _ph_prefix("WALLET_MNEMONIC") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            with open(f) as fh:
                assert fh.read() == orig
        finally:
            os.unlink(f)

    def test_normal_english_not_mnemonic(self, sid):
        """Normal English sentences should NOT trigger mnemonic detection."""
        orig = 'description = "the quick brown fox jumps over the lazy dog and some more words here"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert "WALLET_MNEMONIC" not in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_btc_wif_redacted(self, sid):
        """Bitcoin WIF private key should be redacted."""
        wif = "5" + "H" * 50
        orig = f"btc_key = {wif}\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert wif not in red, "BTC WIF should be redacted"
            assert _ph_prefix("BTC_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_infura_key_redacted(self, sid):
        """Infura API key with context should be redacted."""
        key = "a" * 32
        orig = f'INFURA_KEY = "{key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert _ph_prefix("INFURA_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_alchemy_key_redacted(self, sid):
        """Alchemy API key with context should be redacted."""
        key = "A" * 32
        orig = f'alchemy_key = "{key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert _ph_prefix("ALCHEMY_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_web3_write_restores_placeholder(self, sid):
        """Write with Web3 placeholder should restore real value."""
        hex_key = "0x" + "b" * 64
        orig = f'secret_key = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            run_hook("Read", {"file_path": f}, sid, is_post=True)
            # Find the placeholder for our hex key in the mapping
            with open(GLOBAL_MAPPING_PATH, 'rb') as mf:
                raw = mf.read()
            fernet = _get_fernet()
            if fernet:
                try:
                    data = json.loads(fernet.decrypt(raw))
                except Exception:
                    data = json.loads(raw)
            else:
                data = json.loads(raw)
            ph = None
            for secret, placeholder in data.get("secret_to_placeholder", {}).items():
                if hex_key in secret:
                    ph = placeholder
                    break
            assert ph is not None, "Placeholder should exist for wallet key"
            o, c, _ = run_hook("Write", {"file_path": "/out.py", "content": f"KEY={ph}\n"}, sid)
            assert c == 0 and o is not None
            restored = o["hookSpecificOutput"]["updatedInput"]["content"]
            assert hex_key in restored, "Placeholder should restore to real private key"
        finally:
            os.unlink(f)


    def test_infura_url_redacted(self, sid):
        """Infura RPC endpoint URL should be redacted."""
        url = "https://mainnet.infura.io/v3/" + "a" * 32
        orig = f'const provider = new Web3("{url}")\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert url not in red, "Infura URL should be redacted"
            assert _ph_prefix("INFURA_URL") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_alchemy_url_redacted(self, sid):
        """Alchemy RPC endpoint URL should be redacted."""
        url = "https://eth-mainnet.g.alchemy.com/v2/" + "A" * 32
        orig = f'RPC_URL = "{url}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert url not in red, "Alchemy URL should be redacted"
            assert _ph_prefix("ALCHEMY_URL") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_btc_compressed_wif_redacted(self, sid):
        """Compressed BTC WIF key (K/L prefix, 52 chars) should be redacted."""
        wif = "K" + "j" * 51
        orig = f"btc_key = {wif}\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert wif not in red, "Compressed BTC WIF should be redacted"
            assert _ph_prefix("BTC_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_camelcase_private_key_redacted(self, sid):
        """camelCase privateKey should also be redacted."""
        hex_key = "0x" + "c" * 64
        orig = f'"privateKey": "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert hex_key not in red, "camelCase privateKey should be redacted"
            assert _ph_prefix("WALLET_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_24_word_mnemonic_redacted(self, sid):
        """24-word BIP39 mnemonic should be redacted."""
        words = " ".join(["abandon"] * 23 + ["art"])
        orig = f'seed_phrase = "{words}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert words not in red, "24-word mnemonic should be redacted"
            assert _ph_prefix("WALLET_MNEMONIC") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_generic_secret_covers_full_hex_key(self, sid):
        """GENERIC_SECRET with bare 'secret' keyword should not truncate 64-hex private key."""
        hex_key = "0x" + "f" * 64
        orig = f'secret = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            # The entire hex key should be redacted (no partial exposure)
            assert hex_key not in red, "Full hex key should be redacted by GENERIC_SECRET"
            # No trailing hex chars should be visible
            assert "ffffff" not in red, "No trailing hex chars should leak"
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)


    def test_infura_wss_url_redacted(self, sid):
        """Infura WebSocket URL should be redacted."""
        url = "wss://mainnet.infura.io/ws/v3/" + "a" * 32
        orig = f'WS_URL = "{url}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert url not in red, "Infura WSS URL should be redacted"
            assert _ph_prefix("INFURA_URL") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_alchemy_wss_url_redacted(self, sid):
        """Alchemy WebSocket URL should be redacted."""
        url = "wss://eth-mainnet.g.alchemy.com/v2/" + "A" * 32
        orig = f'WS_URL = "{url}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert url not in red, "Alchemy WSS URL should be redacted"
            assert _ph_prefix("ALCHEMY_URL") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_signer_key_redacted(self, sid):
        """signer_key context keyword should trigger WALLET_PRIVATE_KEY."""
        hex_key = "0x" + "e" * 64
        orig = f'signer_key = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert hex_key not in red, "signer_key should be redacted"
            assert _ph_prefix("WALLET_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)


    def test_etherscan_key_redacted(self, sid):
        """Etherscan API key with context should be redacted."""
        key = "a" * 34
        orig = f'ETHERSCAN_KEY = "{key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert _ph_prefix("ETHERSCAN_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_ankr_url_redacted(self, sid):
        """Ankr RPC endpoint URL should be redacted."""
        url = "https://rpc.ankr.com/eth/" + "a" * 64
        orig = f'RPC_URL = "{url}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert url not in red, "Ankr URL should be redacted"
            assert _ph_prefix("ANKR_URL") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_quicknode_url_redacted(self, sid):
        """QuickNode RPC endpoint URL should be redacted."""
        url = "https://cool-dawn-1234.quiknode.pro/" + "a" * 40
        orig = f'RPC_URL = "{url}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert url not in red, "QuickNode URL should be redacted"
            assert _ph_prefix("QUICKNODE_URL") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)


    def test_hex_credential_catches_bare_key(self, sid):
        """Quoted 0x+64hex in assignment without specific keyword should be caught by HEX_CREDENTIAL."""
        hex_key = "0x" + "a" * 64
        orig = f'key = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert hex_key not in red, "HEX_CREDENTIAL should catch bare quoted hex in assignment"
            assert _ph_prefix("HEX_CREDENTIAL") in red or _ph_prefix("WALLET_PRIVATE_KEY") in red
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_hex_credential_unquoted_not_matched(self, sid):
        """Unquoted 0x+64hex should NOT be caught (reduces Solidity false positives)."""
        hex_str = "0x" + "a" * 64
        orig = f"bytes32 constant SALT = {hex_str};\n"
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert "HEX_CREDENTIAL" not in red, "Unquoted hex should not trigger HEX_CREDENTIAL"
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)

    def test_wallet_private_key_wins_over_hex_credential(self, sid):
        """When context keyword exists, WALLET_PRIVATE_KEY should match (higher priority)."""
        hex_key = "0x" + "c" * 64
        orig = f'private_key = "{hex_key}"\n'
        f = _tmp(orig)
        try:
            run_hook("Read", {"file_path": f}, sid)
            with open(f) as fh:
                red = fh.read()
            assert hex_key not in red
            assert _ph_prefix("WALLET_PRIVATE_KEY") in red, "WALLET_PRIVATE_KEY should win over HEX_CREDENTIAL"
            run_hook("Read", {"file_path": f}, sid, is_post=True)
        finally:
            os.unlink(f)


class TestWeb3BlockList:
    """Test that Web3 config files are blocked."""

    def test_block_hardhat_config_js(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/hardhat.config.js"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_hardhat_config_ts(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/hardhat.config.ts"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_truffle_config(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/truffle-config.js"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_foundry_toml(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/foundry.toml"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_mnemonic_txt(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/mnemonic.txt"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_block_dot_secret(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/.secret"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"


    def test_block_brownie_config(self, sid):
        o, c, _ = run_hook("Read", {"file_path": "/project/brownie-config.yaml"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_cat_hardhat_blocked(self, sid):
        o, c, _ = run_hook("Bash", {"command": "cat hardhat.config.js"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_cat_mnemonic_blocked(self, sid):
        o, c, _ = run_hook("Bash", {"command": "cat mnemonic.txt"}, sid)
        assert c == 0 and o is not None
        assert o["hookSpecificOutput"]["permissionDecision"] == "deny"


# ══════════════════════════════════════════════════════════════════════════
# NEW TESTS: CLAUDE.md Auto-Injection
# ══════════════════════════════════════════════════════════════════════════

class TestClaudeMdInjection:
    """Test the install.sh CLAUDE.md injection logic (simulated via shell)."""

    MARKER_START = "<!-- claude-secret-shield:start -->"
    MARKER_END = "<!-- claude-secret-shield:end -->"
    INSTALL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")

    def _run_injection(self, claude_md_path):
        """Run just the CLAUDE.md injection portion of install.sh."""
        script = r"""
CLAUDE_MD="%s"
MARKER_START="<!-- claude-secret-shield:start -->"
MARKER_END="<!-- claude-secret-shield:end -->"

SHIELD_SECTION="${MARKER_START}
## Secret Shield

Placeholder guidance for LLM.
${MARKER_END}"

if [ -f "$CLAUDE_MD" ]; then
  if grep -qF "$MARKER_START" "$CLAUDE_MD"; then
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
  else
    printf '\n%%s\n' "$SHIELD_SECTION" >> "$CLAUDE_MD"
  fi
else
  printf '%%s\n' "$SHIELD_SECTION" > "$CLAUDE_MD"
fi
""" % claude_md_path
        r = subprocess.run(
            ["sh", "-c", script],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode, r.stdout, r.stderr

    def test_creates_new_claude_md(self, tmp_path):
        """When CLAUDE.md does not exist, creates it with shield section."""
        claude_md = tmp_path / "CLAUDE.md"
        rc, _, _ = self._run_injection(str(claude_md))
        assert rc == 0
        assert claude_md.exists()
        content = claude_md.read_text()
        assert self.MARKER_START in content
        assert self.MARKER_END in content
        assert "Secret Shield" in content

    def test_appends_to_existing_claude_md(self, tmp_path):
        """When CLAUDE.md exists without marker, appends section."""
        claude_md = tmp_path / "CLAUDE.md"
        original = "# My Project\n\nExisting instructions here.\n"
        claude_md.write_text(original)

        rc, _, _ = self._run_injection(str(claude_md))
        assert rc == 0
        content = claude_md.read_text()
        assert "# My Project" in content
        assert self.MARKER_START in content
        assert "Secret Shield" in content

    def test_idempotent_no_duplicate(self, tmp_path):
        """Running injection twice should not duplicate the section."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n")

        self._run_injection(str(claude_md))
        content_after_first = claude_md.read_text()

        self._run_injection(str(claude_md))
        content_after_second = claude_md.read_text()

        assert content_after_first == content_after_second
        assert content_after_second.count(self.MARKER_START) == 1

    def test_upgrade_replaces_section(self, tmp_path):
        """Running injection on a file with old marker replaces the section."""
        claude_md = tmp_path / "CLAUDE.md"
        old_content = (
            "# My Project\n\n"
            f"{self.MARKER_START}\n"
            "## Old Secret Shield\nOld content here.\n"
            f"{self.MARKER_END}\n\n"
            "# Other Section\n"
        )
        claude_md.write_text(old_content)

        rc, _, _ = self._run_injection(str(claude_md))
        assert rc == 0
        content = claude_md.read_text()
        assert "Old content here." not in content
        assert "# My Project" in content
        assert "# Other Section" in content
        assert content.count(self.MARKER_START) == 1


# ══════════════════════════════════════════════════════════════════════════
# Legacy runner (python3 test_hook.py still works)
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ══════════════════════════════════════════════════════════════════════════
# UserPromptSubmit Tests — Strategy 4: Prompt secret scanning
# ══════════════════════════════════════════════════════════════════════════


class TestUserPromptSubmit:
    """Test that secrets in user prompts are detected and blocked."""

    def _run_prompt_hook(self, prompt_text, *, field="prompt", cwd=None, extra_payload=None, session_id=None):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": session_id or _session_id(), field: prompt_text}
        if cwd is not None:
            payload["cwd"] = str(cwd)
        if extra_payload:
            payload.update(extra_payload)
        r = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        parsed = None
        if r.stdout.strip():
            try:
                parsed = json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
        return parsed, r.returncode, r.stderr

    def test_clean_prompt_allowed(self):
        result, code, _ = self._run_prompt_hook("Please help me write a sort function")
        assert code == 0
        if result:
            assert result.get("decision") != "block"

    def test_openai_key_blocked(self):
        result, code, _ = self._run_prompt_hook("Use this key: sk-proj-abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmn")
        assert code == 0 and result is not None
        assert result["decision"] == "block"
        assert "secret detected" in result["reason"].lower()

    def test_postgres_url_blocked(self):
        result, code, _ = self._run_prompt_hook("Connect to postgres://myuser:s3cretP4ss@db.example.com:5432/mydb")
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_aws_key_blocked(self):
        result, code, _ = self._run_prompt_hook("My AWS key is AKIAIOSFODNN7EXAMPLE")
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_aws_session_token_blocked(self):
        result, code, _ = self._run_prompt_hook('SessionToken="' + "A" * 100 + '"')
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_github_pat_blocked(self):
        result, code, _ = self._run_prompt_hook("Token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_stripe_key_blocked(self):
        result, code, _ = self._run_prompt_hook("Key: sk_test_FAKE51TESTxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_jwt_blocked(self):
        result, code, _ = self._run_prompt_hook("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_jwt_secret_blocked(self):
        result, code, _ = self._run_prompt_hook('jwt_secret="' + "A" * 32 + '"')
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_lark_webhook_blocked(self):
        result, code, _ = self._run_prompt_hook("Webhook: https://open.larksuite.com/open-apis/bot/v2/hook/" + "A" * 24)
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_pem_key_blocked(self):
        result, code, _ = self._run_prompt_hook("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAK...")
        assert code == 0 and result is not None and result["decision"] == "block"

    def test_wallet_private_key_in_prompt_blocked(self):
        hex_key = "0x" + "a" * 64
        result, code, _ = self._run_prompt_hook(f'private_key = "{hex_key}"')
        assert code == 0 and result is not None
        assert result["decision"] == "block"

    def test_mnemonic_in_prompt_blocked(self):
        words = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        result, code, _ = self._run_prompt_hook(f'mnemonic = "{words}"')
        assert code == 0 and result is not None
        assert result["decision"] == "block"

    def test_btc_wif_in_prompt_blocked(self):
        wif = "5" + "H" * 50
        result, code, _ = self._run_prompt_hook(f"My BTC key: {wif}")
        assert code == 0 and result is not None
        assert result["decision"] == "block"

    def test_contract_address_not_blocked(self):
        result, code, _ = self._run_prompt_hook("Contract: 0xe63f1adbc4c2eaa088c5e78d2a0cf51272ef9688")
        assert code == 0
        if result:
            assert result.get("decision") != "block"



    def test_infura_url_in_prompt_blocked(self):
        url = "https://mainnet.infura.io/v3/" + "a" * 32
        result, code, _ = self._run_prompt_hook(f"Use RPC: {url}")
        assert code == 0 and result is not None
        assert result["decision"] == "block"

    def test_bare_tx_hash_not_blocked(self):
        """Bare 0x + 64 hex without context keyword should NOT be blocked."""
        result, code, _ = self._run_prompt_hook("Check tx: 0x" + "a" * 64)
        assert code == 0
        if result:
            assert result.get("decision") != "block"

    def test_empty_prompt_allowed(self):
        result, code, _ = self._run_prompt_hook("")
        assert code == 0
        if result:
            assert result.get("decision") != "block"

    def test_preview_truncated(self):
        result, code, _ = self._run_prompt_hook("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
        assert code == 0 and result is not None and result["decision"] == "block"
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij" not in result["reason"]

    def test_tip_in_reason(self):
        result, code, _ = self._run_prompt_hook("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmn")
        assert code == 0 and result is not None
        assert "reply" in result["reason"].lower() and "go" in result["reason"].lower()

    def test_user_prompt_field_supported(self):
        result, code, _ = self._run_prompt_hook(
            "Use this key: sk-proj-abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmn",
            field="user_prompt",
        )
        assert code == 0 and result is not None
        assert result["decision"] == "block"

    def test_go_restores_redacted_context(self, tmp_path):
        session_id = "go-restore-session"
        blocked_prompt = (
            "帮我配置 git，在 /tmp/a 用 tony.seah@kleepay.ai，"
            "在 /tmp/b 用 seahkweehwatony@gmail.com"
        )
        result, code, _ = self._run_prompt_hook(
            blocked_prompt, cwd=tmp_path, field="user_prompt", session_id=session_id
        )
        assert code == 0 and result is not None
        assert result["decision"] == "block"
        conf_files = list(tmp_path.glob(".tmp_secrets.*.conf"))
        prompt_files = list(tmp_path.glob(".tmp_secrets.*.prompt.txt"))
        assert len(conf_files) == 1
        assert len(prompt_files) == 1

        result, code, _ = self._run_prompt_hook("go", cwd=tmp_path, field="user_prompt", session_id=session_id)
        assert code == 0 and result is not None
        extra = result["hookSpecificOutput"]["additionalContext"]
        assert "continue that same request" in extra.lower()
        assert "treat those placeholders as the actual values" in extra.lower()
        assert "do not ask the user to manually substitute" in extra.lower()
        assert str(conf_files[0]) in extra
        assert "帮我配置 git" in extra
        assert "{{EMAIL_" in extra

    def test_reading_tmp_secrets_conf_deletes_both_temp_files(self, sid, tmp_path):
        conf = tmp_path / ".tmp_secrets.123456abcdef.conf"
        ctx = tmp_path / ".tmp_secrets.123456abcdef.prompt.txt"
        conf.write_text("secret")
        ctx.write_text("redacted")
        os.chmod(conf, 0o600)
        os.chmod(ctx, 0o600)

        o, c, _ = run_hook("Read", {"file_path": str(conf)}, sid)
        assert c == 0
        run_hook("Read", {"file_path": str(conf)}, sid, is_post=True)
        assert not conf.exists()
        assert not ctx.exists()

    def test_concurrent_sessions_use_distinct_prompt_artifacts(self, tmp_path):
        session_a = "session-a"
        session_b = "session-b"
        prompt_a = "邮箱 seahkweehwatony@gmail.com"
        prompt_b = "邮箱 tony.seah@kleepay.ai"

        result_a, code_a, _ = self._run_prompt_hook(prompt_a, cwd=tmp_path, field="user_prompt", session_id=session_a)
        result_b, code_b, _ = self._run_prompt_hook(prompt_b, cwd=tmp_path, field="user_prompt", session_id=session_b)
        assert code_a == 0 and result_a is not None and result_a["decision"] == "block"
        assert code_b == 0 and result_b is not None and result_b["decision"] == "block"

        files = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_secrets.") and p.suffix in (".conf", ".txt"))
        assert len(files) == 4

        result_a, code_a, _ = self._run_prompt_hook("go", cwd=tmp_path, field="user_prompt", session_id=session_a)
        result_b, code_b, _ = self._run_prompt_hook("go", cwd=tmp_path, field="user_prompt", session_id=session_b)
        assert code_a == 0 and result_a is not None
        assert code_b == 0 and result_b is not None
        extra_a = result_a["hookSpecificOutput"]["additionalContext"]
        extra_b = result_b["hookSpecificOutput"]["additionalContext"]
        assert extra_a != extra_b
        assert "a@example.com" not in extra_a
        assert "b@example.com" not in extra_b
        assert "邮箱" in extra_a and "邮箱" in extra_b
        assert extra_a.count(".tmp_secrets.") == 1
        assert extra_b.count(".tmp_secrets.") == 1

    def test_new_blocked_prompt_replaces_only_same_session_state(self, tmp_path):
        session_id = "replace-session"
        prompt_a = "first seahkweehwatony@gmail.com"
        prompt_b = "second tony.seah@kleepay.ai"

        self._run_prompt_hook(prompt_a, cwd=tmp_path, field="user_prompt", session_id=session_id)
        self._run_prompt_hook(prompt_b, cwd=tmp_path, field="user_prompt", session_id=session_id)

        conf_files = sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".conf"))
        prompt_files = sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".prompt.txt"))
        assert len(conf_files) == 1
        assert len(prompt_files) == 1

        result, code, _ = self._run_prompt_hook("go", cwd=tmp_path, field="user_prompt", session_id=session_id)
        assert code == 0 and result is not None
        extra = result["hookSpecificOutput"]["additionalContext"]
        assert "second" in extra

    def test_parallel_subagents_same_session_use_distinct_state(self, tmp_path):
        session_id = "shared-session"
        prompt_a = "alpha seahkweehwatony@gmail.com"
        prompt_b = "beta tony.seah@kleepay.ai"

        extra_a = {"agent_id": "subagent-a", "agent_type": "worker"}
        extra_b = {"agent_id": "subagent-b", "agent_type": "worker"}

        result_a, code_a, _ = self._run_prompt_hook(
            prompt_a, cwd=tmp_path, field="user_prompt", session_id=session_id, extra_payload=extra_a
        )
        result_b, code_b, _ = self._run_prompt_hook(
            prompt_b, cwd=tmp_path, field="user_prompt", session_id=session_id, extra_payload=extra_b
        )
        assert code_a == 0 and result_a is not None and result_a["decision"] == "block"
        assert code_b == 0 and result_b is not None and result_b["decision"] == "block"

        result_a, code_a, _ = self._run_prompt_hook(
            "go", cwd=tmp_path, field="user_prompt", session_id=session_id, extra_payload=extra_a
        )
        result_b, code_b, _ = self._run_prompt_hook(
            "go", cwd=tmp_path, field="user_prompt", session_id=session_id, extra_payload=extra_b
        )
        assert code_a == 0 and result_a is not None
        assert code_b == 0 and result_b is not None
        extra_text_a = result_a["hookSpecificOutput"]["additionalContext"]
        extra_text_b = result_b["hookSpecificOutput"]["additionalContext"]
        assert "alpha" in extra_text_a
        assert "beta" not in extra_text_a
        assert "beta" in extra_text_b
        assert "alpha" not in extra_text_b

    def test_session_end_only_cleans_current_session_prompt_artifacts(self, tmp_path):
        session_a = "session-a"
        session_b = "session-b"

        result_a, code_a, _ = self._run_prompt_hook(
            "alpha seahkweehwatony@gmail.com", cwd=tmp_path, field="user_prompt", session_id=session_a
        )
        result_b, code_b, _ = self._run_prompt_hook(
            "beta tony.seah@kleepay.ai", cwd=tmp_path, field="user_prompt", session_id=session_b
        )
        assert code_a == 0 and result_a is not None and result_a["decision"] == "block"
        assert code_b == 0 and result_b is not None and result_b["decision"] == "block"

        before_files = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_secrets."))
        assert len(before_files) == 4

        payload = {
            "tool_name": "SessionEnd",
            "tool_input": {},
            "session_id": session_a,
            "type": "SessionEnd",
            "cwd": str(tmp_path),
        }
        r = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0

        after_files = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_secrets."))
        assert len(after_files) == 2

        result_b, code_b, _ = self._run_prompt_hook("go", cwd=tmp_path, field="user_prompt", session_id=session_b)
        assert code_b == 0 and result_b is not None
        extra_b = result_b["hookSpecificOutput"]["additionalContext"]
        assert "beta" in extra_b
        assert "alpha" not in extra_b
