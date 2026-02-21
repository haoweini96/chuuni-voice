import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w

CONFIG_DIR = Path.home() / ".config" / "chuuni"
CONFIG_FILE = CONFIG_DIR / "config.toml"
CHARACTERS_DIR = CONFIG_DIR / "characters"

DEFAULT_CONFIG: dict[str, Any] = {
    "active_character": "default",
    "character_dir": str(CHARACTERS_DIR / "default"),
    "rvc_model_path": "",
    "rvc_index_path": "",
    "volume": 0.8,
    "enabled": True,
    "cooldown_seconds": 5.0,
}

# Default per-event cooldown times in seconds.
DEFAULT_COOLDOWNS: dict[str, float] = {
    "task_start": 0,
    "task_done": 0,
    "coding": 30,
    "bash_run": 30,
    "test_pass": 5,
    "test_fail": 5,
    "error": 5,
    "permission_prompt": 5,
}


def get_cooldowns(cfg: dict[str, Any]) -> dict[str, float]:
    """Return per-event cooldown times, merging config with defaults.

    Reads the ``[cooldown]`` table from *cfg*.  Keys present in config
    override the defaults; unrecognised keys are passed through as-is so
    custom character events can also have cooldowns.
    """
    on_disk = cfg.get("cooldown", {})
    merged = dict(DEFAULT_COOLDOWNS)
    merged.update(
        {k: float(v) for k, v in on_disk.items() if isinstance(v, (int, float))}
    )
    return merged


def load_config() -> dict[str, Any]:
    """Load config from disk, merging missing keys with defaults.

    Returns a flat dict with all keys guaranteed to be present.
    """
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    with CONFIG_FILE.open("rb") as f:
        on_disk = tomllib.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(on_disk)
    return merged


def save_config(config: dict[str, Any]) -> None:
    """Persist *config* to ~/.config/chuuni/config.toml as TOML."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("wb") as f:
        tomli_w.dump(config, f)


def get_character_dir(cfg: dict[str, Any]) -> Path:
    """Return the active character's audio directory.

    Prefers ``active_character`` (derives path as CHARACTERS_DIR/<name>);
    falls back to the explicit ``character_dir`` value for backward compat.
    """
    active = cfg.get("active_character", "")
    if active:
        return CHARACTERS_DIR / active
    return Path(cfg.get("character_dir", DEFAULT_CONFIG["character_dir"])).expanduser()
