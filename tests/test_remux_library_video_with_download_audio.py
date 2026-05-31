import math
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
                "track_title_suffix": "(Assemblarr - possibly incorrect or incomplete)",
                "compat_stereo": {"enabled": False, "codec": "aac", "channels": 2, "bitrate": "192k", "prefer_default": True},
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
    assert "title=CZ (Assemblarr - possibly incorrect or incomplete)" in command


def test_build_ffmpeg_command_prefers_compat_stereo_as_default(monkeypatch):
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
                "track_title_suffix": "(Assemblarr - possibly incorrect or incomplete)",
                "compat_stereo": {
                    "enabled": True,
                    "codec": "aac",
                    "channels": 2,
                    "bitrate": "192k",
                    "prefer_default": True,
                    "title_suffix": "Stereo (Assemblarr - possibly incorrect or incomplete)",
                },
            }
        }
    }
    copied_tracks = [
        {
            "language": "cz",
            "output_path": "source.mka",
            "prepared_output_path": "synced.eac3",
            "mux_input_offset_seconds": 0.0,
        }
    ]
    stereo_tracks = [
        {
            "language": "cz",
            "output_path": "source.mka",
            "prepared_output_path": "stereo.aac",
            "mux_input_offset_seconds": 0.0,
        }
    ]

    monkeypatch.setattr(remux, "ffprobe_streams", fake_ffprobe_streams)

    command, _plan = remux.build_ffmpeg_command(Path("movie.mkv"), copied_tracks, stereo_tracks, Path("out.mkv"), config)

    assert "-disposition:a:2" in command
    disposition_positions = [index for index, value in enumerate(command) if value == "-disposition:a:2"]
    assert command[disposition_positions[-1] + 1] == "default"
    assert "title=CZ Stereo (Assemblarr - possibly incorrect or incomplete)" in command


def test_estimate_synced_duration_detects_short_source_audio():
    duration = remux.estimate_synced_duration(
        source_duration=6987.973,
        trim_start=0.969960786732231,
        trim_end=7043.488,
        rate=0.9999956509699354,
    )

    assert duration == pytest.approx(6987.033)


def test_estimate_synced_duration_accounts_for_negative_trim_start():
    duration = remux.estimate_synced_duration(
        source_duration=100.0,
        trim_start=-1.25,
        trim_end=120.0,
        rate=1.0,
    )

    assert duration == pytest.approx(101.25)


def test_sync_filter_can_pad_missing_tail_with_silence():
    config = {"postprocess_import": {"library_audio_remux": {"sync_audio": {"rate_tolerance": 0.0000001}}}}

    filter_value = remux.sync_filter(
        trim_start=0.5,
        trim_end=10.0,
        rate=1.0,
        channels=2,
        config=config,
        pad_to_duration=12.0,
    )

    assert filter_value.endswith("apad,atrim=0:12.000000000,asetpts=PTS-STARTPTS")


def test_can_pad_silence_only_for_short_candidate_within_limit():
    settings = {"duration_mismatch_policy": "pad_silence", "max_silence_padding_seconds": 90.0}

    assert remux.can_pad_silence(settings=settings, reference_duration=7043.488, candidate_duration=6987.033)
    assert not remux.can_pad_silence(settings=settings, reference_duration=7043.488, candidate_duration=6900.0)
    assert not remux.can_pad_silence(settings=settings, reference_duration=7043.488, candidate_duration=7050.0)


def test_synchronize_track_falls_back_to_passthrough_when_synaudio_produces_no_measurement(monkeypatch, tmp_path):
    config = {
        "postprocess_import": {
            "library_audio_remux": {
                "sync_audio": {
                    "enabled": True,
                    "precision_scale": 0.25,
                    "preflight_duration_tolerance_seconds": 45.0,
                    "passthrough_on_failure": True,
                }
            }
        }
    }
    track_path = tmp_path / "audio-01-cz.mka"
    track_path.write_bytes(b"fake")

    monkeypatch.setattr(remux, "reference_audio_stream_index", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(remux, "reference_audio_video_offset", lambda *_args, **_kwargs: 0.125)
    monkeypatch.setattr(remux, "archive_sync_root", lambda *_args, **_kwargs: tmp_path / "synced")
    monkeypatch.setattr(remux, "extract_reference_audio", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remux, "format_duration", lambda _path: 100.0)

    class FakeCompletedProcess:
        def __init__(self):
            self.stdout = "Decoding files..."
            self.stderr = "Wasm SIMD unsupported"
            self.returncode = 1

    monkeypatch.setattr(remux.subprocess, "run", lambda *_args, **_kwargs: FakeCompletedProcess())

    prepared, metadata = remux.synchronize_track(
        Path("movie.mkv"),
        {"language": "cz", "output_path": str(track_path), "channels": 2},
        "queue-id",
        config,
        dry_run=False,
    )

    assert prepared["prepared_output_path"] == str(track_path)
    assert prepared["mux_input_offset_seconds"] == pytest.approx(0.125)
    assert not prepared["sync_applied"]
    assert metadata["passthrough_on_sync_failure"] is True


def test_choose_sync_strategy_prefers_single_anchor_for_nearly_equal_duration():
    config = {
        "postprocess_import": {
            "library_audio_remux": {
                "sync_audio": {
                    "strategy": {
                        "method": "adaptive_anchor_sync",
                        "single_anchor_max_duration_diff_seconds": 2.0,
                    }
                }
            }
        }
    }

    strategy = remux.choose_sync_strategy(reference_duration=5000.0, source_duration=4998.8, config=config)

    assert strategy["method"] == "adaptive_anchor_sync"
    assert strategy["profile_name"] == "single_anchor"


def test_choose_sync_strategy_prefers_multi_anchor_for_larger_duration_diff():
    config = {
        "postprocess_import": {
            "library_audio_remux": {
                "sync_audio": {
                    "strategy": {
                        "method": "adaptive_anchor_sync",
                        "single_anchor_max_duration_diff_seconds": 2.0,
                    }
                }
            }
        }
    }

    strategy = remux.choose_sync_strategy(reference_duration=5000.0, source_duration=4990.0, config=config)

    assert strategy["profile_name"] == "multi_anchor"


def test_build_synaudio_command_uses_selected_profile_parameters():
    strategy = {
        "profile": {
            "rectify": True,
            "rate_tolerance": 0.5,
            "sample_length": 0.25,
            "sample_gap": 90.0,
            "start_range": 240.0,
            "end_range": 120.0,
        }
    }

    command = remux.build_synaudio_command(Path("reference.mka"), Path("source.mka"), strategy)

    assert command == [
        "synaudio-cli",
        "--rate-tolerance",
        "0.5",
        "--sample-length",
        "0.25",
        "--sample-gap",
        "90.0",
        "--start-range",
        "240.0",
        "--end-range",
        "120.0",
        "reference.mka",
        "source.mka",
    ]


def test_centered_padding_filter_centers_shorter_audio():
    filter_value = remux.centered_padding_filter(113.5, 5074.154, 2)

    assert filter_value == "adelay=113500|113500,apad,atrim=0:5074.154000000,asetpts=PTS-STARTPTS"


def test_parse_synaudio_measurement_accepts_nan_tokens():
    trim_start, trim_end, rate = remux.parse_synaudio_measurement("Trim start NaN Trim end 45 Rate NaN")

    assert math.isnan(trim_start)
    assert trim_end == 45.0
    assert math.isnan(rate)
