"""Tests for chuuni_voice.rvc."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as req_lib

from chuuni_voice.events import ChuuniEvent
from chuuni_voice.rvc import RVCClient, _find_source, convert_for_playback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(data: list | None = None) -> MagicMock:
    """Return a mock requests.Response with status 200 and optional data."""
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.json.return_value = {"data": data or []}
    return r


def _error_response(status: int = 500) -> MagicMock:
    r = MagicMock()
    r.ok = False
    r.status_code = status
    return r


def _make_audio(directory: Path, name: str) -> Path:
    f = directory / name
    f.touch()
    return f


# ---------------------------------------------------------------------------
# RVCClient.is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_returns_true_when_server_responds(self):
        client = RVCClient()
        with patch("requests.get", return_value=_ok_response()) as mock_get:
            assert client.is_available() is True
        mock_get.assert_called_once_with("http://127.0.0.1:7865/", timeout=3)

    def test_returns_true_on_any_http_response(self):
        """Even a 404 means the server is reachable."""
        client = RVCClient()
        with patch("requests.get", return_value=_error_response(404)):
            assert client.is_available() is True

    def test_returns_false_on_connection_error(self):
        client = RVCClient()
        with patch("requests.get", side_effect=req_lib.exceptions.ConnectionError()):
            assert client.is_available() is False

    def test_returns_false_on_timeout(self):
        client = RVCClient()
        with patch("requests.get", side_effect=req_lib.exceptions.Timeout()):
            assert client.is_available() is False

    def test_returns_false_on_any_exception(self):
        client = RVCClient()
        with patch("requests.get", side_effect=OSError("no route to host")):
            assert client.is_available() is False

    def test_uses_custom_host_and_port(self):
        client = RVCClient(host="192.168.1.5", port=9999)
        with patch("requests.get", return_value=_ok_response()) as mock_get:
            client.is_available()
        url = mock_get.call_args[0][0]
        assert url.startswith("http://192.168.1.5:9999/")


# ---------------------------------------------------------------------------
# RVCClient.convert
# ---------------------------------------------------------------------------


class TestConvert:
    def test_returns_true_on_success(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        with patch("requests.post", return_value=_ok_response()):
            result = RVCClient().convert(str(audio_in), str(audio_out), "my_model")

        assert result is True

    def test_returns_false_on_http_error(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        with patch("requests.post", return_value=_error_response(500)):
            result = RVCClient().convert(str(audio_in), str(audio_out), "model")

        assert result is False

    def test_returns_false_on_connection_error(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        with patch("requests.post", side_effect=req_lib.exceptions.ConnectionError()):
            result = RVCClient().convert(str(audio_in), str(audio_out), "model")

        assert result is False

    def test_returns_false_when_response_has_error_key(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        err_resp = MagicMock()
        err_resp.ok = True
        err_resp.status_code = 200
        err_resp.json.return_value = {"error": "model not found"}

        with patch("requests.post", return_value=err_resp):
            result = RVCClient().convert(str(audio_in), str(audio_out), "model")

        assert result is False

    def test_returns_false_on_any_exception(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        with patch("requests.post", side_effect=RuntimeError("boom")):
            result = RVCClient().convert(str(audio_in), str(audio_out), "model")

        assert result is False

    def test_never_raises(self, tmp_path):
        """convert() must not raise under any circumstances."""
        with patch("requests.post", side_effect=Exception("unexpected")):
            # Should return False, not raise
            result = RVCClient().convert("/nope/in.wav", "/nope/out.wav", "x")
        assert result is False

    def test_payload_contains_input_path(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        with patch("requests.post", return_value=_ok_response()) as mock_post:
            RVCClient().convert(str(audio_in), str(audio_out), "model", "idx.index")

        payload = mock_post.call_args[1]["json"]
        assert payload["fn_index"] == 0
        assert str(audio_in) in payload["data"]

    def test_payload_contains_model_and_index(self, tmp_path):
        audio_in = _make_audio(tmp_path, "input.wav")
        audio_out = tmp_path / "output.wav"

        with patch("requests.post", return_value=_ok_response()) as mock_post:
            RVCClient().convert(str(audio_in), str(audio_out), "hero.pth", "hero.index")

        data = mock_post.call_args[1]["json"]["data"]
        assert "hero.pth" in data
        assert "hero.index" in data

    def test_copies_server_output_to_output_path(self, tmp_path):
        """If the server returns a file path in data[0], it should be copied."""
        audio_in = _make_audio(tmp_path, "input.wav")
        server_out = _make_audio(tmp_path, "server_temp.wav")
        server_out.write_bytes(b"converted_audio")
        desired_out = tmp_path / "output" / "input.wav"
        desired_out.parent.mkdir()

        resp = _ok_response(data=[str(server_out)])
        with patch("requests.post", return_value=resp):
            result = RVCClient().convert(
                str(audio_in), str(desired_out), "model"
            )

        assert result is True
        assert desired_out.exists()
        assert desired_out.read_bytes() == b"converted_audio"

    def test_respects_custom_fn_index(self, tmp_path):
        audio_in = _make_audio(tmp_path, "in.wav")
        audio_out = tmp_path / "out.wav"

        with patch("requests.post", return_value=_ok_response()) as mock_post:
            RVCClient(fn_index=5).convert(str(audio_in), str(audio_out), "m")

        assert mock_post.call_args[1]["json"]["fn_index"] == 5

    def test_uses_correct_endpoint(self, tmp_path):
        audio_in = _make_audio(tmp_path, "in.wav")
        audio_out = tmp_path / "out.wav"

        with patch("requests.post", return_value=_ok_response()) as mock_post:
            RVCClient().convert(str(audio_in), str(audio_out), "m")

        url = mock_post.call_args[0][0]
        assert url.endswith("/run/predict")


# ---------------------------------------------------------------------------
# convert_for_playback
# ---------------------------------------------------------------------------


class TestConvertForPlayback:
    def _make_client(self, *, available: bool = True, convert_ok: bool = True) -> MagicMock:
        client = MagicMock(spec=RVCClient)
        client.is_available.return_value = available
        client.convert.return_value = convert_ok
        return client

    def test_returns_converted_path_on_success(self, tmp_path):
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _make_audio(source_dir, "coding.mp3")

        def fake_convert(in_path, out_path, model, index=""):
            Path(out_path).touch()
            return True

        client = self._make_client()
        client.convert.side_effect = fake_convert

        result = convert_for_playback(ChuuniEvent.CODING, str(tmp_path), client, "model")

        assert result is not None
        assert Path(result).parent.name == "converted"
        assert Path(result).name == "coding.mp3"

    def test_falls_back_to_source_when_rvc_unavailable(self, tmp_path):
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        audio = _make_audio(source_dir, "coding.mp3")

        client = self._make_client(available=False)

        result = convert_for_playback(ChuuniEvent.CODING, str(tmp_path), client, "model")

        assert result == str(audio)
        client.convert.assert_not_called()

    def test_falls_back_to_source_when_convert_fails(self, tmp_path):
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        audio = _make_audio(source_dir, "error.wav")

        client = self._make_client(convert_ok=False)

        result = convert_for_playback(ChuuniEvent.ERROR, str(tmp_path), client, "model")

        assert result == str(audio)

    def test_falls_back_to_source_when_output_not_created(self, tmp_path):
        """convert() returns True but didn't actually create the file → fallback."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        audio = _make_audio(source_dir, "error.wav")

        client = self._make_client(convert_ok=True)  # returns True but no file

        result = convert_for_playback(ChuuniEvent.ERROR, str(tmp_path), client, "m")

        assert result == str(audio)

    def test_returns_none_when_no_source_audio(self, tmp_path):
        (tmp_path / "source").mkdir()

        client = self._make_client()
        result = convert_for_playback(ChuuniEvent.TASK_START, str(tmp_path), client, "m")

        assert result is None
        client.is_available.assert_not_called()

    def test_returns_none_when_source_dir_missing(self, tmp_path):
        # source/ not created at all
        client = self._make_client()
        result = convert_for_playback(ChuuniEvent.CODING, str(tmp_path), client, "m")

        assert result is None

    def test_creates_converted_dir_on_success(self, tmp_path):
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _make_audio(source_dir, "bash_run.mp3")

        assert not (tmp_path / "converted").exists()

        def fake_convert(in_path, out_path, model, index=""):
            Path(out_path).touch()
            return True

        client = self._make_client()
        client.convert.side_effect = fake_convert

        convert_for_playback(ChuuniEvent.BASH_RUN, str(tmp_path), client, "m")

        assert (tmp_path / "converted").is_dir()

    def test_passes_index_path_to_convert(self, tmp_path):
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _make_audio(source_dir, "task_done.mp3")

        def fake_convert(in_path, out_path, model, index=""):
            Path(out_path).touch()
            return True

        client = self._make_client()
        client.convert.side_effect = fake_convert

        convert_for_playback(
            ChuuniEvent.TASK_DONE,
            str(tmp_path),
            client,
            "hero.pth",
            index_path="hero.index",
        )

        _, _, model_arg, index_arg = client.convert.call_args[0]
        assert model_arg == "hero.pth"
        assert index_arg == "hero.index"

    def test_does_not_raise_on_any_error(self, tmp_path):
        """convert_for_playback must never raise."""
        client = MagicMock(spec=RVCClient)
        client.is_available.side_effect = RuntimeError("chaos")

        result = convert_for_playback(ChuuniEvent.ERROR, str(tmp_path), client, "m")
        assert result is None

    def test_picks_variant_source_files(self, tmp_path):
        """Variant filenames like task_done_1.mp3 should be found."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _make_audio(source_dir, "task_done_1.mp3")

        def fake_convert(in_path, out_path, model, index=""):
            Path(out_path).touch()
            return True

        client = self._make_client()
        client.convert.side_effect = fake_convert

        result = convert_for_playback(ChuuniEvent.TASK_DONE, str(tmp_path), client, "m")

        assert result is not None
        assert "task_done_1" in result


# ---------------------------------------------------------------------------
# _find_source
# ---------------------------------------------------------------------------


class TestFindSource:
    def test_returns_exact_match(self, tmp_path):
        _make_audio(tmp_path, "coding.mp3")
        result = _find_source(ChuuniEvent.CODING, tmp_path)
        assert result is not None
        assert result.name == "coding.mp3"

    def test_returns_none_for_empty_dir(self, tmp_path):
        assert _find_source(ChuuniEvent.ERROR, tmp_path) is None

    def test_returns_none_for_missing_dir(self, tmp_path):
        assert _find_source(ChuuniEvent.ERROR, tmp_path / "nope") is None

    def test_returns_none_for_wrong_event(self, tmp_path):
        _make_audio(tmp_path, "coding.mp3")
        assert _find_source(ChuuniEvent.BASH_RUN, tmp_path) is None

    def test_picks_from_multiple_variants(self, tmp_path):
        files = [
            _make_audio(tmp_path, "error.mp3"),
            _make_audio(tmp_path, "error_1.mp3"),
            _make_audio(tmp_path, "error_2.mp3"),
        ]
        chosen = set()
        for _ in range(30):
            r = _find_source(ChuuniEvent.ERROR, tmp_path)
            if r:
                chosen.add(r.name)
        assert chosen == {"error.mp3", "error_1.mp3", "error_2.mp3"}
