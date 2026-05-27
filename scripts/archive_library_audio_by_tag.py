#!/usr/bin/env python3
"""Archive audio streams from tagged library movie files without modifying the library."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from extract_download_audio import ffprobe_streams
from queue_prowlarr_download import workspace_paths
from sync_library import ROOT, connect_postgres, ensure_schema, load_env, required_env


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("library_audio_backup"), dict):
        raise SystemExit("Missing config: library_audio_backup")
    if not isinstance(config.get("download_workspace"), dict):
        raise SystemExit("Missing config: download_workspace")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive audio streams from tagged library movie files.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--tag", help="Tag label to select, defaults to library_audio_backup.default_tag.")
    parser.add_argument("--movie-id", type=int, help="Specific Radarr movie id to process.")
    parser.add_argument("--batch", action="store_true", help="Process more than one matching item.")
    parser.add_argument("--limit", type=int, help="Maximum number of matching items to process.")
    parser.add_argument("--apply", action="store_true", help="Extract audio and write the audit row.")
    return parser.parse_args()


def backup_config(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config.get("library_audio_backup", {}))
    if not settings.get("enabled", True):
        raise SystemExit("library_audio_backup.enabled is false")
    return settings


def language_alias_map(config: dict[str, Any]) -> dict[str, str]:
    settings = backup_config(config)
    groups = dict(settings.get("language_groups", {}))
    alias_map: dict[str, str] = {}
    for canonical, aliases in groups.items():
        canonical_value = str(canonical).strip().lower()
        if not canonical_value:
            continue
        alias_map[canonical_value] = canonical_value
        for alias in list(aliases or []):
            alias_value = str(alias).strip().lower()
            if alias_value:
                alias_map[alias_value] = canonical_value
    return alias_map


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "item"


def archive_root(config: dict[str, Any], tag_label: str, movie_id: int, title: str, year: int | None) -> Path:
    workspace = workspace_paths(config)
    settings = backup_config(config)
    subdir = str(settings.get("archive_subdir", "library_audio_backups")).strip("/")
    folder_name = f"{movie_id}-{slugify(title)}"
    if year:
        folder_name = f"{folder_name}-{year}"
    return workspace["archive"] / subdir / tag_label / folder_name


def preferred_languages(config: dict[str, Any]) -> set[str]:
    raw = list(backup_config(config).get("include_languages", []))
    aliases = language_alias_map(config)
    selected = set()
    for item in raw:
        value = str(item).strip().lower()
        if not value:
            continue
        selected.add(aliases.get(value, value))
    return selected


def detect_stream_language(stream: dict[str, Any], config: dict[str, Any]) -> str:
    tags = dict(stream.get("tags") or {})
    value = str(tags.get("language") or "und").strip().lower()
    if not value:
        return "und"
    return language_alias_map(config).get(value, value)


def existing_backup_is_valid(row: dict[str, Any], library_video_path: Path) -> bool:
    existing_tracks = list(row.get("existing_extracted_tracks") or [])
    existing_metadata = dict(row.get("existing_metadata") or {})
    if not row.get("existing_status") or not existing_tracks:
        return False
    if str(existing_metadata.get("library_video_path") or "") != str(library_video_path):
        return False
    try:
        stat_result = library_video_path.stat()
    except FileNotFoundError:
        return False
    if int(existing_metadata.get("source_size_bytes") or -1) != int(stat_result.st_size):
        return False
    current_mtime = int(stat_result.st_mtime)
    if int(existing_metadata.get("source_mtime_epoch") or -1) != current_mtime:
        return False
    return all(Path(str(track.get("output_path") or "")).exists() for track in existing_tracks)


def extract_audio_streams(
    library_video_path: Path,
    output_root: Path,
    config: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    output_root.mkdir(parents=True, exist_ok=True)
    selected_languages = preferred_languages(config)
    output_extension = str(backup_config(config).get("output_extension", ".mka"))
    streams = ffprobe_streams(library_video_path)
    audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]
    extracted_tracks: list[dict[str, Any]] = []
    skipped_languages: list[str] = []

    for position, stream in enumerate(audio_streams):
        stream_index = int(stream.get("index"))
        language = detect_stream_language(stream, config)
        if selected_languages and language not in selected_languages:
            skipped_languages.append(language)
            continue
        tags = dict(stream.get("tags") or {})
        output_path = output_root / f"audio-{position:02d}-stream{stream_index:02d}-{language}{output_extension}"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(library_video_path),
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
        extracted_tracks.append(
            {
                "stream_index": stream_index,
                "language": language,
                "codec_name": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "title": tags.get("title"),
                "output_path": str(output_path),
            }
        )

    metadata = {
        "selected_languages": sorted(selected_languages),
        "skipped_languages": skipped_languages,
        "audio_stream_count": len(audio_streams),
    }
    if not audio_streams:
        return "no_audio_streams", extracted_tracks, streams, metadata
    if selected_languages and not extracted_tracks:
        return "no_selected_languages", extracted_tracks, streams, metadata
    return "audio_archived", extracted_tracks, streams, metadata


def upsert_backup(
    conn: Any,
    *,
    row: dict[str, Any],
    tag_label: str,
    library_video_path: str,
    output_root: str,
    extraction_status: str,
    extracted_tracks: list[dict[str, Any]],
    probe_streams: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO library_audio_backup_jobs (
            source, media_type, source_id, file_source_id, tag_label, library_video_path,
            output_root, extraction_status, extracted_tracks, probe_streams, metadata, updated_at
        )
        VALUES (
            %(source)s, 'movie', %(source_id)s, %(file_source_id)s, %(tag_label)s, %(library_video_path)s,
            %(output_root)s, %(extraction_status)s, %(extracted_tracks)s, %(probe_streams)s, %(metadata)s, now()
        )
        ON CONFLICT (source, media_type, source_id, tag_label)
        DO UPDATE SET
            file_source_id = EXCLUDED.file_source_id,
            library_video_path = EXCLUDED.library_video_path,
            output_root = EXCLUDED.output_root,
            extraction_status = EXCLUDED.extraction_status,
            extracted_tracks = EXCLUDED.extracted_tracks,
            probe_streams = EXCLUDED.probe_streams,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "source": row["source"],
            "source_id": int(row["movie_id"]),
            "file_source_id": int(row["file_source_id"]),
            "tag_label": tag_label,
            "library_video_path": library_video_path,
            "output_root": output_root,
            "extraction_status": extraction_status,
            "extracted_tracks": Jsonb(extracted_tracks),
            "probe_streams": Jsonb(probe_streams),
            "metadata": Jsonb(metadata),
        },
    )


def find_candidates(conn: Any, source: str, tag_label: str, movie_id: int | None, limit: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"source": source, "tag_label": tag_label}
    clauses = [
        "mit.source = %(source)s",
        "mit.media_type = 'movie'",
        "mit.tag_label = %(tag_label)s",
        "mf.path IS NOT NULL",
    ]
    if movie_id is not None:
        clauses.append("mi.source_id = %(movie_id)s")
        params["movie_id"] = movie_id

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %(limit)s"
        params["limit"] = limit

    rows = conn.execute(
        f"""
        SELECT
            mi.source,
            mi.source_id AS movie_id,
            mi.title,
            mi.year,
            mf.source_id AS file_source_id,
            mf.path AS library_video_path,
            mit.tag_label,
            labj.id AS existing_backup_id,
            labj.extraction_status AS existing_status,
            labj.extracted_tracks AS existing_extracted_tracks,
            labj.metadata AS existing_metadata
        FROM media_item_tags mit
        INNER JOIN media_items mi
          ON mi.source = mit.source
         AND mi.media_type = 'movie'
         AND mi.source_id = mit.source_id
        INNER JOIN media_files mf
          ON mf.source = mi.source
         AND mf.media_type = 'movie_file'
         AND mf.parent_source_id = mi.source_id
        LEFT JOIN library_audio_backup_jobs labj
          ON labj.source = mi.source
         AND labj.media_type = 'movie'
         AND labj.source_id = mi.source_id
         AND labj.tag_label = mit.tag_label
        WHERE {" AND ".join(clauses)}
        ORDER BY mi.title, mi.year, mi.source_id
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)
    settings = backup_config(config)
    source = str(settings.get("source", "radarr"))
    tag_label = str(args.tag or settings.get("default_tag", "2160p"))
    limit = args.limit if args.limit is not None else (None if args.batch else 1)

    with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
        conn.row_factory = dict_row
        ensure_schema(conn)
        rows = find_candidates(conn, source, tag_label, args.movie_id, limit)
        if not rows:
            print("No tagged library audio-backup candidates found.")
            conn.commit()
            return 0

        results: list[dict[str, Any]] = []
        for row in rows:
            library_video_path = Path(str(row["library_video_path"]))
            if not library_video_path.exists():
                results.append(
                    {
                        "title": row["title"],
                        "year": row["year"],
                        "movie_id": int(row["movie_id"]),
                        "file_source_id": int(row["file_source_id"]),
                        "tag": row["tag_label"],
                        "library_video_path": str(library_video_path),
                        "output_root": None,
                        "status": "missing_library_file",
                        "track_count": 0,
                    }
                )
                continue

            destination_root = archive_root(config, tag_label, int(row["movie_id"]), str(row["title"]), row["year"])
            if settings.get("keep_existing", True) and existing_backup_is_valid(row, library_video_path):
                extracted_tracks = list(row.get("existing_extracted_tracks") or [])
                results.append(
                    {
                        "title": row["title"],
                        "year": row["year"],
                        "movie_id": int(row["movie_id"]),
                        "file_source_id": int(row["file_source_id"]),
                        "tag": row["tag_label"],
                        "library_video_path": str(library_video_path),
                        "output_root": str(destination_root),
                        "status": "already_archived",
                        "track_count": len(extracted_tracks),
                    }
                )
                continue

            streams = ffprobe_streams(library_video_path)
            audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]
            preview_languages = [detect_stream_language(stream, config) for stream in audio_streams]
            extraction_status = "pending_backup"
            extracted_tracks: list[dict[str, Any]] = []
            metadata = {
                "selected_languages": sorted(preferred_languages(config)),
                "audio_stream_count": len(audio_streams),
            }

            if args.apply:
                extraction_status, extracted_tracks, streams, extra_metadata = extract_audio_streams(library_video_path, destination_root, config)
                source_stat = library_video_path.stat()
                metadata = {
                    **extra_metadata,
                    "library_video_path": str(library_video_path),
                    "source_size_bytes": source_stat.st_size,
                    "source_mtime_epoch": int(source_stat.st_mtime),
                    "managed_by": "archive_library_audio_by_tag",
                }
                upsert_backup(
                    conn,
                    row=row,
                    tag_label=tag_label,
                    library_video_path=str(library_video_path),
                    output_root=str(destination_root),
                    extraction_status=extraction_status,
                    extracted_tracks=extracted_tracks,
                    probe_streams=streams,
                    metadata=metadata,
                )

            results.append(
                {
                    "title": row["title"],
                    "year": row["year"],
                    "movie_id": int(row["movie_id"]),
                    "file_source_id": int(row["file_source_id"]),
                    "tag": row["tag_label"],
                    "library_video_path": str(library_video_path),
                    "output_root": str(destination_root),
                    "status": extraction_status,
                    "track_count": len(extracted_tracks) if args.apply else len(audio_streams),
                    "preview_languages": preview_languages,
                }
            )

        if args.apply:
            conn.commit()

    status_counts: dict[str, int] = {}
    for result in results:
        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1

    print("Library audio backup by tag")
    print(f"dry_run: {dry_run}")
    print(f"source: {source}")
    print(f"tag_label: {tag_label}")
    print(f"processed: {len(results)}")
    print("status_counts: " + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())))
    for result in results[:20]:
        print("---")
        print(f"title: {result['title']}")
        print(f"year: {result['year']}")
        print(f"movie_id: {result['movie_id']}")
        print(f"file_source_id: {result['file_source_id']}")
        print(f"tag: {result['tag']}")
        print(f"status: {result['status']}")
        print(f"library_video_path: {result['library_video_path']}")
        print(f"output_root: {result['output_root']}")
        print(f"track_count: {result['track_count']}")
        if "preview_languages" in result:
            print(f"preview_languages: {result['preview_languages']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
