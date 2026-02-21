"""Character system for chuuni-voice.

Each character lives in its own directory::

    ~/.config/chuuni/characters/<name>/
        character.toml          (optional metadata)
        japanese_*_<name>_<event>.mp3   (audio clips)

character.toml format::

    [character]
    name         = "sung-jinwoo"
    display_name = "成ジヌ"
    description  = "最弱のハンターから最強の影のモナーク"
    rvc_model    = ""

Audio files are the sole data source for playback.  Any file whose name
ends with ``_<event_name>.mp3`` (or ``.wav``) is a candidate for that event.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Character dataclass
# ---------------------------------------------------------------------------


@dataclass
class Character:
    """Represents an installed chuuni-voice character."""

    name: str
    display_name: str
    description: str
    audio_dir: str
    rvc_model: str = ""

    def __repr__(self) -> str:
        return f"Character(name={self.name!r}, display_name={self.display_name!r})"


# ---------------------------------------------------------------------------
# CharacterManager
# ---------------------------------------------------------------------------


class CharacterManager:
    """Load and enumerate installed characters."""

    @staticmethod
    def load_from_dir(path: str) -> Character:
        """Load a Character from *path*.

        Reads ``character.toml`` if present.  Falls back to a minimal
        Character derived from the directory name when the file is missing or
        unreadable.
        """
        char_path = Path(path)
        toml_file = char_path / "character.toml"

        if not toml_file.exists():
            return Character(
                name=char_path.name,
                display_name=char_path.name,
                description="",
                audio_dir=str(char_path),
            )

        with toml_file.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)

        meta = data.get("character", {})

        return Character(
            name=meta.get("name", char_path.name),
            display_name=meta.get("display_name", char_path.name),
            description=meta.get("description", ""),
            audio_dir=str(char_path),
            rvc_model=meta.get("rvc_model", ""),
        )

    @staticmethod
    def list_characters(base_dir: str) -> list[Character]:
        """Return all characters installed under *base_dir*.

        Each immediate subdirectory is treated as a character.
        Unreadable directories are silently skipped.
        """
        base = Path(base_dir)
        if not base.is_dir():
            return []

        characters: list[Character] = []
        for subdir in sorted(base.iterdir()):
            if not subdir.is_dir():
                continue
            try:
                characters.append(CharacterManager.load_from_dir(str(subdir)))
            except Exception:
                pass
        return characters


# ---------------------------------------------------------------------------
# Template for scaffolding new characters
# ---------------------------------------------------------------------------

CHARACTER_TOML_TEMPLATE = """\
[character]
name         = "{name}"
display_name = "{display_name}"
description  = ""
rvc_model    = ""
"""
