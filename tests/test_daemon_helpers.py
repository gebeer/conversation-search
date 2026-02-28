"""Unit tests for daemon helper functions."""
from __future__ import annotations

import os
import socket

import pytest

from conftest import cs


# ---------------------------------------------------------------------------
# _daemon_cache_dir()
# ---------------------------------------------------------------------------

class TestDaemonCacheDir:
    def test_creates_directory(self, daemon_cache_dir):
        # Remove it first to test creation
        daemon_cache_dir.rmdir()
        assert not daemon_cache_dir.exists()

        result = cs._daemon_cache_dir()
        assert result.exists()
        assert result.is_dir()

    def test_idempotent(self, daemon_cache_dir):
        cs._daemon_cache_dir()
        cs._daemon_cache_dir()
        assert daemon_cache_dir.exists()


# ---------------------------------------------------------------------------
# _write_daemon_files() / _read_daemon_state() round-trip
# ---------------------------------------------------------------------------

class TestWriteReadRoundtrip:
    def test_roundtrip(self, daemon_cache_dir):
        cs._write_daemon_files(12345, 9237)
        result = cs._read_daemon_state()
        assert result == (12345, 9237)

    def test_read_returns_none_when_no_files(self, daemon_cache_dir):
        assert cs._read_daemon_state() is None

    def test_read_returns_none_when_malformed(self, daemon_cache_dir):
        (daemon_cache_dir / "daemon.pid").write_text("not-a-number")
        (daemon_cache_dir / "daemon.port").write_text("9237")
        assert cs._read_daemon_state() is None


# ---------------------------------------------------------------------------
# _cleanup_daemon_files()
# ---------------------------------------------------------------------------

class TestCleanupDaemonFiles:
    def test_removes_both_files(self, daemon_cache_dir):
        cs._write_daemon_files(12345, 9237)
        assert (daemon_cache_dir / "daemon.pid").exists()
        assert (daemon_cache_dir / "daemon.port").exists()

        cs._cleanup_daemon_files()
        assert not (daemon_cache_dir / "daemon.pid").exists()
        assert not (daemon_cache_dir / "daemon.port").exists()

    def test_no_error_when_files_absent(self, daemon_cache_dir):
        # Should not raise
        cs._cleanup_daemon_files()


# ---------------------------------------------------------------------------
# _is_pid_alive()
# ---------------------------------------------------------------------------

class TestIsPidAlive:
    def test_current_process(self):
        assert cs._is_pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        assert cs._is_pid_alive(99999999) is False

    def test_permission_error_treated_as_alive(self, monkeypatch):
        def mock_kill(pid, sig):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(os, "kill", mock_kill)
        assert cs._is_pid_alive(1) is True


# ---------------------------------------------------------------------------
# _is_port_responding()
# ---------------------------------------------------------------------------

class TestIsPortResponding:
    def test_with_listener(self, free_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", free_port))
            srv.listen(1)
            assert cs._is_port_responding(free_port) is True

    def test_nothing_listening(self, free_port):
        assert cs._is_port_responding(free_port) is False


# ---------------------------------------------------------------------------
# _is_daemon_healthy()
# ---------------------------------------------------------------------------

class TestIsDaemonHealthy:
    def test_healthy_when_both_pass(self, free_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", free_port))
            srv.listen(1)
            assert cs._is_daemon_healthy(os.getpid(), free_port) is True

    def test_unhealthy_when_pid_dead(self, free_port):
        assert cs._is_daemon_healthy(99999999, free_port) is False

    def test_unhealthy_when_port_closed(self, free_port):
        assert cs._is_daemon_healthy(os.getpid(), free_port) is False
