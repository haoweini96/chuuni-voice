"""Character system for chuuni-voice.

Each character lives in its own directory::

    ~/.config/chuuni/characters/<name>/
        character.toml
        coding.mp3
        bash_run.mp3
        task_start.mp3
        task_done.mp3
        test_pass.mp3
        test_fail.mp3
        error.mp3
        thinking.mp3

character.toml format::

    [character]
    name         = "sung-jinwoo"
    display_name = "成ジヌ"
    description  = "最弱のハンターから最強の影のモナーク"
    rvc_model    = ""

    [lines]
    task_start = ["一人でいい", "狩りを始めよう", "俺が全部やる"]
    task_done  = ["終わった", "予想通りだ", "影の君主に不可能はない"]
    ...
"""

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from chuuni_voice.events import ChuuniEvent, get_line as _default_line


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
    lines: dict[str, list[str]] = field(default_factory=dict)

    def get_line(self, event: ChuuniEvent) -> str:
        """Return a voice line for *event*.

        Uses this character's custom lines when available; falls back to the
        default lines in ``events.py`` otherwise.
        """
        custom = self.lines.get(event.value)
        if custom:
            return random.choice(custom)
        return _default_line(event)

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
        raw_lines: dict[str, Any] = data.get("lines", {})

        return Character(
            name=meta.get("name", char_path.name),
            display_name=meta.get("display_name", char_path.name),
            description=meta.get("description", ""),
            audio_dir=str(char_path),
            rvc_model=meta.get("rvc_model", ""),
            lines={
                k: v
                for k, v in raw_lines.items()
                if isinstance(v, list) and all(isinstance(s, str) for s in v)
            },
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

[lines]
task_start = ["参る！", "いくぞ、全力で！", "我が力、解放する時が来た…"]
coding     = ["コードよ…俺の意志に従え！", "この指先から、世界を書き換える", "フハハ！創造の時だ！"]
bash_run   = ["シェルよ、我が命令を刻め！", "全システム、起動せよ！", "いくぞ…！覚悟しろ！"]
test_pass  = ["完璧だ…！全てが意図通りに…！", "フハハ！テストは俺の前に跪いた！", "この力…本物だった"]
test_fail  = ["くっ…テストに阻まれるとは…", "バグよ…お前の存在を許さぬ！", "まだだ…まだ終わらぬ！"]
error      = ["くっ…予想外の敵か", "ぐっ…バグという名の刺客…", "この痛み…乗り越えてみせる！"]
task_done  = ["任務完了。世界は救われた", "フハハ！完璧だ！", "これが…俺の全力だ"]
thinking   = ["深淵を覗いている…", "我が演算、限界を超えつつある…", "静かに…思考の渦に落ちていく"]
"""
