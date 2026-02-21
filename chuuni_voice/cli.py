import json
import platform
import shutil
import sys
import time
from pathlib import Path

import click

from chuuni_voice import __version__
from chuuni_voice.events import ChuuniEvent, get_line

AUDIO_EXTENSIONS = [".mp3", ".wav", ".ogg", ".aiff"]

_EVENT_NAMES = "  ".join(e.value for e in ChuuniEvent)


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="chuuni")
def main() -> None:
    """chuuni-voice: anime character voices for Claude Code hooks."""


# ---------------------------------------------------------------------------
# chuuni init
# ---------------------------------------------------------------------------


@main.command()
def init() -> None:
    """Interactive setup wizard — creates ~/.config/chuuni/config.toml."""
    from chuuni_voice.characters.base import CharacterManager
    from chuuni_voice.config import (
        CHARACTERS_DIR,
        CONFIG_FILE,
        DEFAULT_CONFIG,
        load_config,
        save_config,
    )

    click.echo("── chuuni-voice setup ──────────────────────────────────")

    existing = load_config()

    # --- active character ---
    click.echo()
    installed = CharacterManager.list_characters(str(CHARACTERS_DIR))
    if installed:
        names = ", ".join(c.name for c in installed)
        click.echo(f"Installed characters: {names}")
    else:
        click.echo("No characters installed yet — 'default' will be created.")

    active_character = click.prompt(
        "Active character name",
        default=existing.get("active_character", DEFAULT_CONFIG["active_character"]),
    )
    character_dir = CHARACTERS_DIR / active_character

    # --- volume ---
    click.echo()
    volume = click.prompt(
        "Volume (0.0 – 1.0)",
        default=existing.get("volume", DEFAULT_CONFIG["volume"]),
        type=click.FloatRange(0.0, 1.0),
    )

    # --- enabled ---
    enabled = click.confirm(
        "Enable chuuni-voice?",
        default=existing.get("enabled", DEFAULT_CONFIG["enabled"]),
    )

    # --- RVC (optional) ---
    click.echo()
    configure_rvc = click.confirm(
        "Configure RVC voice conversion? (optional, skip if using pre-converted clips)",
        default=False,
    )
    rvc_model_path = existing.get("rvc_model_path", "")
    rvc_index_path = existing.get("rvc_index_path", "")

    if configure_rvc:
        click.echo()
        rvc_model_path = click.prompt(
            "RVC model path (.pth file)",
            default=rvc_model_path or "",
        )
        rvc_index_path = click.prompt(
            "RVC index path (.index file, leave blank to skip)",
            default=rvc_index_path or "",
        )

    # --- save ---
    config = {
        "active_character": active_character,
        "character_dir": str(character_dir),
        "rvc_model_path": rvc_model_path,
        "rvc_index_path": rvc_index_path,
        "volume": volume,
        "enabled": enabled,
    }

    click.echo()
    save_config(config)
    character_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"✓ Config written     → {CONFIG_FILE}")
    click.echo(f"✓ Character dir      → {character_dir}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  1. Drop audio files into {character_dir}/")
    click.echo(f"     ({_EVENT_NAMES})")
    click.echo("  2. Optionally create a character.toml in that directory")
    click.echo("     (see chuuni_voice/assets/character.toml.example)")
    click.echo("  3. Run  chuuni hook    to inject Claude Code hooks")
    click.echo("  4. Run  chuuni play coding  to test")


# ---------------------------------------------------------------------------
# chuuni hook
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove chuuni hooks from settings.json instead of injecting.",
)
def hook(remove: bool) -> None:
    """Inject chuuni-voice hooks into ~/.claude/settings.json.

    Backs up the original file before making any changes.
    Running this command again is safe — it never duplicates entries.

    Use --remove to cleanly uninstall the hooks.
    """
    from chuuni_voice.hooks.claude_code import inject_hooks, remove_hooks

    if remove:
        click.echo("── Removing chuuni hooks ────────────────────────────────")
        remove_hooks()
    else:
        click.echo("── Injecting chuuni hooks ───────────────────────────────")
        inject_hooks()


# ---------------------------------------------------------------------------
# chuuni play
# ---------------------------------------------------------------------------


@main.command()
@click.argument("event", metavar="EVENT")
def play(event: str) -> None:
    """Manually trigger voice playback for EVENT.

    \b
    Events:
      task_start  coding  bash_run  test_pass  test_fail
      error  task_done  permission_prompt

    Prints the Japanese line even when no audio file is found.
    """
    from chuuni_voice.config import load_config, get_character_dir
    from chuuni_voice.player import _check_and_claim_cooldown, _play_blocking
    from chuuni_voice import daemon as _daemon

    _debug_log(f"play called: event={event}")

    cfg = load_config()

    if not cfg.get("enabled", True):
        click.echo(
            click.style("chuuni-voice is disabled", fg="yellow")
            + " — set enabled = true in config to re-enable.",
            err=True,
        )
        sys.exit(1)

    try:
        chuuni_event = ChuuniEvent(event.lower())
    except ValueError:
        valid = ", ".join(e.value for e in ChuuniEvent)
        click.echo(f"Unknown event {event!r}.\nValid events: {valid}", err=True)
        sys.exit(1)

    char_dir = get_character_dir(cfg)
    volume = float(cfg.get("volume", 0.8))
    audio_path = _resolve_audio(char_dir, chuuni_event.value)

    _debug_log(f"  enabled={cfg.get('enabled')}, audio_path={audio_path}, volume={volume}")

    # ── Daemon path ──────────────────────────────────────────────────────────
    # Auto-start the daemon if it isn't running yet.
    daemon_up = _ensure_daemon_running()
    _debug_log(f"  daemon_up={daemon_up}")
    # send_play() returns None only when the daemon is not running (connection
    # error / no socket).  A cooldown rejection returns {"ok": false} — still
    # a valid response, so we exit here in both the accepted and rejected cases.
    resp = _daemon.send_play(
        chuuni_event.value,
        str(audio_path) if audio_path else "",
        volume,
    )
    _debug_log(f"  daemon resp={resp}")
    if resp is not None:
        if resp.get("ok"):
            click.echo(f"[{chuuni_event.value}]  {_character_line(chuuni_event, str(char_dir))}")
        return  # daemon handled it (or cooldown dropped it)

    # ── Fallback: direct playback with file-based cooldown ───────────────────
    _debug_log("  daemon unreachable, using fallback")
    from chuuni_voice.config import get_cooldowns
    cooldowns = get_cooldowns(cfg)
    cooldown = cooldowns.get(chuuni_event.value, float(cfg.get("cooldown_seconds", 5.0)))
    if not _check_and_claim_cooldown(chuuni_event.value, cooldown):
        _debug_log(f"  fallback cooldown blocked (cd={cooldown}s)")
        return  # silently skip — within cooldown window

    line = _character_line(chuuni_event, str(char_dir))
    click.echo(f"[{chuuni_event.value}]  {line}")

    if audio_path is None:
        click.echo(
            click.style("  (no audio file — drop ", fg="yellow")
            + click.style(f"{chuuni_event.value}.mp3", bold=True)
            + click.style(f" into {char_dir})", fg="yellow"),
        )
        return

    click.echo(click.style(f"  ♪  {audio_path.name}", fg="cyan"))
    _play_blocking(str(audio_path), volume)


# ---------------------------------------------------------------------------
# chuuni on-hook  (internal — called by Claude Code hooks via stdin JSON)
# ---------------------------------------------------------------------------


@main.command("on-hook", hidden=True)
@click.argument("hook_ctx", metavar="CTX")
def on_hook(hook_ctx: str) -> None:
    """Internal dispatcher: reads Claude Code hook JSON from stdin and plays."""
    from chuuni_voice.config import load_config, get_character_dir
    from chuuni_voice.player import _check_and_claim_cooldown, _play_blocking
    from chuuni_voice import daemon as _daemon

    cfg = load_config()
    if not cfg.get("enabled", True):
        return

    try:
        raw = sys.stdin.read()
        data: dict = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    event = _dispatch(hook_ctx, data)
    if event is None:
        return

    char_dir = get_character_dir(cfg)
    volume = float(cfg.get("volume", 0.8))
    audio_path = _resolve_audio(char_dir, event.value)

    # ── Daemon path ──────────────────────────────────────────────────────────
    resp = _daemon.send_play(
        event.value,
        str(audio_path) if audio_path else "",
        volume,
    )
    if resp is not None:
        if resp.get("ok"):
            click.echo(f"[{event.value}]  {_character_line(event, str(char_dir))}")
        return  # daemon handled it

    # ── Fallback: file-based cooldown + direct playback ──────────────────────
    from chuuni_voice.config import get_cooldowns
    cooldowns = get_cooldowns(cfg)
    cooldown = cooldowns.get(event.value, float(cfg.get("cooldown_seconds", 5.0)))
    if not _check_and_claim_cooldown(event.value, cooldown):
        return

    click.echo(f"[{event.value}]  {_character_line(event, str(char_dir))}")
    if audio_path:
        _play_blocking(str(audio_path), volume)


# ---------------------------------------------------------------------------
# chuuni character
# ---------------------------------------------------------------------------


@main.group()
def character() -> None:
    """Manage installed characters."""


@character.command("list")
def character_list() -> None:
    """List all installed characters and show which one is active."""
    from chuuni_voice.characters.base import CharacterManager
    from chuuni_voice.config import load_config, CHARACTERS_DIR

    cfg = load_config()
    active = cfg.get("active_character", "default")

    click.echo(f"── Characters  (base: {CHARACTERS_DIR}) ────────────────────")
    click.echo()

    chars = CharacterManager.list_characters(str(CHARACTERS_DIR))
    if not chars:
        click.echo("  No characters found.")
        click.echo(f"  Create a directory under {CHARACTERS_DIR}/ to add one.")
        return

    for char in chars:
        marker = click.style("*", fg="green", bold=True) if char.name == active else " "
        name_col = click.style(char.name, bold=(char.name == active))
        disp = char.display_name if char.display_name != char.name else ""
        desc = char.description or "(no description)"
        click.echo(f"  {marker} {name_col:<20}  {disp:<12}  {desc}")

    click.echo()
    click.echo(f"  Active: {click.style(active, fg='green', bold=True)}")


@character.command("use")
@click.argument("name")
def character_use(name: str) -> None:
    """Switch to character NAME and update config."""
    from chuuni_voice.config import load_config, save_config, CHARACTERS_DIR

    char_dir = CHARACTERS_DIR / name
    if not char_dir.is_dir():
        click.echo(
            click.style(f"Character directory not found: {char_dir}", fg="red"),
            err=True,
        )
        click.echo(f"Create it and add audio files first.", err=True)
        sys.exit(1)

    cfg = load_config()
    cfg["active_character"] = name
    cfg["character_dir"] = str(char_dir)
    save_config(cfg)

    click.echo(f"✓ Active character → {click.style(name, bold=True)}")
    click.echo(f"  Audio dir        → {char_dir}")


# ---------------------------------------------------------------------------
# chuuni daemon
# ---------------------------------------------------------------------------


@main.group()
def daemon() -> None:
    """Manage the persistent audio daemon (prevents sound overlap)."""


@daemon.command("start")
def daemon_start() -> None:
    """Start the background audio daemon."""
    from chuuni_voice import daemon as _daemon
    from chuuni_voice.config import load_config

    if _daemon.is_running():
        click.echo("daemon: already running")
        return

    bin_path = _chuuni_bin()
    _daemon.CHUUNI_DIR.mkdir(parents=True, exist_ok=True)

    with _daemon.LOG_FILE.open("a") as lf:
        proc = __import__("subprocess").Popen(
            [bin_path, "_daemon-run"],
            start_new_session=True,
            stdout=lf,
            stderr=lf,
        )

    # Poll up to 2 s for the daemon to become ready
    for _ in range(20):
        time.sleep(0.1)
        if _daemon.is_running():
            click.echo(f"daemon: started  (pid={proc.pid})")
            return

    click.echo(
        f"daemon: spawned (pid={proc.pid}) — check {_daemon.LOG_FILE} if it doesn't respond",
        err=True,
    )


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the background audio daemon."""
    from chuuni_voice import daemon as _daemon

    if not _daemon.is_running():
        click.echo("daemon: not running")
        return

    resp = _daemon.send_stop()
    if not (resp and resp.get("ok")):
        click.echo("daemon: failed to stop — try killing the process manually", err=True)
        return

    # Block until the daemon has cleaned up its socket (up to 3 s).
    # The daemon sets _running=False immediately but takes up to 1 s to close
    # the server socket due to the accept() timeout.  Without this wait,
    # a back-to-back "daemon stop && daemon start" would see is_running()=True
    # and refuse to start.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _daemon.is_running():
            break
        time.sleep(0.05)

    click.echo("daemon: stopped")


@daemon.command("status")
def daemon_status() -> None:
    """Show daemon running status and queue depth."""
    from chuuni_voice import daemon as _daemon

    if not _daemon.is_running():
        click.echo("daemon: not running")
        return

    resp = _daemon.send_status()
    if resp:
        q = resp.get("queue_size", 0)
        click.echo(f"daemon: running  (queue_size={q})")
    else:
        click.echo("daemon: socket exists but not responding", err=True)


@main.command("_daemon-run", hidden=True)
def daemon_run() -> None:
    """Internal: run the audio daemon in the foreground (called by 'daemon start')."""
    import logging as _logging
    from chuuni_voice import daemon as _daemon
    from chuuni_voice.config import load_config

    _daemon.CHUUNI_DIR.mkdir(parents=True, exist_ok=True)
    _logging.basicConfig(
        filename=str(_daemon.LOG_FILE),
        level=_logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from chuuni_voice.config import get_cooldowns
    cfg = load_config()
    cooldowns = get_cooldowns(cfg)
    default_cooldown = float(cfg.get("cooldown_seconds", 5.0))
    _daemon.AudioDaemon(cooldowns=cooldowns, default_cooldown=default_cooldown).run()


# ---------------------------------------------------------------------------
# chuuni status
# ---------------------------------------------------------------------------


@main.command()
def status() -> None:
    """Show current config and component status at a glance."""
    from chuuni_voice.characters.base import CharacterManager
    from chuuni_voice.config import load_config, CONFIG_FILE, get_character_dir

    cfg = load_config()
    ok = click.style("✓", fg="green")
    ng = click.style("✗", fg="red")

    # ── Config ──────────────────────────────────────────────────────────────
    click.echo("── Config ──────────────────────────────────────────────")
    cfg_sym = ok if CONFIG_FILE.exists() else ng
    click.echo(f"  File        {cfg_sym}  {CONFIG_FILE}")
    enabled = cfg.get("enabled", True)
    enabled_label = click.style("yes", fg="green") if enabled else click.style("no", fg="yellow")
    click.echo(f"  Enabled        {enabled_label}")
    click.echo(f"  Volume         {cfg.get('volume', 0.8)}")

    # ── Character ────────────────────────────────────────────────────────────
    click.echo()
    click.echo("── Character ───────────────────────────────────────────")
    active = cfg.get("active_character", "default")
    char_dir = get_character_dir(cfg)
    dir_sym = ok if char_dir.is_dir() else ng
    click.echo(f"  Active      {click.style(active, bold=True)}")
    click.echo(f"  Directory   {dir_sym}  {char_dir}")

    try:
        char = CharacterManager.load_from_dir(str(char_dir))
        if char.display_name and char.display_name != char.name:
            click.echo(f"  Display     {char.display_name}")
        if char.description:
            click.echo(f"  Desc        {char.description}")
    except Exception:
        pass

    click.echo()
    click.echo("  Audio files:")
    for event in ChuuniEvent:
        path = _resolve_audio(char_dir, event.value)
        if path:
            click.echo(f"    {ok}  {event.value:<14}  {path.name}")
        else:
            click.echo(f"    {ng}  {event.value:<14}  {click.style('(not found)', fg='yellow')}")

    # ── RVC ─────────────────────────────────────────────────────────────────
    click.echo()
    click.echo("── RVC ─────────────────────────────────────────────────")
    model_path = cfg.get("rvc_model_path", "")
    index_path = cfg.get("rvc_index_path", "")
    if model_path:
        click.echo(f"  Model  {ok if Path(model_path).expanduser().exists() else ng}  {model_path}")
    else:
        click.echo(f"  Model      {click.style('(not configured)', fg='yellow')}")
    if index_path:
        click.echo(f"  Index  {ok if Path(index_path).expanduser().exists() else ng}  {index_path}")
    else:
        click.echo(f"  Index      {click.style('(not configured)', fg='yellow')}")

    # ── Player ───────────────────────────────────────────────────────────────
    click.echo()
    click.echo("── Player ──────────────────────────────────────────────")
    system = platform.system()
    bins = ["afplay"] if system == "Darwin" else ["aplay", "mpg123"]
    for b in bins:
        found = shutil.which(b)
        sym = ok if found else ng
        loc = found or click.style("not found in PATH", fg="red")
        click.echo(f"  {sym}  {b:<10}  {loc}")

    # ── Daemon ──────────────────────────────────────────────────────────────
    click.echo()
    click.echo("── Daemon ──────────────────────────────────────────────")
    from chuuni_voice import daemon as _daemon

    if not _daemon.is_running():
        click.echo("  not running")
    else:
        resp = _daemon.send_status()
        if resp:
            q = resp.get("queue_size", 0)
            click.echo(f"  running  (queue_size={q})")
        else:
            click.echo("  socket exists but not responding")

    # ── Cooldowns ──────────────────────────────────────────────────────────
    click.echo()
    click.echo("── Cooldowns ───────────────────────────────────────────")
    from chuuni_voice.config import get_cooldowns
    cooldowns = get_cooldowns(cfg)
    for ev in ChuuniEvent:
        cd = cooldowns.get(ev.value, float(cfg.get("cooldown_seconds", 5.0)))
        click.echo(f"  {ev.value:<20}  {cd:>5.0f}s")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEBUG_LOG = Path.home() / ".config" / "chuuni" / "debug.log"


def _debug_log(msg: str) -> None:
    """Append a timestamped line to ~/.config/chuuni/debug.log."""
    try:
        from datetime import datetime
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def _ensure_daemon_running() -> bool:
    """Start the daemon if it isn't running. Return True if daemon is up."""
    import subprocess as _subprocess
    from chuuni_voice import daemon as _daemon

    if _daemon.is_running():
        return True

    bin_path = _chuuni_bin()
    _daemon.CHUUNI_DIR.mkdir(parents=True, exist_ok=True)
    with _daemon.LOG_FILE.open("a") as lf:
        _subprocess.Popen(
            [bin_path, "_daemon-run"],
            start_new_session=True,
            stdout=lf,
            stderr=lf,
        )

    for _ in range(20):
        time.sleep(0.1)
        if _daemon.is_running():
            return True

    return False


def _chuuni_bin() -> str:
    """Return the absolute path to the chuuni binary.

    Search order:
      1. shutil.which("chuuni")                — works when chuuni is on PATH
      2. Path(sys.executable).parent / "chuuni" — same venv as running Python
      3. bare "chuuni"                          — last resort
    """
    found = shutil.which("chuuni")
    if found:
        return found
    candidate = Path(sys.executable).parent / "chuuni"
    if candidate.exists():
        return str(candidate)
    return "chuuni"


def _resolve_audio(character_dir: Path, stem: str) -> Path | None:
    """Return a random audio file for *stem* in *character_dir*.

    Matches any file ending with ``_<stem>.<ext>`` or the bare ``<stem>.<ext>``.
    """
    import random as _random

    candidates: list[Path] = []
    for ext in AUDIO_EXTENSIONS:
        candidates.extend(sorted(character_dir.glob(f"*_{stem}{ext}")))
        exact = character_dir / f"{stem}{ext}"
        if exact.exists() and exact not in candidates:
            candidates.append(exact)
    return _random.choice(candidates) if candidates else None


def _character_line(event: ChuuniEvent, char_dir: str) -> str:
    """Return a default voice line for *event*."""
    return get_line(event)


# Keywords that indicate a Python runtime crash (not a test assertion failure).
# Intentionally excludes "Error", "Exception", "FAILED": those appear in normal
# pytest output (AssertionError, FAILED tests/...) and would mislabel test
# failures as runtime errors.
_CRASH_KEYWORDS = (
    "Traceback",
    "ModuleNotFoundError",
    "ImportError",
    "SyntaxError",
    "NameError",
)


def _dispatch(hook_ctx: str, data: dict) -> ChuuniEvent | None:
    if hook_ctx == "post-bash":
        exit_code = (
            data.get("tool_response", {}).get("exit_code")
            or data.get("exit_code")
            or 0
        )
        if exit_code == 0:
            return ChuuniEvent.TEST_PASS

        # Non-zero exit: check output for crash signatures before defaulting to
        # test_fail.  error > test_fail so runtime crashes get their own sound.
        output: str = (
            data.get("tool_response", {}).get("output", "")
            or data.get("output", "")
            or ""
        )
        if any(kw in output for kw in _CRASH_KEYWORDS):
            return ChuuniEvent.ERROR

        return ChuuniEvent.TEST_FAIL
    return None
