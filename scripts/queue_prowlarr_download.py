#!/usr/bin/env python3
"""Select a Prowlarr candidate and optionally save its download artifact to a managed workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from find_missing_language import find_missing
from search_prowlarr_language import search_movie
from sync_library import connect_postgres, load_env, required_env


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS download_jobs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  release_title TEXT NOT NULL,
  release_guid TEXT,
  indexer TEXT,
  indexer_id INTEGER,
  score INTEGER,
  status TEXT NOT NULL,
  staging_path TEXT,
  client TEXT,
  client_queue_id TEXT,
  content_path TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
)
"""


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("assemblarr_library"), dict):
        raise SystemExit("Missing config: assemblarr_library")
    if not isinstance(config.get("prowlarr_search"), dict):
        raise SystemExit("Missing config: prowlarr_search")
    return config


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return cleaned[:180] or "download"


def workspace_paths(config: dict[str, Any]) -> dict[str, Path]:
    workspace = config["assemblarr_library"]
    folders = workspace.get("folders", {})
    root_env = workspace.get("root_env")
    root = Path(os.getenv(str(root_env), str(workspace["root"])) if root_env else str(workspace["root"]))
    return {
        "root": root,
        "incoming": root / str(folders.get("incoming", "incoming")),
        "processing": root / str(folders.get("processing", "processing")),
        "library": root / str(folders.get("library", "library")),
        "archive": root / str(folders.get("archive", "archive")),
        "failed": root / str(folders.get("failed", "failed")),
    }


def ensure_workspace(paths: dict[str, Path], apply: bool) -> None:
    if not apply:
        return
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)


def free_gb(path: Path) -> float:
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    return usage.free / 1024 / 1024 / 1024


def check_space(config: dict[str, Any], candidate: dict[str, Any], paths: dict[str, Path]) -> None:
    workspace = config["assemblarr_library"]
    available = free_gb(paths["root"])
    min_free = float(workspace.get("min_free_gb", 0))
    max_size = float(workspace.get("max_candidate_size_gb", 0))
    candidate_size = float(candidate.get("size_gb") or 0)

    if available < min_free:
        raise SystemExit(f"Not enough free space: {available:.1f} GB available, {min_free:.1f} GB required")
    if max_size and candidate_size > max_size:
        raise SystemExit(f"Candidate too large: {candidate_size:.1f} GB, max {max_size:.1f} GB")


def save_artifact(candidate: dict[str, Any], paths: dict[str, Path], config: dict[str, Any]) -> Path:
    prefix = str(config["assemblarr_library"].get("filename_prefix", "assemblarr"))
    base_name = slug(f"{prefix}-{candidate['title']}")

    if candidate.get("magnet_url"):
        target = paths["incoming"] / f"{base_name}.magnet"
        target.write_text(str(candidate["magnet_url"]) + "\n", encoding="utf-8")
        return target

    download_url = candidate.get("download_url")
    if not download_url:
        raise SystemExit("Selected candidate has no downloadUrl or magnetUrl")

    request = urllib.request.Request(str(download_url), headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"Download failed: {exc}") from exc

    suffix = ".nzb" if "xml" in content_type or "nzb" in content_type else ".torrent"
    target = paths["incoming"] / f"{base_name}{suffix}"
    target.write_bytes(payload)
    return target


def insert_job(
    conn: Any,
    target: dict[str, Any],
    candidate: dict[str, Any],
    status: str,
    staging_path: Path | None,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    conn.execute(
        """
        INSERT INTO download_jobs (
            source, media_type, source_id, title, release_title, release_guid,
            indexer, indexer_id, score, status, staging_path, metadata, updated_at
        )
        VALUES (
            %(source)s, %(media_type)s, %(source_id)s, %(title)s, %(release_title)s,
            %(release_guid)s, %(indexer)s, %(indexer_id)s, %(score)s, %(status)s,
            %(staging_path)s, %(metadata)s, now()
        )
        """,
        {
            "source": target["source"],
            "media_type": target["media_type"],
            "source_id": target["source_id"],
            "title": target.get("title") or "unknown",
            "release_title": candidate["title"],
            "release_guid": candidate.get("guid"),
            "indexer": candidate.get("indexer"),
            "indexer_id": candidate.get("indexer_id"),
            "score": candidate.get("score"),
            "status": status,
            "staging_path": str(staging_path) if staging_path else None,
            "metadata": Jsonb({"candidate": candidate, "target": target}),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue one Prowlarr candidate into the managed workspace.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--max-targets", type=int, default=25, help="How many missing-language targets to try.")
    parser.add_argument("--apply", action="store_true", help="Save the download artifact and insert a DB job.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)
    paths = workspace_paths(config)

    with connect_postgres(required_env(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))) as conn:
        conn.row_factory = dict_row
        conn.execute(DOWNLOAD_TABLE_SQL)
        search_check = dict(config["library_checks"]["missing_language"])
        search_check["limit"] = args.max_targets
        missing = find_missing(conn, search_check)
        if not missing:
            print("No missing-language target found for Prowlarr search.")
            return 0

        target = None
        candidate = None
        skipped = []
        for possible_target in missing:
            candidates = search_movie(possible_target, config["prowlarr_search"])
            if candidates:
                target = possible_target
                candidate = candidates[0]
                break
            skipped.append(f"{possible_target.get('title')} ({possible_target.get('year')})")

        if target is None or candidate is None:
            print(f"No Prowlarr candidates found for first {len(missing)} missing-language targets.")
            for skipped_target in skipped[:20]:
                print(f"skipped_no_candidate: {skipped_target}")
            return 2

        check_space(config, candidate, paths)
        ensure_workspace(paths, args.apply)
        staging_path = save_artifact(candidate, paths, config) if args.apply else None
        insert_job(conn, target, candidate, "artifact_saved" if args.apply else "dry_run", staging_path, dry_run)
        if args.apply:
            conn.commit()

    print("Prowlarr download candidate")
    print(f"dry_run: {dry_run}")
    print(f"targets_tried: {len(skipped) + 1}")
    for skipped_target in skipped[:10]:
        print(f"skipped_no_candidate: {skipped_target}")
    print(f"target: {target.get('title')} ({target.get('year')})")
    print(f"release: {candidate['title']}")
    print(f"score: {candidate['score']}")
    print(f"indexer: {candidate.get('indexer')}")
    print(f"size_gb: {candidate.get('size_gb')}")
    print(f"workspace_free_gb: {free_gb(paths['root']):.1f}")
    print(f"staging_path: {staging_path or 'not-written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
