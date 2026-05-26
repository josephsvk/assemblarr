#!/usr/bin/env python3
"""Scan configured media files for sidecar subtitles and write subtitle tags."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from find_missing_language import token_pattern
from sync_library import connect_postgres, load_env, required_env


ROOT = Path(__file__).resolve().parents[1]
FILE_TAG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS media_file_tags (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  parent_source_id INTEGER,
  tag_label TEXT NOT NULL,
  tag_reason TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id, tag_label)
)
"""


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("subtitle_scan"), dict):
        raise SystemExit("Missing config: subtitle_scan")
    return config


def media_rows(conn: psycopg.Connection[Any], config: dict[str, Any], limit: int | None) -> list[dict[str, Any]]:
    scan = config["subtitle_scan"]
    source_priority = scan.get("source_priority") or config.get("library", {}).get("source_priority") or ["radarr", "sonarr"]
    media_types = scan.get("media_types") or {}
    rows: list[dict[str, Any]] = []

    for source in source_priority:
        media_type = media_types.get(source)
        if not media_type:
            continue
        source_rows = conn.execute(
            """
            SELECT source, media_type, source_id, parent_source_id, path, raw
            FROM media_files
            WHERE source = %(source)s
              AND media_type = %(media_type)s
              AND path IS NOT NULL
            ORDER BY path
            """,
            {"source": source, "media_type": media_type},
        ).fetchall()
        for row in source_rows:
            rows.append(dict(row))
            if limit is not None and len(rows) >= limit:
                return rows
    return rows


def sidecar_files(media_path: str, extensions: list[str]) -> list[Path]:
    path = Path(media_path)
    if not path.parent.exists():
        return []

    normalized_extensions = {ext.casefold() for ext in extensions}
    candidates = []
    for candidate in path.parent.iterdir():
        if candidate == path or not candidate.is_file():
            continue
        if candidate.suffix.casefold() not in normalized_extensions:
            continue
        if candidate.stem.startswith(path.stem):
            candidates.append(candidate)
    return sorted(candidates)


def embedded_subtitle_text(row: dict[str, Any]) -> str:
    mediainfo = row.get("raw", {}).get("mediaInfo") or {}
    subtitles = mediainfo.get("subtitles")
    if subtitles is None:
        return ""
    if isinstance(subtitles, list):
        return " ".join(str(item) for item in subtitles)
    return str(subtitles)


def detect_subtitle_tags(
    files: list[Path],
    embedded_text: str,
    scan_config: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    detected = []
    matched_files: dict[str, list[str]] = {}
    matched_embedded = []

    for code, definition in scan_config.get("languages", {}).items():
        tokens = [str(token) for token in definition.get("tokens", [])]
        if not tokens:
            continue
        pattern = token_pattern(tokens)
        label = str(definition.get("label") or f"{code}-tit")
        if embedded_text and pattern.search(embedded_text):
            detected.append(label)
            matched_embedded.append(label)
            continue
        for file_path in files:
            if pattern.search(file_path.name):
                detected.append(label)
                matched_files.setdefault(label, []).append(str(file_path))
                break

    if detected:
        return sorted(set(detected)), {
            "matched_files": matched_files,
            "matched_embedded": sorted(set(matched_embedded)),
            "embedded_subtitles": embedded_text,
        }

    no_subtitle = scan_config["no_subtitle_tag"]
    return [str(no_subtitle["label"])], {
        "matched_files": {},
        "matched_embedded": [],
        "embedded_subtitles": embedded_text,
    }


def upsert_file_tag(conn: psycopg.Connection[Any], row: dict[str, Any], tag: str, reason: str, metadata: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO media_file_tags (
            source, media_type, source_id, parent_source_id, tag_label, tag_reason, metadata, updated_at
        )
        VALUES (
            %(source)s, %(media_type)s, %(source_id)s, %(parent_source_id)s,
            %(tag_label)s, %(tag_reason)s, %(metadata)s, now()
        )
        ON CONFLICT (source, media_type, source_id, tag_label)
        DO UPDATE SET
            parent_source_id = EXCLUDED.parent_source_id,
            tag_reason = EXCLUDED.tag_reason,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "source": row["source"],
            "media_type": row["media_type"],
            "source_id": row["source_id"],
            "parent_source_id": row["parent_source_id"],
            "tag_label": tag,
            "tag_reason": reason,
            "metadata": Jsonb(metadata),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan sidecar subtitles and tag media files locally.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true", help="Write subtitle tags to the local database.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)
    scan = config["subtitle_scan"]

    with connect_postgres(required_env(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))) as conn:
        conn.row_factory = dict_row
        conn.execute(FILE_TAG_TABLE_SQL)
        rows = media_rows(conn, config, args.limit)

        counts: dict[str, int] = {}
        samples = []
        for row in rows:
            files = sidecar_files(row["path"], [str(ext) for ext in scan.get("sidecar_extensions", [])])
            embedded_text = embedded_subtitle_text(row) if scan.get("check_embedded", True) else ""
            tags, metadata = detect_subtitle_tags(files, embedded_text, scan)
            metadata.update({"media_path": row["path"], "sidecar_count": len(files)})
            for tag in tags:
                counts[tag] = counts.get(tag, 0) + 1
                reason = (
                    scan["no_subtitle_tag"]["reason"]
                    if tag == scan["no_subtitle_tag"]["label"]
                    else "configured subtitle language found"
                )
                if not dry_run and scan.get("apply_to_database", True):
                    upsert_file_tag(conn, row, tag, reason, metadata)
            if len(samples) < 20:
                samples.append((row, tags, files))

        if not dry_run:
            conn.commit()

    print("Subtitle scan")
    print(f"dry_run: {dry_run}")
    print(f"processed: {len(rows)}")
    print("tag_counts: " + ", ".join(f"{tag}={count}" for tag, count in sorted(counts.items())))
    for row, tags, files in samples:
        print("---")
        print(f"source: {row['source']}")
        print(f"media_type: {row['media_type']}")
        print(f"source_id: {row['source_id']}")
        print(f"tags: {', '.join(tags)}")
        print(f"sidecars: {len(files)}")
        print(f"path: {row['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
