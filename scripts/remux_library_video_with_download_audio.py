#!/usr/bin/env python3
"""Mux extracted CZ/SK audio tracks into the existing Radarr library movie file."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from extract_download_audio import ffprobe_streams
from queue_prowlarr_download import staging_paths
from remux_and_import_radarr_movie import (
    latest_movie_file_row,
    post_radarr_command,
    preferred_languages,
    radarr_client,
    replace_radarr_language_tags,
    sync_local_radarr_state,
    update_local_language_tag,
    verify_library_file_audio,
    wait_for_radarr_command,
)
from sync_library import ROOT, connect_postgres, ensure_schema, load_env, required_env


REMUX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS library_audio_remux_jobs (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  movie_id INTEGER NOT NULL,
  client_queue_id TEXT,
  library_video_path TEXT NOT NULL,
  temp_output_path TEXT NOT NULL,
  final_library_path TEXT,
  remux_status TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(download_job_id)
)
"""

LANGUAGE_CODE_MAP = {
    "cz": "cze",
    "sk": "slk",
}

SYNC_VALUE_PATTERN = r"(?:[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?|nan|inf(?:inity)?)"
SYNC_PATTERN = re.compile(
    rf"Trim start\s+({SYNC_VALUE_PATTERN})\s+Trim end\s+({SYNC_VALUE_PATTERN})\s+Rate\s+({SYNC_VALUE_PATTERN})",
    re.IGNORECASE,
)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("postprocess_import"), dict):
        raise SystemExit("Missing config: postprocess_import")
    if not isinstance(config.get("download_scan"), dict):
        raise SystemExit("Missing config: download_scan")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mux extracted CZ/SK audio into the existing Radarr library movie file.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--download-job-id", type=int, help="Specific download_jobs.id to process.")
    parser.add_argument("--apply", action="store_true", help="Perform the remux and replace the library movie file.")
    return parser.parse_args()


def remux_config(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config["postprocess_import"].get("library_audio_remux", {}))
    if not settings.get("enabled", True):
        raise SystemExit("postprocess_import.library_audio_remux.enabled is false")
    return settings


def choose_job(conn: Any, requested_id: int | None) -> dict[str, Any]:
    clauses = [
        "dj.source = 'radarr'",
        "dj.media_type = 'movie_file'",
        "dae.extraction_status = 'audio_extracted'",
        "mf.path IS NOT NULL",
    ]
    params: dict[str, Any] = {}
    if requested_id is not None:
        clauses.append("dj.id = %(download_job_id)s")
        params["download_job_id"] = requested_id
    else:
        clauses.append("dj.status IN ('audio_extracted', 'import_verification_failed', 'imported', 'missing_in_client')")
        clauses.append("larj.id IS NULL")

    row = conn.execute(
        f"""
        SELECT
            dj.id,
            dj.source,
            dj.media_type,
            dj.source_id,
            dj.status,
            dj.title,
            dj.release_title,
            dj.client_queue_id,
            dj.metadata,
            COALESCE(
                (dj.metadata->'target'->>'parent_source_id')::int,
                mf.parent_source_id
            ) AS movie_id,
            mf.path AS library_video_path,
            mi.path AS movie_library_path,
            dae.extracted_tracks,
            dae.output_root AS extraction_output_root
        FROM download_jobs dj
        LEFT JOIN media_files mf
          ON mf.source = dj.source
         AND mf.media_type = dj.media_type
         AND mf.source_id = dj.source_id
        LEFT JOIN media_items mi
          ON mi.source = dj.source
         AND mi.media_type = 'movie'
         AND mi.source_id = COALESCE(
             (dj.metadata->'target'->>'parent_source_id')::int,
             mf.parent_source_id
         )
        LEFT JOIN download_audio_extractions dae
          ON dae.client_queue_id = dj.client_queue_id
        LEFT JOIN library_audio_remux_jobs larj
          ON larj.download_job_id = dj.id
        WHERE {" AND ".join(clauses)}
        ORDER BY dj.updated_at DESC, dj.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        raise SystemExit("No eligible Radarr movie download job found for library-audio remux.")
    job = dict(row)
    if not job["movie_id"]:
        raise SystemExit("Eligible download job is missing parent Radarr movie id.")
    return job


def select_preferred_tracks(job: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    preferred = preferred_languages(config)
    extracted_tracks = list(job.get("extracted_tracks") or [])
    selected: list[dict[str, Any]] = []
    for language in preferred:
        for track in extracted_tracks:
            if str(track.get("language") or "").lower() != language:
                continue
            output_path = Path(str(track.get("output_path") or ""))
            if not output_path.exists():
                continue
            selected.append(dict(track))
            break
    if not selected:
        raise SystemExit("No extracted CZ/SK audio tracks are available on disk for this job.")
    return selected


def temp_output_path(library_video_path: Path, config: dict[str, Any]) -> Path:
    suffix = str(remux_config(config).get("temp_suffix", ".assemblarr-remux"))
    return library_video_path.with_name(f"{library_video_path.stem}{suffix}{library_video_path.suffix}")


def archive_root(config: dict[str, Any], queue_id: str) -> Path:
    workspace = staging_paths(config)
    subdir = str(remux_config(config).get("archive_subdir", "library_audio_archive")).strip("/")
    return workspace["archive"] / subdir / queue_id


def compat_stereo_config(config: dict[str, Any]) -> dict[str, Any]:
    return dict(remux_config(config).get("compat_stereo", {}))


def sync_audio_config(config: dict[str, Any]) -> dict[str, Any]:
    return dict(remux_config(config).get("sync_audio", {}))


def copied_track_title_suffix(config: dict[str, Any]) -> str:
    return str(remux_config(config).get("track_title_suffix", "(Assemblarr - possibly incorrect or incomplete)"))


def passthrough_on_sync_failure(settings: dict[str, Any]) -> bool:
    return bool(settings.get("passthrough_on_failure", True))


def sync_strategy_settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict(sync_audio_config(config).get("strategy", {}))


def choose_sync_strategy(
    *,
    reference_duration: float,
    source_duration: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    strategy = sync_strategy_settings(config)
    method = str(strategy.get("method", "adaptive_anchor_sync"))
    duration_diff = abs(reference_duration - source_duration)
    threshold = float(strategy.get("single_anchor_max_duration_diff_seconds", 2.0))
    profiles = dict(strategy.get("profiles", {}))
    default_single = {
        "sample_length": 0.25,
        "sample_gap": 90.0,
        "start_range": 240.0,
        "end_range": 120.0,
        "rate_tolerance": 0.5,
        "rectify": True,
    }
    default_multi = {
        "sample_length": 0.125,
        "sample_gap": 10.0,
        "start_range": 180.0,
        "end_range": 60.0,
        "rate_tolerance": 0.5,
        "rectify": True,
    }
    if method == "adaptive_anchor_sync":
        profile_name = "single_anchor" if duration_diff <= threshold else "multi_anchor"
    else:
        profile_name = str(strategy.get("fixed_profile", "multi_anchor"))
    raw_profile = dict(profiles.get(profile_name, {}))
    defaults = default_single if profile_name == "single_anchor" else default_multi
    profile = {**defaults, **raw_profile}
    return {
        "method": method,
        "profile_name": profile_name,
        "duration_diff_seconds": duration_diff,
        "single_anchor_max_duration_diff_seconds": threshold,
        "prefer_silence_windows": bool(strategy.get("prefer_silence_windows", False)),
        "silence_window_status": str(strategy.get("silence_window_status", "planned")),
        "profile": profile,
    }


def build_synaudio_command(reference_audio_path: Path, source_audio_path: Path, strategy_choice: dict[str, Any]) -> list[str]:
    profile = dict(strategy_choice.get("profile", {}))
    command = ["synaudio-cli"]
    if not bool(profile.get("rectify", True)):
        command.append("--no-rectify")
    if profile.get("rate_tolerance") is not None:
        command.extend(["--rate-tolerance", str(profile["rate_tolerance"])])
    if profile.get("sample_length") is not None:
        command.extend(["--sample-length", str(profile["sample_length"])])
    if profile.get("sample_gap") is not None:
        command.extend(["--sample-gap", str(profile["sample_gap"])])
    if profile.get("start_range") is not None:
        command.extend(["--start-range", str(profile["start_range"])])
    if profile.get("end_range") is not None:
        command.extend(["--end-range", str(profile["end_range"])])
    command.extend([str(reference_audio_path), str(source_audio_path)])
    return command


def extract_wav_segment(input_path: Path, start_seconds: float, duration_seconds: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start_seconds):.3f}",
            "-i",
            str(input_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def run_synaudio_measurement(
    reference_audio_path: Path,
    source_audio_path: Path,
    strategy_choice: dict[str, Any],
    precision_scale: float,
) -> tuple[subprocess.CompletedProcess[str], str]:
    synced_output_path = source_audio_path.with_name(f"{source_audio_path.stem}.synced{source_audio_path.suffix}")
    synced_output_path.unlink(missing_ok=True)
    synaudio_env = os.environ.copy()
    synaudio_env["SYNAUDIO_PRECISION_SCALE"] = str(precision_scale)
    synaudio = subprocess.run(
        build_synaudio_command(reference_audio_path, source_audio_path, strategy_choice),
        capture_output=True,
        text=True,
        check=False,
        env=synaudio_env,
    )
    combined_output = "\n".join(part for part in (synaudio.stdout, synaudio.stderr) if part)
    return synaudio, combined_output


def centered_padding_filter(start_padding_seconds: float, reference_duration: float, channels: int) -> str:
    delay_ms = max(0, int(round(start_padding_seconds * 1000)))
    return ",".join(
        [
            "adelay=" + "|".join([str(delay_ms)] * channels),
            "apad",
            f"atrim=0:{reference_duration:.9f}",
            "asetpts=PTS-STARTPTS",
        ]
    )


def centered_edge_probe(
    *,
    reference_audio_path: Path,
    source_audio_path: Path,
    reference_duration: float,
    source_duration: float,
    queue_id: str,
    language: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    strategy = sync_strategy_settings(config)
    probe = dict(strategy.get("center_trimmed_edges_probe", {}))
    probe_duration = float(probe.get("segment_duration_seconds", 45.0))
    probe_points = [float(item) for item in probe.get("anchor_points", [0.5, 0.75])]
    trim_tolerance = float(probe.get("trim_tolerance_seconds", 2.0))
    rate_tolerance = float(probe.get("rate_tolerance", 0.002))
    precision_scale = float(sync_audio_config(config).get("precision_scale", 1.0))
    probe_strategy = {
        "profile": {
            "rectify": True,
            "rate_tolerance": 0.5,
            "sample_length": probe.get("sample_length", 0.5),
            "sample_gap": probe.get("sample_gap", 9999.0),
            "start_range": probe.get("start_range", 12.0),
            "end_range": probe.get("end_range", 12.0),
        }
    }

    sync_root = archive_sync_root(config, queue_id) / f"probe-{language}"
    results: list[dict[str, Any]] = []
    for anchor in probe_points:
        reference_start = max(0.0, (reference_duration * anchor) - (probe_duration / 2.0))
        source_start = max(0.0, (source_duration * anchor) - (probe_duration / 2.0))
        reference_segment = sync_root / f"reference-{anchor:.2f}.wav"
        source_segment = sync_root / f"source-{anchor:.2f}.wav"
        extract_wav_segment(reference_audio_path, reference_start, probe_duration, reference_segment)
        extract_wav_segment(source_audio_path, source_start, probe_duration, source_segment)
        synaudio, combined_output = run_synaudio_measurement(reference_segment, source_segment, probe_strategy, precision_scale)
        trim_start, trim_end, rate = parse_synaudio_measurement(combined_output)
        finite_measurement = all(math.isfinite(value) for value in (trim_start, trim_end, rate))
        results.append(
            {
                "anchor": anchor,
                "reference_start_seconds": reference_start,
                "source_start_seconds": source_start,
                "segment_duration_seconds": probe_duration,
                "trim_start": trim_start,
                "trim_end": trim_end,
                "rate": rate,
                "exit_code": synaudio.returncode,
                "ok": (
                    synaudio.returncode == 0
                    and finite_measurement
                    and abs(trim_start) <= trim_tolerance
                    and abs(rate - 1.0) <= rate_tolerance
                ),
            }
        )

    start_padding_seconds = max(0.0, (reference_duration - source_duration) / 2.0)
    end_padding_seconds = max(0.0, reference_duration - source_duration - start_padding_seconds)
    return {
        "verified": all(item["ok"] for item in results),
        "probe_points": probe_points,
        "trim_tolerance_seconds": trim_tolerance,
        "rate_tolerance": rate_tolerance,
        "results": results,
        "start_padding_seconds": start_padding_seconds,
        "end_padding_seconds": end_padding_seconds,
    }


def normalized_stream_language(stream: dict[str, Any]) -> str | None:
    tags = dict(stream.get("tags") or {})
    value = str(tags.get("language") or "").lower()
    if value in {"cz", "cs", "cze", "ces", "czech"}:
        return "cz"
    if value in {"sk", "svk", "slk", "slo", "slovak", "slovakian"}:
        return "sk"
    return None


def archive_sync_root(config: dict[str, Any], queue_id: str) -> Path:
    subdir = str(sync_audio_config(config).get("archive_subdir", "synced_audio")).strip("/")
    return archive_root(config, queue_id) / subdir


def reference_audio_stream_index(library_video_path: Path, config: dict[str, Any]) -> int:
    configured = sync_audio_config(config).get("reference_audio_stream_index")
    if configured is not None:
        return int(configured)
    audio_streams = [stream for stream in ffprobe_streams(library_video_path) if str(stream.get("codec_type") or "") == "audio"]
    if not audio_streams:
        raise SystemExit(f"No audio stream found in library video: {library_video_path}")
    for position, stream in enumerate(audio_streams):
        disposition = dict(stream.get("disposition") or {})
        if int(disposition.get("default") or 0) == 1:
            return position
    return 0


def sync_audio_bitrate(channels: int, config: dict[str, Any]) -> str:
    settings = sync_audio_config(config)
    if channels <= 2:
        return str(settings.get("synced_bitrate_2ch", "384k"))
    return str(settings.get("synced_bitrate_multichannel", "640k"))


def float_or_none(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stream_start_time(stream: dict[str, Any]) -> float:
    start_time = float_or_none(stream.get("start_time"))
    if start_time is not None:
        return start_time
    tags = dict(stream.get("tags") or {})
    duration = float_or_none(tags.get("DURATION"))
    if duration is not None:
        return 0.0
    return 0.0


def reference_audio_video_offset(library_video_path: Path, audio_stream_index: int) -> float:
    streams = ffprobe_streams(library_video_path)
    video_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "video"]
    audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]
    if not video_streams:
        raise SystemExit(f"No video stream found in library video: {library_video_path}")
    if audio_stream_index >= len(audio_streams):
        raise SystemExit(f"Reference audio stream index {audio_stream_index} does not exist in: {library_video_path}")
    return stream_start_time(audio_streams[audio_stream_index]) - stream_start_time(video_streams[0])


def parse_synaudio_measurement(output: str) -> tuple[float, float, float]:
    match = SYNC_PATTERN.search(output)
    if not match:
        raise RuntimeError("Failed to parse Trim start / Trim end / Rate from synaudio output")
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def sync_filter(
    trim_start: float,
    trim_end: float,
    rate: float,
    channels: int,
    config: dict[str, Any],
    pad_to_duration: float | None = None,
) -> str:
    rate_tolerance = float(sync_audio_config(config).get("rate_tolerance", 0.0000001))
    parts: list[str] = []
    if trim_start < 0:
        delay_ms = int(round(abs(trim_start) * 1000))
        parts.append("adelay=" + "|".join([str(delay_ms)] * channels))
    else:
        parts.append(f"atrim=start={trim_start:.9f}")
        parts.append("asetpts=PTS-STARTPTS")
    if abs(rate - 1.0) > rate_tolerance:
        parts.append(f"atempo={rate:.15g}")
    if pad_to_duration is not None:
        parts.append("apad")
        parts.append(f"atrim=0:{pad_to_duration:.9f}")
    else:
        parts.append(f"atrim=0:{trim_end:.9f}")
    parts.append("asetpts=PTS-STARTPTS")
    return ",".join(parts)


def estimate_synced_duration(source_duration: float, trim_start: float, trim_end: float, rate: float) -> float:
    if rate <= 0:
        raise RuntimeError(f"Invalid synaudio rate: {rate}")
    if trim_start < 0:
        available_duration = source_duration + abs(trim_start)
    else:
        available_duration = max(0.0, source_duration - trim_start)
    adjusted_duration = available_duration / rate
    return min(trim_end, adjusted_duration)


def can_pad_silence(
    *,
    settings: dict[str, Any],
    reference_duration: float,
    candidate_duration: float,
) -> bool:
    if str(settings.get("duration_mismatch_policy", "reject")).lower() != "pad_silence":
        return False
    missing_duration = reference_duration - candidate_duration
    if missing_duration <= 0:
        return False
    max_padding = float(settings.get("max_silence_padding_seconds", 0.0))
    return missing_duration <= max_padding


def raise_duration_mismatch(
    *,
    phase: str,
    reference_duration: float,
    source_duration: float,
    candidate_duration: float,
    tolerance: float,
    reference_audio_path: Path,
    source_audio_path: Path,
) -> None:
    duration_diff = abs(reference_duration - candidate_duration)
    raise SystemExit(
        "Audio sync candidate duration mismatch "
        f"during {phase}: {duration_diff:.6f}s > {tolerance:.6f}s. "
        f"reference={reference_duration:.6f}s, source={source_duration:.6f}s, "
        f"candidate={candidate_duration:.6f}s. "
        "This usually means the extracted dubbing is from a different cut/release "
        "and should not be muxed into the library file. "
        f"reference_audio={reference_audio_path}; source_audio={source_audio_path}"
    )


def format_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def extract_reference_audio(library_video_path: Path, stream_index: int, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-i",
            str(library_video_path),
            "-map",
            f"0:a:{stream_index}",
            "-vn",
            "-sn",
            "-dn",
            "-c",
            "copy",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def synchronize_track(
    library_video_path: Path,
    track: dict[str, Any],
    queue_id: str,
    config: dict[str, Any],
    dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = sync_audio_config(config)
    language = str(track["language"]).lower()
    source_audio_path = Path(str(track["output_path"]))
    sync_root = archive_sync_root(config, queue_id)
    reference_index = reference_audio_stream_index(library_video_path, config)
    mux_input_offset = reference_audio_video_offset(library_video_path, reference_index)
    reference_audio_path = sync_root / f"reference-a{reference_index:02d}.mka"
    synced_output_path = sync_root / f"audio-synced-{language}.eac3"
    metadata = {
        "enabled": bool(settings.get("enabled", True)),
        "reference_audio_stream_index": reference_index,
        "reference_audio_video_offset_seconds": mux_input_offset,
        "reference_audio_path": str(reference_audio_path),
        "source_audio_path": str(source_audio_path),
        "synced_output_path": str(synced_output_path),
    }
    if dry_run or not bool(settings.get("enabled", True)):
        prepared = dict(track)
        prepared["prepared_output_path"] = str(source_audio_path)
        prepared["sync_applied"] = False
        prepared["mux_input_offset_seconds"] = 0.0
        return prepared, metadata

    sync_root.mkdir(parents=True, exist_ok=True)
    extract_reference_audio(library_video_path, reference_index, reference_audio_path)
    reference_duration = format_duration(reference_audio_path)
    source_duration = format_duration(source_audio_path)
    source_duration_diff = abs(reference_duration - source_duration)
    metadata.update(
        {
            "reference_duration": reference_duration,
            "source_duration": source_duration,
            "source_duration_diff": source_duration_diff,
        }
    )
    strategy_choice = choose_sync_strategy(
        reference_duration=reference_duration,
        source_duration=source_duration,
        config=config,
    )
    metadata["sync_strategy"] = strategy_choice
    preflight_tolerance = settings.get("preflight_duration_tolerance_seconds")
    if preflight_tolerance is not None:
        preflight_tolerance_float = float(preflight_tolerance)
        if source_duration_diff > preflight_tolerance_float and not can_pad_silence(
            settings=settings,
            reference_duration=reference_duration,
            candidate_duration=source_duration,
        ):
            if (
                str(strategy_choice.get("method")) == "center_anchored_trimmed_edges"
                and source_duration < reference_duration
            ):
                probe = centered_edge_probe(
                    reference_audio_path=reference_audio_path,
                    source_audio_path=source_audio_path,
                    reference_duration=reference_duration,
                    source_duration=source_duration,
                    queue_id=queue_id,
                    language=language,
                    config=config,
                )
                metadata["center_trimmed_edges_probe"] = probe
                if probe["verified"]:
                    channels = int(track.get("channels") or 6)
                    bitrate = sync_audio_bitrate(channels, config)
                    filter_value = centered_padding_filter(
                        probe["start_padding_seconds"],
                        reference_duration,
                        channels,
                    )
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-hide_banner",
                            "-i",
                            str(source_audio_path),
                            "-filter:a",
                            filter_value,
                            "-ar",
                            "48000",
                            "-ac",
                            str(channels),
                            "-c:a",
                            str(settings.get("synced_codec", "eac3")),
                            "-b:a",
                            bitrate,
                            str(synced_output_path),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    synced_duration = format_duration(synced_output_path)
                    metadata.update(
                        {
                            "sync_applied": True,
                            "center_trimmed_edges_applied": True,
                            "filter": filter_value,
                            "channels": channels,
                            "bitrate": bitrate,
                            "synced_duration": synced_duration,
                            "duration_diff": abs(reference_duration - synced_duration),
                        }
                    )
                    prepared = dict(track)
                    prepared["prepared_output_path"] = str(synced_output_path)
                    prepared["sync_applied"] = True
                    prepared["mux_input_offset_seconds"] = mux_input_offset
                    return prepared, metadata
            raise_duration_mismatch(
                phase="source preflight",
                reference_duration=reference_duration,
                source_duration=source_duration,
                candidate_duration=source_duration,
                tolerance=preflight_tolerance_float,
                reference_audio_path=reference_audio_path,
                source_audio_path=source_audio_path,
            )

    try:
        synaudio, combined_output = run_synaudio_measurement(
            reference_audio_path,
            source_audio_path,
            strategy_choice,
            float(settings.get("precision_scale", 1.0)),
        )
    except FileNotFoundError:
        if passthrough_on_sync_failure(settings):
            metadata.update(
                {
                    "synaudio_exit_code": None,
                    "synaudio_warning": (
                        "synaudio-cli is not available in this runtime image; "
                        "using source audio as a provisional passthrough track with only the reference mux offset applied"
                    ),
                    "sync_applied": False,
                    "passthrough_on_sync_failure": True,
                }
            )
            prepared = dict(track)
            prepared["prepared_output_path"] = str(source_audio_path)
            prepared["sync_applied"] = False
            prepared["mux_input_offset_seconds"] = mux_input_offset
            return prepared, metadata
        raise
    synaudio_warning = None
    try:
        trim_start, trim_end, rate = parse_synaudio_measurement(combined_output)
    except RuntimeError:
        if passthrough_on_sync_failure(settings):
            metadata.update(
                {
                    "synaudio_exit_code": synaudio.returncode,
                    "synaudio_warning": (
                        "synaudio-cli failed before producing sync measurements; "
                        "using source audio as a provisional passthrough track with only the reference mux offset applied"
                    ),
                    "sync_applied": False,
                    "passthrough_on_sync_failure": True,
                }
            )
            prepared = dict(track)
            prepared["prepared_output_path"] = str(source_audio_path)
            prepared["sync_applied"] = False
            prepared["mux_input_offset_seconds"] = mux_input_offset
            return prepared, metadata
        raise
    synaudio_warning = None
    if synaudio.returncode != 0:
        synaudio_warning = (
            "synaudio-cli returned a non-zero exit code after producing sync measurements; "
            "continuing with the local ffmpeg remux path"
        )

    channels = int(track.get("channels") or 6)
    bitrate = sync_audio_bitrate(channels, config)
    estimated_duration = estimate_synced_duration(source_duration, trim_start, trim_end, rate)
    tolerance = float(settings.get("duration_tolerance_seconds", 0.25))
    pad_to_duration: float | None = None
    if abs(reference_duration - estimated_duration) > tolerance:
        if can_pad_silence(
            settings=settings,
            reference_duration=reference_duration,
            candidate_duration=estimated_duration,
        ):
            pad_to_duration = reference_duration
        else:
            raise_duration_mismatch(
                phase="sync measurement",
                reference_duration=reference_duration,
                source_duration=source_duration,
                candidate_duration=estimated_duration,
                tolerance=tolerance,
                reference_audio_path=reference_audio_path,
                source_audio_path=source_audio_path,
            )

    filter_value = sync_filter(trim_start, trim_end, rate, channels, config, pad_to_duration=pad_to_duration)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-i",
            str(source_audio_path),
            "-filter:a",
            filter_value,
            "-ar",
            "48000",
            "-ac",
            str(channels),
            "-c:a",
            str(settings.get("synced_codec", "eac3")),
            "-b:a",
            bitrate,
            str(synced_output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    synced_duration = format_duration(synced_output_path)
    duration_diff = abs(reference_duration - synced_duration)
    if duration_diff > tolerance:
        raise_duration_mismatch(
            phase="encoded output validation",
            reference_duration=reference_duration,
            source_duration=source_duration,
            candidate_duration=synced_duration,
            tolerance=tolerance,
            reference_audio_path=reference_audio_path,
            source_audio_path=source_audio_path,
        )

    metadata.update(
        {
            "trim_start": trim_start,
            "trim_end": trim_end,
            "rate": rate,
            "channels": channels,
            "bitrate": bitrate,
            "filter": filter_value,
            "estimated_synced_duration": estimated_duration,
            "synced_duration": synced_duration,
            "duration_diff": duration_diff,
            "synaudio_exit_code": synaudio.returncode,
            "synaudio_warning": synaudio_warning,
            "silence_padding_applied": pad_to_duration is not None,
            "silence_padding_seconds": max(0.0, reference_duration - estimated_duration) if pad_to_duration else 0.0,
            "sync_applied": True,
        }
    )
    prepared = dict(track)
    prepared["prepared_output_path"] = str(synced_output_path)
    prepared["sync_applied"] = True
    prepared["mux_input_offset_seconds"] = mux_input_offset
    return prepared, metadata


def synchronize_tracks(
    library_video_path: Path,
    selected_tracks: list[dict[str, Any]],
    queue_id: str,
    config: dict[str, Any],
    dry_run: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared_tracks: list[dict[str, Any]] = []
    sync_metadata: list[dict[str, Any]] = []
    for track in selected_tracks:
        prepared, metadata = synchronize_track(library_video_path, track, queue_id, config, dry_run)
        prepared_tracks.append(prepared)
        sync_metadata.append(metadata)
    return prepared_tracks, sync_metadata


def verify_compat_stereo_tracks(library_path: Path, expected_languages: list[str], config: dict[str, Any]) -> dict[str, Any]:
    compat_settings = compat_stereo_config(config)
    if not bool(compat_settings.get("enabled", True)):
        return {"enabled": False, "required_languages": [], "present_languages": [], "verified": True}
    streams = ffprobe_streams(library_path)
    present_languages: set[str] = set()
    expected_codec = str(compat_settings.get("codec", "aac")).lower()
    expected_channels = int(compat_settings.get("channels", 2))
    for stream in streams:
        if str(stream.get("codec_type") or "") != "audio":
            continue
        language = normalized_stream_language(stream)
        if language is None:
            continue
        channels = int(stream.get("channels") or 0)
        codec_name = str(stream.get("codec_name") or "").lower()
        if channels == expected_channels and codec_name == expected_codec:
            present_languages.add(language)
    required_languages = sorted({language for language in expected_languages})
    return {
        "enabled": True,
        "required_languages": required_languages,
        "present_languages": sorted(present_languages),
        "verified": set(required_languages).issubset(present_languages),
        "codec": expected_codec,
        "channels": expected_channels,
    }


def build_ffmpeg_command(
    library_video_path: Path,
    copied_tracks: list[dict[str, Any]],
    stereo_tracks: list[dict[str, Any]],
    output_path: Path,
    config: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    settings = remux_config(config)
    compat_settings = compat_stereo_config(config)
    library_streams = ffprobe_streams(library_video_path)
    original_audio_count = sum(1 for stream in library_streams if str(stream.get("codec_type") or "") == "audio")

    command = ["ffmpeg", "-y", "-i", str(library_video_path)]
    input_tracks = copied_tracks + stereo_tracks
    for track in input_tracks:
        offset = float_or_none(track.get("mux_input_offset_seconds")) or 0.0
        if abs(offset) >= 0.001:
            command.extend(["-itsoffset", f"{offset:.9f}"])
        command.extend(["-i", str(track.get("prepared_output_path") or track["output_path"])])

    if settings.get("include_original_audio", True):
        command.extend(["-map", "0"])
    else:
        command.extend(["-map", "0:v", "-map", "0:s?", "-map", "0:d?", "-map", "0:t?"])

    for input_index in range(1, len(copied_tracks) + 1):
        command.extend(["-map", f"{input_index}:a:0"])
    stereo_start_input = 1 + len(copied_tracks)
    for input_index in range(stereo_start_input, stereo_start_input + len(stereo_tracks)):
        command.extend(["-map", f"{input_index}:a:0"])

    command.extend(["-c", "copy"])

    copied_selected_count = len(copied_tracks)
    compat_selected_count = len(stereo_tracks)
    total_output_audio = (
        (original_audio_count if settings.get("include_original_audio", True) else 0)
        + copied_selected_count
        + compat_selected_count
    )
    extra_start = total_output_audio - (copied_selected_count + compat_selected_count)
    compat_start = extra_start + copied_selected_count
    if settings.get("set_preferred_audio_default", True) and total_output_audio > 0:
        for output_audio_index in range(total_output_audio):
            command.extend([f"-disposition:a:{output_audio_index}", "0"])
        default_index = 0 if settings.get("include_original_audio", True) else None
        if compat_selected_count and compat_settings.get("prefer_default", True):
            default_index = compat_start
        elif copied_selected_count:
            default_index = extra_start
        if default_index is not None:
            command.extend([f"-disposition:a:{default_index}", "default"])

    for offset, track in enumerate(copied_tracks):
        language = str(track["language"]).lower()
        ffmpeg_code = LANGUAGE_CODE_MAP.get(language, language)
        output_audio_index = extra_start + offset
        command.extend([f"-metadata:s:a:{output_audio_index}", f"language={ffmpeg_code}"])
        command.extend([f"-metadata:s:a:{output_audio_index}", f"title={language.upper()} {copied_track_title_suffix(config)}"])

    for offset, track in enumerate(stereo_tracks):
        language = str(track["language"]).lower()
        ffmpeg_code = LANGUAGE_CODE_MAP.get(language, language)
        output_audio_index = compat_start + offset
        command.extend([f"-c:a:{output_audio_index}", str(compat_settings.get("codec", "aac"))])
        command.extend([f"-ac:a:{output_audio_index}", str(compat_settings.get("channels", 2))])
        command.extend([f"-b:a:{output_audio_index}", str(compat_settings.get("bitrate", "192k"))])
        command.extend([f"-metadata:s:a:{output_audio_index}", f"language={ffmpeg_code}"])
        title_suffix = str(compat_settings.get("title_suffix", "Stereo (Assemblarr)"))
        command.extend([f"-metadata:s:a:{output_audio_index}", f"title={language.upper()} {title_suffix}"])

    command.append(str(output_path))
    plan = {
        "original_audio_stream_count": original_audio_count,
        "copied_languages": [str(track["language"]).lower() for track in copied_tracks],
        "copied_track_paths": [str(track.get("prepared_output_path") or track["output_path"]) for track in copied_tracks],
        "compat_stereo_enabled": bool(compat_settings.get("enabled", True)),
        "compat_stereo_languages": [str(track["language"]).lower() for track in stereo_tracks],
        "compat_stereo_track_paths": [str(track.get("prepared_output_path") or track["output_path"]) for track in stereo_tracks],
        "compat_stereo_codec": str(compat_settings.get("codec", "aac")),
        "compat_stereo_bitrate": str(compat_settings.get("bitrate", "192k")),
        "copied_track_title_suffix": copied_track_title_suffix(config),
        "compat_stereo_prefer_default": bool(compat_settings.get("prefer_default", True)),
    }
    return command, plan


def archive_selected_tracks(selected_tracks: list[dict[str, Any]], archive_dir: Path, dry_run: bool) -> list[str]:
    archived_paths: list[str] = []
    if dry_run:
        for track in selected_tracks:
            archived_paths.append(str(archive_dir / Path(str(track["output_path"])).name))
        return archived_paths
    archive_dir.mkdir(parents=True, exist_ok=True)
    for track in selected_tracks:
        source_path = Path(str(track["output_path"]))
        destination = archive_dir / source_path.name
        shutil.copy2(source_path, destination)
        archived_paths.append(str(destination))
    return archived_paths


def replace_library_file(original_path: Path, temp_path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    if not temp_path.exists():
        raise RuntimeError(f"Temporary remux output does not exist: {temp_path}")
    backup_path = original_path.with_name(f"{original_path.name}.assemblarr-backup")
    if backup_path.exists():
        backup_path.unlink()
    original_path.replace(backup_path)
    try:
        temp_path.replace(original_path)
    except Exception:
        if backup_path.exists() and not original_path.exists():
            backup_path.replace(original_path)
        raise
    backup_path.unlink(missing_ok=True)


def upsert_remux_job(
    conn: Any,
    *,
    job: dict[str, Any],
    temp_output: Path,
    final_library_path: str | None,
    remux_status: str,
    metadata: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    conn.execute(
        """
        INSERT INTO library_audio_remux_jobs (
            download_job_id, source, source_id, movie_id, client_queue_id,
            library_video_path, temp_output_path, final_library_path, remux_status, metadata, updated_at
        )
        VALUES (
            %(download_job_id)s, %(source)s, %(source_id)s, %(movie_id)s, %(client_queue_id)s,
            %(library_video_path)s, %(temp_output_path)s, %(final_library_path)s, %(remux_status)s, %(metadata)s, now()
        )
        ON CONFLICT (download_job_id)
        DO UPDATE SET
            library_video_path = EXCLUDED.library_video_path,
            temp_output_path = EXCLUDED.temp_output_path,
            final_library_path = EXCLUDED.final_library_path,
            remux_status = EXCLUDED.remux_status,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "download_job_id": job["id"],
            "source": job["source"],
            "source_id": job["source_id"],
            "movie_id": job["movie_id"],
            "client_queue_id": job.get("client_queue_id"),
            "library_video_path": str(job["library_video_path"]),
            "temp_output_path": str(temp_output),
            "final_library_path": final_library_path,
            "remux_status": remux_status,
            "metadata": Jsonb(metadata),
        },
    )


def update_download_job(conn: Any, job_id: int, status: str, metadata: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    conn.execute(
        """
        UPDATE download_jobs
        SET status = %(status)s,
            metadata = metadata || %(metadata)s::jsonb,
            updated_at = now()
        WHERE id = %(id)s
        """,
        {"id": job_id, "status": status, "metadata": Jsonb(metadata)},
    )


def record_missing_library_video_job(
    conn: Any,
    *,
    job: dict[str, Any],
    library_video_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    metadata = {
        "error": "library_video_missing",
        "library_video_path": str(library_video_path),
    }
    temp_output = library_video_path.with_name(f"{library_video_path.name}.assemblarr-temp")
    upsert_remux_job(
        conn,
        job=job,
        temp_output=temp_output,
        final_library_path=None,
        remux_status="library_video_missing",
        metadata=metadata,
        dry_run=dry_run,
    )
    update_download_job(
        conn,
        int(job["id"]),
        "library_audio_missing_library_path",
        {"library_audio_remux": metadata},
        dry_run=dry_run,
    )
    if not dry_run:
        conn.commit()
    return metadata


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)
    base_url, api_key = radarr_client(config)

    with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
        conn.row_factory = dict_row
        ensure_schema(conn)
        conn.execute(REMUX_TABLE_SQL)

        job = choose_job(conn, args.download_job_id)
        library_video_path = Path(str(job["library_video_path"]))
        if not library_video_path.exists():
            missing_library_metadata = record_missing_library_video_job(
                conn,
                job=job,
                library_video_path=library_video_path,
                dry_run=dry_run,
            )
            print("Library video + download audio remux")
            print(f"dry_run: {dry_run}")
            print(f"download_job_id: {job['id']}")
            print(f"title: {job['title']}")
            print(f"library_video_path: {library_video_path}")
            print("note: skipped stale remux job because the current library video path does not exist")
            print("error: " + str(missing_library_metadata["error"]))
            return 0

        selected_tracks = select_preferred_tracks(job, config)
        temp_output = temp_output_path(library_video_path, config)
        queue_id = str(job.get("client_queue_id") or f"job-{job['id']}")
        archive_dir = archive_root(config, queue_id)

        remux_verification: dict[str, Any] | None = None
        remux_compat_verification: dict[str, Any] | None = None
        final_library_verification: dict[str, Any] | None = None
        final_library_compat_verification: dict[str, Any] | None = None
        final_tag: dict[str, Any] | None = None
        final_library_path = str(library_video_path)
        archived_audio_paths: list[str] = archive_selected_tracks(selected_tracks, archive_dir, dry_run)
        prepared_tracks, sync_metadata = synchronize_tracks(
            library_video_path,
            selected_tracks,
            queue_id,
            config,
            dry_run,
        )
        current_library_verification = verify_library_file_audio(library_video_path, config)
        current_library_compat_verification = verify_compat_stereo_tracks(
            library_video_path,
            [str(track["language"]).lower() for track in selected_tracks],
            config,
        )
        current_languages = set(current_library_verification["preferred_hits"])
        current_compat_languages = set(current_library_compat_verification["present_languages"])
        copied_tracks = [track for track in prepared_tracks if str(track["language"]).lower() not in current_languages]
        stereo_tracks = [track for track in prepared_tracks if str(track["language"]).lower() not in current_compat_languages]
        ffmpeg_command, remux_plan = build_ffmpeg_command(library_video_path, copied_tracks, stereo_tracks, temp_output, config)

        if args.apply:
            if current_library_verification["verified"] and current_library_compat_verification["verified"]:
                final_library_verification = current_library_verification
                final_library_compat_verification = current_library_compat_verification
                verification_payload = {
                    "preferred_hits": final_library_verification["preferred_hits"],
                    "detected_languages": final_library_verification["matched_languages"],
                }
                final_tag = replace_radarr_language_tags(
                    base_url,
                    api_key,
                    int(job["movie_id"]),
                    final_library_path,
                    verification_payload,
                    config,
                )
                update_local_language_tag(conn, int(job["movie_id"]), final_library_path, final_tag, config)
                upsert_remux_job(
                    conn,
                    job=job,
                    temp_output=temp_output,
                    final_library_path=final_library_path,
                    remux_status="library_audio_applied",
                    metadata={
                        "archived_audio_paths": archived_audio_paths,
                        "sync_audio": sync_metadata,
                        "final_library_verification": final_library_verification,
                        "final_library_compat_verification": final_library_compat_verification,
                        "final_tag": final_tag,
                        "recovered_existing_library_audio": True,
                    },
                    dry_run=dry_run,
                )
                update_download_job(
                    conn,
                    int(job["id"]),
                    "imported",
                    {
                        "library_audio_remux": {
                            "library_video_path": final_library_path,
                            "archived_audio_paths": archived_audio_paths,
                            "sync_audio": sync_metadata,
                            "final_library_verification": final_library_verification,
                            "final_library_compat_verification": final_library_compat_verification,
                            "final_tag": final_tag,
                            "recovered_existing_library_audio": True,
                        }
                    },
                    dry_run=dry_run,
                )
                conn.commit()
                print("Library video + download audio remux")
                print(f"dry_run: {dry_run}")
                print(f"download_job_id: {job['id']}")
                print(f"title: {job['title']}")
                print(f"movie_id: {job['movie_id']}")
                print(f"movie_file_id: {job['source_id']}")
                print(f"library_video_path: {library_video_path}")
                print(f"temp_output_path: {temp_output}")
                print(f"selected_audio_languages: {[str(track['language']).lower() for track in selected_tracks]}")
                print(f"selected_audio_paths: {[str(track['output_path']) for track in selected_tracks]}")
                print(f"prepared_audio_paths: {[str(track.get('prepared_output_path') or track['output_path']) for track in prepared_tracks]}")
                print(f"copied_audio_languages: {[str(track['language']).lower() for track in copied_tracks]}")
                print(f"compat_stereo_languages: {[str(track['language']).lower() for track in stereo_tracks]}")
                print(f"archived_audio_paths: {archived_audio_paths}")
                print(f"final_library_verification_preferred_hits: {final_library_verification['preferred_hits']}")
                print(f"final_library_verification_verified: {final_library_verification['verified']}")
                print(f"final_library_compat_present_languages: {final_library_compat_verification['present_languages']}")
                print(f"final_library_compat_verified: {final_library_compat_verification['verified']}")
                print(f"final_library_path: {final_library_path}")
                if final_tag is not None:
                    print(f"final_tag_label: {final_tag['tag_label']}")
                print("note: library file already contained preferred audio; recovery path used")
                return 0

            subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
            remux_verification = verify_library_file_audio(temp_output, config)
            remux_compat_verification = verify_compat_stereo_tracks(
                temp_output,
                [str(track["language"]).lower() for track in stereo_tracks],
                config,
            )
            if not remux_verification["verified"] or not remux_compat_verification["verified"]:
                upsert_remux_job(
                    conn,
                    job=job,
                    temp_output=temp_output,
                    final_library_path=None,
                    remux_status="temp_verification_failed",
                    metadata={
                        "remux_plan": remux_plan,
                        "sync_audio": sync_metadata,
                        "temp_verification": remux_verification,
                        "temp_compat_verification": remux_compat_verification,
                    },
                    dry_run=dry_run,
                )
                update_download_job(
                    conn,
                    int(job["id"]),
                    "library_audio_verification_failed",
                    {
                        "library_audio_remux": {
                            "temp_output_path": str(temp_output),
                            "sync_audio": sync_metadata,
                            "temp_verification": remux_verification,
                            "temp_compat_verification": remux_compat_verification,
                        }
                    },
                    dry_run=dry_run,
                )
                conn.commit()
                raise SystemExit("Temporary library-audio remux verification failed.")

            replace_library_file(library_video_path, temp_output, dry_run)
            for command_name in ("RescanMovie", "RenameMovie"):
                response = post_radarr_command(base_url, api_key, command_name, int(job["movie_id"]))
                wait_for_radarr_command(base_url, api_key, int(response["id"]))
            sync_local_radarr_state(config, dry_run)

            current_movie_file = latest_movie_file_row(conn, int(job["movie_id"]))
            final_library_path = str(current_movie_file["path"])
            final_library_verification = verify_library_file_audio(Path(final_library_path), config)
            final_library_compat_verification = verify_compat_stereo_tracks(
                Path(final_library_path),
                [str(track["language"]).lower() for track in stereo_tracks],
                config,
            )
            overall_verified = final_library_verification["verified"] and final_library_compat_verification["verified"]
            if overall_verified:
                verification_payload = {
                    "preferred_hits": final_library_verification["preferred_hits"],
                    "detected_languages": final_library_verification["matched_languages"],
                }
                final_tag = replace_radarr_language_tags(
                    base_url,
                    api_key,
                    int(job["movie_id"]),
                    final_library_path,
                    verification_payload,
                    config,
                )
                update_local_language_tag(conn, int(job["movie_id"]), final_library_path, final_tag, config)

            remux_status = "library_audio_applied" if overall_verified else "library_audio_verification_failed"
            upsert_remux_job(
                conn,
                job=job,
                temp_output=temp_output,
                final_library_path=final_library_path,
                remux_status=remux_status,
                metadata={
                    "remux_plan": remux_plan,
                    "archived_audio_paths": archived_audio_paths,
                    "sync_audio": sync_metadata,
                    "temp_verification": remux_verification,
                    "temp_compat_verification": remux_compat_verification,
                    "final_library_verification": final_library_verification,
                    "final_library_compat_verification": final_library_compat_verification,
                    "final_tag": final_tag,
                },
                dry_run=dry_run,
            )
            update_download_job(
                conn,
                int(job["id"]),
                "imported" if overall_verified else "library_audio_verification_failed",
                {
                    "library_audio_remux": {
                        "library_video_path": final_library_path,
                        "archived_audio_paths": archived_audio_paths,
                        "sync_audio": sync_metadata,
                        "final_library_verification": final_library_verification,
                        "final_library_compat_verification": final_library_compat_verification,
                        "final_tag": final_tag,
                    }
                },
                dry_run=dry_run,
            )
            conn.commit()
        else:
            remux_verification = {"preferred_hits": [str(track["language"]).lower() for track in selected_tracks]}
            remux_compat_verification = verify_compat_stereo_tracks(
                library_video_path,
                [str(track["language"]).lower() for track in stereo_tracks],
                config,
            )

    print("Library video + download audio remux")
    print(f"dry_run: {dry_run}")
    print(f"download_job_id: {job['id']}")
    print(f"title: {job['title']}")
    print(f"movie_id: {job['movie_id']}")
    print(f"movie_file_id: {job['source_id']}")
    print(f"library_video_path: {library_video_path}")
    print(f"temp_output_path: {temp_output}")
    print(f"selected_audio_languages: {[str(track['language']).lower() for track in selected_tracks]}")
    print(f"selected_audio_paths: {[str(track['output_path']) for track in selected_tracks]}")
    print(f"prepared_audio_paths: {[str(track.get('prepared_output_path') or track['output_path']) for track in prepared_tracks]}")
    print(f"copied_audio_languages: {[str(track['language']).lower() for track in copied_tracks]}")
    print(f"compat_stereo_languages: {[str(track['language']).lower() for track in stereo_tracks]}")
    print(f"archived_audio_paths: {archived_audio_paths}")
    print(f"sync_audio_enabled: {bool(sync_audio_config(config).get('enabled', True))}")
    if remux_verification is not None:
        print(f"temp_verification_preferred_hits: {remux_verification['preferred_hits']}")
    if remux_compat_verification is not None:
        print(f"temp_compat_present_languages: {remux_compat_verification['present_languages']}")
        print(f"temp_compat_verified: {remux_compat_verification['verified']}")
    if final_library_verification is not None:
        print(f"final_library_verification_preferred_hits: {final_library_verification['preferred_hits']}")
        print(f"final_library_verification_verified: {final_library_verification['verified']}")
    if final_library_compat_verification is not None:
        print(f"final_library_compat_present_languages: {final_library_compat_verification['present_languages']}")
        print(f"final_library_compat_verified: {final_library_compat_verification['verified']}")
        print(f"final_library_path: {final_library_path}")
    if final_tag is not None:
        print(f"final_tag_label: {final_tag['tag_label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
