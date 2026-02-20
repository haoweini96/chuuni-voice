import json
import platform
import shutil
import sys
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
      error  task_done  thinking

    Prints the Japanese line even when no audio file is found.
    """
    from chuuni_voice.characters.base import CharacterManager
    from chuuni_voice.config import load_config, get_character_dir
    from chuuni_voice.player import _play_blocking

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

    # Load character for custom lines (falls back to default lines on error)
    line = _character_line(chuuni_event, str(char_dir))
    click.echo(f"[{chuuni_event.value}]  {line}")

    audio_path = _resolve_audio(char_dir, chuuni_event.value)
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
    from chuuni_voice.player import _play_blocking

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

    click.echo(f"[{event.value}]  {_character_line(event, str(char_dir))}")

    audio_path = _resolve_audio(char_dir, event.value)
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
        has_custom = len(char.lines)
        click.echo(f"  Custom lines  {has_custom}/{len(ChuuniEvent)} events overridden")
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_audio(character_dir: Path, stem: str) -> Path | None:
    """Return the best audio file for *stem* in *character_dir*.

    Priority mirrors player._find_candidates:
      1. 日语_<charname>_<stem>.<ext>
      2. japanese_*<charname>_<stem>.<ext>  (first match, alphabetical)
      3. <stem>.<ext>
    """
    import glob as _glob
    char_name = character_dir.name
    # Priority 1: 日语 named format
    for ext in AUDIO_EXTENSIONS:
        candidate = character_dir / f"日语_{char_name}_{stem}{ext}"
        if candidate.exists():
            return candidate
    # Priority 2: japanese_* format
    for ext in AUDIO_EXTENSIONS:
        matches = sorted(character_dir.glob(f"japanese_*{char_name}_{stem}{ext}"))
        if matches:
            return matches[0]
    # Priority 3: generic format
    for ext in AUDIO_EXTENSIONS:
        candidate = character_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


def _character_line(event: ChuuniEvent, char_dir: str) -> str:
    """Return a line for *event*, using character custom lines when available."""
    try:
        from chuuni_voice.characters.base import CharacterManager
        char = CharacterManager.load_from_dir(char_dir)
        return char.get_line(event)
    except Exception:
        return get_line(event)


def _dispatch(hook_ctx: str, data: dict) -> ChuuniEvent | None:
    if hook_ctx == "post-bash":
        exit_code = (
            data.get("tool_response", {}).get("exit_code")
            or data.get("exit_code")
            or 0
        )
        return ChuuniEvent.TEST_PASS if exit_code == 0 else ChuuniEvent.TEST_FAIL
    return None
