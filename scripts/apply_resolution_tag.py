#!/usr/bin/env python3
"""Apply configured resolution tags to Radarr parent items."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apply_language_tag import ROOT, TAG_TABLE_SQL, arr_request, ensure_arr_tag, load_env, required_env
from sync_library import connect_postgres


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("quality_tagging"), dict):
        raise SystemExit("Missing config: quality_tagging")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply configured resolution tags to Radarr parent items.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--source", default="radarr", choices=("radarr",))
    parser.add_argument("--batch", action="store_true", help="Process more than one item.")
    parser.add_argument("--limit", type=int, help="Maximum number of items to process.")
    parser.add_argument("--apply", action="store_true", help="Write tags to the database and Radarr.")
    return parser.parse_args()


def resolution_tag_config(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config.get("quality_tagging", {}))
    if not isinstance(settings.get("resolution_tags"), dict):
        raise SystemExit("Missing config: quality_tagging.resolution_tags")
    return settings


def managed_labels(config: dict[str, Any]) -> set[str]:
    settings = resolution_tag_config(config)
    labels = set()
    for definition in dict(settings.get("resolution_tags", {})).values():
        label = str(dict(definition).get("label") or "").strip()
        if label:
            labels.add(label)
    return labels


def detect_resolution_label(row: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    settings = resolution_tag_config(config)
    quality = dict(row.get("quality") or {})
    quality_inner = dict(quality.get("quality") or {})
    quality_resolution = quality_inner.get("resolution")
    path = str(row.get("path") or "")
    normalized_path = path.casefold()

    for name, definition in dict(settings.get("resolution_tags", {})).items():
        tag_def = dict(definition)
        label = str(tag_def.get("label") or name)
        expected_resolution = tag_def.get("quality_resolution")
        if expected_resolution is not None and quality_resolution == expected_resolution:
            return label, f"matched quality resolution {expected_resolution}"
        tokens = [str(token).casefold() for token in tag_def.get("tokens", [])]
        if any(token and token in normalized_path for token in tokens):
            return label, "matched filename resolution token"

    raise SystemExit(f"No configured resolution tag matched: {path}")


def find_candidates(conn: Any, source: str, limit: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            mf.source,
            mf.media_type,
            mf.source_id,
            mf.parent_source_id,
            mf.path,
            mf.quality,
            mf.mediainfo,
            mi.title,
            mi.year
        FROM media_files mf
        LEFT JOIN media_items mi
          ON mi.source = mf.source
         AND mi.media_type = 'movie'
         AND mi.source_id = mf.parent_source_id
        WHERE mf.source = %(source)s
          AND mf.media_type = 'movie_file'
          AND mf.path IS NOT NULL
        ORDER BY mi.title, mf.path
        """,
        {"source": source},
    ).fetchall()
    candidates = [dict(row) for row in rows]
    if limit is not None:
        return candidates[:limit]
    return candidates


def replace_arr_tags(arr_config: dict[str, Any], item_id: int, desired_label: str, config: dict[str, Any], dry_run: bool) -> int | None:
    base_url = required_env(arr_config["base_url_env"])
    api_key = required_env(arr_config["api_key_env"])
    item_path = arr_config["item_path"].format(id=item_id)
    item = arr_request(base_url, api_key, "GET", item_path)
    all_tags = arr_request(base_url, api_key, "GET", arr_config.get("tag_path", "/api/v3/tag"))
    if not isinstance(item, dict) or not isinstance(all_tags, list):
        raise RuntimeError("Unexpected Arr payload while replacing resolution tags")

    label_by_id = {int(tag["id"]): str(tag.get("label") or "") for tag in all_tags if tag.get("id") is not None}
    remove_labels = managed_labels(config)
    desired_tag_id = ensure_arr_tag(arr_config, desired_label, dry_run)
    current_tags = [int(tag_id) for tag_id in list(item.get("tags") or [])]
    kept_tags = [tag_id for tag_id in current_tags if label_by_id.get(tag_id) not in remove_labels]
    if desired_tag_id is not None:
        kept_tags.append(int(desired_tag_id))
    item["tags"] = sorted(set(kept_tags))
    if not dry_run:
        arr_request(base_url, api_key, "PUT", item_path, item)
    return int(desired_tag_id) if desired_tag_id is not None else None


def replace_db_tags(
    conn: Any,
    row: dict[str, Any],
    label: str,
    reason: str,
    arr_tag_id: int | None,
    config: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    item_id = int(row["parent_source_id"] or row["source_id"])
    labels = sorted(managed_labels(config))
    conn.execute(
        """
        DELETE FROM media_item_tags
        WHERE source = %(source)s
          AND media_type = 'movie'
          AND source_id = %(source_id)s
          AND tag_label = ANY(%(labels)s)
        """,
        {"source": row["source"], "source_id": item_id, "labels": labels},
    )
    conn.execute(
        """
        INSERT INTO media_item_tags (
            source, media_type, source_id, tag_label, tag_reason, arr_tag_id, metadata, updated_at
        )
        VALUES (
            %(source)s, 'movie', %(source_id)s, %(tag_label)s, %(tag_reason)s, %(arr_tag_id)s, %(metadata)s, now()
        )
        ON CONFLICT (source, media_type, source_id, tag_label)
        DO UPDATE SET
            tag_reason = EXCLUDED.tag_reason,
            arr_tag_id = EXCLUDED.arr_tag_id,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "source": row["source"],
            "source_id": item_id,
            "tag_label": label,
            "tag_reason": reason,
            "arr_tag_id": arr_tag_id,
            "metadata": Jsonb(
                {
                    "path": row["path"],
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "file_source_id": row["source_id"],
                    "managed_by": "apply_resolution_tag",
                }
            ),
        },
    )


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)
    settings = resolution_tag_config(config)
    arr_config = dict(config.get("tagging", {}).get("arr", {}).get(args.source, {}))
    if not arr_config:
        raise SystemExit(f"Missing tagging.arr config for source: {args.source}")

    with connect_postgres(required_env(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))) as conn:
        conn.row_factory = dict_row
        conn.execute(TAG_TABLE_SQL)
        limit = args.limit if args.limit is not None else (None if args.batch else 1)
        rows = find_candidates(conn, args.source, limit)
        if not rows:
            print("No resolution-tag candidates found.")
            conn.commit()
            return 0

        results: list[dict[str, Any]] = []
        for row in rows:
            label, reason = detect_resolution_label(row, config)
            arr_tag_id = None
            if settings.get("apply_to_arr", True):
                arr_tag_id = replace_arr_tags(arr_config, int(row["parent_source_id"] or row["source_id"]), label, config, dry_run)
            if settings.get("apply_to_database", True):
                replace_db_tags(conn, row, label, reason, arr_tag_id, config, dry_run)
            results.append(
                {
                    "title": row.get("title") or "unknown",
                    "year": row.get("year") or "unknown",
                    "item_id": int(row["parent_source_id"] or row["source_id"]),
                    "file_source_id": int(row["source_id"]),
                    "path": str(row["path"]),
                    "tag": label,
                    "reason": reason,
                }
            )

        if not dry_run:
            conn.commit()

    counts: dict[str, int] = {}
    for result in results:
        counts[result["tag"]] = counts.get(result["tag"], 0) + 1

    print("Resolution tag batch")
    print(f"dry_run: {dry_run}")
    print(f"source: {args.source}")
    print(f"processed: {len(results)}")
    print("tag_counts: " + ", ".join(f"{tag}={count}" for tag, count in sorted(counts.items())))
    for result in results[:20]:
        print("---")
        print(f"title: {result['title']}")
        print(f"year: {result['year']}")
        print(f"item_id: {result['item_id']}")
        print(f"file_source_id: {result['file_source_id']}")
        print(f"path: {result['path']}")
        print(f"tag: {result['tag']}")
        print(f"reason: {result['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
