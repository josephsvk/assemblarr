#!/usr/bin/env python3
"""Scan a completed download path and store real file observations in Postgres."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml
from psycopg.types.json import Jsonb

from sync_library import connect_postgres, load_env, required_env


ROOT = Path(__file__).resolve().parents[1]
SCAN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS download_artifact_scans (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE SET NULL,
  client TEXT,
  client_queue_id TEXT NOT NULL,
  content_path TEXT NOT NULL,
  scan_status TEXT NOT NULL,
  file_count INTEGER NOT NULL DEFAULT 0,
  total_size_bytes BIGINT NOT NULL DEFAULT 0,
  primary_video_path TEXT,
  detected_audio_languages JSONB NOT NULL DEFAULT '{}'::jsonb,
  detected_subtitle_languages JSONB NOT NULL DEFAULT '{}'::jsonb,
  files JSONB NOT NULL DEFAULT '[]'::jsonb,
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
    return config


def token_pattern(tokens: list[str]) -> re.Pattern[str]:
    escaped = [re.escape(token.lower()) for token in tokens if token]
    return re.compile(r"(?<![a-z0-9])(" + "|".join(escaped) + r")(?![a-z0-9])", re.IGNORECASE)


def detect_languages(name: str, language_config: dict[str, Any]) -> dict[str, list[str]]:
    detected: dict[str, list[str]] = {}
    text = name.lower()
    for language, settings in language_config.items():
        tokens = [str(token) for token in settings.get("tokens", [])]
        if not tokens:
            continue
        matches = sorted({match.group(1).lower() for match in token_pattern(tokens).finditer(text)})
        if matches:
            detected[str(language)] = matches
    return detected


def scan_path(content_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    scan_config = config["download_scan"]
    video_exts = {str(item).lower() for item in scan_config.get("video_extensions", [])}
    subtitle_exts = {str(item).lower() for item in scan_config.get("subtitle_extensions", [])}
    archive_exts = {str(item).lower() for item in scan_config.get("archive_extensions", [])}
    language_config = {
        str(name): {"tokens": [str(token) for token in settings.get("tokens", [])]}
        for name, settings in dict(scan_config.get("audio_languages", {})).items()
    }

    if not content_path.exists():
        return {
            "scan_status": "missing_path",
            "file_count": 0,
            "total_size_bytes": 0,
            "primary_video_path": None,
            "detected_audio_languages": {},
            "detected_subtitle_languages": {},
            "files": [],
            "metadata": {"content_path_exists": False},
        }

    files = [content_path] if content_path.is_file() else sorted(path for path in content_path.rglob("*") if path.is_file())
    entries: list[dict[str, Any]] = []
    total_size_bytes = 0
    audio_hits: dict[str, set[str]] = {}
    subtitle_hits: dict[str, set[str]] = {}
    primary_video_path: Path | None = None
    primary_video_size = -1

    for file_path in files:
        try:
            size_bytes = file_path.stat().st_size
        except OSError:
            size_bytes = 0
        total_size_bytes += size_bytes
        suffix = file_path.suffix.lower()
        relative_path = str(file_path.relative_to(content_path)) if content_path.is_dir() else file_path.name
        detected_audio = detect_languages(file_path.name, language_config)
        detected_subtitles = detect_languages(file_path.name, language_config) if suffix in subtitle_exts else {}

        for language, tokens in detected_audio.items():
            audio_hits.setdefault(language, set()).update(tokens)
        for language, tokens in detected_subtitles.items():
            subtitle_hits.setdefault(language, set()).update(tokens)

        file_kind = "other"
        if suffix in video_exts:
            file_kind = "video"
            if size_bytes > primary_video_size:
                primary_video_size = size_bytes
                primary_video_path = file_path
        elif suffix in subtitle_exts:
            file_kind = "subtitle"
        elif suffix in archive_exts:
            file_kind = "archive"

        entries.append(
            {
                "path": relative_path,
                "size_bytes": size_bytes,
                "extension": suffix,
                "kind": file_kind,
                "audio_languages": detected_audio,
                "subtitle_languages": detected_subtitles,
            }
        )

    return {
        "scan_status": "scanned",
        "file_count": len(entries),
        "total_size_bytes": total_size_bytes,
        "primary_video_path": str(primary_video_path) if primary_video_path else None,
        "detected_audio_languages": {name: sorted(tokens) for name, tokens in sorted(audio_hits.items())},
        "detected_subtitle_languages": {name: sorted(tokens) for name, tokens in sorted(subtitle_hits.items())},
        "files": entries,
        "metadata": {
            "content_path_exists": True,
            "content_path_type": "file" if content_path.is_file() else "directory",
        },
    }


def upsert_scan(
    conn: Any,
    *,
    download_job_id: int | None,
    client: str,
    client_queue_id: str,
    content_path: str,
    scan: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO download_artifact_scans (
            download_job_id, client, client_queue_id, content_path, scan_status,
            file_count, total_size_bytes, primary_video_path,
            detected_audio_languages, detected_subtitle_languages, files, metadata, updated_at
        )
        VALUES (
            %(download_job_id)s, %(client)s, %(client_queue_id)s, %(content_path)s, %(scan_status)s,
            %(file_count)s, %(total_size_bytes)s, %(primary_video_path)s,
            %(detected_audio_languages)s, %(detected_subtitle_languages)s, %(files)s, %(metadata)s, now()
        )
        ON CONFLICT (client_queue_id)
        DO UPDATE SET
            download_job_id = EXCLUDED.download_job_id,
            client = EXCLUDED.client,
            content_path = EXCLUDED.content_path,
            scan_status = EXCLUDED.scan_status,
            file_count = EXCLUDED.file_count,
            total_size_bytes = EXCLUDED.total_size_bytes,
            primary_video_path = EXCLUDED.primary_video_path,
            detected_audio_languages = EXCLUDED.detected_audio_languages,
            detected_subtitle_languages = EXCLUDED.detected_subtitle_languages,
            files = EXCLUDED.files,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "download_job_id": download_job_id,
            "client": client,
            "client_queue_id": client_queue_id,
            "content_path": content_path,
            "scan_status": scan["scan_status"],
            "file_count": scan["file_count"],
            "total_size_bytes": scan["total_size_bytes"],
            "primary_video_path": scan["primary_video_path"],
            "detected_audio_languages": Jsonb(scan["detected_audio_languages"]),
            "detected_subtitle_languages": Jsonb(scan["detected_subtitle_languages"]),
            "files": Jsonb(scan["files"]),
            "metadata": Jsonb(scan["metadata"]),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan a completed download path and store observations.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--content-path", required=True, type=Path)
    parser.add_argument("--client", default="qbittorrent")
    parser.add_argument("--client-queue-id", required=True)
    parser.add_argument("--download-job-id", type=int)
    parser.add_argument("--apply", action="store_true", help="Write the scan result into Postgres.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)
    scan = scan_path(args.content_path, config)

    if args.apply:
        with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
            conn.execute(SCAN_TABLE_SQL)
            upsert_scan(
                conn,
                download_job_id=args.download_job_id,
                client=args.client,
                client_queue_id=args.client_queue_id,
                content_path=str(args.content_path),
                scan=scan,
            )
            conn.commit()

    print("Download artifact scan")
    print(f"dry_run: {not args.apply}")
    print(f"content_path: {args.content_path}")
    print(f"scan_status: {scan['scan_status']}")
    print(f"file_count: {scan['file_count']}")
    print(f"total_size_bytes: {scan['total_size_bytes']}")
    print(f"primary_video_path: {scan['primary_video_path'] or 'not-found'}")
    print(f"detected_audio_languages: {json.dumps(scan['detected_audio_languages'], ensure_ascii=True)}")
    print(f"detected_subtitle_languages: {json.dumps(scan['detected_subtitle_languages'], ensure_ascii=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
