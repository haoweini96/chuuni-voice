"""Persistent audio daemon for chuuni-voice.

The daemon runs as a background process, listens on a Unix domain socket, and
serialises all audio playback through a single worker thread so sounds never
overlap regardless of how many concurrent ``chuuni play`` processes fire.

Protocol
--------
All messages are newline-delimited JSON over the Unix socket.

Client → daemon::

    {"type": "play", "event": "<event_value>", "audio_path": "<path>", "volume": <float>}
    {"type": "status"}
    {"type": "stop"}

Daemon → client::

    {"ok": true}                              — accepted / done
    {"ok": false, "reason": "<str>"}          — rejected (e.g. cooldown)
    {"ok": true, "queue_size": <int>}         — status response
"""

import json
import logging
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

_main_thread = threading.main_thread()

log = logging.getLogger(__name__)

CHUUNI_DIR = Path.home() / ".config" / "chuuni"
SOCKET_PATH = CHUUNI_DIR / "chuuni.sock"
PID_FILE = CHUUNI_DIR / "chuuni.pid"
LOG_FILE = CHUUNI_DIR / "daemon.log"

_QUEUE_MAX = 8
_CLIENT_TIMEOUT = 0.5


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class AudioDaemon:
    """Unix socket server that serialises audio playback in-process."""

    def __init__(
        self,
        cooldowns: dict[str, float] | None = None,
        default_cooldown: float = 5.0,
    ) -> None:
        self._cooldowns: dict[str, float] = cooldowns or {}
        self._default_cooldown = default_cooldown
        self._last_played: dict[str, float] = {}
        self._last_lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, float]] = queue.Queue()
        self._running = False
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="chuuni-daemon-worker"
        )

    # ------------------------------------------------------------------
    # Public entry point

    def run(self) -> None:
        """Main blocking server loop. Returns when stop is requested."""
        CHUUNI_DIR.mkdir(parents=True, exist_ok=True)

        # Remove stale socket from a crashed previous run
        SOCKET_PATH.unlink(missing_ok=True)

        # Record PID so external tools can identify the process
        PID_FILE.write_text(str(os.getpid()))

        self._worker.start()
        self._running = True

        def _handle_signal(sig: int, _frame: object) -> None:
            log.info("daemon: received signal %d — shutting down", sig)
            self._running = False

        # signal.signal() is only allowed on the main thread
        if threading.current_thread() is _main_thread:
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(SOCKET_PATH))
            server.listen(16)
            server.settimeout(1.0)  # allows periodic _running checks
            log.info("daemon: listening on %s (pid=%d)", SOCKET_PATH, os.getpid())

            while self._running:
                try:
                    conn, _ = server.accept()
                    threading.Thread(
                        target=self._handle_conn, args=(conn,), daemon=True
                    ).start()
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            self._cleanup(server)

    # ------------------------------------------------------------------
    # Connection handler (runs in its own thread per client)

    def _handle_conn(self, conn: socket.socket) -> None:
        """Read one request from *conn*, dispatch, write response."""
        try:
            with conn:
                conn.settimeout(0.5)
                data = b""
                try:
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                        if b"\n" in data:
                            break
                except socket.timeout:
                    pass

                if not data.strip():
                    return

                try:
                    msg: dict = json.loads(data.decode().strip())
                except Exception:
                    conn.sendall(
                        json.dumps({"ok": False, "reason": "bad JSON"}).encode() + b"\n"
                    )
                    return

                resp = self._dispatch(msg)
                conn.sendall(json.dumps(resp).encode() + b"\n")
        except Exception as exc:
            log.debug("daemon: _handle_conn error: %s", exc)

    # ------------------------------------------------------------------
    # Request routing

    def _dispatch(self, msg: dict) -> dict:
        msg_type = msg.get("type")
        if msg_type == "play":
            return self._handle_play(msg)
        if msg_type == "status":
            return {
                "ok": True,
                "queue_size": self._queue.qsize(),
            }
        if msg_type == "stop":
            self._running = False
            return {"ok": True}
        return {"ok": False, "reason": f"unknown type: {msg_type!r}"}

    def _handle_play(self, msg: dict) -> dict:
        """Apply per-event cooldown check, then enqueue."""
        event = str(msg.get("event", ""))
        audio_path = str(msg.get("audio_path", ""))
        volume = float(msg.get("volume", 0.8))

        cooldown = self._cooldowns.get(event, self._default_cooldown)
        now = time.time()
        with self._last_lock:
            last = self._last_played.get(event, 0.0)
            if now - last < cooldown:
                remaining = cooldown - (now - last)
                log.debug(
                    "daemon: cooldown active for %s (%.1fs remaining)", event, remaining
                )
                return {"ok": False, "reason": "cooldown"}

            self._last_played[event] = now

        if audio_path:
            # Drop oldest pending item if queue is full
            while self._queue.qsize() >= _QUEUE_MAX:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            self._queue.put_nowait((audio_path, volume))
            log.debug("daemon: enqueued %s (event=%s)", audio_path, event)

        return {"ok": True}

    # ------------------------------------------------------------------
    # Worker thread

    def _worker_loop(self) -> None:
        """Consume (path, volume) items from the queue and play serially."""
        while True:
            path, volume = self._queue.get()
            try:
                _play_audio(path, volume)
            except Exception as exc:
                log.debug("daemon: worker error: %s", exc)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Cleanup

    def _cleanup(self, server: socket.socket) -> None:
        try:
            server.close()
        except Exception:
            pass
        for path in (SOCKET_PATH, PID_FILE):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        log.info("daemon: cleaned up and exiting")


# ---------------------------------------------------------------------------
# Client API
# ---------------------------------------------------------------------------


def is_running() -> bool:
    """Return True if the daemon socket exists and the daemon responds."""
    if not SOCKET_PATH.exists():
        return False
    resp = _send({"type": "status"})
    return resp is not None and bool(resp.get("ok"))


def send_play(event: str, audio_path: str, volume: float) -> dict | None:
    """Ask the daemon to play *audio_path* for *event*.

    Returns the daemon's response dict, or None if the daemon is not running.
    """
    return _send(
        {"type": "play", "event": event, "audio_path": audio_path, "volume": volume}
    )


def send_status() -> dict | None:
    """Return the daemon's status dict, or None if unreachable."""
    return _send({"type": "status"})


def send_stop() -> dict | None:
    """Tell the daemon to shut down gracefully."""
    return _send({"type": "stop"})


def _send(msg: dict, timeout: float = _CLIENT_TIMEOUT) -> dict | None:
    """Send *msg* to the daemon and return the parsed JSON response.

    Returns None on any error (connection refused, timeout, bad JSON, etc.).
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(json.dumps(msg).encode() + b"\n")
        data = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        if data.strip():
            return json.loads(data.decode().strip())
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Standalone audio playback (called exclusively from the daemon worker thread)
# ---------------------------------------------------------------------------


def _play_audio(path: str, volume: float) -> None:
    """Blocking audio playback via the system player."""
    from chuuni_voice.player import _build_command

    try:
        p = Path(path)
        if not p.exists():
            log.debug("_play_audio: file not found: %s", path)
            return
        cmd = _build_command(p, volume)
        if cmd is None:
            log.debug("_play_audio: no suitable player found")
            return
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
    except Exception as exc:
        log.debug("_play_audio: error: %s", exc)
