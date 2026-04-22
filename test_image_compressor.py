#!/usr/bin/env python3
"""
Unit + integration tests for the image compressor.

Unit tests exercise the decision logic (opt-outs, thresholds, cache
invalidation) with a monkey-patched `compress_to_cache` so they run
anywhere. One integration test actually calls `sips` and is skipped if
the binary isn't on PATH.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

HOOKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks")
sys.path.insert(0, HOOKS_DIR)

from image_compressor import (  # noqa: E402
    DIM_THRESHOLD_PX,
    OPT_OUT_ENV,
    OPT_OUT_FILE,
    SIZE_THRESHOLD_BYTES,
    cache_path_for,
    compress_to_cache,
    get_image_dims,
    is_image_path,
    maybe_compress_read,
    maybe_handle_bash_original_request,
    maybe_notify_post_read,
    opt_out_active,
    request_original,
)
import image_compressor as ic  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the module's cache directory per-test to prevent bleed."""
    cache = tmp_path / "imgcache"
    monkeypatch.setattr(ic, "CACHE_DIR", str(cache))
    # Clear the opt-out env var unconditionally — tests that need it set it
    monkeypatch.delenv(OPT_OUT_ENV, raising=False)
    return cache


@pytest.fixture
def big_fake_image(tmp_path):
    """A file that's BIG enough on disk but not necessarily a valid image.
    Used for logic tests where get_image_dims is mocked."""
    p = tmp_path / "screenshot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (SIZE_THRESHOLD_BYTES + 10))
    return p


@pytest.fixture
def small_png(tmp_path):
    p = tmp_path / "tiny.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return p


# ── is_image_path / opt_out_active ────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("/tmp/foo.png", True),
    ("/tmp/foo.PNG", True),
    ("/tmp/foo.jpg", True),
    ("/tmp/foo.jpeg", True),
    ("/tmp/foo.webp", True),
    ("/tmp/foo.heic", True),
    ("/tmp/foo.txt", False),
    ("/tmp/foo", False),
    ("", False),
    (None, False),
])
def test_is_image_path(path, expected):
    assert is_image_path(path) is expected


def test_opt_out_env_var(monkeypatch):
    monkeypatch.setenv(OPT_OUT_ENV, "1")
    assert opt_out_active("/tmp/foo.png", "/tmp") is True


def test_opt_out_project_file(tmp_path):
    (tmp_path / OPT_OUT_FILE).touch()
    assert opt_out_active("/tmp/foo.png", str(tmp_path)) is True


def test_opt_out_filename_markers():
    assert opt_out_active("/tmp/foo.orig.png", "") is True
    assert opt_out_active("/tmp/foo.nocompress.png", "") is True
    assert opt_out_active("/tmp/foo.png", "") is False


# ── cache_path_for ────────────────────────────────────────────────────


def test_cache_path_includes_mtime(big_fake_image):
    """Cache path must change when the source mtime changes — otherwise
    we'd serve stale downscales after edits."""
    p1 = cache_path_for(str(big_fake_image))
    os.utime(big_fake_image, (0, 0))  # force a different mtime
    p2 = cache_path_for(str(big_fake_image))
    assert p1 != p2


def test_cache_path_deterministic_for_same_file(big_fake_image):
    p1 = cache_path_for(str(big_fake_image))
    p2 = cache_path_for(str(big_fake_image))
    assert p1 == p2


# ── maybe_compress_read: decision logic (compress is mocked) ──────────


def _hook_payload(file_path, tool="Read", cwd="/tmp"):
    return {
        "tool_name": tool,
        "tool_input": {"file_path": str(file_path)},
        "cwd": cwd,
    }


def test_non_read_tool_passes_through():
    assert maybe_compress_read(_hook_payload("/tmp/x.png", tool="Write")) is None


def test_non_image_passes_through(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"x" * (SIZE_THRESHOLD_BYTES + 1))
    assert maybe_compress_read(_hook_payload(p)) is None


def test_small_file_passes_through(small_png):
    assert maybe_compress_read(_hook_payload(small_png)) is None


def test_missing_file_passes_through():
    assert maybe_compress_read(_hook_payload("/tmp/does-not-exist.png")) is None


def test_small_dimensions_pass_through(big_fake_image, monkeypatch):
    # Size is large but dims are tiny — should skip.
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (100, 100))
    assert maybe_compress_read(_hook_payload(big_fake_image)) is None


def test_large_image_gets_rewritten(big_fake_image, tmp_path, monkeypatch):
    fake_out = tmp_path / "compressed.png"
    fake_out.write_bytes(b"shrunk")

    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    monkeypatch.setattr(ic, "compress_to_cache", lambda p, **kw: str(fake_out))

    resp = maybe_compress_read(_hook_payload(big_fake_image))
    assert resp is not None
    hso = resp["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["file_path"] == str(fake_out)


def test_env_opt_out_short_circuits(big_fake_image, monkeypatch):
    monkeypatch.setenv(OPT_OUT_ENV, "1")
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    # If opt-out is active we shouldn't even reach compress_to_cache.
    called = {"n": 0}
    def boom(*a, **k):
        called["n"] += 1
        return None
    monkeypatch.setattr(ic, "compress_to_cache", boom)
    assert maybe_compress_read(_hook_payload(big_fake_image)) is None
    assert called["n"] == 0


def test_project_file_opt_out_short_circuits(big_fake_image, tmp_path, monkeypatch):
    (tmp_path / OPT_OUT_FILE).touch()
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    monkeypatch.setattr(ic, "compress_to_cache", lambda *a, **k: "should-not-be-used")
    resp = maybe_compress_read(_hook_payload(big_fake_image, cwd=str(tmp_path)))
    assert resp is None


def test_filename_marker_opt_out(tmp_path, monkeypatch):
    p = tmp_path / "high-detail.orig.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (SIZE_THRESHOLD_BYTES + 10))
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    assert maybe_compress_read(_hook_payload(p)) is None


def test_compress_failure_falls_open(big_fake_image, monkeypatch):
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    monkeypatch.setattr(ic, "compress_to_cache", lambda *a, **k: None)
    assert maybe_compress_read(_hook_payload(big_fake_image)) is None


# ── Integration: actually call sips ────────────────────────────────────


@pytest.fixture
def real_large_png(tmp_path):
    """Use sips itself to synthesise a real PNG larger than the threshold.
    Skip if sips isn't available (non-macOS CI)."""
    if shutil.which("sips") is None:
        pytest.skip("sips not on PATH")
    # Start from a tiny 1x1 png written by hand, then use sips to resize
    # up. This gives us a real (valid) PNG at arbitrary dimensions.
    src = tmp_path / "seed.png"
    # 1x1 opaque black PNG
    src.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000100a0d7e9a80000000049454e44ae426082"
    ))
    big = tmp_path / "big.png"
    r = subprocess.run(
        ["sips", "-z", "3000", "3000", str(src), "-o", str(big)],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not big.is_file():
        pytest.skip(f"sips upscale unavailable: {r.stderr[:200]}")
    # Make it definitely pass size threshold by padding (sips output may
    # be small because it's solid color; we'll still accept whatever it is
    # as long as sips knows the dims).
    return big


def test_sips_integration_real_compress(real_large_png):
    w_before, h_before = get_image_dims(str(real_large_png))
    assert max(w_before, h_before) >= 1920, "seed should be large enough"
    out = compress_to_cache(str(real_large_png))
    assert out is not None and os.path.isfile(out)
    w_after, h_after = get_image_dims(out)
    assert max(w_after, h_after) <= DIM_THRESHOLD_PX
    # Second call must hit cache: mtime should equal the first one.
    mt1 = os.path.getmtime(out)
    out2 = compress_to_cache(str(real_large_png))
    assert out2 == out
    assert os.path.getmtime(out2) == mt1


# ── request-original flow ─────────────────────────────────────────────


def test_bash_intercept_sets_flag_and_denies():
    """`redmem-original /x.png` → deny + flag file written, no shell run."""
    resp = maybe_handle_bash_original_request({
        "session_id": "sx",
        "tool_name": "Bash",
        "tool_input": {"command": "redmem-original /tmp/foo.png"},
    })
    assert resp is not None
    hso = resp["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Flag set" in hso["permissionDecisionReason"]


def test_bash_intercept_strips_quotes():
    resp = maybe_handle_bash_original_request({
        "session_id": "sx",
        "tool_name": "Bash",
        "tool_input": {"command": "redmem-original '/tmp/my image.png'"},
    })
    assert resp is not None


def test_bash_intercept_ignores_other_commands():
    for cmd in ["ls", "echo redmem-original foo", "rm file", ""]:
        assert maybe_handle_bash_original_request({
            "session_id": "sx",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }) is None


def test_flag_then_read_passes_through(big_fake_image, monkeypatch):
    """After flag is set, next Read should pass the original through."""
    request_original("sess1", str(big_fake_image))
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    # Should NOT call compress_to_cache even though dims exceed threshold.
    monkeypatch.setattr(ic, "compress_to_cache",
                        lambda *a, **k: pytest.fail("compress must not fire"))
    resp = maybe_compress_read({
        "session_id": "sess1",
        "tool_name": "Read",
        "tool_input": {"file_path": str(big_fake_image)},
    })
    assert resp is None


def test_flag_is_one_shot(big_fake_image, tmp_path, monkeypatch):
    """After serving the original once, the flag is cleared."""
    request_original("sess2", str(big_fake_image))
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    calls = {"n": 0}
    def fake_compress(*a, **k):
        calls["n"] += 1
        out = tmp_path / "c.png"
        out.write_bytes(b"x")
        return str(out)
    monkeypatch.setattr(ic, "compress_to_cache", fake_compress)

    # First call: flag present → pass through, no compress
    assert maybe_compress_read({
        "session_id": "sess2", "tool_name": "Read",
        "tool_input": {"file_path": str(big_fake_image)},
    }) is None
    assert calls["n"] == 0

    # Second call: flag consumed → normal compression resumes
    resp = maybe_compress_read({
        "session_id": "sess2", "tool_name": "Read",
        "tool_input": {"file_path": str(big_fake_image)},
    })
    assert resp is not None
    assert calls["n"] == 1


def test_flag_scoped_by_session(big_fake_image, tmp_path, monkeypatch):
    """A flag set for session A must not affect a Read in session B."""
    request_original("sessA", str(big_fake_image))
    monkeypatch.setattr(ic, "get_image_dims", lambda p: (4032, 3024))
    out = tmp_path / "c.png"
    out.write_bytes(b"x")
    monkeypatch.setattr(ic, "compress_to_cache", lambda *a, **k: str(out))
    # Session B should still get compression (flag was for A).
    resp = maybe_compress_read({
        "session_id": "sessB", "tool_name": "Read",
        "tool_input": {"file_path": str(big_fake_image)},
    })
    assert resp is not None


def test_post_read_notice_with_sidecar(tmp_path, isolated_cache):
    """A Read of a cache path with a sidecar emits an additionalContext."""
    cache = isolated_cache
    cache.mkdir(parents=True, exist_ok=True)
    compressed = cache / "abc-123.png"
    compressed.write_bytes(b"x")
    meta = cache / "abc-123.png.meta.json"
    meta.write_text(json.dumps({
        "original_path": "/Users/me/big.png",
        "original_dims": [4032, 3024],
        "compressed_dims": [1920, 1440],
    }))
    resp = maybe_notify_post_read({
        "tool_name": "Read",
        "tool_input": {"file_path": str(compressed)},
    })
    assert resp is not None
    ctx = resp["hookSpecificOutput"]["additionalContext"]
    assert "4032×3024" in ctx and "1920×1440" in ctx
    assert "/Users/me/big.png" in ctx
    assert "redmem-original" in ctx


def test_post_read_no_notice_for_non_cache_path():
    """Regular Read (not our cache) produces no notice."""
    assert maybe_notify_post_read({
        "tool_name": "Read",
        "tool_input": {"file_path": "/Users/me/regular.png"},
    }) is None


def test_post_read_no_notice_without_sidecar(tmp_path, isolated_cache):
    """If the sidecar is missing, stay silent (unknown provenance)."""
    cache = isolated_cache
    cache.mkdir(parents=True, exist_ok=True)
    fake = cache / "abc-999.png"
    fake.write_bytes(b"x")
    assert maybe_notify_post_read({
        "tool_name": "Read",
        "tool_input": {"file_path": str(fake)},
    }) is None


# ── Standalone CLI smoke ──────────────────────────────────────────────


def test_cli_non_read_is_no_op(tmp_path):
    script = os.path.join(HOOKS_DIR, "image_compressor.py")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    r = subprocess.run(
        [sys.executable, script], input=payload,
        capture_output=True, text=True, timeout=5,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == ""
