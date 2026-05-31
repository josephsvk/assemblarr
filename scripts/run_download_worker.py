#!/usr/bin/env python3
"""Continuously watch qBittorrent jobs, scan completed downloads, and refill the queue."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import yaml
from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from queue_prowlarr_download import QbittorrentClient, upsert_rss_waitlist
from extract_download_audio import EXTRACTION_TABLE_SQL, extract_tracks, output_root, upsert_extraction
from remux_library_video_with_download_audio import REMUX_TABLE_SQL as LIBRARY_AUDIO_REMUX_TABLE_SQL
from scan_download_artifact import SCAN_TABLE_SQL, scan_path, upsert_scan
from sync_library import ROOT, connect_postgres, ensure_schema, load_env, required_env


SCHEMA_PATH = ROOT / "schema.sql"
LOGGER = logging.getLogger("assemblarr.download_worker")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("download_clients"), dict):
        raise SystemExit("Missing config: download_clients")
    if not isinstance(config.get("download_worker"), dict):
        raise SystemExit("Missing config: download_worker")
    if not isinstance(config.get("download_scan"), dict):
        raise SystemExit("Missing config: download_scan")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Assemblarr download worker loop.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--once", action="store_true", help="Run one worker cycle and exit.")
    parser.add_argument("--no-fill-queue", action="store_true", help="Do not queue new torrents in this run.")
    parser.add_argument(
        "--no-library-audio-remux",
        action="store_true",
        help="Do not run the library audio remux postprocess in this run.",
    )
    parser.add_argument("--poll-seconds", type=int, help="Override download_worker.poll_seconds.")
    return parser.parse_args()


def qbit_config(config: dict[str, Any]) -> dict[str, Any]:
    clients = config["download_clients"]
    if not clients.get("enabled"):
        raise SystemExit("download_clients.enabled is false")
    if str(clients.get("preferred", "qbittorrent")) != "qbittorrent":
        raise SystemExit("Only qbittorrent worker mode is supported")
    qbittorrent = dict(clients.get("qbittorrent", {}))
    if not qbittorrent:
        raise SystemExit("Missing config: download_clients.qbittorrent")
    return qbittorrent


def build_qbit_client(config: dict[str, Any]) -> tuple[QbittorrentClient, dict[str, Any]]:
    qbittorrent = qbit_config(config)
    client = QbittorrentClient(
        required_env(str(qbittorrent.get("url_env", "QBITTORRENT_URL"))),
        required_env(str(qbittorrent.get("username_env", "QBITTORRENT_USERNAME"))),
        required_env(str(qbittorrent.get("password_env", "QBITTORRENT_PASSWORD"))),
    )
    client.login()
    return client, qbittorrent


def qbittorrent_category(qbittorrent: dict[str, Any]) -> str:
    return str(qbittorrent.get("category", "assemblarr"))


def list_torrents(client: QbittorrentClient, category: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"category": category})
    status, payload = client.request(f"/api/v2/torrents/info?{query}")
    if status != 200:
        raise SystemExit(f"qBittorrent info listing failed with HTTP {status}")
    data = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        raise SystemExit("qBittorrent info listing returned unexpected payload")
    return data


def active_torrent_count(torrents: list[dict[str, Any]]) -> int:
    count = 0
    for torrent in torrents:
        if float(torrent.get("progress") or 0) < 1.0:
            count += 1
    return count


def worker_state(job: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(job.get("metadata") or {})
    worker = dict(metadata.get("worker") or {})
    return worker


def merged_metadata(job: dict[str, Any], worker_updates: dict[str, Any]) -> Jsonb:
    metadata = dict(job.get("metadata") or {})
    worker = dict(metadata.get("worker") or {})
    for key, value in worker_updates.items():
        if value is None:
            worker.pop(key, None)
        else:
            worker[key] = value
    metadata["worker"] = worker
    return Jsonb(metadata)


def stalled_policy(config: dict[str, Any]) -> dict[str, Any]:
    stalled = dict(config["download_worker"].get("stalled", {}))
    minutes = int(stalled.get("minutes", 180))
    return {
        "enabled": bool(stalled.get("enabled", True)),
        "minutes": max(1, minutes),
        "remove_from_client": bool(stalled.get("remove_from_client", True)),
        "move_to_rss_waitlist": bool(stalled.get("move_to_rss_waitlist", True)),
    }


def library_audio_remux_policy(config: dict[str, Any]) -> dict[str, Any]:
    worker_postprocess = dict(config["download_worker"].get("postprocess", {}))
    remux = dict(worker_postprocess.get("library_audio_remux", {}))
    return {
        "enabled": bool(remux.get("enabled", True)),
        "max_per_cycle": max(1, int(remux.get("max_per_cycle", 1))),
    }


def map_status(torrent: dict[str, Any]) -> str:
    progress = float(torrent.get("progress") or 0)
    state = str(torrent.get("state") or "")
    if progress >= 1.0 or state.endswith("UP") or state == "uploading":
        return "downloaded"
    if "stalled" in state.lower():
        return "stalled"
    if "error" in state.lower():
        return "client_error"
    if state in {"queuedDL", "metaDL", "forcedMetaDL"}:
        return "queued_download"
    return "downloading"


def derived_source_id(queue_id: str) -> int:
    return int(queue_id[:8], 16) % 2147483647


def fetch_extraction_queue_ids(conn: Any) -> set[str]:
    rows = conn.execute("SELECT client_queue_id FROM download_audio_extractions").fetchall()
    return {str(row["client_queue_id"]).lower() for row in rows if row["client_queue_id"]}


def latest_jobs_by_queue_id(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (client_queue_id)
            id, source, media_type, source_id, title, status, client, client_queue_id, content_path, metadata
        FROM download_jobs
        WHERE client = 'qbittorrent'
          AND client_queue_id IS NOT NULL
          AND status != 'superseded_duplicate'
        ORDER BY client_queue_id, id DESC
        """
    ).fetchall()
    return {str(row["client_queue_id"]).lower(): row for row in rows if row["client_queue_id"]}


def adopt_or_refresh_missing_jobs(conn: Any, torrents: list[dict[str, Any]], known_jobs: dict[str, dict[str, Any]]) -> int:
    adopted = 0
    for torrent in torrents:
        queue_id = str(torrent.get("hash") or "").lower()
        if not queue_id or queue_id in known_jobs:
            continue
        title = str(torrent.get("name") or queue_id)
        status = map_status(torrent)
        content_path = str(torrent.get("content_path") or torrent.get("save_path") or "")
        conn.execute(
            """
            INSERT INTO download_jobs (
                source, media_type, source_id, title, release_title, release_guid,
                indexer, indexer_id, score, status, staging_path, client, client_queue_id,
                content_path, metadata, updated_at
            )
            VALUES (
                'adopted_qbittorrent', 'download', %(source_id)s, %(title)s, %(release_title)s, %(release_guid)s,
                %(indexer)s, NULL, NULL, %(status)s, NULL, 'qbittorrent', %(client_queue_id)s,
                %(content_path)s, %(metadata)s, now()
            )
            """,
            {
                "source_id": derived_source_id(queue_id),
                "title": title,
                "release_title": title,
                "release_guid": queue_id,
                "indexer": str(torrent.get("tracker") or ""),
                "status": status,
                "client_queue_id": queue_id,
                "content_path": content_path or None,
                "metadata": Jsonb({"adopted": True, "qbittorrent": torrent}),
            },
        )
        adopted += 1
    return adopted


def process_completed_job(
    conn: Any,
    config: dict[str, Any],
    client: QbittorrentClient,
    job: dict[str, Any],
    queue_id: str,
    content_path: str,
    scan: dict[str, Any],
    extraction_exists: bool,
) -> tuple[str, bool]:
    upsert_scan(
        conn,
        download_job_id=int(job["id"]),
        client="qbittorrent",
        client_queue_id=queue_id,
        content_path=content_path,
        scan=scan,
    )

    extraction_status = "audio_extraction_skipped"
    extracted_tracks: list[dict[str, Any]] = []
    probe_streams: list[dict[str, Any]] = []
    primary_video_path = scan.get("primary_video_path")
    if scan["scan_status"] == "scanned" and primary_video_path and not extraction_exists:
        extraction_root = output_root(config, queue_id)
        try:
            extraction_status, extracted_tracks, probe_streams = extract_tracks(
                Path(primary_video_path),
                extraction_root,
                config,
            )
        except subprocess.CalledProcessError as exc:
            extraction_status = "audio_extraction_failed"
            LOGGER.warning(
                "job %s audio extraction failed: title=%s returncode=%s",
                job["id"],
                job["title"],
                exc.returncode,
            )
        else:
            upsert_extraction(
                conn,
                download_job_id=int(job["id"]),
                client_queue_id=queue_id,
                source_video_path=str(primary_video_path),
                output_root_path=str(extraction_root),
                extraction_status=extraction_status,
                extracted_tracks=extracted_tracks,
                probe_streams=probe_streams,
                metadata={"content_path": content_path},
            )
    elif extraction_exists:
        extraction_status = "audio_extracted"

    remove_completed = bool(config["download_clients"]["qbittorrent"].get("remove_completed_from_client", True))
    seed_ratio_limit = float(config["download_clients"]["qbittorrent"].get("seed_ratio_limit", 0))
    removed_from_client = False
    if remove_completed and seed_ratio_limit <= 0:
        client.delete_torrent(queue_id, delete_files=False)
        removed_from_client = True

    if extraction_status == "audio_extracted":
        final_status = "audio_extracted"
    elif extraction_status == "audio_extraction_failed":
        final_status = "audio_extraction_failed"
    else:
        final_status = "scanned" if scan["scan_status"] == "scanned" else "downloaded_missing_path"

    conn.execute(
        """
        UPDATE download_jobs
        SET status = %(status)s,
            metadata = metadata || %(metadata)s::jsonb,
            updated_at = now()
        WHERE id = %(id)s
        """,
        {
            "id": job["id"],
            "status": final_status,
            "metadata": Jsonb(
                {
                    "worker": {
                        "removed_from_client": removed_from_client,
                        "seed_ratio_limit": seed_ratio_limit,
                        "audio_extraction_status": extraction_status,
                    }
                }
            ),
        },
    )
    LOGGER.info(
        "job %s completed: title=%s content_path=%s file_count=%s extraction_status=%s removed_from_client=%s",
        job["id"],
        job["title"],
        content_path,
        scan["file_count"],
        extraction_status,
        removed_from_client,
    )
    return final_status, removed_from_client


def backfill_local_audio_extractions(conn: Any, config: dict[str, Any], extraction_queue_ids: set[str]) -> int:
    rows = conn.execute(
        """
        SELECT id, title, client_queue_id, content_path
        FROM download_jobs
        WHERE client = 'qbittorrent'
          AND status = 'scanned'
          AND client_queue_id IS NOT NULL
          AND content_path IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    updated = 0
    for row in rows:
        queue_id = str(row["client_queue_id"]).lower()
        if queue_id in extraction_queue_ids:
            continue
        scan = scan_path(Path(str(row["content_path"])), config)
        primary_video_path = scan.get("primary_video_path")
        if scan["scan_status"] != "scanned" or not primary_video_path:
            continue
        extraction_root = output_root(config, queue_id)
        try:
            extraction_status, extracted_tracks, probe_streams = extract_tracks(
                Path(primary_video_path),
                extraction_root,
                config,
            )
        except subprocess.CalledProcessError as exc:
            extraction_status = "audio_extraction_failed"
            extracted_tracks = []
            probe_streams = []
            LOGGER.warning(
                "job %s local extraction backfill failed: title=%s returncode=%s",
                row["id"],
                row["title"],
                exc.returncode,
            )
        upsert_extraction(
            conn,
            download_job_id=int(row["id"]),
            client_queue_id=queue_id,
            source_video_path=str(primary_video_path),
            output_root_path=str(extraction_root),
            extraction_status=extraction_status,
            extracted_tracks=extracted_tracks,
            probe_streams=probe_streams,
            metadata={"content_path": str(row["content_path"]), "backfilled": True},
        )
        conn.execute(
            """
            UPDATE download_jobs
            SET status = %(status)s, updated_at = now()
            WHERE id = %(id)s
            """,
            {
                "id": row["id"],
                "status": "audio_extracted" if extraction_status == "audio_extracted" else "audio_extraction_failed",
            },
        )
        extraction_queue_ids.add(queue_id)
        updated += 1
        LOGGER.info(
            "job %s local extraction backfilled: title=%s extraction_status=%s",
            row["id"],
            row["title"],
            extraction_status,
        )
    return updated


def collapse_duplicate_jobs(conn: Any) -> int:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY client, client_queue_id
                    ORDER BY id
                ) AS rank_in_group
            FROM download_jobs
            WHERE client = 'qbittorrent'
              AND client_queue_id IS NOT NULL
              AND status NOT IN ('imported', 'archived', 'superseded_duplicate')
        )
        UPDATE download_jobs AS jobs
        SET status = 'superseded_duplicate',
            updated_at = now()
        FROM ranked
        WHERE jobs.id = ranked.id
          AND ranked.rank_in_group > 1
        RETURNING jobs.id
        """
    ).fetchall()
    return len(rows)


def expire_stalled_job(
    conn: Any,
    config: dict[str, Any],
    client: QbittorrentClient,
    job: dict[str, Any],
    queue_id: str,
    torrent: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    removed_from_client = False
    if policy["remove_from_client"]:
        client.delete_torrent(queue_id, delete_files=False)
        removed_from_client = True

    if policy["move_to_rss_waitlist"]:
        target = dict(job.get("metadata") or {}).get("target") or {}
        upsert_rss_waitlist(
            conn,
            {
                "source": target.get("source") or job["source"],
                "media_type": target.get("media_type") or job["media_type"],
                "source_id": target.get("source_id") or job["source_id"],
                "title": target.get("title") or job["title"],
                "year": target.get("year"),
            },
            "waiting_for_rss",
            "torrent stalled beyond configured limit",
            {
                "stalled_minutes": policy["minutes"],
                "client_queue_id": queue_id,
                "release_title": job.get("release_title"),
                "qbittorrent": torrent,
            },
            dry_run=False,
        )

    conn.execute(
        """
        UPDATE download_jobs
        SET status = 'stalled_expired',
            metadata = %(metadata)s::jsonb,
            updated_at = now()
        WHERE id = %(id)s
        """,
        {
            "id": job["id"],
            "metadata": merged_metadata(
                job,
                {
                    "stalled_since_epoch": None,
                    "stalled_expired": True,
                    "stalled_expired_at_epoch": int(time.time()),
                    "removed_from_client": removed_from_client,
                },
            ),
        },
    )
    LOGGER.warning(
        "job %s stalled too long: title=%s limit_minutes=%s removed_from_client=%s",
        job["id"],
        job["title"],
        policy["minutes"],
        removed_from_client,
    )


def sync_download_jobs(conn: Any, config: dict[str, Any], client: QbittorrentClient, category: str) -> int:
    duplicate_count = collapse_duplicate_jobs(conn)
    if duplicate_count:
        LOGGER.warning("collapsed %s duplicate download job(s)", duplicate_count)

    torrents = list_torrents(client, category)
    torrent_by_hash = {str(item.get("hash") or "").lower(): item for item in torrents if item.get("hash")}
    active_count = active_torrent_count(torrents)
    extraction_queue_ids = fetch_extraction_queue_ids(conn)
    backfilled_count = backfill_local_audio_extractions(conn, config, extraction_queue_ids)
    if backfilled_count:
        LOGGER.warning("backfilled %s local audio extraction(s)", backfilled_count)
    latest_jobs = latest_jobs_by_queue_id(conn)
    adopted_count = adopt_or_refresh_missing_jobs(conn, torrents, latest_jobs)
    if adopted_count:
        LOGGER.warning("adopted %s qBittorrent torrent(s) into download_jobs", adopted_count)
    stall_policy = stalled_policy(config)

    jobs = conn.execute(
        """
        SELECT id, source, media_type, source_id, title, release_title, status, client, client_queue_id, content_path, metadata
        FROM download_jobs
        WHERE client = 'qbittorrent'
          AND client_queue_id IS NOT NULL
          AND status NOT IN (
              'imported',
              'archived',
              'superseded_duplicate',
              'stalled_expired'
          )
        ORDER BY id
        """
    ).fetchall()

    for job in jobs:
        queue_id = str(job["client_queue_id"] or "").lower()
        if not queue_id:
            continue
        torrent = torrent_by_hash.get(queue_id)
        if not torrent:
            if job["status"] in {"scanned", "audio_extracted", "audio_extraction_failed"}:
                continue
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'missing_in_client', updated_at = now()
                WHERE id = %(id)s
                """,
                {"id": job["id"]},
            )
            LOGGER.warning("job %s missing in qBittorrent: %s", job["id"], job["title"])
            continue

        status = map_status(torrent)
        content_path = str(torrent.get("content_path") or torrent.get("save_path") or "")
        existing_worker = worker_state(job)
        worker_updates: dict[str, Any] = {}
        if status == "stalled":
            worker_updates["stalled_since_epoch"] = int(existing_worker.get("stalled_since_epoch") or time.time())
        else:
            worker_updates["stalled_since_epoch"] = None
        conn.execute(
            """
            UPDATE download_jobs
            SET status = %(status)s,
                content_path = %(content_path)s,
                metadata = %(metadata)s::jsonb,
                updated_at = now()
            WHERE id = %(id)s
            """,
            {
                "id": job["id"],
                "status": status,
                "content_path": content_path or None,
                "metadata": merged_metadata(job, worker_updates),
            },
        )
        conn.execute(
            """
            UPDATE download_jobs
            SET metadata = metadata || %(metadata)s::jsonb,
                updated_at = now()
            WHERE id = %(id)s
            """,
            {
                "id": job["id"],
                "metadata": Jsonb({"qbittorrent": torrent}),
            },
        )

        needs_postprocess = status == "downloaded" or job["status"] in {"scanned", "audio_extraction_failed"}
        if needs_postprocess:
            scan = scan_path(Path(content_path), config)
            try:
                process_completed_job(
                    conn,
                    config,
                    client,
                    job,
                    queue_id,
                    content_path,
                    scan,
                    extraction_exists=queue_id in extraction_queue_ids,
                )
            except OperationalError as exc:
                LOGGER.error("job %s database operation failed during postprocess: %s", job["id"], exc)
                raise
        elif status == "stalled" and stall_policy["enabled"]:
            stalled_since = int(worker_updates.get("stalled_since_epoch") or existing_worker.get("stalled_since_epoch") or time.time())
            stalled_for_seconds = max(0, int(time.time()) - stalled_since)
            if stalled_for_seconds >= stall_policy["minutes"] * 60:
                expire_stalled_job(conn, config, client, job, queue_id, torrent, stall_policy)
                continue
            LOGGER.warning(
                "job %s stalled: title=%s progress=%.3f stalled_minutes=%s/%s",
                job["id"],
                job["title"],
                float(torrent.get("progress") or 0),
                stalled_for_seconds // 60,
                stall_policy["minutes"],
            )
        else:
            LOGGER.info(
                "job %s synced: title=%s status=%s progress=%.3f",
                job["id"],
                job["title"],
                status,
                float(torrent.get("progress") or 0),
            )

    return active_count


def queue_more_downloads(
    config: dict[str, Any],
    *,
    max_active_torrents: int,
    active_count: int,
    max_targets: int,
    fill_limit: int,
) -> int:
    if active_count >= max_active_torrents:
        LOGGER.info("queue is full: active=%s max=%s", active_count, max_active_torrents)
        return 0

    added = 0
    available_slots = max_active_torrents - active_count
    attempts = min(available_slots, fill_limit)
    queue_script = ROOT / "scripts" / "queue_prowlarr_download.py"

    for index in range(attempts):
        command = [
            sys.executable,
            str(queue_script),
            "--max-targets",
            str(max_targets),
            "--apply",
        ]
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                LOGGER.info("queue_run[%s]: %s", index + 1, line)
        if result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                LOGGER.warning("queue_run[%s] stderr: %s", index + 1, line)

        if result.returncode == 0:
            added += 1
            continue
        if result.returncode == 2:
            LOGGER.info("no more acceptable candidates available for queueing")
            break
        LOGGER.error("queue run failed with exit code %s", result.returncode)
        break

    return added


def run_library_audio_remux_postprocess(config: dict[str, Any], limit: int) -> int:
    script = ROOT / "scripts" / "remux_library_video_with_download_audio.py"
    completed = 0
    for index in range(limit):
        command = [
            sys.executable,
            str(script),
            "--apply",
        ]
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                LOGGER.info("library_audio_remux[%s]: %s", index + 1, line)
        if result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                LOGGER.warning("library_audio_remux[%s] stderr: %s", index + 1, line)

        combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode == 0:
            completed += 1
            continue
        if "No eligible Radarr movie download job found for library-audio remux." in combined_output:
            LOGGER.info("no eligible Radarr library-audio remux job found")
            break
        LOGGER.error("library-audio remux run failed with exit code %s", result.returncode)
        break
    return completed


def run_cycle(config: dict[str, Any], fill_queue: bool, perform_library_audio_remux: bool) -> None:
    worker_config = config["download_worker"]
    max_targets = int(worker_config.get("max_targets_per_queue_run", 50))
    fill_limit = int(worker_config.get("queue_fill_per_cycle", 1))

    client, qbittorrent = build_qbit_client(config)
    category = qbittorrent_category(qbittorrent)
    max_active_torrents = int(qbittorrent.get("max_active_torrents", 5))
    remux_policy = library_audio_remux_policy(config)

    with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
        conn.row_factory = dict_row
        ensure_schema(conn)
        conn.execute(LIBRARY_AUDIO_REMUX_TABLE_SQL)
        active_count = sync_download_jobs(conn, config, client, category)
        LOGGER.info("active torrents: %s / %s", active_count, max_active_torrents)
        conn.commit()

        added = 0
        remuxed = 0
        if remux_policy["enabled"] and perform_library_audio_remux:
            remuxed = run_library_audio_remux_postprocess(config, remux_policy["max_per_cycle"])
        if fill_queue:
            added = queue_more_downloads(
                config,
                max_active_torrents=max_active_torrents,
                active_count=active_count,
                max_targets=max_targets,
                fill_limit=fill_limit,
            )
        conn.commit()

    if fill_queue and added:
        LOGGER.info("queued %s new torrent(s) this cycle", added)
    if remux_policy["enabled"] and remuxed:
        LOGGER.info("completed %s library-audio remux postprocess job(s) this cycle", remuxed)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env(args.env_file)
    config = load_config(args.config)
    poll_seconds = int(args.poll_seconds or config["download_worker"].get("poll_seconds", 60))
    fill_queue = not args.no_fill_queue
    perform_library_audio_remux = not args.no_library_audio_remux

    while True:
        try:
            run_cycle(config, fill_queue, perform_library_audio_remux)
        except Exception as exc:  # pragma: no cover - defensive loop logging
            LOGGER.exception("worker cycle failed: %s", exc)

        if args.once:
            break
        time.sleep(max(5, poll_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
