#!/usr/bin/env python3
"""Find media files that do not contain configured language tokens in the filename."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row

from sync_library import connect_postgres, load_env, required_env


ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    check = config.get("library_checks", {}).get("missing_language")
    if not isinstance(check, dict):
        raise SystemExit("Missing config: library_checks.missing_language")

    tokens = check.get("required_any_tokens")
    if not isinstance(tokens, list) or not tokens:
        raise SystemExit("Missing config: library_checks.missing_language.required_any_tokens")

    return config


def token_pattern(tokens: list[str]) -> re.Pattern[str]:
    escaped = [re.escape(token) for token in tokens]
    # Trash-style filenames usually store languages in bracketed token groups.
    return re.compile(r"(?<![A-Z0-9])(" + "|".join(escaped) + r")(?![A-Z0-9])", re.IGNORECASE)


def has_required_token(path: str, tokens: list[str], filename_only: bool) -> bool:
    value = Path(path).name if filename_only else path
    return bool(token_pattern(tokens).search(value))


def find_missing(conn: psycopg.Connection[Any], check: dict[str, Any]) -> list[dict[str, Any]]:
    limit = int(check.get("limit", 1))
    filename_only = bool(check.get("filename_only", True))
    tokens = [str(token) for token in check["required_any_tokens"]]
    sources = check.get("sources") or [check.get("source", "radarr")]
    media_types = check.get("media_types") or {}

    missing = []
    for source in sources:
        media_type = media_types.get(source, check.get("media_type", "movie_file"))
        parent_media_type = "movie" if source == "radarr" else "series"
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
            WHERE mf.source = %(source)s
              AND mf.media_type = %(media_type)s
              AND mf.path IS NOT NULL
            ORDER BY mf.path
            """,
            {"source": source, "media_type": media_type, "parent_media_type": parent_media_type},
        ).fetchall()

        for row in rows:
            if not has_required_token(row["path"], tokens, filename_only):
                missing.append(dict(row))
                if len(missing) >= limit:
                    return missing
    return missing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find the first media file missing configured language tokens.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)
    dsn_env = config.get("database", {}).get("dsn_env", "POSTGRES_DSN")
    check = config["library_checks"]["missing_language"]

    with connect_postgres(required_env(dsn_env)) as conn:
        conn.row_factory = dict_row
        rows = find_missing(conn, check)

    tokens = ", ".join(str(token) for token in check["required_any_tokens"])
    if not rows:
        print(f"No {check.get('source', 'radarr')} {check.get('media_type', 'movie_file')} files missing: {tokens}")
        return 0

    for row in rows:
        print("Missing configured language token")
        print(f"source: {row['source']}")
        print(f"media_type: {row['media_type']}")
        print(f"title: {row.get('title') or 'unknown'}")
        print(f"year: {row.get('year') or 'unknown'}")
        print(f"tmdb_id: {row.get('tmdb_id') or 'unknown'}")
        print(f"file_id: {row['source_id']}")
        print(f"path: {row['path']}")
        print(f"required_any_tokens: {tokens}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
