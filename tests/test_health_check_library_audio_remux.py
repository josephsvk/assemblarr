from pathlib import Path
import sys
import types


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

import health_check_library_audio_remux as health  # noqa: E402


def test_sample_windows_for_short_file_returns_single_window():
    assert health.sample_windows(8.0, sample_length=12.0) == [(0.0, 8.0)]


def test_sample_windows_for_long_file_returns_start_middle_end():
    windows = health.sample_windows(120.0, sample_length=12.0)
    assert windows == [(0.0, 12.0), (54.0, 12.0), (108.0, 12.0)]


def test_default_track_health_warns_when_default_is_not_compat_profile():
    config = {
        "postprocess_import": {
            "library_audio_remux": {
                "compat_stereo": {"enabled": True, "prefer_default": True, "codec": "aac", "channels": 2}
            }
        }
    }
    streams = [
        {
            "codec_type": "audio",
            "index": 1,
            "codec_name": "eac3",
            "channels": 6,
            "disposition": {"default": 1},
            "tags": {"language": "cze", "title": "CZ"},
        }
    ]

    results = health.default_track_health(streams, config)

    assert results[0]["severity"] == "pass"
    assert results[1]["severity"] == "warn"


def test_metadata_health_warns_for_passthrough_sync_mode():
    results = health.metadata_health(
        {
            "sync_audio": [
                {
                    "sync_applied": False,
                    "synaudio_warning": "fallback",
                    "source_audio_path": "/tmp/cz.mka",
                }
            ]
        }
    )

    assert any(item["severity"] == "warn" and item["check"] == "sync_mode" for item in results)


def test_expected_languages_for_health_check_prefers_job_specific_remux_plan():
    verification = {"preferred_hits": ["cz"]}
    context = {"remux_metadata": {"remux_plan": {"copied_languages": ["cz"], "compat_stereo_languages": ["cz"]}}}

    expected = health.expected_languages_for_health_check({"postprocess_import": {"preferred_audio_languages": ["cz", "sk"]}}, context, verification)

    assert expected == ["cz"]
