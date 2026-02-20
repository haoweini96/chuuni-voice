"""Tests for chuuni_voice.characters.base."""

from pathlib import Path

import pytest

from chuuni_voice.characters.base import Character, CharacterManager
from chuuni_voice.events import ChuuniEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(directory: Path, content: str) -> Path:
    toml = directory / "character.toml"
    toml.write_text(content, encoding="utf-8")
    return toml


FULL_TOML = """\
[character]
name         = "sung-jinwoo"
display_name = "成ジヌ"
description  = "最弱のハンターから最強の影のモナーク"
rvc_model    = "jinwoo.pth"

[lines]
task_start = ["一人でいい", "狩りを始めよう"]
coding     = ["コードを刻む", "世界を書き換える"]
bash_run   = ["命令を下す", "起動せよ"]
test_pass  = ["問題ない", "当然だ"]
test_fail  = ["修正が必要だ", "まだ終わりじゃない"]
error      = ["...チッ", "想定外か"]
task_done  = ["終わった", "予想通りだ"]
thinking   = ["考えている", "分析中だ"]
"""


# ---------------------------------------------------------------------------
# Character.get_line
# ---------------------------------------------------------------------------


class TestCharacterGetLine:
    def test_returns_custom_line(self):
        char = Character(
            name="test",
            display_name="Test",
            description="",
            audio_dir="/tmp",
            lines={"coding": ["カスタムライン"]},
        )
        assert char.get_line(ChuuniEvent.CODING) == "カスタムライン"

    def test_falls_back_to_default_when_event_missing(self):
        char = Character(
            name="test",
            display_name="Test",
            description="",
            audio_dir="/tmp",
            lines={},
        )
        # Should not raise; returns a non-empty string from events.py defaults
        line = char.get_line(ChuuniEvent.CODING)
        assert isinstance(line, str)
        assert len(line) > 0

    def test_falls_back_to_default_when_list_empty(self):
        """An empty list in lines dict should fall back to defaults."""
        char = Character(
            name="test",
            display_name="Test",
            description="",
            audio_dir="/tmp",
            lines={"coding": []},
        )
        line = char.get_line(ChuuniEvent.CODING)
        assert isinstance(line, str)
        assert len(line) > 0

    def test_picks_from_multiple_custom_lines(self):
        options = ["ライン一", "ライン二", "ライン三"]
        char = Character(
            name="test",
            display_name="Test",
            description="",
            audio_dir="/tmp",
            lines={"task_done": options},
        )
        # Run many times — at least one match proves selection works
        results = {char.get_line(ChuuniEvent.TASK_DONE) for _ in range(30)}
        assert results.issubset(set(options))

    def test_all_events_covered_by_defaults(self):
        char = Character(
            name="test",
            display_name="Test",
            description="",
            audio_dir="/tmp",
            lines={},
        )
        for event in ChuuniEvent:
            line = char.get_line(event)
            assert isinstance(line, str) and len(line) > 0, f"No default line for {event}"


# ---------------------------------------------------------------------------
# CharacterManager.load_from_dir — no toml
# ---------------------------------------------------------------------------


class TestLoadFromDirNoToml:
    def test_returns_character_from_dir_name(self, tmp_path):
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.name == tmp_path.name

    def test_display_name_equals_dir_name_when_no_toml(self, tmp_path):
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.display_name == tmp_path.name

    def test_description_empty_when_no_toml(self, tmp_path):
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.description == ""

    def test_lines_empty_when_no_toml(self, tmp_path):
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.lines == {}

    def test_audio_dir_matches_path(self, tmp_path):
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.audio_dir == str(tmp_path)


# ---------------------------------------------------------------------------
# CharacterManager.load_from_dir — full toml
# ---------------------------------------------------------------------------


class TestLoadFromDirWithToml:
    def test_reads_name(self, tmp_path):
        _write_toml(tmp_path, FULL_TOML)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.name == "sung-jinwoo"

    def test_reads_display_name(self, tmp_path):
        _write_toml(tmp_path, FULL_TOML)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.display_name == "成ジヌ"

    def test_reads_description(self, tmp_path):
        _write_toml(tmp_path, FULL_TOML)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.description == "最弱のハンターから最強の影のモナーク"

    def test_reads_rvc_model(self, tmp_path):
        _write_toml(tmp_path, FULL_TOML)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.rvc_model == "jinwoo.pth"

    def test_reads_all_event_lines(self, tmp_path):
        _write_toml(tmp_path, FULL_TOML)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert len(char.lines) == 8

    def test_correct_line_values(self, tmp_path):
        _write_toml(tmp_path, FULL_TOML)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.lines["task_start"] == ["一人でいい", "狩りを始めよう"]
        assert char.lines["thinking"] == ["考えている", "分析中だ"]

    def test_fallback_name_from_dir_when_key_missing(self, tmp_path):
        toml = """\
[character]
display_name = "テスト"
"""
        _write_toml(tmp_path, toml)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.name == tmp_path.name

    def test_ignores_non_list_line_values(self, tmp_path):
        toml = """\
[character]
name = "test"

[lines]
coding = "not a list"
task_done = ["終わった"]
"""
        _write_toml(tmp_path, toml)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert "coding" not in char.lines
        assert "task_done" in char.lines

    def test_ignores_list_with_non_string_items(self, tmp_path):
        toml = """\
[character]
name = "test"

[lines]
coding = [1, 2, 3]
task_done = ["終わった"]
"""
        _write_toml(tmp_path, toml)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert "coding" not in char.lines

    def test_empty_lines_section(self, tmp_path):
        toml = """\
[character]
name = "empty"
"""
        _write_toml(tmp_path, toml)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.lines == {}


# ---------------------------------------------------------------------------
# CharacterManager.list_characters
# ---------------------------------------------------------------------------


class TestListCharacters:
    def test_empty_when_base_missing(self, tmp_path):
        missing = tmp_path / "nonexistent"
        result = CharacterManager.list_characters(str(missing))
        assert result == []

    def test_empty_when_no_subdirs(self, tmp_path):
        result = CharacterManager.list_characters(str(tmp_path))
        assert result == []

    def test_returns_one_character(self, tmp_path):
        (tmp_path / "jinwoo").mkdir()
        result = CharacterManager.list_characters(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "jinwoo"

    def test_returns_multiple_characters_sorted(self, tmp_path):
        for name in ("zel", "alice", "bob"):
            (tmp_path / name).mkdir()
        result = CharacterManager.list_characters(str(tmp_path))
        names = [c.name for c in result]
        assert names == sorted(names)

    def test_skips_files_in_base(self, tmp_path):
        (tmp_path / "jinwoo").mkdir()
        (tmp_path / "readme.txt").touch()
        result = CharacterManager.list_characters(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "jinwoo"

    def test_loads_toml_for_characters_that_have_it(self, tmp_path):
        char_dir = tmp_path / "jinwoo"
        char_dir.mkdir()
        _write_toml(char_dir, FULL_TOML)

        result = CharacterManager.list_characters(str(tmp_path))
        assert len(result) == 1
        assert result[0].display_name == "成ジヌ"

    def test_mixes_toml_and_no_toml(self, tmp_path):
        (tmp_path / "bare").mkdir()
        char_dir = tmp_path / "jinwoo"
        char_dir.mkdir()
        _write_toml(char_dir, FULL_TOML)

        result = CharacterManager.list_characters(str(tmp_path))
        names = {c.name for c in result}
        assert "bare" in names
        assert "sung-jinwoo" in names  # loaded from toml name field
