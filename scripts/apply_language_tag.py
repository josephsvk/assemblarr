#!/usr/bin/env python3
"""Apply configured language status tags to the local database and Radarr/Sonarr."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from find_missing_language import has_required_token
from sync_library import connect_postgres, load_env, required_env


ROOT = Path(__file__).resolve().parents[1]
TAG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS media_item_tags (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  tag_label TEXT NOT NULL,
  tag_reason TEXT NOT NULL,
  arr_tag_id INTEGER,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id, tag_label)
)
"""


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if not isinstance(config.get("tagging"), dict):
        raise SystemExit("Missing config: tagging")
    if not isinstance(config.get("library_checks", {}).get("missing_language"), dict):
        raise SystemExit("Missing config: library_checks.missing_language")
    return config


def arr_request(base_url: str, api_key: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

    return json.loads(response_body) if response_body else None


def ensure_arr_tag(arr_config: dict[str, Any], label: str, dry_run: bool) -> int | None:
    base_url = required_env(arr_config["base_url_env"])
    api_key = required_env(arr_config["api_key_env"])
    tags = arr_request(base_url, api_key, "GET", arr_config.get("tag_path", "/api/v3/tag"))
    normalized = label.casefold()

    for tag in tags:
        if str(tag.get("label", "")).casefold() == normalized:
            return int(tag["id"])

    if dry_run:
        return None

    created = arr_request(base_url, api_key, "POST", arr_config.get("tag_path", "/api/v3/tag"), {"label": label})
    return int(created["id"])


def apply_arr_tag(arr_config: dict[str, Any], item_id: int, tag_id: int, dry_run: bool) -> bool:
    base_url = required_env(arr_config["base_url_env"])
    api_key = required_env(arr_config["api_key_env"])
    item_path = arr_config["item_path"].format(id=item_id)
    item = arr_request(base_url, api_key, "GET", item_path)

    tags = list(item.get("tags") or [])
    if tag_id in tags:
        return False

    tags.append(tag_id)
    item["tags"] = sorted(set(tags))
    if not dry_run:
        arr_request(base_url, api_key, "PUT", item_path, item)
    return True


def upsert_db_tag(
    conn: Any,
    row: dict[str, Any],
    tag_label: str,
    tag_reason: str,
    arr_tag_id: int | None,
    metadata: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return

    conn.execute(
        """
        INSERT INTO media_item_tags (
            source, media_type, source_id, tag_label, tag_reason, arr_tag_id, metadata, updated_at
        )
        VALUES (
            %(source)s, %(media_type)s, %(source_id)s, %(tag_label)s, %(tag_reason)s,
            %(arr_tag_id)s, %(metadata)s, now()
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
            "media_type": parent_media_type(row["source"]),
            "source_id": row["parent_source_id"] or row["source_id"],
            "tag_label": tag_label,
            "tag_reason": tag_reason,
            "arr_tag_id": arr_tag_id,
            "metadata": Jsonb(metadata),
        },
    )


def detect_language_tag(path: str, tagging: dict[str, Any]) -> tuple[str, str] | None:
    matched = []
    for name, definition in tagging.get("detected_language_tags", {}).items():
        tokens = [str(token) for token in definition.get("tokens", [])]
        if tokens and has_required_token(path, tokens, True):
            matched.append(str(definition.get("label") or name))

    if not matched:
        return None
    label = "".join(sorted(set(matched)))
    return label, "configured language token found in filename"


def tag_for_path(path: str, config: dict[str, Any]) -> tuple[str, str]:
    detected = detect_language_tag(path, config["tagging"])
    if detected:
        return detected

    missing = config["tagging"]["missing_language_tag"]
    return str(missing["label"]), str(missing["reason"])


def is_mode_match(tag_label: str, config: dict[str, Any], mode: str) -> bool:
    missing_label = str(config["tagging"]["missing_language_tag"]["label"])
    if mode == "missing":
        return tag_label == missing_label
    if mode == "detected":
        return tag_label != missing_label
    return False


def find_untagged(conn: Any, config: dict[str, Any], mode: str, limit: int | None = 1) -> list[dict[str, Any]]:
    check = config["library_checks"]["missing_language"]
    filename_only = bool(check.get("filename_only", True))
    tokens = [str(token) for token in check["required_any_tokens"]]
    source_priority = config.get("library", {}).get("source_priority") or check.get("sources") or [check.get("source", "radarr")]
    enabled_sources = set(check.get("sources") or source_priority)
    media_types = check.get("media_types") or {}

    selected = []
    selected_parents: set[tuple[str, str, int]] = set()
    for source in source_priority:
        if source not in enabled_sources:
            continue
        media_type = media_types.get(source, check.get("media_type", "movie_file"))
        p_media_type = parent_media_type(source)

        rows = conn.execute(
            """
            SELECT
                mf.source,
                mf.media_type,
                mf.source_id,
                mf.parent_source_id,
                mf.path,
                mf.size_bytes,
                mi.title,
                mi.year,
                mi.tmdb_id,
                mi.imdb_id
            FROM media_files mf
            LEFT JOIN media_items mi
              ON mi.source = mf.source
             AND mi.source_id = mf.parent_source_id
             AND mi.media_type = %(parent_media_type)s
            LEFT JOIN media_item_tags mit
              ON mit.source = mf.source
             AND mit.media_type = %(parent_media_type)s
             AND mit.source_id = mf.parent_source_id
            WHERE mf.source = %(source)s
              AND mf.media_type = %(media_type)s
              AND mf.path IS NOT NULL
              AND mit.id IS NULL
            ORDER BY mf.path
            """,
            {"source": source, "media_type": media_type, "parent_media_type": p_media_type},
        ).fetchall()

        if source == "sonarr":
            grouped: dict[int, list[Any]] = {}
            for row in rows:
                grouped.setdefault(int(row["parent_source_id"] or row["source_id"]), []).append(row)

            for parent_id, parent_rows in grouped.items():
                tags = {tag_for_path(row["path"], config)[0] for row in parent_rows}
                if len(tags) != 1:
                    continue
                tag_label = next(iter(tags))
                if not is_mode_match(tag_label, config, mode):
                    continue
                representative = dict(parent_rows[0])
                representative["_aggregate_file_count"] = len(parent_rows)
                representative["_aggregate_policy"] = "all episode files have the same configured language tag"
                selected.append(representative)
                if limit is not None and len(selected) >= limit:
                    return selected
            continue

        for row in rows:
            parent_key = (row["source"], p_media_type, int(row["parent_source_id"] or row["source_id"]))
            if parent_key in selected_parents:
                continue
            tag_label = tag_for_path(row["path"], config)[0]
            if is_mode_match(tag_label, config, mode):
                selected.append(dict(row))
                selected_parents.add(parent_key)
                if limit is not None and len(selected) >= limit:
                    return selected

    return selected


def parent_media_type(source: str) -> str:
    return "movie" if source == "radarr" else "series"


def choose_tag(row: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    return tag_for_path(row["path"], config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply a configured language status tag to one media item.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument(
        "--mode",
        choices=("missing", "detected", "all"),
        default="missing",
        help="missing tags items without configured language tokens; detected tags items with them; all tags both groups.",
    )
    parser.add_argument("--batch", action="store_true", help="Process more than one untagged item.")
    parser.add_argument("--limit", type=int, help="Maximum number of items to process in batch mode.")
    parser.add_argument("--apply", action="store_true", help="Write tags to the database and Radarr/Sonarr.")
    return parser.parse_args()


def process_row(
    conn: Any,
    row: dict[str, Any],
    config: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    tagging = config["tagging"]
    tag_label, tag_reason = choose_tag(row, config)
    target_media_type = parent_media_type(row["source"])
    target_source_id = row["parent_source_id"] or row["source_id"]
    arr_config = tagging.get("arr", {}).get(row["source"])
    arr_tag_id = None
    arr_changed = False

    if tagging.get("apply_to_arr", True) and not dry_run:
        if not arr_config:
            raise SystemExit(f"Missing tagging.arr config for source: {row['source']}")
        arr_tag_id = ensure_arr_tag(arr_config, tag_label, dry_run)
        arr_changed = apply_arr_tag(arr_config, int(target_source_id), arr_tag_id, dry_run)

    if tagging.get("apply_to_database", True):
        upsert_db_tag(
            conn,
            row,
            tag_label,
            tag_reason,
            arr_tag_id,
            {
                "path": row["path"],
                "title": row.get("title"),
                "year": row.get("year"),
                "file_source_id": row["source_id"],
                "aggregate_file_count": row.get("_aggregate_file_count"),
                "aggregate_policy": row.get("_aggregate_policy"),
                "dry_run": dry_run,
            },
            dry_run,
        )

    return {
        "source": row["source"],
        "media_type": target_media_type,
        "source_id": target_source_id,
        "title": row.get("title") or "unknown",
        "year": row.get("year") or "unknown",
        "tag": tag_label,
        "reason": tag_reason,
        "arr_tag_id": arr_tag_id,
        "arr_changed": arr_changed,
        "path": row["path"],
    }


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)

    with connect_postgres(required_env(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))) as conn:
        conn.row_factory = dict_row
        conn.execute(TAG_TABLE_SQL)

        per_mode_limit = None if args.batch else 1
        if args.limit is not None:
            per_mode_limit = args.limit

        modes = ["missing", "detected"] if args.mode == "all" else [args.mode]
        rows = []
        for mode in modes:
            rows.extend(find_untagged(conn, config, mode, per_mode_limit))

        if not rows:
            print(f"No untagged {args.mode} media item found.")
            conn.commit()
            return 0

        results = [process_row(conn, row, config, dry_run) for row in rows]

        if not dry_run:
            conn.commit()

    counts: dict[str, int] = {}
    for result in results:
        counts[result["tag"]] = counts.get(result["tag"], 0) + 1

    print("Language tag batch")
    print(f"dry_run: {dry_run}")
    print(f"mode: {args.mode}")
    print(f"processed: {len(results)}")
    print("tag_counts: " + ", ".join(f"{tag}={count}" for tag, count in sorted(counts.items())))
    for result in results[:20]:
        print("---")
        print(f"source: {result['source']}")
        print(f"media_type: {result['media_type']}")
        print(f"source_id: {result['source_id']}")
        print(f"title: {result['title']}")
        print(f"year: {result['year']}")
        print(f"tag: {result['tag']}")
        print(f"arr_tag_id: {result['arr_tag_id'] if result['arr_tag_id'] is not None else 'not-created'}")
        print(f"arr_changed: {result['arr_changed']}")
        print(f"path: {result['path']}")
    if len(results) > 20:
        print(f"... {len(results) - 20} more items omitted from console output")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
