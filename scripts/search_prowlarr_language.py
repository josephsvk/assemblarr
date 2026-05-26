#!/usr/bin/env python3
"""Search Prowlarr for better language matches for the first configured missing-language movie."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row

from find_missing_language import find_missing, token_pattern
from sync_library import connect_postgres, load_env, required_env


ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if not isinstance(config.get("prowlarr_search"), dict):
        raise SystemExit("Missing config: prowlarr_search")
    if not isinstance(config.get("library_checks", {}).get("missing_language"), dict):
        raise SystemExit("Missing config: library_checks.missing_language")

    return config


def prowlarr_get(base_url: str, api_key: str, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(params, doseq=True)
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if query:
        url = f"{url}?{query}"

    request = urllib.request.Request(url, headers={"X-Api-Key": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc

    if not isinstance(payload, list):
        raise RuntimeError(f"GET {url} returned {type(payload).__name__}, expected list")
    return payload


def release_text(release: dict[str, Any]) -> str:
    return " ".join(str(value or "") for value in (release.get("title"), release.get("fileName")))


def score_named_tokens(text: str, scores: dict[str, int]) -> tuple[int, list[str]]:
    total = 0
    matched = []
    for token, points in scores.items():
        if token_pattern([str(token)]).search(text):
            total += int(points)
            matched.append(str(token))
    return total, matched


def score_release(release: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any] | None:
    text = release_text(release)
    language = search_config["language"]
    required_tokens = [str(token) for token in language["required_any_tokens"]]
    if not token_pattern(required_tokens).search(text):
        return None

    scoring = search_config.get("scoring", {})
    reject_tokens = [str(token) for token in scoring.get("reject_tokens", [])]
    if reject_tokens and token_pattern(reject_tokens).search(text):
        return None

    total = 0
    reasons = []

    points, matched = score_named_tokens(text, {str(k): int(v) for k, v in language.get("score", {}).items()})
    total += points
    if matched:
        reasons.append(f"language={'+'.join(matched)}:{points}")

    for group_name in ("audio", "video", "source"):
        points, matched = score_named_tokens(text, {str(k): int(v) for k, v in scoring.get(group_name, {}).items()})
        total += points
        if matched:
            reasons.append(f"{group_name}={'+'.join(matched)}:{points}")

    seeders = release.get("seeders") or 0
    seed_points = int(seeders) * int(scoring.get("seeders_multiplier", 0))
    total += seed_points
    if seed_points:
        reasons.append(f"seeders={seeders}:{seed_points}")

    size = release.get("size") or 0
    return {
        "score": total,
        "reasons": reasons,
        "title": release.get("title") or release.get("fileName") or "",
        "indexer": release.get("indexer") or "",
        "indexer_id": release.get("indexerId"),
        "seeders": seeders,
        "size_gb": round(int(size) / 1024 / 1024 / 1024, 2) if size else 0,
        "guid": release.get("guid") or "",
        "download_url": release.get("downloadUrl") or "",
        "magnet_url": release.get("magnetUrl") or "",
        "download_url_present": bool(release.get("downloadUrl") or release.get("magnetUrl")),
    }


def search_movie(row: dict[str, Any], search_config: dict[str, Any]) -> list[dict[str, Any]]:
    query = f"{row['title']} {row['year']}".strip()
    params: dict[str, Any] = {
        "query": query,
        "type": search_config.get("type", "movie"),
        "limit": int(search_config.get("limit", 50)),
    }
    categories = search_config.get("categories")
    if categories:
        params["categories"] = [int(category) for category in categories]

    releases = prowlarr_get(
        required_env(search_config.get("base_url_env", "PROWLARR_URL")),
        required_env(search_config.get("api_key_env", "PROWLARR_API_KEY")),
        "/api/v1/search",
        params,
    )

    scored = []
    for release in releases:
        item = score_release(release, search_config)
        if item is not None:
            scored.append(item)

    return sorted(scored, key=lambda item: (item["score"], item["seeders"], item["size_gb"]), reverse=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search Prowlarr for CZ/SK candidates for one missing-language movie.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--top", default=5, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)

    with connect_postgres(required_env(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))) as conn:
        conn.row_factory = dict_row
        missing = find_missing(conn, config["library_checks"]["missing_language"])

    if not missing:
        print("No missing-language movie found.")
        return 0

    movie = missing[0]
    results = search_movie(movie, config["prowlarr_search"])

    print(f"Search target: {movie['title']} ({movie['year']})")
    print(f"Current file: {movie['path']}")
    if not results:
        print("No Prowlarr releases matched configured language tokens.")
        return 2

    print(f"Top {min(args.top, len(results))} Prowlarr candidates:")
    for index, result in enumerate(results[: args.top], start=1):
        print(f"{index}. score={result['score']} seeders={result['seeders']} size_gb={result['size_gb']}")
        print(f"   indexer: {result['indexer']}")
        print(f"   title: {result['title']}")
        print(f"   reasons: {', '.join(result['reasons'])}")
        print(f"   download_url_present: {result['download_url_present']}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
