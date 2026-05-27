#!/usr/bin/env python3
"""Select a Prowlarr candidate and optionally save its download artifact to a staging workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from find_missing_language import find_missing
from search_prowlarr_language import search_movie_diagnostics
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
RSS_WAITLIST_SQL = """
CREATE TABLE IF NOT EXISTS rss_waitlist (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  year INTEGER,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id)
)
"""
OPEN_DOWNLOAD_JOB_STATUSES = (
    "artifact_saved",
    "client_pending",
    "client_queued",
    "queued_download",
    "downloading",
    "stalled",
    "downloaded",
    "downloaded_missing_path",
    "scanned",
    "audio_extracted",
    "audio_extraction_failed",
)


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


def staging_paths(config: dict[str, Any]) -> dict[str, Path]:
    download_workspace = config.get("download_workspace", {})
    if download_workspace.get("use_library", True):
        return workspace_paths(config)

    root_env = download_workspace.get("root_env")
    if root_env:
        root_value = required_env(str(root_env))
    else:
        library_paths = config.get("library", {}).get("paths", {})
        download_root_env = library_paths.get("download_root_env", "DOWNLOAD_ROOT")
        root_value = required_env(str(download_root_env))

    root = Path(root_value)
    subdir = str(download_workspace.get("subdir", "assemblarr"))
    incoming = str(download_workspace.get("incoming_folder", "incoming"))
    processing = str(download_workspace.get("processing_folder", "processing"))
    archive = str(download_workspace.get("archive_folder", "archive"))
    failed = str(download_workspace.get("failed_folder", "failed"))

    if subdir:
        root = root / subdir

    return {
        "root": root,
        "incoming": root / incoming,
        "processing": root / processing,
        "library": workspace_paths(config)["library"],
        "archive": root / archive,
        "failed": root / failed,
    }


def ensure_workspace(paths: dict[str, Path], apply: bool) -> None:
    if not apply:
        return
    for key in ("root", "incoming", "processing", "archive", "failed"):
        path = paths[key]
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


def bencode(value: Any) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return str(len(encoded)).encode("ascii") + b":" + encoded
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = []
        for key in sorted(value):
            key_bytes = key if isinstance(key, bytes) else str(key).encode("utf-8")
            items.append(bencode(key_bytes))
            items.append(bencode(value[key]))
        return b"d" + b"".join(items) + b"e"
    raise TypeError(f"Unsupported bencode type: {type(value).__name__}")


def bdecode(data: bytes, index: int = 0) -> tuple[Any, int]:
    token = data[index:index + 1]
    if token == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if token == b"l":
        result = []
        index += 1
        while data[index:index + 1] != b"e":
            item, index = bdecode(data, index)
            result.append(item)
        return result, index + 1
    if token == b"d":
        result: dict[bytes, Any] = {}
        index += 1
        while data[index:index + 1] != b"e":
            key, index = bdecode(data, index)
            value, index = bdecode(data, index)
            if not isinstance(key, bytes):
                raise ValueError("Invalid bencode dictionary key")
            result[key] = value
        return result, index + 1
    if token.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end
    raise ValueError("Invalid bencode payload")


def torrent_info_hash(staging_path: Path) -> str | None:
    if staging_path.suffix != ".torrent":
        return None
    payload = staging_path.read_bytes()
    decoded, _ = bdecode(payload)
    if not isinstance(decoded, dict):
        return None
    info = decoded.get(b"info")
    if info is None:
        return None
    return hashlib.sha1(bencode(info)).hexdigest()


def magnet_info_hash(staging_path: Path) -> str | None:
    if staging_path.suffix != ".magnet":
        return None
    magnet_url = staging_path.read_text(encoding="utf-8").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(magnet_url).query)
    xt_values = query.get("xt", [])
    for xt in xt_values:
        prefix = "urn:btih:"
        if xt.startswith(prefix):
            return xt[len(prefix):].lower()
    return None


def multipart_form_data(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = hashlib.sha1(os.urandom(16)).hexdigest()
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8"),
                content,
                b"\r\n",
            ]
        )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class QbittorrentClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers or {},
            method=method,
        )
        with self.opener.open(request, timeout=60) as response:
            return response.status, response.read()

    def login(self) -> None:
        payload = urllib.parse.urlencode(
            {
                "username": self.username,
                "password": self.password,
            }
        ).encode("utf-8")
        status, response = self.request(
            "/api/v2/auth/login",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
        text = response.decode("utf-8", errors="replace").strip()
        if status in (200, 204) and text in ("", "Ok."):
            return
        if text != "Ok.":
            raise SystemExit(f"qBittorrent login failed: {text or f'HTTP {status}'}")
        if status not in (200, 204):
            raise SystemExit(f"qBittorrent login failed: {response.strip() or 'unexpected response'}")

    def add_torrent(
        self,
        staging_path: Path,
        *,
        save_path: str,
        category: str | None,
    ) -> None:
        fields = {"savepath": save_path}
        if category:
            fields["category"] = category

        if staging_path.suffix == ".magnet":
            fields["urls"] = staging_path.read_text(encoding="utf-8").strip()
            payload = urllib.parse.urlencode(fields).encode("utf-8")
            status, response = self.request(
                "/api/v2/torrents/add",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=payload,
            )
        else:
            payload, content_type = multipart_form_data(
                fields,
                {"torrents": (staging_path.name, staging_path.read_bytes(), "application/x-bittorrent")},
            )
            status, response = self.request(
                "/api/v2/torrents/add",
                method="POST",
                headers={"Content-Type": content_type},
                data=payload,
            )

        text = response.decode("utf-8", errors="replace").strip()
        if status in (200, 204) and text in ("", "Ok."):
            return
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and int(payload.get("success_count", 0)) > 0:
                return
        if text != "Ok.":
            raise SystemExit(f"qBittorrent add failed: {text or 'unexpected response'}")

    def torrent_info(self, queue_id: str) -> list[dict[str, Any]]:
        status, response = self.request(f"/api/v2/torrents/info?hashes={urllib.parse.quote(queue_id)}")
        if status != 200:
            raise SystemExit(f"qBittorrent info lookup failed with HTTP {status}")
        payload = json.loads(response.decode("utf-8", errors="replace"))
        if not isinstance(payload, list):
            raise SystemExit("qBittorrent info lookup returned unexpected payload")
        return payload

    def delete_torrent(self, queue_id: str, *, delete_files: bool) -> None:
        payload = urllib.parse.urlencode(
            {
                "hashes": queue_id,
                "deleteFiles": "true" if delete_files else "false",
            }
        ).encode("utf-8")
        status, response = self.request(
            "/api/v2/torrents/delete",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
        text = response.decode("utf-8", errors="replace").strip()
        if status in (200, 204) and text in ("", "Ok."):
            return
        raise SystemExit(f"qBittorrent delete failed: {text or f'HTTP {status}'}")


def client_save_path(config: dict[str, Any], paths: dict[str, Path]) -> str:
    client_config = config.get("download_clients", {}).get("qbittorrent", {})
    save_path_env = client_config.get("save_path_env")
    if save_path_env:
        root = Path(required_env(str(save_path_env)))
        subdir = str(client_config.get("save_path_subdir", "")).strip("/")
        return str(root / subdir) if subdir else str(root)
    if client_config.get("save_path"):
        return str(client_config["save_path"])
    return str(paths["incoming"])


def maybe_submit_download_client(
    config: dict[str, Any],
    staging_path: Path | None,
    paths: dict[str, Path],
    dry_run: bool,
) -> tuple[str, str | None]:
    download_clients = config.get("download_clients", {})
    if not download_clients.get("enabled"):
        return "artifact_saved" if not dry_run else "dry_run", None
    if staging_path is None:
        return "client_pending" if not dry_run else "dry_run", None

    preferred = str(download_clients.get("preferred", "qbittorrent"))
    if preferred != "qbittorrent":
        raise SystemExit(f"Unsupported preferred download client: {preferred}")

    queue_id = torrent_info_hash(staging_path) or magnet_info_hash(staging_path)
    if dry_run:
        return "client_dry_run", queue_id

    client_config = download_clients.get("qbittorrent", {})
    client = QbittorrentClient(
        required_env(str(client_config.get("url_env", "QBITTORRENT_URL"))),
        required_env(str(client_config.get("username_env", "QBITTORRENT_USERNAME"))),
        required_env(str(client_config.get("password_env", "QBITTORRENT_PASSWORD"))),
    )
    client.login()
    if queue_id and client.torrent_info(queue_id):
        return "client_queued", queue_id
    client.add_torrent(
        staging_path,
        save_path=client_save_path(config, paths),
        category=str(client_config.get("category", "")).strip() or None,
    )
    return "client_queued", queue_id


def insert_job(
    conn: Any,
    target: dict[str, Any],
    candidate: dict[str, Any],
    status: str,
    staging_path: Path | None,
    client: str | None,
    client_queue_id: str | None,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    conn.execute(
        """
        INSERT INTO download_jobs (
            source, media_type, source_id, title, release_title, release_guid,
            indexer, indexer_id, score, status, staging_path, client, client_queue_id, metadata, updated_at
        )
        VALUES (
            %(source)s, %(media_type)s, %(source_id)s, %(title)s, %(release_title)s,
            %(release_guid)s, %(indexer)s, %(indexer_id)s, %(score)s, %(status)s,
            %(staging_path)s, %(client)s, %(client_queue_id)s, %(metadata)s, now()
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
            "client": client,
            "client_queue_id": client_queue_id,
            "metadata": Jsonb({"candidate": candidate, "target": target}),
        },
    )


def upsert_rss_waitlist(
    conn: Any,
    target: dict[str, Any],
    status: str,
    reason: str,
    metadata: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    conn.execute(
        """
        INSERT INTO rss_waitlist (
            source, media_type, source_id, title, year, status, reason, metadata, updated_at
        )
        VALUES (
            %(source)s, %(media_type)s, %(source_id)s, %(title)s, %(year)s,
            %(status)s, %(reason)s, %(metadata)s, now()
        )
        ON CONFLICT (source, media_type, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            year = EXCLUDED.year,
            status = EXCLUDED.status,
            reason = EXCLUDED.reason,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "source": target["source"],
            "media_type": target["media_type"],
            "source_id": target["source_id"],
            "title": target.get("title") or "unknown",
            "year": target.get("year"),
            "status": status,
            "reason": reason,
            "metadata": Jsonb(metadata),
        },
    )


def target_has_open_download_job(conn: Any, target: dict[str, Any]) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM download_jobs
        WHERE source = %(source)s
          AND media_type = %(media_type)s
          AND source_id = %(source_id)s
          AND status = ANY(%(statuses)s)
        LIMIT 1
        """,
        {
            "source": target["source"],
            "media_type": target["media_type"],
            "source_id": target["source_id"],
            "statuses": list(OPEN_DOWNLOAD_JOB_STATUSES),
        },
    ).fetchone()
    return row is not None


def candidate_already_recorded(conn: Any, target: dict[str, Any], candidate: dict[str, Any]) -> bool:
    release_guid = str(candidate.get("guid") or "").strip()
    if release_guid:
        row = conn.execute(
            """
            SELECT 1
            FROM download_jobs
            WHERE source = %(source)s
              AND media_type = %(media_type)s
              AND source_id = %(source_id)s
              AND release_guid = %(release_guid)s
              AND status = ANY(%(statuses)s)
            LIMIT 1
            """,
            {
                "source": target["source"],
                "media_type": target["media_type"],
                "source_id": target["source_id"],
                "release_guid": release_guid,
                "statuses": list(OPEN_DOWNLOAD_JOB_STATUSES),
            },
        ).fetchone()
        if row is not None:
            return True

    queue_id = str(candidate.get("queue_id") or "").strip().lower()
    if queue_id:
        row = conn.execute(
            """
            SELECT 1
            FROM download_jobs
            WHERE client_queue_id = %(client_queue_id)s
              AND status = ANY(%(statuses)s)
            LIMIT 1
            """,
            {
                "client_queue_id": queue_id,
                "statuses": list(OPEN_DOWNLOAD_JOB_STATUSES),
            },
        ).fetchone()
        if row is not None:
            return True
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue one Prowlarr candidate into the configured staging workspace.")
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
    paths = staging_paths(config)

    with connect_postgres(required_env(config.get("database", {}).get("dsn_env", "POSTGRES_DSN"))) as conn:
        conn.row_factory = dict_row
        conn.execute(DOWNLOAD_TABLE_SQL)
        conn.execute(RSS_WAITLIST_SQL)
        search_check = dict(config["library_checks"]["missing_language"])
        search_check["limit"] = args.max_targets
        missing = find_missing(conn, search_check)
        if not missing:
            print("No missing-language target found for Prowlarr search.")
            return 0

        target = None
        candidate = None
        skipped = []
        candidate_diagnostics: dict[str, int] | None = None
        for possible_target in missing:
            if target_has_open_download_job(conn, possible_target):
                skipped.append(f"{possible_target.get('title')} ({possible_target.get('year')})")
                continue
            candidates, diagnostics = search_movie_diagnostics(possible_target, config["prowlarr_search"])
            if candidates:
                filtered_candidates = [item for item in candidates if not candidate_already_recorded(conn, possible_target, item)]
            else:
                filtered_candidates = []
            if filtered_candidates:
                target = possible_target
                candidate = filtered_candidates[0]
                candidate_diagnostics = diagnostics
                break
            skipped.append(f"{possible_target.get('title')} ({possible_target.get('year')})")
            if diagnostics["peer_rejected"] > 0:
                upsert_rss_waitlist(
                    conn,
                    possible_target,
                    "waiting_for_rss",
                    "all matching torrent releases were below the configured minimum peers",
                    diagnostics,
                    dry_run,
                )
            else:
                upsert_rss_waitlist(
                    conn,
                    possible_target,
                    "waiting_for_rss",
                    "no acceptable Prowlarr candidate found yet",
                    diagnostics,
                    dry_run,
                )

        if target is None or candidate is None:
            print(f"No Prowlarr candidates found for first {len(missing)} missing-language targets.")
            for skipped_target in skipped[:20]:
                print(f"skipped_no_candidate: {skipped_target}")
            if not dry_run:
                conn.commit()
            return 2

        check_space(config, candidate, paths)
        ensure_workspace(paths, args.apply)
        staging_path = save_artifact(candidate, paths, config) if args.apply else None
        status, client_queue_id = maybe_submit_download_client(config, staging_path, paths, dry_run)
        client_name = None
        if config.get("download_clients", {}).get("enabled"):
            client_name = str(config.get("download_clients", {}).get("preferred", "qbittorrent"))
        insert_job(conn, target, candidate, status, staging_path, client_name, client_queue_id, dry_run)
        if args.apply:
            conn.commit()

    print("Prowlarr download candidate")
    print(f"dry_run: {dry_run}")
    print(f"targets_tried: {len(skipped) + 1}")
    for skipped_target in skipped[:10]:
        print(f"skipped_no_candidate: {skipped_target}")
    if candidate_diagnostics is not None:
        print(f"peer_rejected_before_match: {candidate_diagnostics['peer_rejected']}")
    print(f"target: {target.get('title')} ({target.get('year')})")
    print(f"release: {candidate['title']}")
    print(f"score: {candidate['score']}")
    print(f"indexer: {candidate.get('indexer')}")
    print(f"size_gb: {candidate.get('size_gb')}")
    print(f"peers: {candidate.get('peers')}")
    print(f"staging_root: {paths['root']}")
    print(f"staging_free_gb: {free_gb(paths['root']):.1f}")
    print(f"staging_path: {staging_path or 'not-written'}")
    print(f"download_client: {client_name or 'disabled'}")
    print(f"job_status: {status}")
    print(f"client_queue_id: {client_queue_id or 'not-set'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
