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
    send_session_reset,
    send_status,
    send_stop,
)


# ---------------------------------------------------------------------------
# Unit: AudioDaemon._dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def setup_method(self):
        self.d = AudioDaemon(cooldown=3.0)
        self.d._running = True

    def test_unknown_type_returns_error(self):
        resp = self.d._dispatch({"type": "bogus"})
        assert resp["ok"] is False
        assert "bogus" in resp["reason"]

    def test_status_returns_ok_with_queue_size(self):
        resp = self.d._dispatch({"type": "status"})
        assert resp["ok"] is True
        assert "queue_size" in resp
        assert "session_counts" in resp
        assert "session_limits" in resp

    def test_session_reset_clears_counts(self):
        self.d._session_counts["coding"] = 99
        resp = self.d._dispatch({"type": "session_reset"})
        assert resp["ok"] is True
        assert self.d._session_counts == {}

    def test_stop_sets_running_false(self):
        resp = self.d._dispatch({"type": "stop"})
        assert resp["ok"] is True
        assert self.d._running is False

    def test_play_dispatches_to_handle_play(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        resp = self.d._dispatch({"type": "play", "event": "coding", "audio_path": str(audio), "volume": 0.8})
        assert resp["ok"] is True


# ---------------------------------------------------------------------------
# Unit: AudioDaemon._handle_play (cooldown logic)
# ---------------------------------------------------------------------------


class TestHandlePlay:
    def setup_method(self):
        # repeat_probability=1.0 so probability never interferes with cooldown tests
        self.d = AudioDaemon(cooldown=3.0, repeat_probability=1.0)

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
        d = AudioDaemon(cooldown=0.0, repeat_probability=1.0)
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
        d = AudioDaemon(cooldown=0.05, repeat_probability=1.0)  # 50 ms cooldown
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


# ---------------------------------------------------------------------------
# Unit: session limits
# ---------------------------------------------------------------------------


class TestSessionLimits:
    # All constructors use repeat_probability=1.0 so probability never interferes
    def test_play_accepted_up_to_limit(self):
        d = AudioDaemon(cooldown=0.0, session_limits={"coding": 2}, repeat_probability=1.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True  # second play still within limit

    def test_play_rejected_at_limit(self):
        d = AudioDaemon(cooldown=0.0, session_limits={"coding": 2}, repeat_probability=1.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is False
        assert resp["reason"] == "session_limit"
        assert resp["count"] == 2
        assert resp["limit"] == 2

    def test_session_limit_does_not_affect_other_events(self):
        d = AudioDaemon(cooldown=0.0, session_limits={"coding": 1}, repeat_probability=1.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        # coding is at limit but bash_run is a different event
        resp = d._handle_play({"event": "bash_run", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True

    def test_zero_limit_means_unlimited(self):
        d = AudioDaemon(cooldown=0.0, session_limits={"coding": 0}, repeat_probability=1.0)
        for _ in range(10):
            resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
            assert resp["ok"] is True

    def test_no_limit_entry_means_unlimited(self):
        d = AudioDaemon(cooldown=0.0, session_limits={}, repeat_probability=1.0)
        for _ in range(10):
            resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
            assert resp["ok"] is True

    def test_session_reset_allows_play_again(self):
        d = AudioDaemon(cooldown=0.0, session_limits={"coding": 1}, repeat_probability=1.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        # at limit
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is False

        d._session_counts.clear()  # simulate session_reset

        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True

    def test_session_counts_incremented_per_play(self):
        d = AudioDaemon(cooldown=0.0, session_limits={"coding": 5}, repeat_probability=1.0)
        for i in range(3):
            d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert d._session_counts.get("coding") == 3

    def test_cooldown_rejection_does_not_increment_count(self):
        d = AudioDaemon(cooldown=60.0, session_limits={"coding": 5}, repeat_probability=1.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert d._session_counts.get("coding") == 1
        # Second play within cooldown — should be rejected without touching count
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert d._session_counts.get("coding") == 1


# ---------------------------------------------------------------------------
# Unit: probabilistic play
# ---------------------------------------------------------------------------


class TestProbabilisticPlay:
    def test_first_play_always_accepted_even_with_zero_probability(self):
        """count == 0 → 100% play regardless of repeat_probability."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=0.0)
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True

    def test_repeat_always_skipped_when_probability_zero(self):
        """repeat_probability=0.0 means every play after the first is dropped."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=0.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is False
        assert resp["reason"] == "skipped"

    def test_repeat_always_accepted_when_probability_one(self):
        """repeat_probability=1.0 means every play goes through (just cooldown gating)."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=1.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True

    def test_roll_below_threshold_plays(self):
        """_roll() < repeat_probability → play."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=0.5)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first
        d._roll = lambda: 0.49  # just below 0.5 → play
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True

    def test_roll_at_or_above_threshold_skips(self):
        """_roll() >= repeat_probability → skip."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=0.5)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first
        d._roll = lambda: 0.5  # exactly at threshold → skip
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is False
        assert resp["reason"] == "skipped"

    def test_skip_does_not_increment_session_count(self):
        """Probability skip must not consume session quota."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=0.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # count → 1
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # skip
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # skip
        assert d._session_counts.get("coding") == 1  # only the first play counted

    def test_skip_does_not_update_last_played_timestamp(self):
        """Probability skip must not reset the cooldown timer."""
        d = AudioDaemon(cooldown=3.0, repeat_probability=0.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first
        ts_before = d._last_played["coding"]
        time.sleep(0.01)
        # second attempt: count=1, prob=0.0 → skip BEFORE cooldown check
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert d._last_played["coding"] == ts_before  # timestamp unchanged

    def test_session_reset_makes_next_play_first_play(self):
        """After session reset, count is 0 again → next play is always 100%."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=0.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first
        # Now at limit; second would be skipped
        assert d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})["ok"] is False

        d._session_counts.clear()  # simulate session_reset

        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        assert resp["ok"] is True  # first play again after reset

    def test_probability_check_precedes_cooldown(self):
        """When within cooldown AND probability would skip, reason is 'skipped' not 'cooldown'."""
        d = AudioDaemon(cooldown=60.0, repeat_probability=0.0)
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # first, sets cooldown
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        # Probability check fires first → skipped, not cooldown
        assert resp["reason"] == "skipped"

    def test_session_limit_check_precedes_probability(self):
        """When at session limit, reason is 'session_limit' even if probability would play."""
        d = AudioDaemon(cooldown=0.0, repeat_probability=1.0, session_limits={"coding": 1})
        d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})  # count → 1
        resp = d._handle_play({"event": "coding", "audio_path": "", "volume": 0.8})
        # Session limit fires first → session_limit, not a probability decision
        assert resp["reason"] == "session_limit"


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

    d = AudioDaemon(cooldown=0.05, repeat_probability=1.0)  # short cooldown; no probability filtering
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

    def test_status_includes_session_counts_and_limits(self, live_daemon):
        resp = send_status()
        assert resp is not None
        assert "session_counts" in resp
        assert "session_limits" in resp

    def test_session_reset_clears_counts_via_socket(self, live_daemon, tmp_path):
        audio = tmp_path / "fake.mp3"
        audio.write_bytes(b"fake audio")
        send_play("coding", str(audio), 0.5)
        # Counts should have coding=1 now
        before = send_status()
        assert before["session_counts"].get("coding", 0) == 1

        resp = send_session_reset()
        assert resp is not None
        assert resp["ok"] is True

        after = send_status()
        assert after["session_counts"].get("coding", 0) == 0

    def test_session_limit_enforced_via_daemon(self, live_daemon, tmp_path, monkeypatch):
        # Patch the daemon's limits so coding limit = 2
        live_daemon._session_limits["coding"] = 2
        audio = tmp_path / "fake.mp3"
        audio.write_bytes(b"fake audio")

        r1 = send_play("coding", str(audio), 0.5)
        assert r1 is not None and r1["ok"] is True

        time.sleep(0.1)  # wait for cooldown to expire (cooldown=0.05 in fixture)

        r2 = send_play("coding", str(audio), 0.5)
        assert r2 is not None and r2["ok"] is True

        time.sleep(0.1)

        r3 = send_play("coding", str(audio), 0.5)
        assert r3 is not None
        assert r3["ok"] is False
        assert r3["reason"] == "session_limit"
        assert r3["count"] == 2
        assert r3["limit"] == 2

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
