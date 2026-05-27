#!/usr/bin/env python3
"""Extract Czech and Slovak audio tracks from a completed download video file."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from psycopg.types.json import Jsonb

from queue_prowlarr_download import workspace_paths
from sync_library import ROOT, connect_postgres, load_env, required_env


EXTRACTION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS download_audio_extractions (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE SET NULL,
  client_queue_id TEXT NOT NULL,
  source_video_path TEXT NOT NULL,
  output_root TEXT NOT NULL,
  extraction_status TEXT NOT NULL,
  extracted_tracks JSONB NOT NULL DEFAULT '[]'::jsonb,
  probe_streams JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(client_queue_id)
)
"""


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("download_scan"), dict):
        raise SystemExit("Missing config: download_scan")
    if not isinstance(config.get("assemblarr_library"), dict):
        raise SystemExit("Missing config: assemblarr_library")
    return config


def token_pattern(tokens: list[str]) -> re.Pattern[str]:
    escaped = [re.escape(token.lower()) for token in tokens if token]
    return re.compile(r"(?<![a-z0-9])(" + "|".join(escaped) + r")(?![a-z0-9])", re.IGNORECASE)


def audio_language_config(config: dict[str, Any]) -> dict[str, list[str]]:
    scan_config = config["download_scan"]
    return {
        str(language): [str(token).lower() for token in settings.get("tokens", [])]
        for language, settings in dict(scan_config.get("audio_languages", {})).items()
    }


def output_root(config: dict[str, Any], client_queue_id: str) -> Path:
    scan_config = config["download_scan"].get("audio_extract", {})
    workspace = workspace_paths(config)
    processing_root = workspace["processing"]
    subdir = str(scan_config.get("output_subdir", "audio_extracts")).strip("/")
    return processing_root / subdir / client_queue_id


def ffprobe_streams(video_path: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        return []
    return [stream for stream in streams if isinstance(stream, dict)]


def detect_language(stream: dict[str, Any], language_tokens: dict[str, list[str]]) -> tuple[str | None, list[str]]:
    tags = dict(stream.get("tags") or {})
    values = [
        str(stream.get("codec_name") or ""),
        str(stream.get("codec_long_name") or ""),
        str(tags.get("language") or ""),
        str(tags.get("title") or ""),
        str(tags.get("handler_name") or ""),
    ]
    text = " ".join(values).lower()
    for language, tokens in language_tokens.items():
        matches = sorted({match.group(1).lower() for match in token_pattern(tokens).finditer(text)})
        if matches:
            return language, matches
    return None, []


def extract_tracks(video_path: Path, destination_root: Path, config: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    destination_root.mkdir(parents=True, exist_ok=True)
    language_tokens = audio_language_config(config)
    output_ext = str(config["download_scan"].get("audio_extract", {}).get("output_extension", ".mka"))
    streams = ffprobe_streams(video_path)
    audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]

    extracted: list[dict[str, Any]] = []
    matched_any = False

    for stream in audio_streams:
        language, matches = detect_language(stream, language_tokens)
        if language is None:
            continue
        matched_any = True
        stream_index = int(stream.get("index"))
        output_path = destination_root / f"audio-{stream_index:02d}-{language}{output_ext}"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-map",
                f"0:{stream_index}",
                "-c",
                "copy",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        extracted.append(
            {
                "stream_index": stream_index,
                "language": language,
                "matched_tokens": matches,
                "codec_name": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "output_path": str(output_path),
            }
        )

    if not audio_streams:
        return "no_audio_streams", extracted, streams
    if not matched_any:
        return "no_matching_audio", extracted, streams
    return "audio_extracted", extracted, streams


def upsert_extraction(
    conn: Any,
    *,
    download_job_id: int | None,
    client_queue_id: str,
    source_video_path: str,
    output_root_path: str,
    extraction_status: str,
    extracted_tracks: list[dict[str, Any]],
    probe_streams: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO download_audio_extractions (
            download_job_id, client_queue_id, source_video_path, output_root, extraction_status,
            extracted_tracks, probe_streams, metadata, updated_at
        )
        VALUES (
            %(download_job_id)s, %(client_queue_id)s, %(source_video_path)s, %(output_root)s, %(extraction_status)s,
            %(extracted_tracks)s, %(probe_streams)s, %(metadata)s, now()
        )
        ON CONFLICT (client_queue_id)
        DO UPDATE SET
            download_job_id = EXCLUDED.download_job_id,
            source_video_path = EXCLUDED.source_video_path,
            output_root = EXCLUDED.output_root,
            extraction_status = EXCLUDED.extraction_status,
            extracted_tracks = EXCLUDED.extracted_tracks,
            probe_streams = EXCLUDED.probe_streams,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "download_job_id": download_job_id,
            "client_queue_id": client_queue_id,
            "source_video_path": source_video_path,
            "output_root": output_root_path,
            "extraction_status": extraction_status,
            "extracted_tracks": Jsonb(extracted_tracks),
            "probe_streams": Jsonb(probe_streams),
            "metadata": Jsonb(metadata),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract CZ/SK audio tracks from a completed download video file.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--video-path", required=True, type=Path)
    parser.add_argument("--client-queue-id", required=True)
    parser.add_argument("--download-job-id", type=int)
    parser.add_argument("--apply", action="store_true", help="Write extraction results into Postgres.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)
    extract_config = config["download_scan"].get("audio_extract", {})
    if not extract_config.get("enabled", True):
        raise SystemExit("Audio extraction is disabled in config.")

    destination_root = output_root(config, args.client_queue_id)
    status, extracted_tracks, probe_streams = extract_tracks(args.video_path, destination_root, config)
    metadata = {
        "keep_original_files": bool(extract_config.get("keep_original_files", True)),
    }

    if args.apply:
        with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
            conn.execute(EXTRACTION_TABLE_SQL)
            upsert_extraction(
                conn,
                download_job_id=args.download_job_id,
                client_queue_id=args.client_queue_id,
                source_video_path=str(args.video_path),
                output_root_path=str(destination_root),
                extraction_status=status,
                extracted_tracks=extracted_tracks,
                probe_streams=probe_streams,
                metadata=metadata,
            )
            conn.commit()

    print("Download audio extraction")
    print(f"dry_run: {not args.apply}")
    print(f"video_path: {args.video_path}")
    print(f"output_root: {destination_root}")
    print(f"extraction_status: {status}")
    print(f"extracted_track_count: {len(extracted_tracks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
