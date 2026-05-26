#!/usr/bin/env python3
"""Load Radarr and Sonarr libraries into Postgres."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: psycopg. Install it with: python3 -m pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema.sql"


def load_env(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def api_get(base_url: str, api_key: str, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    request = urllib.request.Request(url, headers={"X-Api-Key": api_key, "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc

    if not isinstance(payload, list):
        raise RuntimeError(f"GET {url} returned {type(payload).__name__}, expected list")
    return payload


def init_db(conn: psycopg.Connection[Any]) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def connect_postgres(dsn: str, attempts: int = 10, delay_seconds: float = 1.0) -> psycopg.Connection[Any]:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(dsn)
        except psycopg.OperationalError as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(f"Postgres not ready yet, retrying ({attempt}/{attempts})...", file=sys.stderr)
            time.sleep(delay_seconds)

    raise RuntimeError(f"Postgres connection failed after {attempts} attempts") from last_error


def upsert_media_item(conn: psycopg.Connection[Any], source: str, media_type: str, item: dict[str, Any]) -> None:
    source_id = item.get("id")
    title = item.get("title")
    if source_id is None or not title:
        print(f"Skipping {source} {media_type} without id/title: {item!r}", file=sys.stderr)
        return

    conn.execute(
        """
        INSERT INTO media_items (
            source, source_id, media_type, title, year, tmdb_id, imdb_id, tvdb_id,
            path, monitored, raw, updated_at
        )
        VALUES (
            %(source)s, %(source_id)s, %(media_type)s, %(title)s, %(year)s, %(tmdb_id)s,
            %(imdb_id)s, %(tvdb_id)s, %(path)s, %(monitored)s, %(raw)s, now()
        )
        ON CONFLICT (source, media_type, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            year = EXCLUDED.year,
            tmdb_id = EXCLUDED.tmdb_id,
            imdb_id = EXCLUDED.imdb_id,
            tvdb_id = EXCLUDED.tvdb_id,
            path = EXCLUDED.path,
            monitored = EXCLUDED.monitored,
            raw = EXCLUDED.raw,
            updated_at = now()
        """,
        {
            "source": source,
            "source_id": source_id,
            "media_type": media_type,
            "title": title,
            "year": item.get("year"),
            "tmdb_id": item.get("tmdbId"),
            "imdb_id": item.get("imdbId"),
            "tvdb_id": item.get("tvdbId"),
            "path": item.get("path"),
            "monitored": item.get("monitored"),
            "raw": Jsonb(item),
        },
    )


def upsert_media_file(
    conn: psycopg.Connection[Any],
    source: str,
    media_type: str,
    file_item: dict[str, Any] | None,
    parent_source_id: int | None,
) -> bool:
    if not file_item:
        return False

    source_id = file_item.get("id")
    if source_id is None:
        return False

    conn.execute(
        """
        INSERT INTO media_files (
            source, source_id, parent_source_id, media_type, path, size_bytes,
            quality, languages, mediainfo, raw, updated_at
        )
        VALUES (
            %(source)s, %(source_id)s, %(parent_source_id)s, %(media_type)s,
            %(path)s, %(size_bytes)s, %(quality)s, %(languages)s, %(mediainfo)s,
            %(raw)s, now()
        )
        ON CONFLICT (source, media_type, source_id)
        DO UPDATE SET
            parent_source_id = EXCLUDED.parent_source_id,
            path = EXCLUDED.path,
            size_bytes = EXCLUDED.size_bytes,
            quality = EXCLUDED.quality,
            languages = EXCLUDED.languages,
            mediainfo = EXCLUDED.mediainfo,
            raw = EXCLUDED.raw,
            updated_at = now()
        """,
        {
            "source": source,
            "source_id": source_id,
            "parent_source_id": parent_source_id,
            "media_type": media_type,
            "path": file_item.get("path"),
            "size_bytes": file_item.get("size"),
            "quality": Jsonb(file_item.get("quality")),
            "languages": Jsonb(file_item.get("languages")),
            "mediainfo": Jsonb(file_item.get("mediaInfo")),
            "raw": Jsonb(file_item),
        },
    )
    return True


def sync_radarr(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    movies = api_get(required_env("RADARR_URL"), required_env("RADARR_API_KEY"), "/api/v3/movie")
    file_count = 0

    for movie in movies:
        upsert_media_item(conn, "radarr", "movie", movie)
        if upsert_media_file(conn, "radarr", "movie_file", movie.get("movieFile"), movie.get("id")):
            file_count += 1

    conn.execute(
        """
        INSERT INTO sync_state (source, last_full_sync)
        VALUES ('radarr', now())
        ON CONFLICT (source) DO UPDATE SET last_full_sync = now()
        """
    )
    return len(movies), file_count


def sync_sonarr(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    sonarr_url = required_env("SONARR_URL")
    sonarr_api_key = required_env("SONARR_API_KEY")
    series = api_get(sonarr_url, sonarr_api_key, "/api/v3/series")

    for item in series:
        upsert_media_item(conn, "sonarr", "series", item)

    file_count = 0
    for item in series:
        series_id = item.get("id")
        if series_id is None:
            continue
        episode_files = api_get(sonarr_url, sonarr_api_key, "/api/v3/episodefile", {"seriesId": series_id})
        for episode_file in episode_files:
            if upsert_media_file(
                conn,
                "sonarr",
                "episode_file",
                episode_file,
                episode_file.get("seriesId"),
            ):
                file_count += 1

    conn.execute(
        """
        INSERT INTO sync_state (source, last_full_sync)
        VALUES ('sonarr', now())
        ON CONFLICT (source) DO UPDATE SET last_full_sync = now()
        """
    )
    return len(series), file_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Radarr/Sonarr libraries into Postgres.")
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--init-db", action="store_true", help="Create/update required tables before sync.")
    parser.add_argument(
        "--source",
        choices=("all", "radarr", "sonarr"),
        default="all",
        help="Limit sync to one source.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_file)

    with connect_postgres(required_env("POSTGRES_DSN")) as conn:
        if args.init_db:
            init_db(conn)

        results: dict[str, tuple[int, int]] = {}
        if args.source in ("all", "radarr"):
            results["radarr"] = sync_radarr(conn)
        if args.source in ("all", "sonarr"):
            results["sonarr"] = sync_sonarr(conn)

        conn.commit()

    for source, (items, files) in results.items():
        print(f"{source}: synced {items} library items, {files} media files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
