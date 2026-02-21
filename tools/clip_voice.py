#!/usr/bin/env python3
"""Extract voice clips from YouTube videos for chuuni-voice characters.

Usage:
    python tools/clip_voice.py "https://youtube.com/watch?v=xxx"

Pipeline:
    1. yt-dlp              → download audio
    2. ElevenLabs API      → isolate vocals (remove BGM)
    3. OpenAI Whisper API  → transcribe with timestamps + detect language
    4. Claude API          → translate (Chinese + English) + recommend events
    5. interactive         → user selects lines, confirms events, picks character
    6. ffmpeg              → clip & save to ~/.config/chuuni/characters/<char>/

Dependencies:
    pip install yt-dlp openai anthropic requests python-dotenv
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CHARACTERS_DIR = Path.home() / ".config" / "chuuni" / "characters"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
SEGMENT_PAD = 0.3  # seconds of padding around each clip

ELEVENLABS_ISOLATION_URL = "https://api.elevenlabs.io/v1/audio-isolation"

VALID_EVENTS = [
    "task_start", "task_done", "coding", "bash_run",
    "test_pass", "test_fail", "error", "permission_prompt",
]


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/clip_voice.py <youtube-url>")
        sys.exit(1)

    url = sys.argv[1]

    with tempfile.TemporaryDirectory(prefix="chuuni_clip_", delete=False) as tmpdir:
        tmp = Path(tmpdir)
        print(f"\n   Temp dir: {tmp}")

        # ── 1. Download ──────────────────────────────────────────────────
        print("\n── 1/5  yt-dlp: downloading audio...")
        raw_audio = tmp / "audio.wav"
        subprocess.run(
            [
                "yt-dlp",
                "-x",
                "--audio-format", "wav",
                "-o", str(tmp / "audio.%(ext)s"),
                url,
            ],
            check=True,
        )
        if not raw_audio.exists():
            wavs = list(tmp.glob("audio.*"))
            if not wavs:
                print("Error: yt-dlp did not produce an audio file.")
                sys.exit(1)
            raw_audio = wavs[0]
        print(f"   → {raw_audio.name}  ({raw_audio.stat().st_size / 1024 / 1024:.1f} MB)")

        # ── 2. Vocal isolation (ElevenLabs) ──────────────────────────────
        vocals = _isolate_vocals(raw_audio, tmp)
        print(f"   → {vocals.name}  ({vocals.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"   Preview:  afplay {vocals}")

        # ── 3. Transcription (OpenAI Whisper API) ────────────────────────
        # Transcribe the ORIGINAL audio (accurate timestamps).
        # If hallucination detected, retry with vocals as fallback.
        print("\n── 3/5  OpenAI Whisper API: transcribing...")
        segments, language = _transcribe_whisper_api(
            raw_audio,
            fallback_path=vocals if vocals != raw_audio else None,
        )

        if not segments:
            print("Error: Whisper API produced no segments.")
            sys.exit(1)
        print(f"   → {len(segments)} segments, language: {language}")

        # Auto-trim leading silence from each segment
        print("   Detecting voice boundaries...")
        for seg in segments:
            voice_start = _detect_voice_start(raw_audio, seg["start"], seg["end"])
            if voice_start > seg["start"]:
                trimmed = voice_start - seg["start"]
                print(f"   [{_fmt_ts(seg['start'], seg['end'])}] "
                      f"trimmed {trimmed:.1f}s silence → starts at {voice_start:.1f}s")
                seg["original_start"] = seg["start"]
                seg["start"] = voice_start

        # Confidence warning: check if segment count vs duration looks off
        audio_duration = segments[-1]["end"] if segments else 0
        avg_seg_duration = audio_duration / len(segments) if segments else 0
        if avg_seg_duration > 15 and audio_duration > 5:
            print(f"\n   ⚠️  识别结果可能不准确")
            print(f"      {audio_duration:.0f}秒音频只有{len(segments)}个片段"
                  f"（平均{avg_seg_duration:.0f}秒/段）")
            print(f"      请确认后再继续")

        # Quick preview: translate to Chinese so user can verify
        quick_zh = _quick_translate(segments)
        print(f"\n   识别结果：")
        for i, seg in enumerate(segments):
            ts = _fmt_ts(seg["start"], seg["end"])
            text = seg["text"].strip()
            zh = quick_zh[i] if i < len(quick_zh) else ""
            print(f"   [{ts}] {text}")
            if zh:
                print(f"              → 中文：{zh}")
        print(f"\n   (如果识别不对，Ctrl+C 退出换一个视频)")

        # ── 4. Claude: translate + recommend events ──────────────────────
        print(f"\n── 4/5  Claude ({CLAUDE_MODEL}): analyzing...")
        analysis = _analyze_segments(segments, language)

        # ── 5. Display + Select ──────────────────────────────────────────
        _display_segments(segments, analysis, language)

        selection = input("Enter clip numbers (e.g. 1,3,5  1-3  all): ").strip()
        if not selection:
            print("No selection — exiting.")
            return

        indices = _parse_selection(selection, len(segments))
        if not indices:
            print("No valid numbers — exiting.")
            return

        # ── 5b. Confirm events + adjust timestamps ──────────────────────
        print("\n── Confirm events & timestamps ─────────────────────────\n")
        for idx in indices:
            seg = segments[idx]
            info = analysis[idx] if idx < len(analysis) else {}
            suggested = info.get("event", "")
            en = info.get("en", seg["text"].strip())
            orig_start = seg.get("original_start")

            print(f"  #{idx + 1}  {seg['text'].strip()}")
            print(f"        en: {en}")
            ts = _fmt_ts(seg["start"], seg["end"])
            if orig_start is not None:
                print(f"        Time: {ts}  (was {orig_start:.1f}s, trimmed to {seg['start']:.1f}s)")
            else:
                print(f"        Time: {ts}")

            # Adjust start time
            adj = input(f"        Start [{seg['start']:.1f}s]: ").strip()
            if adj:
                try:
                    new_start = float(adj)
                    if 0 <= new_start < seg["end"]:
                        seg["start"] = new_start
                        print(f"        → start adjusted to {new_start:.1f}s")
                    else:
                        print(f"        Invalid time, keeping {seg['start']:.1f}s")
                except ValueError:
                    print(f"        Invalid input, keeping {seg['start']:.1f}s")

            # Confirm event
            print(f"        Suggested: {suggested}")
            override = input(f"        Event [{suggested}]: ").strip()
            if override and override in VALID_EVENTS:
                info["event"] = override
            elif override:
                print(f"        Invalid event, keeping: {suggested}")
            print()

        # ── 5c. Pick character ───────────────────────────────────────────
        character = _pick_character()
        if not character:
            print("No character selected — exiting.")
            return

        # ── 6. Clip & save ───────────────────────────────────────────────
        char_dir = CHARACTERS_DIR / character
        char_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n── 5/5  ffmpeg: clipping {len(indices)} segment(s)...\n")

        for idx in indices:
            seg = segments[idx]
            info = analysis[idx] if idx < len(analysis) else {}
            event = info.get("event", "unknown") or "unknown"
            en = info.get("en", "clip")

            out_file = _save_clip(
                audio_path=vocals,
                start=seg["start"],
                end=seg["end"],
                language=language,
                en_translation=en,
                character=character,
                event=event,
                char_dir=char_dir,
            )
            if out_file:
                ts = _fmt_ts(seg["start"], seg["end"])
                print(f"   ✓  {out_file.name}")
                print(f"      [{ts}]  {seg['text'].strip()}")
                print(f"      afplay {out_file}\n")

        print(f"Saved to {char_dir}/")


# ---------------------------------------------------------------------------
# ElevenLabs vocal isolation
# ---------------------------------------------------------------------------


def _isolate_vocals(audio_path: Path, tmp: Path) -> Path:
    """Remove background music via ElevenLabs Audio Isolation API."""
    print("\n── 2/5  ElevenLabs: isolating vocals...")

    api_key = _load_env_key("ELEVEN_API_KEY")
    if not api_key:
        print("   (ELEVEN_API_KEY not set — skipping vocal isolation)")
        print("   Tip: register a free account at https://elevenlabs.io")
        print("   then: export ELEVEN_API_KEY=your_key")
        return audio_path

    vocals_raw = tmp / "vocals_raw.mp3"
    vocals_out = tmp / "vocals.wav"

    with audio_path.open("rb") as f:
        resp = requests.post(
            ELEVENLABS_ISOLATION_URL,
            headers={"xi-api-key": api_key},
            files={"audio": (audio_path.name, f, "audio/wav")},
            timeout=300,
        )

    if resp.status_code != 200:
        print(f"   ElevenLabs API error ({resp.status_code}): {resp.text[:200]}")
        print("   Falling back to original audio.")
        return audio_path

    # ElevenLabs returns mp3 data — save as .mp3 then convert to
    # proper PCM WAV so Whisper gets accurate timestamps.
    vocals_raw.write_bytes(resp.content)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(vocals_raw),
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            str(vocals_out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return vocals_out


# ---------------------------------------------------------------------------
# OpenAI Whisper API transcription
# ---------------------------------------------------------------------------


WHISPER_PROMPT = "これはアニメキャラクターのセリフです。正確に書き起こしてください。"

# Known Whisper hallucination patterns (short/silent audio → these phrases)
_HALLUCINATION_PATTERNS = [
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "お疲れ様でした",
    "Thanks for watching",
    "Thank you for watching",
    "Subscribe",
    # Whisper sometimes echoes back the prompt itself
    "アニメキャラクターのセリフ",
    "書き起こしてください",
]


def _transcribe_whisper_api(
    audio_path: Path,
    fallback_path: Path | None = None,
) -> tuple[list[dict], str]:
    """Transcribe audio via OpenAI Whisper API with hallucination detection.

    1. Transcribes *audio_path* with an anti-hallucination prompt.
    2. If the result matches a known hallucination pattern AND a
       *fallback_path* is provided, retries with the fallback file.
    3. If both produce the same hallucinated text, returns it anyway
       (it might genuinely be what the speaker said).

    Returns (segments, language).
    """
    from openai import OpenAI

    api_key = _load_env_key("OPENAI_API_KEY")
    if not api_key:
        print("   Error: OPENAI_API_KEY not set.")
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    def _call(path: Path) -> tuple[list[dict], str]:
        with path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                prompt=WHISPER_PROMPT,
            )
        lang = result.language or "unknown"
        segs = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in (result.segments or [])
        ]
        return segs, lang

    segments, language = _call(audio_path)
    combined_text = " ".join(s["text"].strip() for s in segments)

    if _is_hallucination(combined_text) and fallback_path and fallback_path != audio_path:
        print(f"   Possible hallucination detected: \"{combined_text}\"")
        print(f"   Retrying with fallback audio...")
        segments2, language2 = _call(fallback_path)
        combined2 = " ".join(s["text"].strip() for s in segments2)

        if _is_hallucination(combined2):
            # Both hallucinated — warn user
            print(f"   Fallback also hallucinated: \"{combined2}\"")
            print(f"   ⚠️  Whisper 无法正确识别此音频，短视频容易出现幻觉")
            return segments, language
        else:
            # Fallback gave a real result — use it
            print(f"   Fallback result: \"{combined2}\"")
            return segments2, language2
    elif not _is_hallucination(combined_text):
        pass  # Normal case, no hallucination
    else:
        # Hallucination but no fallback available
        print(f"   ⚠️  Possible hallucination: \"{combined_text}\"")

    return segments, language


def _is_hallucination(text: str) -> bool:
    """Return True if text matches a known Whisper hallucination pattern."""
    clean = text.strip()
    return any(p in clean for p in _HALLUCINATION_PATTERNS)


# ---------------------------------------------------------------------------
# Quick translate (step 3 preview)
# ---------------------------------------------------------------------------


def _quick_translate(segments: list[dict]) -> list[str]:
    """Quick Chinese translation for step 3 preview (uses Haiku for speed)."""
    try:
        import anthropic
    except ImportError:
        return []

    api_key = _load_env_key("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    lines = "\n".join(
        f"{i + 1}. {seg['text'].strip()}"
        for i, seg in enumerate(segments)
    )
    prompt = (
        "Translate each line to Chinese. "
        "Return ONLY the numbered translations, one per line, same numbering.\n\n"
        f"{lines}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()

    translations: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip "1. " prefix
        m = re.match(r"^\d+\.\s*", line)
        if m:
            translations.append(line[m.end():])
        else:
            translations.append(line)
    return translations


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------


def _analyze_segments(segments: list[dict], language: str) -> list[dict]:
    """Translate + recommend events for each segment via Claude API.

    Returns a list of dicts, one per segment:
        {"zh": "...", "en": "...", "event": "task_start", "reason": "..."}
    """
    try:
        import anthropic
    except ImportError:
        print("   (anthropic not installed — skipping analysis)")
        return []

    api_key = _load_env_key("ANTHROPIC_API_KEY")
    if not api_key:
        print("   (ANTHROPIC_API_KEY not set — skipping analysis)")
        return []

    events_list = ", ".join(VALID_EVENTS)
    lines = "\n".join(
        f"{i + 1}. {seg['text'].strip()}"
        for i, seg in enumerate(segments)
    )
    prompt = (
        "You are helping build a voice-reactive coding assistant (chuuni-voice). "
        "Each event plays an anime character voice clip when triggered.\n\n"
        f"The audio language is: {language}\n\n"
        f"Available events:\n{events_list}\n\n"
        "Event descriptions:\n"
        "- task_start: user sends a new message / starts a task\n"
        "- task_done: assistant finishes responding\n"
        "- coding: writing or editing code\n"
        "- bash_run: running a shell command\n"
        "- test_pass: tests passed\n"
        "- test_fail: tests failed\n"
        "- error: runtime error or crash\n"
        "- permission_prompt: waiting for user permission\n\n"
        "For each line below:\n"
        "1. Translate to Chinese (zh)\n"
        "2. Translate to English (en) — short, suitable for a filename "
        "(lowercase, hyphens instead of spaces, max 6 words)\n"
        "3. Recommend which chuuni-voice event it best fits\n"
        "4. Give a short reason (in Chinese, one sentence)\n\n"
        f"Lines:\n{lines}\n\n"
        "Respond in JSON array format:\n"
        '[{"id": 1, "zh": "中文", "en": "lets-go-full-power", '
        '"event": "task_start", "reason": "理由"}]\n'
        "Return ONLY the JSON array, no other text."
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                print("   (warning: could not parse Claude response)")
                return []
        else:
            print("   (warning: could not parse Claude response)")
            return []

    result: list[dict] = []
    for i in range(len(segments)):
        entry = data[i] if i < len(data) else {}
        en_raw = str(entry.get("en", "clip"))
        # Sanitize English translation for filename use
        en_clean = re.sub(r"[^a-z0-9\-]", "-", en_raw.lower().strip())
        en_clean = re.sub(r"-+", "-", en_clean).strip("-") or "clip"
        result.append({
            "zh": str(entry.get("zh", "")),
            "en": en_clean,
            "event": str(entry.get("event", "")) if entry.get("event") in VALID_EVENTS else "",
            "reason": str(entry.get("reason", "")),
        })
    print(f"   → {len(result)} segments analyzed")
    return result


# ---------------------------------------------------------------------------
# Character picker
# ---------------------------------------------------------------------------


def _pick_character() -> str:
    """Show available characters and let user pick one."""
    print("\n── Select character ────────────────────────────────────\n")

    if not CHARACTERS_DIR.exists():
        print(f"   No characters directory at {CHARACTERS_DIR}")
        return ""

    chars = sorted(
        d.name for d in CHARACTERS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not chars:
        print("   No characters found.")
        return ""

    for i, name in enumerate(chars, 1):
        mp3_count = len(list((CHARACTERS_DIR / name).glob("*.mp3")))
        print(f"  {i}.  {name}  ({mp3_count} clips)")
    print()

    choice = input("Character number: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(chars):
            print(f"   → {chars[idx]}")
            return chars[idx]
    except ValueError:
        # Maybe they typed the name directly
        if choice in chars:
            return choice
    print("   Invalid selection.")
    return ""


# ---------------------------------------------------------------------------
# Save clip
# ---------------------------------------------------------------------------


def _save_clip(
    *,
    audio_path: Path,
    start: float,
    end: float,
    language: str,
    en_translation: str,
    character: str,
    event: str,
    char_dir: Path,
) -> Path | None:
    """Clip a segment and save with proper naming.

    Filename: <language>_<en_translation>_<character>_<event>.mp3

    Uses output seeking (-ss after -i) for frame-accurate cuts on short clips.
    """
    clip_start = max(0, start - SEGMENT_PAD)
    clip_end = end + SEGMENT_PAD

    filename = f"{language}_{en_translation}_{character}_{event}.mp3"
    out_file = char_dir / filename

    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-ss", f"{clip_start:.3f}",
        "-to", f"{clip_end:.3f}",
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        str(out_file),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"   ✗  ffmpeg failed for {filename}")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-3:]:
                print(f"      {line}")
        return None

    if not out_file.exists() or out_file.stat().st_size == 0:
        print(f"   ✗  {filename}  (empty file — skipped)")
        out_file.unlink(missing_ok=True)
        return None

    # Show actual duration via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out_file)],
        capture_output=True, text=True,
    )
    dur = probe.stdout.strip()
    if dur:
        print(f"      duration: {float(dur):.1f}s  ({out_file.stat().st_size // 1024}KB)")

    return out_file


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

BOX_W = 60


def _display_segments(
    segments: list[dict], analysis: list[dict], language: str,
) -> None:
    """Print segments in a rich box format."""
    print(f"\n── Results ({language}) ─────────────────────────────────\n")

    for i, seg in enumerate(segments):
        info = analysis[i] if i < len(analysis) else {}
        ts = _fmt_ts(seg["start"], seg["end"])
        original = seg["text"].strip()
        zh = info.get("zh", "")
        en = info.get("en", "")
        event = info.get("event", "")
        reason = info.get("reason", "")

        header = f"#{i + 1}  [{ts}]"

        print(f"  ┌{'─' * BOX_W}┐")
        print(f"  │ {header:<{BOX_W - 1}}│")
        _print_wrapped(f"原文：{original}")
        if zh:
            _print_wrapped(f"中文：{zh}")
        if en:
            _print_wrapped(f"EN：{en}")
        if event:
            tag = f"推荐：{event}"
            if reason:
                tag += f"（{reason}）"
            _print_wrapped(tag)
        print(f"  └{'─' * BOX_W}┘")

    print()


def _print_wrapped(text: str) -> None:
    """Print a line inside the box, wrapping if needed."""
    while text:
        chunk = text[:BOX_W - 2]
        text = text[BOX_W - 2:]
        print(f"  │ {chunk:<{BOX_W - 1}}│")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_voice_start(audio_path: Path, start: float, end: float) -> float:
    """Detect where voice actually begins in an audio segment.

    Uses ffmpeg silencedetect to find leading silence.  If the segment
    begins with silence (silence_start ≈ 0 relative to segment start),
    returns the silence_end as the real voice start.

    Returns the adjusted start time, or original *start* if no leading
    silence is found.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-i", str(audio_path),
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-af", "silencedetect=noise=-30dB:d=0.3",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )

    # We want the FIRST silence block that starts at the very beginning
    # (silence_start ≈ 0, relative to -ss).  Its silence_end is where
    # the voice begins.
    first_silence_start: float | None = None
    first_silence_end: float | None = None

    for line in result.stderr.splitlines():
        if "silence_start" in line and first_silence_start is None:
            m = re.search(r"silence_start:\s*([\d.]+)", line)
            if m:
                first_silence_start = float(m.group(1))
        elif "silence_end" in line and first_silence_end is None:
            m = re.search(r"silence_end:\s*([\d.]+)", line)
            if m:
                first_silence_end = float(m.group(1))
                break  # Only care about the first silence block

    # Only trim if silence starts at the beginning (within 0.5s of segment start)
    if (first_silence_start is not None
            and first_silence_end is not None
            and first_silence_start < 0.5):
        voice_start = start + first_silence_end
        # Don't trim past 80% of the segment
        if voice_start < start + (end - start) * 0.8:
            return round(voice_start, 2)

    return start


def _fmt_ts(start: float, end: float) -> str:
    """Format a time range like 00:02 - 00:04."""
    def _t(s: float) -> str:
        m, sec = divmod(int(s), 60)
        return f"{m:02d}:{sec:02d}"
    return f"{_t(start)} - {_t(end)}"


def _load_env_key(name: str) -> str:
    """Load a key from environment variable (populated by python-dotenv)."""
    return os.environ.get(name, "")


def _parse_selection(text: str, total: int) -> list[int]:
    """Parse selection string into 0-based indices.

    Supports: "1,3,5"  "1-3"  "1,3-5,7"  "all"
    """
    text = text.strip().lower()
    if text == "all":
        return list(range(total))

    indices: list[int] = []
    for part in text.replace(" ", "").split(","):
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                lo, hi = int(bounds[0]), int(bounds[1])
                for n in range(lo, hi + 1):
                    if 1 <= n <= total:
                        indices.append(n - 1)
            except ValueError:
                continue
        else:
            try:
                n = int(part)
                if 1 <= n <= total:
                    indices.append(n - 1)
            except ValueError:
                continue

    seen: set[int] = set()
    unique: list[int] = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            unique.append(idx)
    return unique


if __name__ == "__main__":
    main()
