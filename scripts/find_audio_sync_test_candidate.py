#!/usr/bin/env python3
"""Find a small Radarr movie candidate for library-audio sync/remux testing."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row

from sync_library import ROOT, connect_postgres, load_env, required_env


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("database"), dict):
        raise SystemExit("Missing config: database")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find a small movie/audio pair for sync-remux testing.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--max-size-gb",
        type=float,
        default=8.0,
        help="Only show library video files up to this size when they are visible on this host.",
    )
    return parser.parse_args()


def existing_audio_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = []
    for track in tracks:
        output_path = Path(str(track.get("output_path") or ""))
        if output_path.exists():
            existing.append(track)
    return existing


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)
    max_size_bytes = int(args.max_size_gb * 1024 * 1024 * 1024)

    with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            """
            SELECT
                dj.id AS download_job_id,
                dj.title,
                dj.release_title,
                dj.client_queue_id,
                mf.path AS library_video_path,
                dae.extracted_tracks
            FROM download_jobs dj
            JOIN download_audio_extractions dae
              ON dae.client_queue_id = dj.client_queue_id
            JOIN media_files mf
              ON mf.source = dj.source
             AND mf.media_type = dj.media_type
             AND mf.source_id = dj.source_id
            LEFT JOIN library_audio_remux_jobs larj
              ON larj.download_job_id = dj.id
            WHERE dj.source = 'radarr'
              AND dj.media_type = 'movie_file'
              AND dae.extraction_status = 'audio_extracted'
              AND mf.path IS NOT NULL
              AND larj.id IS NULL
            ORDER BY dj.updated_at DESC, dj.id DESC
            LIMIT %(limit)s
            """,
            {"limit": max(args.limit * 5, args.limit)},
        ).fetchall()

    candidates = []
    for row in rows:
        library_path = Path(str(row["library_video_path"]))
        if not library_path.exists():
            continue
        size = library_path.stat().st_size
        if size > max_size_bytes:
            continue
        tracks = existing_audio_tracks(list(row.get("extracted_tracks") or []))
        if not tracks:
            continue
        candidates.append((size, dict(row), tracks))

    candidates.sort(key=lambda item: item[0])
    print("Audio sync test candidates")
    print(f"max_size_gb: {args.max_size_gb}")
    print(f"candidate_count: {len(candidates[:args.limit])}")
    for size, row, tracks in candidates[: args.limit]:
        gib = size / 1024 / 1024 / 1024
        languages = sorted({str(track.get("language") or "").lower() for track in tracks})
        print("")
        print(f"download_job_id: {row['download_job_id']}")
        print(f"title: {row['title']}")
        print(f"release_title: {row['release_title']}")
        print(f"library_video_path: {row['library_video_path']}")
        print(f"library_video_size_gb: {gib:.2f}")
        print(f"audio_languages: {languages}")
        print(f"audio_paths: {[str(track.get('output_path')) for track in tracks]}")
        print(f"dry_run: python3 scripts/remux_library_video_with_download_audio.py --download-job-id {row['download_job_id']}")
        print(f"apply: python3 scripts/remux_library_video_with_download_audio.py --download-job-id {row['download_job_id']} --apply")

    if not candidates:
        raise SystemExit("No visible small candidate found. Try a higher --max-size-gb or run sync/extraction first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
