#!/usr/bin/env python3
"""Remux a completed Radarr movie download and import the result back into Radarr."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apply_language_tag import ensure_arr_tag, tag_for_path
from extract_download_audio import detect_language, ffprobe_streams
from queue_prowlarr_download import workspace_paths
from sync_library import ROOT, connect_postgres, ensure_schema, load_env, required_env


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("postprocess_import"), dict):
        raise SystemExit("Missing config: postprocess_import")
    if not isinstance(config.get("download_scan"), dict):
        raise SystemExit("Missing config: download_scan")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remux a completed Radarr movie and import it back into Radarr.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--download-job-id", type=int, help="Specific download_jobs.id to process.")
    parser.add_argument("--apply", action="store_true", help="Run ffmpeg and perform the Radarr import.")
    return parser.parse_args()


def arr_request(base_url: str, api_key: str, method: str, path: str, payload: Any | None = None) -> Any:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

    return json.loads(response_body) if response_body else None


def radarr_client(config: dict[str, Any]) -> tuple[str, str]:
    radarr = dict(config["postprocess_import"].get("radarr", {}))
    if not radarr.get("enabled", True):
        raise SystemExit("postprocess_import.radarr.enabled is false")
    return required_env(str(radarr.get("base_url_env", "RADARR_URL"))), required_env(
        str(radarr.get("api_key_env", "RADARR_API_KEY"))
    )


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip().rstrip(".")
    return cleaned or "assemblarr-remux"


def preferred_languages(config: dict[str, Any]) -> list[str]:
    raw = config["postprocess_import"].get("preferred_audio_languages", ["cz", "sk"])
    return [str(item).lower() for item in raw]


def audio_language_config(config: dict[str, Any]) -> dict[str, list[str]]:
    scan_config = config["download_scan"]
    return {
        str(language): [str(token).lower() for token in settings.get("tokens", [])]
        for language, settings in dict(scan_config.get("audio_languages", {})).items()
    }


def detect_languages_in_text(text: str, config: dict[str, Any]) -> dict[str, list[str]]:
    normalized = text.lower()
    detected: dict[str, list[str]] = {}
    for language, tokens in audio_language_config(config).items():
        matches = sorted({token for token in tokens if token and token in normalized})
        if matches:
            detected[language] = matches
    return detected


def detect_languages_from_audio_metadata(movie_file: dict[str, Any], config: dict[str, Any]) -> dict[str, list[str]]:
    mediainfo = dict(movie_file.get("mediainfo") or {})
    raw = dict(movie_file.get("raw") or {})
    values = [
        str(mediainfo.get("audioLanguages") or ""),
        str(mediainfo.get("audioCodec") or ""),
        json.dumps(raw.get("languages") or []),
    ]
    return detect_languages_in_text(" ".join(values), config)


def verify_library_file_audio(library_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    streams = ffprobe_streams(library_path)
    audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]
    matched_languages: dict[str, list[int]] = {}
    for stream in audio_streams:
        language, _matches = detect_language(stream, audio_language_config(config))
        if language is None:
            continue
        matched_languages.setdefault(language, []).append(int(stream.get("index")))
    preferred = preferred_languages(config)
    preferred_hits = [language for language in preferred if language in matched_languages]
    return {
        "audio_stream_count": len(audio_streams),
        "matched_languages": matched_languages,
        "preferred_hits": preferred_hits,
        "verified": bool(preferred_hits),
    }


def remux_root(config: dict[str, Any], queue_id: str) -> Path:
    workspace = workspace_paths(config)
    processing_root = workspace["processing"]
    remux = dict(config["postprocess_import"].get("remux", {}))
    subdir = str(remux.get("output_subdir", "remux_imports")).strip("/")
    return processing_root / subdir / queue_id


def choose_job(conn: Any, requested_id: int | None) -> dict[str, Any]:
    clauses = [
        "dj.source = 'radarr'",
        "dj.media_type = 'movie_file'",
        "COALESCE(das_by_queue.scan_status, das_by_job.scan_status) = 'scanned'",
        "COALESCE(das_by_queue.primary_video_path, das_by_job.primary_video_path) IS NOT NULL",
    ]
    params: dict[str, Any] = {}
    if requested_id is not None:
        clauses.append("dj.id = %(download_job_id)s")
        params["download_job_id"] = requested_id
    else:
        clauses.append("dj.status IN ('audio_extracted', 'scanned')")
        clauses.append("dmi.id IS NULL")

    row = conn.execute(
        f"""
        SELECT
            dj.id,
            dj.source,
            dj.media_type,
            dj.source_id,
            dj.status,
            dj.title,
            dj.release_title,
            dj.client_queue_id,
            dj.content_path,
            dj.metadata,
            COALESCE(
                (dj.metadata->'target'->>'parent_source_id')::int,
                mf.parent_source_id
            ) AS movie_id,
            COALESCE(das_by_queue.primary_video_path, das_by_job.primary_video_path) AS primary_video_path,
            COALESCE(das_by_queue.detected_audio_languages, das_by_job.detected_audio_languages) AS detected_audio_languages,
            COALESCE(dae_by_queue.extracted_tracks, dae_by_job.extracted_tracks) AS extracted_tracks,
            mi.path AS movie_path,
            mi.raw AS movie_raw,
            dmi.id AS existing_import_id,
            dmi.import_status AS existing_import_status,
            dmi.imported_path AS existing_imported_path,
            dmi.metadata AS existing_import_metadata
        FROM download_jobs dj
        LEFT JOIN download_artifact_scans das_by_job
          ON das_by_job.download_job_id = dj.id
        LEFT JOIN download_artifact_scans das_by_queue
          ON das_by_queue.client_queue_id = dj.client_queue_id
        LEFT JOIN download_audio_extractions dae_by_job
          ON dae_by_job.download_job_id = dj.id
        LEFT JOIN download_audio_extractions dae_by_queue
          ON dae_by_queue.client_queue_id = dj.client_queue_id
        LEFT JOIN media_files mf
          ON mf.source = dj.source
         AND mf.media_type = dj.media_type
         AND mf.source_id = dj.source_id
        LEFT JOIN media_items mi
         ON mi.source = dj.source
         AND mi.media_type = 'movie'
         AND mi.source_id = COALESCE(
             (dj.metadata->'target'->>'parent_source_id')::int,
             mf.parent_source_id
         )
        LEFT JOIN download_media_imports dmi
          ON dmi.download_job_id = dj.id
        WHERE {" AND ".join(clauses)}
        ORDER BY dj.updated_at DESC, dj.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        raise SystemExit("No eligible Radarr movie download job found for remux/import.")
    if not row["movie_id"]:
        raise SystemExit("Eligible download job is missing parent Radarr movie id.")
    return dict(row)


def classify_streams(
    streams: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    language_tokens = audio_language_config(config)
    preferred = preferred_languages(config)
    video_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "video"]
    subtitle_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "subtitle"]
    audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]

    ranked: list[tuple[int, int, dict[str, Any]]] = []
    fallback: list[dict[str, Any]] = []
    for position, stream in enumerate(audio_streams):
        language, _matches = detect_language(stream, language_tokens)
        if language in preferred:
            ranked.append((preferred.index(language), position, stream))
        else:
            fallback.append(stream)

    ranked.sort(key=lambda item: (item[0], item[1]))
    prioritized_audio = [stream for _, _, stream in ranked] + fallback
    return video_streams, prioritized_audio, subtitle_streams, len(ranked)


def remux_command(
    source_video: Path,
    destination_video: Path,
    config: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    streams = ffprobe_streams(source_video)
    video_streams, audio_streams, subtitle_streams, preferred_match_count = classify_streams(streams, config)
    if not video_streams:
        raise SystemExit(f"No video streams found in: {source_video}")
    if not audio_streams:
        raise SystemExit(f"No audio streams found in: {source_video}")
    if preferred_match_count < 1:
        raise SystemExit(f"No configured preferred audio stream found in: {source_video}")

    include_subtitles = bool(dict(config["postprocess_import"].get("remux", {})).get("include_subtitles", True))
    selected_streams = video_streams + audio_streams + (subtitle_streams if include_subtitles else [])

    command = ["ffmpeg", "-y", "-i", str(source_video)]
    for stream in selected_streams:
        command.extend(["-map", f"0:{int(stream['index'])}"])
    command.extend(["-c", "copy"])

    audio_count = len(audio_streams)
    for output_audio_index in range(audio_count):
        disposition = "default" if output_audio_index == 0 else "0"
        command.extend([f"-disposition:a:{output_audio_index}", disposition])

    command.append(str(destination_video))
    plan = {
        "video_stream_indexes": [int(stream["index"]) for stream in video_streams],
        "audio_stream_indexes": [int(stream["index"]) for stream in audio_streams],
        "subtitle_stream_indexes": [int(stream["index"]) for stream in subtitle_streams] if include_subtitles else [],
        "preferred_audio_languages": preferred_languages(config),
        "preferred_audio_match_count": preferred_match_count,
    }
    return command, plan


def verify_remux_audio(remux_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    streams = ffprobe_streams(remux_path)
    audio_streams = [stream for stream in streams if str(stream.get("codec_type") or "") == "audio"]
    matched_languages: dict[str, list[int]] = {}
    for stream in audio_streams:
        language, _matches = detect_language(stream, audio_language_config(config))
        if language is None:
            continue
        matched_languages.setdefault(language, []).append(int(stream.get("index")))
    preferred = preferred_languages(config)
    preferred_hits = [language for language in preferred if language in matched_languages]
    return {
        "audio_stream_count": len(audio_streams),
        "matched_languages": matched_languages,
        "preferred_hits": preferred_hits,
        "verified": bool(preferred_hits),
    }


def find_manual_import_candidate(
    base_url: str,
    api_key: str,
    remux_path: Path,
    movie_id: int,
) -> dict[str, Any]:
    normalized_target = str(remux_path)
    query_shapes = [
        {
            "folder": str(remux_path.parent),
            "filterExistingFiles": "false",
        },
        {
            "folder": str(remux_path.parent),
            "movieId": movie_id,
            "filterExistingFiles": "false",
        },
    ]

    last_seen_paths: list[str] = []
    for query in query_shapes:
        params = urllib.parse.urlencode(query)
        candidates = arr_request(base_url, api_key, "GET", f"/api/v3/manualimport?{params}")
        if not isinstance(candidates, list):
            raise RuntimeError("Radarr manual import lookup returned unexpected payload")

        for candidate in candidates:
            candidate_path = str(candidate.get("path") or "")
            if candidate_path:
                last_seen_paths.append(candidate_path)
            if candidate_path != normalized_target:
                continue
            candidate["movieId"] = movie_id
            movie = candidate.get("movie")
            if isinstance(movie, dict) and not movie.get("id"):
                movie["id"] = movie_id
            return candidate

    seen = ", ".join(sorted(set(last_seen_paths))) if last_seen_paths else "no candidates"
    raise RuntimeError(
        f"Radarr manual import did not return a candidate for: {normalized_target}. Returned: {seen}"
    )


def upsert_import_record(
    conn: Any,
    *,
    job: dict[str, Any],
    remux_path: Path,
    imported_path: str | None,
    import_status: str,
    metadata: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    conn.execute(
        """
        INSERT INTO download_media_imports (
            download_job_id, source, source_id, client_queue_id, remux_path,
            import_folder, imported_path, import_status, metadata, updated_at
        )
        VALUES (
            %(download_job_id)s, %(source)s, %(source_id)s, %(client_queue_id)s, %(remux_path)s,
            %(import_folder)s, %(imported_path)s, %(import_status)s, %(metadata)s, now()
        )
        ON CONFLICT (download_job_id)
        DO UPDATE SET
            remux_path = EXCLUDED.remux_path,
            import_folder = EXCLUDED.import_folder,
            imported_path = EXCLUDED.imported_path,
            import_status = EXCLUDED.import_status,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "download_job_id": job["id"],
            "source": job["source"],
            "source_id": job["source_id"],
            "client_queue_id": job.get("client_queue_id"),
            "remux_path": str(remux_path),
            "import_folder": str(remux_path.parent),
            "imported_path": imported_path,
            "import_status": import_status,
            "metadata": Jsonb(metadata),
        },
    )


def update_job_status(conn: Any, job_id: int, status: str, metadata: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    conn.execute(
        """
        UPDATE download_jobs
        SET status = %(status)s,
            metadata = metadata || %(metadata)s::jsonb,
            updated_at = now()
        WHERE id = %(id)s
        """,
        {"id": job_id, "status": status, "metadata": Jsonb(metadata)},
    )


def sync_local_radarr_state(config: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    if not bool(dict(config["postprocess_import"].get("radarr", {})).get("sync_after_import", True)):
        return
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_library.py"), "--source", "radarr"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def post_radarr_command(base_url: str, api_key: str, name: str, movie_id: int) -> dict[str, Any]:
    if name == "RenameMovie":
        attempts = [
            {"name": name, "movieIds": [movie_id]},
        ]
    else:
        attempts = [
            {"name": name, "movieId": movie_id},
            {"name": name, "movieIds": [movie_id]},
        ]
    last_error: Exception | None = None
    for payload in attempts:
        try:
            response = arr_request(base_url, api_key, "POST", "/api/v3/command", payload)
        except RuntimeError as exc:
            last_error = exc
            continue
        if isinstance(response, dict):
            return response
    raise RuntimeError(f"Radarr command {name} failed for movie {movie_id}: {last_error}")


def wait_for_radarr_command(base_url: str, api_key: str, command_id: int, *, timeout_seconds: int = 300) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = arr_request(base_url, api_key, "GET", f"/api/v3/command/{command_id}")
        if not isinstance(status, dict):
            raise RuntimeError(f"Radarr command {command_id} returned unexpected payload")
        state = str(status.get("status") or "").lower()
        result = str(status.get("result") or "").lower()
        if state == "completed" or result == "success":
            return status
        if state in {"failed", "aborted"} or result in {"failed", "aborted"}:
            raise RuntimeError(f"Radarr command {command_id} failed: {status}")
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for Radarr command {command_id}")


def managed_language_labels(config: dict[str, Any]) -> set[str]:
    tagging = dict(config.get("tagging", {}))
    labels = {str(tagging.get("missing_language_tag", {}).get("label", "nodab"))}
    detected = [str(settings.get("label") or name) for name, settings in dict(tagging.get("detected_language_tags", {})).items()]
    unique = sorted({label for label in detected if label})
    labels.update(unique)
    if unique:
        joined = "".join(unique)
        labels.add(joined)
    return {label for label in labels if label}


def choose_verified_language_tag(import_verification: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    tagging = dict(config.get("tagging", {}))
    detected_config = dict(tagging.get("detected_language_tags", {}))
    preferred_hits = [str(language) for language in import_verification.get("preferred_hits") or []]
    labels: list[str] = []
    for language in preferred_hits:
        settings = dict(detected_config.get(language, {}))
        label = str(settings.get("label") or language)
        if label:
            labels.append(label)
    if labels:
        return "".join(sorted(set(labels))), "verified preferred CZ/SK audio found in Radarr media info"

    missing = dict(tagging.get("missing_language_tag", {}))
    return (
        str(missing.get("label", "nodab")),
        str(missing.get("reason", "no configured CZ/SK dubbing found")),
    )


def replace_radarr_language_tags(
    base_url: str,
    api_key: str,
    movie_id: int,
    final_path: str,
    import_verification: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    desired_label, desired_reason = choose_verified_language_tag(import_verification, config)
    movie = arr_request(base_url, api_key, "GET", f"/api/v3/movie/{movie_id}")
    all_tags = arr_request(base_url, api_key, "GET", "/api/v3/tag")
    if not isinstance(movie, dict) or not isinstance(all_tags, list):
        raise RuntimeError("Unexpected Radarr payload while replacing language tags")

    label_by_id = {int(tag["id"]): str(tag.get("label") or "") for tag in all_tags if tag.get("id") is not None}
    managed_labels = managed_language_labels(config)
    desired_tag_id = ensure_arr_tag(dict(config["tagging"]["arr"]["radarr"]), desired_label, dry_run=False)
    desired_tag_id = int(desired_tag_id) if desired_tag_id is not None else None
    current_tags = [int(tag_id) for tag_id in list(movie.get("tags") or [])]
    kept_tags = [tag_id for tag_id in current_tags if label_by_id.get(tag_id) not in managed_labels]
    if desired_tag_id is not None:
        kept_tags.append(desired_tag_id)
    movie["tags"] = sorted(set(kept_tags))
    arr_request(base_url, api_key, "PUT", f"/api/v3/movie/{movie_id}", movie)
    return {
        "tag_label": desired_label,
        "tag_reason": desired_reason,
        "arr_tag_id": desired_tag_id,
    }


def update_local_language_tag(conn: Any, movie_id: int, final_path: str, tag_result: dict[str, Any], config: dict[str, Any]) -> None:
    managed_labels = tuple(sorted(managed_language_labels(config)))
    conn.execute(
        """
        DELETE FROM media_item_tags
        WHERE source = 'radarr'
          AND media_type = 'movie'
          AND source_id = %(movie_id)s
          AND tag_label = ANY(%(managed_labels)s)
        """,
        {"movie_id": movie_id, "managed_labels": list(managed_labels)},
    )
    conn.execute(
        """
        INSERT INTO media_item_tags (
            source, media_type, source_id, tag_label, tag_reason, arr_tag_id, metadata, updated_at
        )
        VALUES (
            'radarr', 'movie', %(movie_id)s, %(tag_label)s, %(tag_reason)s, %(arr_tag_id)s, %(metadata)s, now()
        )
        ON CONFLICT (source, media_type, source_id, tag_label)
        DO UPDATE SET
            tag_reason = EXCLUDED.tag_reason,
            arr_tag_id = EXCLUDED.arr_tag_id,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        {
            "movie_id": movie_id,
            "tag_label": tag_result["tag_label"],
            "tag_reason": tag_result["tag_reason"],
            "arr_tag_id": tag_result["arr_tag_id"],
            "metadata": Jsonb({"path": final_path, "managed_by": "remux_and_import_radarr_movie"}),
        },
    )


def latest_movie_file_path(conn: Any, movie_id: int) -> str:
    row = latest_movie_file_row(conn, movie_id)
    return str(row["path"])


def latest_movie_file_row(conn: Any, movie_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT source_id, path, languages, mediainfo, raw
        FROM media_files
        WHERE source = 'radarr'
          AND media_type = 'movie_file'
          AND parent_source_id = %(movie_id)s
          AND path IS NOT NULL
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        {"movie_id": movie_id},
    ).fetchone()
    if not row or not row["path"]:
        raise RuntimeError(f"No Radarr movie_file path found after import for movie {movie_id}")
    return dict(row)


def verify_radarr_import_applied(job: dict[str, Any], movie_file: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    original_path = str((dict(job.get("metadata") or {}).get("target") or {}).get("path") or "")
    current_path = str(movie_file["path"])
    source_id = int(movie_file.get("source_id") or 0)
    original_source_id = int(job.get("source_id") or 0)
    metadata_detected = detect_languages_from_audio_metadata(movie_file, config)
    ffprobe_detected = verify_library_file_audio(Path(current_path), config) if Path(current_path).exists() else {
        "audio_stream_count": 0,
        "matched_languages": {},
        "preferred_hits": [],
        "verified": False,
    }
    preferred = preferred_languages(config)
    preferred_hits = [language for language in preferred if language in ffprobe_detected["matched_languages"]]
    path_changed = bool(original_path) and current_path != original_path
    file_id_changed = source_id > 0 and source_id != original_source_id
    verified = bool(preferred_hits) and (path_changed or file_id_changed)
    return {
        "verified": verified,
        "current_path": current_path,
        "original_path": original_path,
        "current_movie_file_id": source_id,
        "original_movie_file_id": original_source_id,
        "path_changed": path_changed,
        "movie_file_id_changed": file_id_changed,
        "metadata_detected_languages": metadata_detected,
        "ffprobe_verification": ffprobe_detected,
        "detected_languages": ffprobe_detected["matched_languages"],
        "preferred_hits": preferred_hits,
    }


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    load_env(args.env_file)
    config = load_config(args.config)
    base_url, api_key = radarr_client(config)

    with connect_postgres(required_env(str(config.get("database", {}).get("dsn_env", "POSTGRES_DSN")))) as conn:
        conn.row_factory = dict_row
        ensure_schema(conn)

        job = choose_job(conn, args.download_job_id)
        source_video = Path(str(job["primary_video_path"]))
        if not source_video.exists():
            raise SystemExit(f"Primary video path does not exist: {source_video}")

        queue_id = str(job.get("client_queue_id") or f"job-{job['id']}")
        output_ext = str(dict(config["postprocess_import"].get("remux", {})).get("output_extension", ".mkv"))
        output_dir = remux_root(config, queue_id)
        output_name = sanitize_filename(f"{job['title']} ({dict(job.get('movie_raw') or {}).get('year') or ''}) {job['release_title']}") + output_ext
        remux_path = output_dir / output_name
        command, plan = remux_command(source_video, remux_path, config)

        import_candidate = None
        import_rejections: list[str] = []
        imported_path = None
        post_import_commands: list[dict[str, Any]] = []
        final_tag: dict[str, Any] | None = None
        remux_verification: dict[str, Any] | None = None
        import_verification: dict[str, Any] | None = None
        finalize_only = str(job["status"]) == "imported"

        if not dry_run and not finalize_only:
            output_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(command, check=True, capture_output=True, text=True)
            remux_verification = verify_remux_audio(remux_path, config)
            if not remux_verification["verified"]:
                upsert_import_record(
                    conn,
                    job=job,
                    remux_path=remux_path,
                    imported_path=None,
                    import_status="remux_verification_failed",
                    metadata={"remux_plan": plan, "remux_verification": remux_verification},
                    dry_run=dry_run,
                )
                update_job_status(
                    conn,
                    int(job["id"]),
                    "remux_verification_failed",
                    {"import": {"remux_path": str(remux_path), "remux_verification": remux_verification}},
                    dry_run=dry_run,
                )
                conn.commit()
                raise SystemExit("Remux verification failed: no preferred CZ/SK audio found in remux output.")

        if finalize_only:
            if args.apply:
                for command_name in ("RescanMovie", "RenameMovie"):
                    command_response = post_radarr_command(base_url, api_key, command_name, int(job["movie_id"]))
                    command_id = int(command_response["id"])
                    command_status = wait_for_radarr_command(base_url, api_key, command_id)
                    post_import_commands.append(command_status)
                sync_local_radarr_state(config, dry_run)
                latest_movie_file = latest_movie_file_row(conn, int(job["movie_id"]))
                imported_path = str(latest_movie_file["path"])
                import_verification = verify_radarr_import_applied(job, latest_movie_file, config)
                if import_verification["verified"]:
                    final_tag = replace_radarr_language_tags(
                        base_url,
                        api_key,
                        int(job["movie_id"]),
                        imported_path,
                        import_verification,
                        config,
                    )
                    update_local_language_tag(conn, int(job["movie_id"]), imported_path, final_tag, config)
                upsert_import_record(
                    conn,
                    job=job,
                    remux_path=remux_path,
                    imported_path=imported_path,
                    import_status="imported" if import_verification["verified"] else "import_verification_failed",
                    metadata={
                        "recovered_finalize_only": True,
                        "post_import_commands": post_import_commands,
                        "final_tag": final_tag,
                        "import_verification": import_verification,
                    },
                    dry_run=dry_run,
                )
                update_job_status(
                    conn,
                    int(job["id"]),
                    "imported" if import_verification["verified"] else "import_verification_failed",
                    {
                        "import": {
                            "remux_path": str(remux_path),
                            "manual_imported": True,
                            "imported_path": imported_path,
                            "final_tag": final_tag,
                            "recovered_finalize_only": True,
                            "import_verification": import_verification,
                        }
                    },
                    dry_run=dry_run,
                )
                conn.commit()
            preview_note = "job was already imported; finalize-only recovery path used"
        elif dry_run and not remux_path.exists():
            preview_note = "remux output does not exist yet; Radarr manual import will be checked only after --apply"
        else:
            import_candidate = find_manual_import_candidate(base_url, api_key, remux_path, int(job["movie_id"]))
            import_rejections = [str(item.get("reason") or item) for item in (import_candidate.get("rejections") or [])]
            if args.apply:
                if import_rejections:
                    raise SystemExit("Radarr manual import candidate has rejections: " + "; ".join(import_rejections))
                imported = arr_request(base_url, api_key, "POST", "/api/v3/manualimport", [import_candidate])
                imported_path = str(import_candidate.get("path") or remux_path)
                for command_name in ("RescanMovie", "RenameMovie"):
                    command_response = post_radarr_command(base_url, api_key, command_name, int(job["movie_id"]))
                    command_id = int(command_response["id"])
                    command_status = wait_for_radarr_command(base_url, api_key, command_id)
                    post_import_commands.append(command_status)
                sync_local_radarr_state(config, dry_run)
                latest_movie_file = latest_movie_file_row(conn, int(job["movie_id"]))
                imported_path = str(latest_movie_file["path"])
                import_verification = verify_radarr_import_applied(job, latest_movie_file, config)
                if import_verification["verified"]:
                    final_tag = replace_radarr_language_tags(
                        base_url,
                        api_key,
                        int(job["movie_id"]),
                        imported_path,
                        import_verification,
                        config,
                    )
                    update_local_language_tag(conn, int(job["movie_id"]), imported_path, final_tag, config)
                upsert_import_record(
                    conn,
                    job=job,
                    remux_path=remux_path,
                    imported_path=imported_path,
                    import_status="imported" if import_verification["verified"] else "import_verification_failed",
                    metadata={
                        "manual_import_candidate": import_candidate,
                        "manual_import_response": imported,
                        "remux_plan": plan,
                        "remux_verification": remux_verification,
                        "post_import_commands": post_import_commands,
                        "final_tag": final_tag,
                        "import_verification": import_verification,
                    },
                    dry_run=dry_run,
                )
                update_job_status(
                    conn,
                    int(job["id"]),
                    "imported" if import_verification["verified"] else "import_verification_failed",
                    {
                        "import": {
                            "remux_path": str(remux_path),
                            "manual_imported": True,
                            "imported_path": imported_path,
                            "final_tag": final_tag,
                            "remux_verification": remux_verification,
                            "import_verification": import_verification,
                        }
                    },
                    dry_run=dry_run,
                )
                conn.commit()
            preview_note = None

        print("Radarr movie remux/import")
        print(f"dry_run: {dry_run}")
        print(f"download_job_id: {job['id']}")
        print(f"title: {job['title']}")
        print(f"movie_id: {job['movie_id']}")
        print(f"movie_file_id: {job['source_id']}")
        print(f"source_video: {source_video}")
        print(f"remux_path: {remux_path}")
        print(f"movie_library_path: {job.get('movie_path')}")
        print(f"selected_audio_stream_indexes: {plan['audio_stream_indexes']}")
        print(f"selected_subtitle_stream_indexes: {plan['subtitle_stream_indexes']}")
        if import_candidate is not None:
            print(f"manual_import_candidate_found: True")
            print(f"manual_import_rejection_count: {len(import_rejections)}")
            for reason in import_rejections:
                print(f"manual_import_rejection: {reason}")
        else:
            print("manual_import_candidate_found: False")
        if imported_path:
            print(f"imported_path: {imported_path}")
        if final_tag:
            print(f"final_tag_label: {final_tag['tag_label']}")
        if remux_verification is not None:
            print(f"remux_verification_preferred_hits: {remux_verification['preferred_hits']}")
        if import_verification is not None:
            print(f"import_verification_preferred_hits: {import_verification['preferred_hits']}")
            print(f"import_verification_path_changed: {import_verification['path_changed']}")
            print(f"import_verification_movie_file_id_changed: {import_verification['movie_file_id_changed']}")
            print(f"import_verification_verified: {import_verification['verified']}")
        if preview_note:
            print(f"note: {preview_note}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
