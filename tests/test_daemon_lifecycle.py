"""Integration tests for daemon lifecycle — starts real subprocesses."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from conftest import SAMPLE_SESSION_ID, cs, _wait_for_port

SCRIPT = str(Path(__file__).parent.parent / "conversation_search.py")


def _start_daemon(port, cache_dir, projects_dir, idle_timeout=300):
    """Helper to start a daemon subprocess with env isolation."""
    env = os.environ.copy()
    env["CONVERSATION_SEARCH_CACHE_DIR"] = str(cache_dir)
    env["CONVERSATION_SEARCH_PROJECTS_ROOT"] = str(projects_dir)

    return subprocess.Popen(
        [
            sys.executable, SCRIPT, "daemon",
            "--port", str(port),
            "--idle-timeout", str(idle_timeout),
        ],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        env=env,
    )


# ---------------------------------------------------------------------------
# Daemon startup and health
# ---------------------------------------------------------------------------

class TestDaemonStartup:
    def test_daemon_starts_and_binds_port(self, daemon_process):
        port = daemon_process["port"]
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass  # Connection succeeded

    def test_daemon_writes_pid_port_files(self, daemon_process):
        cache_dir = daemon_process["cache_dir"]
        proc = daemon_process["proc"]
        port = daemon_process["port"]

        pid_file = cache_dir / "daemon.pid"
        port_file = cache_dir / "daemon.port"

        assert pid_file.exists()
        assert port_file.exists()
        assert int(pid_file.read_text().strip()) == proc.pid
        assert int(port_file.read_text().strip()) == port

    def test_health_endpoint(self, daemon_process):
        port = daemon_process["port"]
        resp = urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        data = json.loads(resp.read())
        assert resp.status == 200
        assert data == {"status": "ok"}


# ---------------------------------------------------------------------------
# Duplicate daemon detection
# ---------------------------------------------------------------------------

class TestDuplicateDetection:
    def test_second_daemon_exits(self, daemon_process, sample_jsonl):
        port = daemon_process["port"]
        cache_dir = daemon_process["cache_dir"]

        proc2 = _start_daemon(
            port, cache_dir, sample_jsonl["projects_dir"]
        )
        try:
            proc2.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc2.kill()
            proc2.wait()
            pytest.fail("Second daemon did not exit within 15s")

        stderr = proc2.stderr.read().decode() if proc2.stderr else ""
        assert "already running" in stderr


# ---------------------------------------------------------------------------
# SIGTERM shutdown and cleanup
# ---------------------------------------------------------------------------

class TestSignalCleanup:
    def test_sigterm_cleans_up_files(self, free_port, tmp_path, sample_jsonl):
        cache_dir = tmp_path / "sigterm-cache"
        cache_dir.mkdir()

        proc = _start_daemon(
            free_port, cache_dir, sample_jsonl["projects_dir"]
        )

        assert _wait_for_port(free_port), "Daemon failed to start"

        # Verify files exist before signal
        assert (cache_dir / "daemon.pid").exists()

        # Send SIGTERM
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Daemon did not exit after SIGTERM within 10s")

        # Files should be cleaned up
        assert not (cache_dir / "daemon.pid").exists()
        assert not (cache_dir / "daemon.port").exists()


# ---------------------------------------------------------------------------
# Stale file recovery
# ---------------------------------------------------------------------------

class TestStaleFileRecovery:
    def test_starts_despite_stale_files(self, free_port, tmp_path, sample_jsonl):
        cache_dir = tmp_path / "stale-cache"
        cache_dir.mkdir()

        # Write fake stale PID/port files
        (cache_dir / "daemon.pid").write_text("99999999")
        (cache_dir / "daemon.port").write_text(str(free_port))

        proc = _start_daemon(
            free_port, cache_dir, sample_jsonl["projects_dir"]
        )

        try:
            assert _wait_for_port(free_port), "Daemon failed to start with stale files"

            # Verify it wrote its own PID
            actual_pid = int((cache_dir / "daemon.pid").read_text().strip())
            assert actual_pid == proc.pid
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


# ---------------------------------------------------------------------------
# MCP search through daemon (end-to-end)
# ---------------------------------------------------------------------------

class TestMCPSearch:
    def test_search_through_daemon(self, daemon_process):
        """Connect via SSE, call search_conversations, verify results."""
        port = daemon_process["port"]

        import anyio
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _do_search():
            async with sse_client(f"http://127.0.0.1:{port}/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "search_conversations",
                        {"query": "sort Python list", "limit": 5},
                    )
                    return result

        result = anyio.run(_do_search)

        # result.content is a list of TextContent objects
        data = json.loads(result.content[0].text)
        assert len(data["results"]) > 0
        assert data["results"][0]["session_id"] == SAMPLE_SESSION_ID
