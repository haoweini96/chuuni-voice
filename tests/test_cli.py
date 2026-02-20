"""Tests for chuuni_voice.cli — focusing on _dispatch logic."""

import pytest

from chuuni_voice.cli import _dispatch
from chuuni_voice.events import ChuuniEvent


def _bash_data(exit_code: int, output: str = "") -> dict:
    """Build a minimal PostToolUse Bash hook payload."""
    return {"tool_response": {"exit_code": exit_code, "output": output}}


class TestDispatchPostBash:
    # ── success ─────────────────────────────────────────────────────────────

    def test_exit_zero_returns_test_pass(self):
        assert _dispatch("post-bash", _bash_data(0)) is ChuuniEvent.TEST_PASS

    def test_exit_zero_with_output_still_test_pass(self):
        assert _dispatch("post-bash", _bash_data(0, "all good")) is ChuuniEvent.TEST_PASS

    # ── test failures (no crash keywords) ───────────────────────────────────

    def test_exit_nonzero_no_keywords_returns_test_fail(self):
        assert _dispatch("post-bash", _bash_data(1, "")) is ChuuniEvent.TEST_FAIL

    def test_pytest_failed_line_returns_test_fail(self):
        """'FAILED tests/...' from pytest must remain test_fail, not error."""
        output = "FAILED tests/test_foo.py::test_bar - assert False\n1 failed in 0.1s"
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.TEST_FAIL

    def test_assertion_error_in_output_returns_test_fail(self):
        """AssertionError (pytest assertion) must not trigger error sound."""
        output = "E   AssertionError: assert 0 == 1\n1 failed"
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.TEST_FAIL

    # ── runtime crashes → error ──────────────────────────────────────────────

    def test_traceback_in_output_returns_error(self):
        output = "Traceback (most recent call last):\n  File ...\nRuntimeError: oops"
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.ERROR

    def test_module_not_found_returns_error(self):
        output = (
            "Traceback (most recent call last):\n"
            "  File \"<string>\", line 1, in <module>\n"
            "ModuleNotFoundError: No module named 'nonexistent_module'\n"
        )
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.ERROR

    def test_import_error_returns_error(self):
        output = "Traceback (most recent call last):\nImportError: cannot import name 'x'"
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.ERROR

    def test_syntax_error_returns_error(self):
        output = "  File \"x.py\", line 1\n    (\nSyntaxError: unexpected EOF"
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.ERROR

    def test_name_error_returns_error(self):
        output = "Traceback (most recent call last):\nNameError: name 'x' is not defined"
        assert _dispatch("post-bash", _bash_data(1, output)) is ChuuniEvent.ERROR

    def test_crash_with_exit_zero_returns_test_pass(self):
        """Crash keywords in output but exit_code=0 must still be test_pass."""
        output = "Traceback...\nModuleNotFoundError: ..."
        assert _dispatch("post-bash", _bash_data(0, output)) is ChuuniEvent.TEST_PASS

    # ── alternate payload shape (flat keys) ──────────────────────────────────

    def test_flat_exit_code_key(self):
        """Some hook payloads put exit_code at the top level."""
        data = {"exit_code": 1, "output": ""}
        assert _dispatch("post-bash", data) is ChuuniEvent.TEST_FAIL

    def test_flat_output_key_with_traceback(self):
        data = {"exit_code": 1, "output": "Traceback (most recent call last):"}
        assert _dispatch("post-bash", data) is ChuuniEvent.ERROR

    # ── unknown context ───────────────────────────────────────────────────────

    def test_unknown_ctx_returns_none(self):
        assert _dispatch("unknown-ctx", {}) is None

    def test_empty_data_returns_test_pass(self):
        """Empty data → exit_code defaults to 0 → test_pass."""
        assert _dispatch("post-bash", {}) is ChuuniEvent.TEST_PASS
