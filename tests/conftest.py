"""Shared fixtures for daemon mode tests."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module import — load conversation_search.py as an importable module
# ---------------------------------------------------------------------------

def _import_cs():
    script = Path(__file__).parent.parent / "conversation_search.py"
    spec = importlib.util.spec_from_file_location("conversation_search", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["conversation_search"] = mod
    spec.loader.exec_module(mod)
    return mod

cs = _import_cs()

# ---------------------------------------------------------------------------
# Sample JSONL data
# ---------------------------------------------------------------------------

SAMPLE_SESSION_ID = "abc12345-1111-2222-3333-444444444444"

SAMPLE_RECORDS = [
    {
        "type": "user",
        "message": {"role": "user", "content": "How do I sort a list in Python?"},
        "timestamp": "2026-01-15T10:00:00Z",
        "cwd": "/home/user/project",
        "slug": "python-sorting",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Use sorted() or list.sort() for sorting."}
            ],
        },
        "timestamp": "2026-01-15T10:00:05Z",
    },
    {
        "type": "user",
        "message": {"role": "user", "content": "What about sorting by a custom key function?"},
        "timestamp": "2026-01-15T10:01:00Z",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Pass key=lambda x: x.name to sorted()."}
            ],
        },
        "timestamp": "2026-01-15T10:01:10Z",
    },
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def daemon_cache_dir(tmp_path, monkeypatch):
    """Redirect _DAEMON_CACHE_DIR to a temp directory for isolation."""
    cache = tmp_path / "daemon-cache"
    cache.mkdir()
    monkeypatch.setattr(cs, "_DAEMON_CACHE_DIR", cache)
    return cache


@pytest.fixture
def free_port():
    """Find a random available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """Redirect _PROJECTS_ROOT to a temp directory."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(cs, "_PROJECTS_ROOT", root)
    return root


@pytest.fixture
def sample_jsonl(projects_dir):
    """Create a sample project directory with one JSONL conversation file."""
    project_dir = projects_dir / "home-user-myproject"
    project_dir.mkdir()
    session_file = project_dir / f"{SAMPLE_SESSION_ID}.jsonl"
    session_file.write_text(
        "\n".join(json.dumps(r) for r in SAMPLE_RECORDS) + "\n"
    )
    return {
        "projects_dir": projects_dir,
        "project_dir": project_dir,
        "session_file": session_file,
        "session_id": SAMPLE_SESSION_ID,
    }


def _wait_for_port(port, timeout=30):
    """Poll until something is listening on port, or raise after timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture
def daemon_process(free_port, tmp_path, sample_jsonl):
    """Start a real daemon subprocess on a random port with temp directories.

    Yields dict with proc, port, cache_dir. Kills daemon on teardown.
    """
    script = str(Path(__file__).parent.parent / "conversation_search.py")
    cache_dir = tmp_path / "sub-cache"
    cache_dir.mkdir()

    env = os.environ.copy()
    env["CONVERSATION_SEARCH_CACHE_DIR"] = str(cache_dir)
    env["CONVERSATION_SEARCH_PROJECTS_ROOT"] = str(sample_jsonl["projects_dir"])

    proc = subprocess.Popen(
        [
            sys.executable, script, "daemon",
            "--port", str(free_port),
            "--idle-timeout", "300",
        ],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        env=env,
    )

    if not _wait_for_port(free_port):
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(
            f"Daemon failed to start on port {free_port}. stderr: {stderr}"
        )

    yield {"proc": proc, "port": free_port, "cache_dir": cache_dir}

    # Teardown — ensure process is dead
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
