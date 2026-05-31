#!/usr/bin/env python3
"""Run a health check against a final library-audio remux output."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row

from extract_download_audio import ffprobe_streams
from remux_and_import_radarr_movie import preferred_languages, verify_library_file_audio
from remux_library_video_with_download_audio import compat_stereo_config, float_or_none, verify_compat_stereo_tracks
from sync_library import ROOT, connect_postgres, load_env, required_env


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("postprocess_import"), dict):
        raise SystemExit("Missing config: postprocess_import")
    if not isinstance(config.get("database"), dict):
        raise SystemExit("Missing config: database")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a health check against a final library audio remux output.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--download-job-id", type=int, help="Use library_audio_remux_jobs/download_jobs metadata for this job.")
    parser.add_argument("--library-path", type=Path, help="Check a specific final library file directly.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = parser.parse_args()
    if args.download_job_id is None and args.library_path is None:
        raise SystemExit("Pass either --download-job-id or --library-path.")
    return args


def result(severity: str, check: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {"severity": severity, "check": check, "message": message}
    if details:
        payload["details"] = details
    return payload


def worst_severity(results: list[dict[str, Any]]) -> str:
    ranks = {"pass": 0, "warn": 1, "fail": 2}
    return max(results, key=lambda item: ranks[item["severity"]])["severity"] if results else "pass"


def severity_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for item in results:
        counts[item["severity"]] += 1
    return counts


def audio_streams(streams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]


def stream_language(stream: dict[str, Any]) -> str:
    return str(dict(stream.get("tags") or {}).get("language") or "und").lower()


def stream_title(stream: dict[str, Any]) -> str:
    return str(dict(stream.get("tags") or {}).get("title") or "")


def is_default_stream(stream: dict[str, Any]) -> bool:
    disposition = dict(stream.get("disposition") or {})
    return int(disposition.get("default") or 0) == 1


def stream_duration_seconds(stream: dict[str, Any], fallback_duration: float | None) -> float | None:
    duration = float_or_none(stream.get("duration"))
    if duration is not None:
        return duration
    return fallback_duration


def file_duration_seconds(path: Path) -> float | None:
    result_obj = subprocess.run(
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
    return float_or_none(result_obj.stdout.strip())


def sample_windows(total_duration: float | None, sample_length: float = 12.0) -> list[tuple[float, float]]:
    if total_duration is None or total_duration <= sample_length:
        return [(0.0, max(1.0, total_duration or sample_length))]
    middle_start = max(0.0, (total_duration / 2.0) - (sample_length / 2.0))
    tail_start = max(0.0, total_duration - sample_length)
    return [
        (0.0, sample_length),
        (middle_start, sample_length),
        (tail_start, sample_length),
    ]


def decode_window(path: Path, map_specs: list[str], start: float, duration: float) -> tuple[bool, str]:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(path),
        "-t",
        f"{duration:.3f}",
    ]
    for spec in map_specs:
        command.extend(["-map", spec])
    command.extend(["-f", "null", "-"])
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (process.stderr or process.stdout or "").strip()
    return process.returncode == 0, output


def decode_health(path: Path, streams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    duration = file_duration_seconds(path)
    windows = sample_windows(duration)
    selected_audio_streams = audio_streams(streams)
    default_audio = next((stream for stream in selected_audio_streams if is_default_stream(stream)), None)

    if default_audio is not None:
        for start, window_duration in windows:
            ok, output = decode_window(
                path,
                ["0:v:0", f"0:{int(default_audio['index'])}"],
                start,
                window_duration,
            )
            severity = "pass" if ok else "fail"
            message = "default video+audio decode window passed" if ok else "default video+audio decode window failed"
            results.append(
                result(
                    severity,
                    "decode_default_av_window",
                    message,
                    start_seconds=round(start, 3),
                    duration_seconds=round(window_duration, 3),
                    stream_index=int(default_audio["index"]),
                    stderr=output,
                )
            )

    for stream in selected_audio_streams:
        for start, window_duration in windows:
            ok, output = decode_window(path, [f"0:{int(stream['index'])}"], start, window_duration)
            severity = "pass" if ok else "fail"
            message = "audio-only decode window passed" if ok else "audio-only decode window failed"
            results.append(
                result(
                    severity,
                    "decode_audio_window",
                    message,
                    start_seconds=round(start, 3),
                    duration_seconds=round(window_duration, 3),
                    stream_index=int(stream["index"]),
                    language=stream_language(stream),
                    title=stream_title(stream),
                    stderr=output,
                )
            )
    return results


def default_track_health(streams: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    compat = compat_stereo_config(config)
    default_audio = next((stream for stream in audio_streams(streams) if is_default_stream(stream)), None)
    if default_audio is None:
        return [result("fail", "default_audio", "no default audio stream found")]

    results.append(
        result(
            "pass",
            "default_audio",
            "default audio stream found",
            stream_index=int(default_audio["index"]),
            codec=str(default_audio.get("codec_name") or ""),
            channels=int(default_audio.get("channels") or 0),
            language=stream_language(default_audio),
            title=stream_title(default_audio),
        )
    )
    if compat.get("enabled", True) and compat.get("prefer_default", True):
        expected_codec = str(compat.get("codec", "aac")).lower()
        expected_channels = int(compat.get("channels", 2))
        is_expected = (
            str(default_audio.get("codec_name") or "").lower() == expected_codec
            and int(default_audio.get("channels") or 0) == expected_channels
        )
        results.append(
            result(
                "pass" if is_expected else "warn",
                "default_audio_preference",
                "default track matches preferred compatibility profile" if is_expected else "default track does not match compatibility profile",
                expected_codec=expected_codec,
                expected_channels=expected_channels,
                stream_index=int(default_audio["index"]),
                codec=str(default_audio.get("codec_name") or ""),
                channels=int(default_audio.get("channels") or 0),
            )
        )
    return results


def expected_languages_for_health_check(
    config: dict[str, Any],
    context: dict[str, Any] | None,
    verification: dict[str, Any],
) -> list[str]:
    if context:
        remux_metadata = dict(context.get("remux_metadata") or {})
        remux_plan = dict(remux_metadata.get("remux_plan") or {})
        compat_languages = [str(language).lower() for language in remux_plan.get("compat_stereo_languages") or []]
        copied_languages = [str(language).lower() for language in remux_plan.get("copied_languages") or []]
        selected = sorted(dict.fromkeys(compat_languages + copied_languages))
        if selected:
            return selected
        sync_audio = list(remux_metadata.get("sync_audio") or [])
        selected_from_sync = sorted(
            {
                str(item.get("language")).lower()
                for item in sync_audio
                if item.get("language")
            }
        )
        if selected_from_sync:
            return selected_from_sync
    preferred_hits = [str(language).lower() for language in verification.get("preferred_hits") or []]
    if preferred_hits:
        return preferred_hits
    return preferred_languages(config)


def stream_map_health(path: Path, streams: list[dict[str, Any]], config: dict[str, Any], context: dict[str, Any] | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    verification = verify_library_file_audio(path, config)
    expected_languages = expected_languages_for_health_check(config, context, verification)
    compat = verify_compat_stereo_tracks(path, expected_languages, config)
    results.append(
        result(
            "pass" if verification["verified"] else "fail",
            "preferred_language_presence",
            "preferred audio languages found" if verification["verified"] else "preferred audio languages missing",
            preferred_hits=verification["preferred_hits"],
            matched_languages=verification["matched_languages"],
            audio_stream_count=verification["audio_stream_count"],
        )
    )
    results.append(
        result(
            "pass" if compat["verified"] else "warn",
            "compat_stereo_presence",
                "compatibility stereo tracks found" if compat["verified"] else "compatibility stereo tracks missing",
                present_languages=compat["present_languages"],
                required_languages=compat["required_languages"],
                expected_languages=expected_languages,
                codec=compat.get("codec"),
                channels=compat.get("channels"),
            )
        )
    fallback_duration = file_duration_seconds(path)
    for stream in audio_streams(streams):
        results.append(
            result(
                "pass",
                "audio_stream_inventory",
                "audio stream inventoried",
                stream_index=int(stream["index"]),
                codec=str(stream.get("codec_name") or ""),
                channels=int(stream.get("channels") or 0),
                language=stream_language(stream),
                title=stream_title(stream),
                start_time_seconds=float_or_none(stream.get("start_time")),
                duration_seconds=stream_duration_seconds(stream, fallback_duration),
                default=is_default_stream(stream),
            )
        )
    return results


def metadata_health(remux_metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not remux_metadata:
        return [result("warn", "remux_metadata", "no remux metadata available; DB-backed sync diagnostics unavailable")]

    results: list[dict[str, Any]] = [result("pass", "remux_metadata", "remux metadata loaded from DB")]
    sync_audio = list(remux_metadata.get("sync_audio") or [])
    if not sync_audio:
        results.append(result("warn", "sync_metadata", "no sync_audio entries recorded in remux metadata"))
        return results
    for item in sync_audio:
        language = str(item.get("source_audio_path") or item.get("synced_output_path") or "")
        sync_applied = bool(item.get("sync_applied", False))
        warning = str(item.get("synaudio_warning") or "")
        results.append(
            result(
                "pass" if sync_applied else "warn",
                "sync_mode",
                "track used measured sync output" if sync_applied else "track used provisional passthrough/fallback mode",
                source=language,
                sync_applied=sync_applied,
                warning=warning,
                source_duration=item.get("source_duration"),
                reference_duration=item.get("reference_duration"),
                duration_diff=item.get("duration_diff") or item.get("source_duration_diff"),
            )
        )
    return results


def resolve_job_context(conn: Any, job_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            dj.id AS download_job_id,
            dj.title,
            dj.release_title,
            dj.status,
            larj.final_library_path,
            larj.remux_status,
            larj.metadata AS remux_metadata
        FROM download_jobs dj
        LEFT JOIN library_audio_remux_jobs larj
          ON larj.download_job_id = dj.id
        WHERE dj.id = %(download_job_id)s
        LIMIT 1
        """,
        {"download_job_id": job_id},
    ).fetchone()
    if not row:
        raise SystemExit(f"download_job_id {job_id} not found")
    payload = dict(row)
    final_path = payload.get("final_library_path")
    if not final_path:
        raise SystemExit(f"download_job_id {job_id} has no final_library_path in library_audio_remux_jobs")
    return payload


def build_report(path: Path, config: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    streams = ffprobe_streams(path)
    results: list[dict[str, Any]] = []
    results.extend(stream_map_health(path, streams, config, context))
    results.extend(default_track_health(streams, config))
    results.extend(metadata_health(dict(context.get("remux_metadata") or {}) if context else None))
    results.extend(decode_health(path, streams))
    summary = {
        "overall": worst_severity(results),
        "counts": severity_counts(results),
    }
    return {
        "summary": summary,
        "library_path": str(path),
        "download_job_id": context.get("download_job_id") if context else None,
        "title": context.get("title") if context else None,
        "release_title": context.get("release_title") if context else None,
        "results": results,
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("Library audio remux health check")
    print(f"overall: {summary['overall']}")
    print(f"counts: {summary['counts']}")
    if report.get("download_job_id") is not None:
        print(f"download_job_id: {report['download_job_id']}")
    if report.get("title"):
        print(f"title: {report['title']}")
    print(f"library_path: {report['library_path']}")
    for item in report["results"]:
        print(f"{item['severity']}: {item['check']}: {item['message']}")


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)

    context: dict[str, Any] | None = None
    library_path: Path
    if args.download_job_id is not None:
        dsn_env = str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))
        with connect_postgres(required_env(dsn_env)) as conn:
            conn.row_factory = dict_row
            context = resolve_job_context(conn, args.download_job_id)
        library_path = Path(str(context["final_library_path"]))
    else:
        library_path = Path(args.library_path)

    if not library_path.exists():
        raise SystemExit(f"Library path does not exist: {library_path}")

    report = build_report(library_path, config, context)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0 if report["summary"]["overall"] != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())
