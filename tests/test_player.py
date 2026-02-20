"""Tests for chuuni_voice.player."""

import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import chuuni_voice.player as player_mod
from chuuni_voice.events import ChuuniEvent
from chuuni_voice.player import (
    _build_command,
    _enqueue_task,
    _find_candidates,
    _linux_command,
    _mac_command,
    _play_blocking,
    play_event,
    play_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audio(directory: Path, name: str) -> Path:
    """Create an empty audio file for testing."""
    f = directory / name
    f.touch()
    return f


# ---------------------------------------------------------------------------
# Shared fixture: reset module-level state between every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_player_state():
    """Clear cooldown timestamps before (and after) each test."""
    player_mod._last_played.clear()
    yield
    player_mod._last_played.clear()


# ---------------------------------------------------------------------------
# play_file — public API (thread-based)
# ---------------------------------------------------------------------------


class TestPlayFile:
    def test_dispatches_to_play_blocking(self, tmp_path):
        """`play_file` should call `_play_blocking` with the right args."""
        audio = _make_audio(tmp_path, "coding.mp3")

        with patch("chuuni_voice.player._play_blocking") as mock_blocking:
            play_file(str(audio), volume=0.8)
            # Daemon thread — give it a moment to start
            time.sleep(0.05)

        mock_blocking.assert_called_once_with(str(audio), 0.8)

    def test_volume_clamped_high(self, tmp_path):
        """Volume > 1.0 should be clamped to 1.0."""
        audio = _make_audio(tmp_path, "test.mp3")

        with patch("chuuni_voice.player._play_blocking") as mock_blocking:
            play_file(str(audio), volume=5.0)
            time.sleep(0.05)

        _, vol = mock_blocking.call_args[0]
        assert vol == 1.0

    def test_volume_clamped_low(self, tmp_path):
        """Volume < 0.0 should be clamped to 0.0."""
        audio = _make_audio(tmp_path, "test.mp3")

        with patch("chuuni_voice.player._play_blocking") as mock_blocking:
            play_file(str(audio), volume=-3.0)
            time.sleep(0.05)

        _, vol = mock_blocking.call_args[0]
        assert vol == 0.0

    def test_does_not_raise(self):
        """play_file must never raise, even with a garbage path."""
        play_file("/absolute/nonsense/path/nope.mp3", volume=0.8)


# ---------------------------------------------------------------------------
# _play_blocking — the blocking worker
# ---------------------------------------------------------------------------


class TestPlayBlocking:
    def test_calls_afplay_on_mac(self, tmp_path):
        """On macOS, _play_blocking should launch afplay with -v flag."""
        audio = _make_audio(tmp_path, "test.mp3")

        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.Popen") as mock_popen,
        ):
            _play_blocking(str(audio), 0.8)

        mock_popen.assert_called_once()
        cmd: list[str] = mock_popen.call_args[0][0]
        assert cmd[0] == "afplay"
        assert "-v" in cmd
        assert "0.8" in cmd
        assert str(audio) in cmd

    def test_afplay_receives_correct_volume(self, tmp_path):
        """Volume value must appear immediately after the -v flag."""
        audio = _make_audio(tmp_path, "test.mp3")

        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.Popen") as mock_popen,
        ):
            _play_blocking(str(audio), 0.5)

        cmd = mock_popen.call_args[0][0]
        v_idx = cmd.index("-v")
        assert cmd[v_idx + 1] == "0.5"

    def test_silent_failure_when_file_missing(self, tmp_path):
        """Must not raise when the audio file does not exist."""
        missing = str(tmp_path / "nope.mp3")

        with patch("subprocess.Popen") as mock_popen:
            _play_blocking(missing, 0.8)  # must not raise

        mock_popen.assert_not_called()

    def test_silent_failure_on_popen_error(self, tmp_path):
        """Must not raise even if Popen itself throws."""
        audio = _make_audio(tmp_path, "test.mp3")

        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.Popen", side_effect=OSError("binary not found")),
        ):
            _play_blocking(str(audio), 0.8)  # must not raise

    def test_popen_called_with_devnull(self, tmp_path):
        """stdout and stderr should be suppressed (DEVNULL)."""
        import subprocess as sp

        audio = _make_audio(tmp_path, "test.mp3")

        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.Popen") as mock_popen,
        ):
            _play_blocking(str(audio), 0.8)

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("stdout") == sp.DEVNULL
        assert kwargs.get("stderr") == sp.DEVNULL


# ---------------------------------------------------------------------------
# play_event — high-level event dispatcher
# ---------------------------------------------------------------------------


class TestPlayEvent:
    def test_plays_exact_match(self, tmp_path):
        audio = _make_audio(tmp_path, "coding.mp3")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.CODING, str(tmp_path), volume=0.7)

        mock_enqueue.assert_called_once_with(str(audio), 0.7)

    def test_random_choice_among_variants(self, tmp_path):
        """When several files match, all of them must be in the candidate pool."""
        files = [
            _make_audio(tmp_path, "task_done.mp3"),
            _make_audio(tmp_path, "task_done_1.mp3"),
            _make_audio(tmp_path, "task_done_2.mp3"),
        ]

        chosen_paths: set[str] = set()
        # Run enough iterations to hit all three with overwhelming probability
        for _ in range(30):
            player_mod._last_played.clear()  # reset cooldown each iteration
            with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
                play_event(ChuuniEvent.TASK_DONE, str(tmp_path))
            chosen_paths.add(mock_enqueue.call_args[0][0])

        assert {Path(p).name for p in chosen_paths} == {
            "task_done.mp3",
            "task_done_1.mp3",
            "task_done_2.mp3",
        }

    def test_random_choice_is_uniform(self, tmp_path):
        """random.choice should receive the full candidate list."""
        _make_audio(tmp_path, "error.mp3")
        _make_audio(tmp_path, "error_alt.wav")

        with (
            patch("chuuni_voice.player._enqueue_task"),
            patch("chuuni_voice.player.random") as mock_random,
        ):
            mock_random.choice.return_value = Path(tmp_path / "error.mp3")
            play_event(ChuuniEvent.ERROR, str(tmp_path))

        candidates_passed = mock_random.choice.call_args[0][0]
        assert len(candidates_passed) == 2

    def test_silent_failure_when_no_audio_files(self, tmp_path):
        """Must not raise when the directory has no matching audio."""
        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.ERROR, str(tmp_path))  # must not raise

        mock_enqueue.assert_not_called()

    def test_silent_failure_when_directory_missing(self, tmp_path):
        """Must not raise when character_dir does not exist."""
        missing = str(tmp_path / "no_such_dir")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.THINKING, missing)  # must not raise

        mock_enqueue.assert_not_called()

    def test_does_not_match_other_events(self, tmp_path):
        """A file for CODING should not be played for BASH_RUN."""
        _make_audio(tmp_path, "coding.mp3")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.BASH_RUN, str(tmp_path))

        mock_enqueue.assert_not_called()

    def test_forwards_volume(self, tmp_path):
        _make_audio(tmp_path, "task_done.mp3")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.TASK_DONE, str(tmp_path), volume=0.3)

        assert mock_enqueue.call_args[0][1] == 0.3


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_second_call_within_cooldown_skipped(self, tmp_path):
        """Same event fired twice quickly: only the first should be enqueued."""
        _make_audio(tmp_path, "coding.mp3")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.CODING, str(tmp_path))
            play_event(ChuuniEvent.CODING, str(tmp_path))

        assert mock_enqueue.call_count == 1

    def test_different_events_have_independent_cooldowns(self, tmp_path):
        """Cooldown on one event must not suppress a different event."""
        _make_audio(tmp_path, "coding.mp3")
        _make_audio(tmp_path, "error.mp3")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            play_event(ChuuniEvent.CODING, str(tmp_path))
            play_event(ChuuniEvent.ERROR, str(tmp_path))

        assert mock_enqueue.call_count == 2

    def test_call_after_cooldown_expires_plays_again(self, tmp_path):
        """Once the cooldown window has passed, the event should fire again."""
        _make_audio(tmp_path, "coding.mp3")

        with patch("chuuni_voice.player._enqueue_task") as mock_enqueue:
            with patch("time.monotonic", return_value=1000.0):
                play_event(ChuuniEvent.CODING, str(tmp_path))
            # 4 s later → past the 3 s default cooldown
            with patch("time.monotonic", return_value=1004.0):
                play_event(ChuuniEvent.CODING, str(tmp_path))

        assert mock_enqueue.call_count == 2


# ---------------------------------------------------------------------------
# Playback queue
# ---------------------------------------------------------------------------


class TestPlaybackQueue:
    def test_queue_drops_oldest_when_over_capacity(self):
        """When the queue holds _QUEUE_MAX items, the oldest is dropped on add."""
        worker_blocked = threading.Event()
        unblock = threading.Event()

        def slow_play(path: str, volume: float) -> None:
            worker_blocked.set()
            unblock.wait(timeout=5)

        with patch.object(player_mod, "_play_blocking_wait", side_effect=slow_play):
            # First item: worker picks it up immediately and blocks inside slow_play
            _enqueue_task("/f1.mp3", 0.8)
            assert worker_blocked.wait(timeout=2), "worker did not pick up first item"

            # Fill the queue to capacity (3 pending items)
            _enqueue_task("/f2.mp3", 0.8)
            _enqueue_task("/f3.mp3", 0.8)
            _enqueue_task("/f4.mp3", 0.8)
            assert player_mod._play_queue.qsize() == 3

            # 4th pending item should evict the oldest (f2)
            _enqueue_task("/f5.mp3", 0.8)
            assert player_mod._play_queue.qsize() == 3

            contents = list(player_mod._play_queue.queue)  # peek the internal deque
            paths = [p for p, _ in contents]
            assert "/f2.mp3" not in paths, "oldest item should have been dropped"
            assert "/f5.mp3" in paths, "newest item should be in the queue"

            unblock.set()  # release the worker

    def test_enqueue_task_does_not_exceed_max(self):
        """Calling _enqueue_task many times must never grow queue beyond _QUEUE_MAX."""
        worker_blocked = threading.Event()
        unblock = threading.Event()

        def slow_play(path: str, volume: float) -> None:
            worker_blocked.set()
            unblock.wait(timeout=5)

        with patch.object(player_mod, "_play_blocking_wait", side_effect=slow_play):
            _enqueue_task("/first.mp3", 0.8)
            assert worker_blocked.wait(timeout=2)

            for i in range(10):
                _enqueue_task(f"/item{i}.mp3", 0.8)

            assert player_mod._play_queue.qsize() <= player_mod._QUEUE_MAX

            unblock.set()


# ---------------------------------------------------------------------------
# _find_candidates — file discovery
# ---------------------------------------------------------------------------


class TestFindCandidates:
    def test_exact_match(self, tmp_path):
        _make_audio(tmp_path, "coding.mp3")
        result = _find_candidates(ChuuniEvent.CODING, tmp_path)
        assert [f.name for f in result] == ["coding.mp3"]

    def test_numbered_variants(self, tmp_path):
        _make_audio(tmp_path, "task_done.mp3")
        _make_audio(tmp_path, "task_done_1.mp3")
        _make_audio(tmp_path, "task_done_2.wav")
        result = _find_candidates(ChuuniEvent.TASK_DONE, tmp_path)
        names = {f.name for f in result}
        assert names == {"task_done.mp3", "task_done_1.mp3", "task_done_2.wav"}

    def test_extension_priority_order(self, tmp_path):
        """mp3 should appear before wav in results."""
        _make_audio(tmp_path, "error.wav")
        _make_audio(tmp_path, "error.mp3")
        result = _find_candidates(ChuuniEvent.ERROR, tmp_path)
        assert result[0].suffix == ".mp3"
        assert result[1].suffix == ".wav"

    def test_no_cross_event_match(self, tmp_path):
        _make_audio(tmp_path, "coding.mp3")
        result = _find_candidates(ChuuniEvent.BASH_RUN, tmp_path)
        assert result == []

    def test_empty_directory(self, tmp_path):
        result = _find_candidates(ChuuniEvent.ERROR, tmp_path)
        assert result == []

    def test_missing_directory(self, tmp_path):
        result = _find_candidates(ChuuniEvent.ERROR, tmp_path / "nope")
        assert result == []

    def test_no_duplicates(self, tmp_path):
        _make_audio(tmp_path, "thinking.mp3")
        result = _find_candidates(ChuuniEvent.THINKING, tmp_path)
        assert len(result) == len(set(result))

    def test_all_supported_extensions(self, tmp_path):
        for ext in [".mp3", ".wav", ".ogg", ".aiff", ".flac"]:
            _make_audio(tmp_path, f"bash_run{ext}")
        result = _find_candidates(ChuuniEvent.BASH_RUN, tmp_path)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# _build_command — platform dispatch
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_mac_returns_afplay(self, tmp_path):
        audio = tmp_path / "test.mp3"
        with patch("platform.system", return_value="Darwin"):
            cmd = _build_command(audio, 0.8)
        assert cmd is not None
        assert cmd[0] == "afplay"
        assert "-v" in cmd

    def test_linux_prefers_paplay(self, tmp_path):
        audio = tmp_path / "test.mp3"

        def which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name == "paplay" else None

        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", side_effect=which),
        ):
            cmd = _build_command(audio, 0.8)

        assert cmd is not None
        assert cmd[0] == "paplay"

    def test_linux_falls_back_to_aplay(self, tmp_path):
        audio = tmp_path / "test.wav"

        def which(name: str) -> str | None:
            return "/usr/bin/aplay" if name == "aplay" else None

        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", side_effect=which),
        ):
            cmd = _build_command(audio, 0.8)

        assert cmd is not None
        assert cmd[0] == "aplay"

    def test_linux_falls_back_to_mpg123(self, tmp_path):
        audio = tmp_path / "test.mp3"

        def which(name: str) -> str | None:
            return "/usr/bin/mpg123" if name == "mpg123" else None

        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", side_effect=which),
        ):
            cmd = _build_command(audio, 0.8)

        assert cmd is not None
        assert cmd[0] == "mpg123"

    def test_linux_returns_none_when_no_player(self, tmp_path):
        audio = tmp_path / "test.mp3"
        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", return_value=None),
        ):
            cmd = _build_command(audio, 0.8)
        assert cmd is None

    def test_unsupported_platform_returns_none(self, tmp_path):
        audio = tmp_path / "test.mp3"
        with patch("platform.system", return_value="Windows"):
            cmd = _build_command(audio, 0.8)
        assert cmd is None


# ---------------------------------------------------------------------------
# _mac_command / _linux_command — unit-level
# ---------------------------------------------------------------------------


class TestMacCommand:
    def test_structure(self, tmp_path):
        audio = tmp_path / "test.mp3"
        cmd = _mac_command(audio, 0.75)
        assert cmd == ["afplay", "-v", "0.75", str(audio)]


class TestLinuxCommand:
    def test_paplay_volume_conversion(self, tmp_path):
        """paplay volume should be an integer in 0–65536."""
        audio = tmp_path / "test.mp3"
        with patch("shutil.which", side_effect=lambda n: "/usr/bin/paplay" if n == "paplay" else None):
            cmd = _linux_command(audio, 0.8)
        assert cmd is not None
        assert cmd[0] == "paplay"
        vol_arg = next(a for a in cmd if a.startswith("--volume="))
        vol = int(vol_arg.split("=")[1])
        assert 0 <= vol <= 65536

    def test_paplay_volume_capped_at_norm(self, tmp_path):
        """Volume 1.0 should not exceed 65536."""
        audio = tmp_path / "test.mp3"
        with patch("shutil.which", side_effect=lambda n: "/usr/bin/paplay" if n == "paplay" else None):
            cmd = _linux_command(audio, 1.0)
        vol_arg = next(a for a in cmd if a.startswith("--volume="))
        assert int(vol_arg.split("=")[1]) == 65536
