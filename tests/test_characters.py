"""Tests for chuuni_voice.characters.base."""

from pathlib import Path

import pytest

from chuuni_voice.characters.base import Character, CharacterManager


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
"""


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

    def test_fallback_name_from_dir_when_key_missing(self, tmp_path):
        toml = """\
[character]
display_name = "テスト"
"""
        _write_toml(tmp_path, toml)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert char.name == tmp_path.name

    def test_ignores_lines_section(self, tmp_path):
        """[lines] in toml is ignored — audio files are the sole data source."""
        toml = """\
[character]
name = "test"

[lines]
coding = ["old custom line"]
"""
        _write_toml(tmp_path, toml)
        char = CharacterManager.load_from_dir(str(tmp_path))
        assert not hasattr(char, "lines")


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
