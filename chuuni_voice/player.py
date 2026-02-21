"""Audio playback for chuuni-voice.

Design goals:
- Never crash the caller.  Every public function catches all exceptions.
- Non-blocking: play_file() hands off to a background thread; the caller
  returns immediately.
- Cooldown: each event has an independent per-event cooldown (default 3 s).
  A second trigger within the window is silently dropped.
  State is persisted to ~/.config/chuuni/cooldown.json and protected by a
  file lock so cooldown works correctly across concurrent chuuni processes.
- Serial queue: play_event() feeds a single background worker so sounds from
  different events are played one after another without overlap.  The queue
  holds at most 3 pending items; the oldest is dropped when it overflows.
- Graceful degrade: no audio file → debug log.  No player binary → debug log.
"""

import json
import logging
import platform
import queue
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

from filelock import FileLock

from chuuni_voice.events import ChuuniEvent

log = logging.getLogger(__name__)

# Searched in this priority order for each event stem
_AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".aiff", ".flac"]

# Linux player fallback chain.
# Each entry: (binary_name, args_builder(path, volume) -> list[str])
_LINUX_PLAYERS: list[tuple[str, object]] = [
    (
        "paplay",
        # PA_VOLUME_NORM = 65536; clamp to 65536 (no amplification)
        lambda path, vol: [f"--volume={min(int(vol * 65536), 65536)}", str(path)],
    ),
    (
        "aplay",
        # aplay has no inline volume flag; system volume applies
        lambda path, vol: ["-q", str(path)],
    ),
    (
        "mpg123",
        lambda path, vol: ["-q", str(path)],
    ),
]


# ---------------------------------------------------------------------------
# Cross-process cooldown state (file-backed)
# ---------------------------------------------------------------------------

COOLDOWN_DIR = Path.home() / ".config" / "chuuni"
COOLDOWN_FILE = COOLDOWN_DIR / "cooldown.json"
COOLDOWN_LOCK_FILE = COOLDOWN_DIR / "cooldown.lock"


def _check_and_claim_cooldown(event_value: str, cooldown: float) -> bool:
    """Return True (and record the play time) if the event may proceed.

    Acquires a file lock before reading/writing so this is safe across
    concurrent ``chuuni play`` processes.  Falls open (returns True) if the
    lock cannot be acquired within 1 s.
    """
    COOLDOWN_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(COOLDOWN_LOCK_FILE), timeout=1)
    try:
        with lock:
            data: dict[str, float] = {}
            if COOLDOWN_FILE.exists():
                try:
                    data = json.loads(COOLDOWN_FILE.read_text())
                except (json.JSONDecodeError, OSError):
                    data = {}

            now = time.time()
            last = data.get(event_value, 0.0)
            if now - last < cooldown:
                log.debug(
                    "play_event: cooldown active for %s (%.1fs remaining), skipping",
                    event_value,
                    cooldown - (now - last),
                )
                return False

            data[event_value] = now
            COOLDOWN_FILE.write_text(json.dumps(data))
            return True
    except Exception as exc:
        log.debug("_check_and_claim_cooldown: lock error: %s — proceeding anyway", exc)
        return True


# ---------------------------------------------------------------------------
# Playback queue
# ---------------------------------------------------------------------------

_QUEUE_MAX = 3
_play_queue: queue.Queue[tuple[str, float]] = queue.Queue()


def _queue_worker() -> None:
    """Daemon worker: consume (path, volume) tasks and play them serially."""
    while True:
        path, volume = _play_queue.get()
        _play_blocking_wait(path, volume)
        _play_queue.task_done()


_worker_thread = threading.Thread(
    target=_queue_worker, daemon=True, name="chuuni-audio-worker"
)
_worker_thread.start()


def _enqueue_task(path: str, volume: float) -> None:
    """Put (path, volume) in the playback queue.

    If the queue already holds *_QUEUE_MAX* pending items, the oldest one is
    dropped first to prevent unbounded accumulation.
    """
    while _play_queue.qsize() >= _QUEUE_MAX:
        try:
            _play_queue.get_nowait()
            _play_queue.task_done()
        except queue.Empty:
            break
    _play_queue.put_nowait((path, volume))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def play_file(path: str, volume: float = 0.8) -> None:
    """Play *path* in a background daemon thread (fire-and-forget).

    Returns immediately.  Never raises.

    Args:
        path:   Absolute or relative path to the audio file.
        volume: Playback volume in [0.0, 1.0].  Clamped silently.
    """
    volume = max(0.0, min(1.0, volume))
    t = threading.Thread(
        target=_play_blocking,
        args=(path, volume),
        daemon=True,
    )
    t.start()


def play_event(
    event: ChuuniEvent,
    character_dir: str | None = None,
    volume: float = 0.8,
) -> None:
    """Find and play an audio clip for *event* via the serial playback queue.

    Cooldown check (per-event from config):
      If the same event was played within its cooldown window, the call is
      silently dropped.  Cooldown state is shared across processes via a
      file lock at ~/.config/chuuni/cooldown.json.

    Filename matching rules (case-insensitive stem):
      - Exact:    ``<event_value>.<ext>``          e.g. ``coding.mp3``
      - Variants: ``<event_value>_*.<ext>``        e.g. ``coding_1.mp3``, ``coding_alt.wav``

    When multiple files match, one is chosen at random.
    When *character_dir* is None, reads ``active_character`` from config and
    derives the directory automatically.
    Silent failure on missing directory, missing files, or playback errors.
    """
    try:
        from chuuni_voice.config import get_cooldowns, load_config, get_character_dir
        cfg = load_config()
        cooldowns = get_cooldowns(cfg)
        cooldown = cooldowns.get(event.value, float(cfg.get("cooldown_seconds", 5.0)))

        if not _check_and_claim_cooldown(event.value, cooldown):
            return

        if character_dir is None:
            character_dir = str(get_character_dir(cfg))

        candidates = _find_candidates(event, Path(character_dir))
        if not candidates:
            log.debug(
                "play_event: no audio for event=%s in %r", event.value, character_dir
            )
            return
        chosen = random.choice(candidates)
        log.debug("play_event: queuing %s for event=%s", chosen.name, event.value)
        _enqueue_task(str(chosen), volume)
    except Exception as exc:
        log.debug("play_event: unexpected error: %s", exc)


# ---------------------------------------------------------------------------
# Internal: blocking launch (runs inside thread or queue worker)
# ---------------------------------------------------------------------------


def _play_blocking(path: str, volume: float) -> None:
    """Resolve the player command and launch it (fire-and-forget).  Must not raise."""
    try:
        p = Path(path)
        if not p.exists():
            log.debug("_play_blocking: file not found: %s", path)
            return

        cmd = _build_command(p, volume)
        if cmd is None:
            log.debug("_play_blocking: no suitable player found on this platform")
            return

        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.debug("_play_blocking: error launching player: %s", exc)


def _play_blocking_wait(path: str, volume: float) -> None:
    """Like _play_blocking but waits for the process to finish.

    Used by the queue worker to serialise playback: the worker must not pick
    up the next item until the current audio has ended.
    """
    try:
        p = Path(path)
        if not p.exists():
            log.debug("_play_blocking_wait: file not found: %s", path)
            return

        cmd = _build_command(p, volume)
        if cmd is None:
            log.debug("_play_blocking_wait: no suitable player found on this platform")
            return

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
    except Exception as exc:
        log.debug("_play_blocking_wait: error: %s", exc)


def _build_command(path: Path, volume: float) -> list[str] | None:
    """Return the OS-appropriate player argv, or None if unavailable."""
    system = platform.system()

    if system == "Darwin":
        return _mac_command(path, volume)
    if system == "Linux":
        return _linux_command(path, volume)

    log.debug("_build_command: unsupported platform %r", system)
    return None


def _mac_command(path: Path, volume: float) -> list[str]:
    """afplay -v <float> <file>  (volume 0.0–1.0 maps to afplay's 0.0–1.0)."""
    return ["afplay", "-v", str(volume), str(path)]


def _linux_command(path: Path, volume: float) -> list[str] | None:
    """Try paplay → aplay → mpg123 in order; return first available."""
    for binary, args_fn in _LINUX_PLAYERS:
        if shutil.which(binary) is not None:
            return [binary] + args_fn(path, volume)
    log.debug("_linux_command: tried paplay, aplay, mpg123 — none found in PATH")
    return None


# ---------------------------------------------------------------------------
# Internal: candidate discovery
# ---------------------------------------------------------------------------


def _find_candidates(event: ChuuniEvent, character_dir: Path) -> list[Path]:
    """Return all audio files in *character_dir* that match *event*.

    Search order (highest priority first — first non-empty tier wins):

      1. ``日语_<charname>_<event>.<ext>``
         e.g. ``日语_genki-girl_coding.mp3``

      2. ``japanese_*<charname>_<event>.<ext>``
         e.g. ``japanese_Amazing-anime-girl-character_genki-girl_task_done.mp3``
         The wildcard absorbs any description text before <charname>, so both
         ``_<charname>_`` and ``-<charname>_`` separators are matched.
         Multiple files for the same event are all returned (random choice upstream).

      3. Generic: ``<event>.<ext>``  and  ``<event>_*.<ext>``
         e.g. ``coding.mp3``, ``coding_1.mp3``

    Within each tier, extension priority follows ``_AUDIO_EXTS`` order.
    """
    if not character_dir.is_dir():
        return []

    stem = event.value              # e.g. "task_done"
    char_name = character_dir.name  # e.g. "genki-girl"
    seen: set[Path] = set()

    # ── Priority 1: 日语_<char>_<event>.<ext> ─────────────────────────────
    p1: list[Path] = []
    for ext in _AUDIO_EXTS:
        candidate = character_dir / f"日语_{char_name}_{stem}{ext}"
        if candidate.exists() and candidate not in seen:
            p1.append(candidate)
            seen.add(candidate)
    if p1:
        return p1

    # ── Priority 2: japanese_*<char>_<event>.<ext> ────────────────────────
    # Glob without an explicit separator before <char_name> so that both
    # "..._genki-girl_" and "...-genki-girl_" variants are matched.
    p2: list[Path] = []
    for ext in _AUDIO_EXTS:
        for f in sorted(character_dir.glob(f"japanese_*{char_name}_{stem}{ext}")):
            if f not in seen:
                p2.append(f)
                seen.add(f)
    if p2:
        return p2

    # ── Priority 3: generic <event>.<ext> and <event>_*.<ext> ─────────────
    p3: list[Path] = []
    for ext in _AUDIO_EXTS:
        exact = character_dir / f"{stem}{ext}"
        if exact.exists() and exact not in seen:
            p3.append(exact)
            seen.add(exact)
        for f in sorted(character_dir.glob(f"{stem}_*{ext}")):
            if f not in seen:
                p3.append(f)
                seen.add(f)

    return p3
