from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

psycopg = types.ModuleType("psycopg")
psycopg_rows = types.ModuleType("psycopg.rows")
psycopg_types = types.ModuleType("psycopg.types")
psycopg_types_json = types.ModuleType("psycopg.types.json")
psycopg_rows.dict_row = object()
psycopg_types_json.Jsonb = lambda value: value
sys.modules.setdefault("psycopg", psycopg)
sys.modules.setdefault("psycopg.rows", psycopg_rows)
sys.modules.setdefault("psycopg.types", psycopg_types)
sys.modules.setdefault("psycopg.types.json", psycopg_types_json)

import remux_library_video_with_download_audio as remux  # noqa: E402


def test_reference_audio_video_offset_uses_stream_start_times(monkeypatch):
    def fake_ffprobe_streams(_path):
        return [
            {"codec_type": "video", "start_time": "0.080000"},
            {"codec_type": "audio", "start_time": "0.000000"},
            {"codec_type": "audio", "start_time": "0.205000"},
        ]

    monkeypatch.setattr(remux, "ffprobe_streams", fake_ffprobe_streams)

    assert remux.reference_audio_video_offset(Path("movie.mkv"), 1) == pytest.approx(0.125)


def test_build_ffmpeg_command_applies_mux_input_offset(monkeypatch):
    def fake_ffprobe_streams(_path):
        return [
            {"codec_type": "video", "index": 0},
            {"codec_type": "audio", "index": 1},
        ]

    config = {
        "postprocess_import": {
            "library_audio_remux": {
                "include_original_audio": True,
                "set_preferred_audio_default": True,
                "compat_stereo": {"enabled": False, "codec": "aac", "channels": 2, "bitrate": "192k"},
            }
        }
    }
    copied_tracks = [
        {
            "language": "cz",
            "output_path": "source.mka",
            "prepared_output_path": "synced.eac3",
            "mux_input_offset_seconds": 0.125,
        }
    ]

    monkeypatch.setattr(remux, "ffprobe_streams", fake_ffprobe_streams)

    command, plan = remux.build_ffmpeg_command(Path("movie.mkv"), copied_tracks, [], Path("out.mkv"), config)

    assert command[:7] == ["ffmpeg", "-y", "-i", "movie.mkv", "-itsoffset", "0.125000000", "-i"]
    assert command[7] == "synced.eac3"
    assert "-map" in command
    assert plan["copied_languages"] == ["cz"]
