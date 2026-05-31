from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

psycopg = types.ModuleType("psycopg")
psycopg.OperationalError = Exception
psycopg_rows = types.ModuleType("psycopg.rows")
psycopg_types = types.ModuleType("psycopg.types")
psycopg_types_json = types.ModuleType("psycopg.types.json")
psycopg_rows.dict_row = object()
psycopg_types_json.Jsonb = lambda value: value
sys.modules["psycopg"] = psycopg
sys.modules["psycopg.rows"] = psycopg_rows
sys.modules["psycopg.types"] = psycopg_types
sys.modules["psycopg.types.json"] = psycopg_types_json

import run_pipeline_orchestrator as orchestrator  # noqa: E402


def test_orchestrator_config_uses_worker_defaults():
    config = {
        "download_worker": {
            "poll_seconds": 45,
            "queue_fill_per_cycle": 4,
            "max_targets_per_queue_run": 55,
            "postprocess": {"library_audio_remux": {"enabled": True, "max_per_cycle": 3}},
        },
        "pipeline_orchestrator": {},
    }

    resolved = orchestrator.orchestrator_config(config)

    assert resolved["missing_search"]["max_targets_per_run"] == 55
    assert resolved["missing_search"]["max_queue_additions_per_run"] == 4
    assert resolved["download_sync_extract"]["interval_seconds"] == 45
    assert resolved["library_audio_remux"]["max_per_run"] == 3


def test_build_queue_command_can_target_rss_waitlist():
    command = orchestrator.build_queue_command(target_source="rss_waitlist", max_targets=12)

    assert "--target-source" in command
    assert "rss_waitlist" in command
    assert command[-1] == "--apply"


def test_build_download_sync_extract_command_disables_queue_and_remux():
    command = orchestrator.build_download_sync_extract_command()

    assert "--once" in command
    assert "--no-fill-queue" in command
    assert "--no-library-audio-remux" in command
