"""RVC WebUI HTTP client and playback helper.

Strategy:
  - RVCClient wraps the Gradio /run/predict endpoint exposed by a local
    RVC WebUI instance (https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI).
  - convert_for_playback() provides a fallback-safe wrapper: if the server
    is unavailable or conversion fails, it returns the original source audio.
  - Nothing in this module raises to the caller.
"""

import logging
import shutil
from pathlib import Path

import requests

from chuuni_voice.events import ChuuniEvent

log = logging.getLogger(__name__)

# Audio extensions searched in priority order (mirrors player.py)
_AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".aiff", ".flac"]


# ---------------------------------------------------------------------------
# RVCClient
# ---------------------------------------------------------------------------


class RVCClient:
    """Thin HTTP client for a running RVC WebUI / Gradio server.

    Args:
        host:     Server hostname or IP.  Default: 127.0.0.1
        port:     Server port.           Default: 7865
        timeout:  HTTP timeout (seconds) for convert calls.
        fn_index: Gradio function index for the vc_single endpoint.
                  The value depends on the specific RVC fork and build;
                  0 is the common default.  Override if your build differs.
    """

    _PREDICT_PATH = "/run/predict"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7865,
        *,
        timeout: int = 30,
        fn_index: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.fn_index = fn_index
        self._base = f"http://{host}:{port}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the RVC WebUI server responds to a GET /.

        Uses a short 3-second timeout so callers don't stall.
        """
        try:
            requests.get(self._base + "/", timeout=3)
            return True
        except Exception:
            return False

    def convert(
        self,
        input_path: str,
        output_path: str,
        model_name: str,
        index_path: str = "",
    ) -> bool:
        """Convert *input_path* with the RVC model and write to *output_path*.

        Calls the Gradio /run/predict endpoint synchronously.
        If the server returns a file path in the response, copies it to
        *output_path*; otherwise the caller is expected to detect the result
        by checking whether *output_path* exists after this call.

        Returns True if the server reported success, False on any failure.
        Never raises.
        """
        try:
            payload = self._build_payload(input_path, model_name, index_path)
            r = requests.post(
                self._base + self._PREDICT_PATH,
                json=payload,
                timeout=self.timeout,
            )

            if not r.ok:
                log.debug("convert: server returned HTTP %d", r.status_code)
                return False

            body = r.json()

            if "error" in body:
                log.debug("convert: API error: %s", body["error"])
                return False

            # Some RVC builds return the output path as the first data element.
            # Copy it to the caller's desired output_path when present.
            data = body.get("data") or []
            if data and isinstance(data[0], str):
                server_output = Path(data[0])
                if server_output.exists() and str(server_output) != output_path:
                    shutil.copy2(server_output, output_path)

            return True

        except Exception as exc:
            log.debug("convert: unexpected error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_payload(
        self, input_path: str, model_name: str, index_path: str
    ) -> dict:
        """Build the Gradio /run/predict JSON payload for vc_single.

        Parameter order matches the standard RVC WebUI vc_single function:
          0  spk_id        (int)   speaker id
          1  input_path    (str)
          2  f0_up_key     (int)   pitch shift in semitones
          3  f0_file       (str)   optional f0 curve file
          4  f0_method     (str)   rmvpe / harvest / pm / crepe
          5  file_index    (str)   .index file path
          6  index_rate    (float) feature retrieval rate
          7  filter_radius (int)   median filter radius
          8  resample_sr   (int)   output sample rate (0 = keep original)
          9  rms_mix_rate  (float) loudness envelope mix
          10 protect       (float) voiceless consonant protection
          11 model_name    (str)   .pth filename (some forks expect this)
        """
        return {
            "fn_index": self.fn_index,
            "data": [
                0,           # spk_id
                input_path,
                0,           # f0_up_key (pitch shift)
                "",          # f0_file
                "rmvpe",     # f0_method
                index_path,
                0.75,        # index_rate
                3,           # filter_radius
                0,           # resample_sr
                0.25,        # rms_mix_rate
                0.33,        # protect
                model_name,
            ],
        }

    def __repr__(self) -> str:
        return f"RVCClient(host={self.host!r}, port={self.port})"


# ---------------------------------------------------------------------------
# convert_for_playback
# ---------------------------------------------------------------------------


def convert_for_playback(
    event: ChuuniEvent,
    character_dir: str,
    client: RVCClient,
    model_name: str,
    *,
    index_path: str = "",
) -> str | None:
    """Find source audio and convert it via RVC; fall back to source on failure.

    Directory layout expected inside *character_dir*::

        character_dir/
          source/       ← original (non-converted) audio files
            coding.mp3
            coding_alt.mp3
          converted/    ← created automatically; converted files land here

    Returns:
        Path to the file to play (converted or source), or None if no source
        audio exists for this event.

    Never raises.
    """
    try:
        char_path = Path(character_dir)
        source_dir = char_path / "source"
        converted_dir = char_path / "converted"

        source = _find_source(event, source_dir)
        if source is None:
            log.debug(
                "convert_for_playback: no source audio for event=%s in %s",
                event.value,
                source_dir,
            )
            return None

        # Fast path: RVC server is not up, use source directly
        if not client.is_available():
            log.debug(
                "convert_for_playback: RVC unavailable, using source %s", source.name
            )
            return str(source)

        # Convert
        converted_dir.mkdir(parents=True, exist_ok=True)
        output = converted_dir / source.name

        success = client.convert(str(source), str(output), model_name, index_path)

        if success and output.exists():
            log.debug("convert_for_playback: returning converted %s", output.name)
            return str(output)

        log.debug(
            "convert_for_playback: conversion failed or output missing, "
            "falling back to source %s",
            source.name,
        )
        return str(source)

    except Exception as exc:
        log.debug("convert_for_playback: unexpected error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_source(event: ChuuniEvent, source_dir: Path) -> Path | None:
    """Return a randomly-chosen source audio file for *event*, or None.

    Matches the same patterns as player._find_candidates:
      - exact:    <event_value>.<ext>
      - variants: <event_value>_*.<ext>
    """
    import random

    if not source_dir.is_dir():
        return None

    stem = event.value
    candidates: list[Path] = []
    seen: set[Path] = set()

    for ext in _AUDIO_EXTS:
        exact = source_dir / f"{stem}{ext}"
        if exact.exists() and exact not in seen:
            candidates.append(exact)
            seen.add(exact)
        for f in sorted(source_dir.glob(f"{stem}_*{ext}")):
            if f not in seen:
                candidates.append(f)
                seen.add(f)

    return random.choice(candidates) if candidates else None
