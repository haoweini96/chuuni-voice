"""Tests for chuuni_voice.daemon."""

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

import chuuni_voice.daemon as daemon_mod
from chuuni_voice.daemon import (
    AudioDaemon,
    _send,
    is_running,
    send_play,
    send_status,
    send_stop,
)


# ---------------------------------------------------------------------------
# Unit: AudioDaemon._dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def setup_method(self):
        self.d = AudioDaemon(cooldowns={"coding": 3.0})
        self.d._running = True

    def test_unknown_type_returns_error(self):
        resp = self.d._dispatch({"type": "bogus"})
        assert resp["ok"] is False
        assert "bogus" in resp["reason"]

    def test_status_returns_ok_with_queue_size(self):
        resp = self.d._dispatch({"type": "status"})
        assert resp["ok"] is True
        assert "queue_size" in resp

    def test_stop_sets_running_false(self):
        resp = self.d._dispatch({"type": "stop"})
        assert resp["ok"] is True
        assert self.d._running is False

    def test_play_dispatches_to_handle_play(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        resp = self.d._dispatch(
            {"type": "play", "event": "coding", "audio_path": str(audio), "volume": 0.8}
        )
        assert resp["ok"] is True


# ---------------------------------------------------------------------------
# Unit: AudioDaemon._handle_play (per-event cooldown logic)
# ---------------------------------------------------------------------------


class TestHandlePlay:
    def setup_method(self):
        self.d = AudioDaemon(cooldowns={"coding": 3.0, "bash_run": 3.0}, default_cooldown=3.0)

    def test_first_play_accepted(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        resp = self.d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        assert resp["ok"] is True

    def test_repeat_within_cooldown_rejected(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        self.d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        resp = self.d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        assert resp["ok"] is False
        assert resp["reason"] == "cooldown"

    def test_different_events_not_blocked_by_each_other(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        self.d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        resp = self.d._handle_play({"event": "bash_run", "audio_path": str(audio), "volume": 0.8})
        assert resp["ok"] is True

    def test_zero_cooldown_allows_rapid_repeat(self, tmp_path):
        d = AudioDaemon(cooldowns={"coding": 0.0})
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        resp = d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        assert resp["ok"] is True

    def test_empty_audio_path_still_accepted_and_updates_cooldown(self):
        resp = self.d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True
        # Should now be in cooldown
        resp2 = self.d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp2["ok"] is False

    def test_cooldown_expires_after_timeout(self, tmp_path):
        d = AudioDaemon(cooldowns={"coding": 0.05})  # 50 ms cooldown
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        time.sleep(0.1)
        resp = d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        assert resp["ok"] is True

    def test_queue_enqueued_on_accepted_play(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        self.d._handle_play({"event": "coding", "audio_path": str(audio), "volume": 0.8})
        assert not self.d._queue.empty()

    def test_queue_not_enqueued_when_no_audio_path(self):
        self.d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert self.d._queue.empty()

    def test_per_event_cooldown_uses_event_specific_value(self):
        """Events with different cooldowns should respect their own values."""
        d = AudioDaemon(cooldowns={"coding": 0.0, "permission_prompt": 60.0})
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        # coding has 0 cooldown, should allow immediate repeat
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True

        d._handle_play({"event": "permission_prompt", "audio_path": "", "volume": 0.8})
        # permission_prompt has 60s cooldown, should reject
        resp = d._handle_play({"event": "permission_prompt", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is False
        assert resp["reason"] == "cooldown"

    def test_unknown_event_uses_default_cooldown(self):
        """Events not in cooldowns dict should use default_cooldown."""
        d = AudioDaemon(cooldowns={}, default_cooldown=60.0)
        d._handle_play({"event": "mystery", "audio_path": "", "volume": 0.8})
        resp = d._handle_play({"event": "mystery", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is False
        assert resp["reason"] == "cooldown"


# ---------------------------------------------------------------------------
# Unit: client helpers when daemon is not running
# ---------------------------------------------------------------------------


class TestClientNoSocket:
    def test_is_running_returns_false_when_no_socket(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_mod, "SOCKET_PATH", tmp_path / "missing.sock")
        assert is_running() is False

    def test_send_play_returns_none_when_no_socket(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_mod, "SOCKET_PATH", tmp_path / "missing.sock")
        assert send_play("coding", "/fake.mp3", 0.8) is None

    def test_send_status_returns_none_when_no_socket(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_mod, "SOCKET_PATH", tmp_path / "missing.sock")
        assert send_status() is None

    def test_send_stop_returns_none_when_no_socket(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_mod, "SOCKET_PATH", tmp_path / "missing.sock")
        assert send_stop() is None


# ---------------------------------------------------------------------------
# Integration: live daemon running in a background thread
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_daemon(tmp_path, monkeypatch):
    """Start a real AudioDaemon in a thread; yield; stop and join."""
    # AF_UNIX paths are limited to ~104 bytes on macOS — use /tmp directly
    sock_fd, sock_str = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(sock_fd)
    sock_path = Path(sock_str)
    sock_path.unlink(missing_ok=True)  # mkstemp creates the file; bind needs it absent

    pid_path = tmp_path / "chuuni_test.pid"
    log_path = tmp_path / "chuuni_test.log"

    monkeypatch.setattr(daemon_mod, "CHUUNI_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "SOCKET_PATH", sock_path)
    monkeypatch.setattr(daemon_mod, "PID_FILE", pid_path)
    monkeypatch.setattr(daemon_mod, "LOG_FILE", log_path)

    # Stub out actual audio playback so tests don't call afplay
    monkeypatch.setattr(daemon_mod, "_play_audio", lambda path, volume: None)

    d = AudioDaemon(cooldowns={"coding": 0.05, "bash_run": 0.05, "task_done": 0.05})
    t = threading.Thread(target=d.run, daemon=True)
    t.start()

    # Wait up to 2.5 s for the daemon to be ready
    deadline = time.time() + 2.5
    while time.time() < deadline:
        if sock_path.exists():
            resp = _send({"type": "status"})
            if resp and resp.get("ok"):
                break
        time.sleep(0.05)

    yield d

    # Teardown: stop daemon and wait for thread
    _send({"type": "stop"})
    t.join(timeout=3.0)


class TestDaemonIntegration:
    def test_status_reports_running(self, live_daemon):
        resp = send_status()
        assert resp is not None
        assert resp["ok"] is True
        assert "queue_size" in resp

    def test_is_running_true_while_daemon_alive(self, live_daemon):
        assert is_running() is True

    def test_play_accepted(self, live_daemon, tmp_path):
        audio = tmp_path / "fake.mp3"
        audio.write_bytes(b"fake audio")
        resp = send_play("task_done", str(audio), 0.5)
        assert resp is not None
        assert resp["ok"] is True

    def test_play_cooldown_blocks_repeat(self, live_daemon, tmp_path):
        audio = tmp_path / "fake.mp3"
        audio.write_bytes(b"fake audio")
        send_play("coding", str(audio), 0.5)
        resp = send_play("coding", str(audio), 0.5)
        assert resp is not None
        assert resp["ok"] is False
        assert resp["reason"] == "cooldown"

    def test_play_different_events_not_blocked(self, live_daemon, tmp_path):
        audio = tmp_path / "fake.mp3"
        audio.write_bytes(b"fake audio")
        send_play("coding", str(audio), 0.5)
        resp = send_play("bash_run", str(audio), 0.5)
        assert resp is not None
        assert resp["ok"] is True

    def test_bad_json_returns_error(self, live_daemon):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(daemon_mod.SOCKET_PATH))
        sock.sendall(b"not json at all\n")
        data = sock.recv(4096)
        sock.close()
        resp = json.loads(data.decode().strip())
        assert resp["ok"] is False

    def test_stop_shuts_down_daemon(self, live_daemon):
        resp = send_stop()
        assert resp is not None
        assert resp["ok"] is True

        # Poll until daemon is no longer reachable
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not is_running():
                break
            time.sleep(0.1)

        assert not is_running()
