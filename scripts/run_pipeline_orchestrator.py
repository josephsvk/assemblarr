#!/usr/bin/env python3
"""Run the Assemblarr pipeline as several coordinated background loops."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from run_download_worker import active_torrent_count, build_qbit_client, list_torrents, qbittorrent_category
from sync_library import ROOT, load_env


LOGGER = logging.getLogger("assemblarr.pipeline_orchestrator")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("download_worker"), dict):
        raise SystemExit("Missing config: download_worker")
    if not isinstance(config.get("pipeline_orchestrator"), dict):
        raise SystemExit("Missing config: pipeline_orchestrator")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Assemblarr pipeline orchestrator.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--once", action="store_true", help="Run every enabled branch once and exit.")
    return parser.parse_args()


def orchestrator_config(config: dict[str, Any]) -> dict[str, Any]:
    worker = dict(config.get("download_worker", {}))
    worker_postprocess = dict(worker.get("postprocess", {}))
    remux = dict(worker_postprocess.get("library_audio_remux", {}))
    orchestrator = dict(config.get("pipeline_orchestrator", {}))

    defaults = {
        "missing_search": {
            "enabled": True,
            "interval_seconds": 180,
            "max_targets_per_run": int(worker.get("max_targets_per_queue_run", 50)),
            "max_queue_additions_per_run": int(worker.get("queue_fill_per_cycle", 1)),
        },
        "rss_waitlist": {
            "enabled": True,
            "interval_seconds": 300,
            "max_targets_per_run": 25,
            "max_queue_additions_per_run": 1,
        },
        "download_sync_extract": {
            "enabled": True,
            "interval_seconds": int(worker.get("poll_seconds", 60)),
        },
        "library_audio_remux": {
            "enabled": bool(remux.get("enabled", True)),
            "interval_seconds": 120,
            "max_per_run": int(remux.get("max_per_cycle", 1)),
        },
    }

    merged: dict[str, Any] = {}
    for name, branch_defaults in defaults.items():
        branch_config = dict(orchestrator.get(name, {}))
        merged[name] = {**branch_defaults, **branch_config}
    return merged


def build_queue_command(*, target_source: str, max_targets: int) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts" / "queue_prowlarr_download.py"),
        "--target-source",
        target_source,
        "--max-targets",
        str(max_targets),
        "--apply",
    ]


def build_download_sync_extract_command() -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts" / "run_download_worker.py"),
        "--once",
        "--no-fill-queue",
        "--no-library-audio-remux",
    ]


def build_library_audio_remux_command() -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts" / "remux_library_video_with_download_audio.py"),
        "--apply",
    ]


def run_logged_subprocess(branch_name: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            LOGGER.info("%s: %s", branch_name, line)
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            LOGGER.warning("%s stderr: %s", branch_name, line)
    return result


def queue_slots_available(config: dict[str, Any]) -> tuple[int, int]:
    client, qbittorrent = build_qbit_client(config)
    try:
        category = qbittorrent_category(qbittorrent)
        torrents = list_torrents(client, category)
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
    max_active = int(qbittorrent.get("max_active_torrents", 5))
    active = active_torrent_count(torrents)
    return max(0, max_active - active), active


def run_queue_branch_once(
    config: dict[str, Any],
    queue_lock: threading.Lock,
    *,
    branch_name: str,
    target_source: str,
    max_targets: int,
    max_queue_additions_per_run: int,
) -> None:
    with queue_lock:
        available_slots, active_count = queue_slots_available(config)
        if available_slots <= 0:
            LOGGER.info("%s: queue is full, active torrents already at limit (%s)", branch_name, active_count)
            return
        attempts = min(int(max_queue_additions_per_run), available_slots)
        for index in range(attempts):
            result = run_logged_subprocess(
                f"{branch_name}[{index + 1}]",
                build_queue_command(target_source=target_source, max_targets=int(max_targets)),
            )
            if result.returncode == 0:
                continue
            if result.returncode == 2:
                LOGGER.info("%s: no acceptable candidates available right now", branch_name)
                break
            LOGGER.error("%s: queue command failed with exit code %s", branch_name, result.returncode)
            break


def run_download_sync_extract_once() -> None:
    result = run_logged_subprocess("download_sync_extract", build_download_sync_extract_command())
    if result.returncode != 0:
        LOGGER.error("download_sync_extract: worker exited with code %s", result.returncode)


def run_library_audio_remux_once(max_per_run: int) -> None:
    for index in range(int(max_per_run)):
        result = run_logged_subprocess(f"library_audio_remux[{index + 1}]", build_library_audio_remux_command())
        if result.returncode == 0:
            continue
        combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if "No eligible Radarr movie download job found for library-audio remux." in combined_output:
            LOGGER.info("library_audio_remux: no eligible remux job found")
            break
        LOGGER.error("library_audio_remux: command failed with exit code %s", result.returncode)
        break


def sleep_or_stop(stop_event: threading.Event, seconds: int) -> bool:
    return stop_event.wait(max(1, int(seconds)))


def branch_loop(
    stop_event: threading.Event,
    *,
    branch_name: str,
    interval_seconds: int,
    once: bool,
    callback: Any,
) -> None:
    while not stop_event.is_set():
        try:
            callback()
        except Exception as exc:  # pragma: no cover - defensive loop logging
            LOGGER.exception("%s failed: %s", branch_name, exc)
        if once:
            break
        if sleep_or_stop(stop_event, int(interval_seconds)):
            break


def start_enabled_branches(
    config: dict[str, Any],
    branch_config: dict[str, Any],
    *,
    once: bool,
) -> list[threading.Thread]:
    stop_event = threading.Event()
    queue_lock = threading.Lock()
    threads: list[threading.Thread] = []

    missing_search = dict(branch_config["missing_search"])
    if missing_search.get("enabled", True):
        threads.append(
            threading.Thread(
                target=branch_loop,
                kwargs={
                    "stop_event": stop_event,
                    "branch_name": "missing_search",
                    "interval_seconds": int(missing_search["interval_seconds"]),
                    "once": once,
                    "callback": lambda: run_queue_branch_once(
                        config,
                        queue_lock,
                        branch_name="missing_search",
                        target_source="missing_language",
                        max_targets=int(missing_search["max_targets_per_run"]),
                        max_queue_additions_per_run=int(missing_search["max_queue_additions_per_run"]),
                    ),
                },
                name="missing_search",
                daemon=not once,
            )
        )

    rss_waitlist = dict(branch_config["rss_waitlist"])
    if rss_waitlist.get("enabled", True):
        threads.append(
            threading.Thread(
                target=branch_loop,
                kwargs={
                    "stop_event": stop_event,
                    "branch_name": "rss_waitlist",
                    "interval_seconds": int(rss_waitlist["interval_seconds"]),
                    "once": once,
                    "callback": lambda: run_queue_branch_once(
                        config,
                        queue_lock,
                        branch_name="rss_waitlist",
                        target_source="rss_waitlist",
                        max_targets=int(rss_waitlist["max_targets_per_run"]),
                        max_queue_additions_per_run=int(rss_waitlist["max_queue_additions_per_run"]),
                    ),
                },
                name="rss_waitlist",
                daemon=not once,
            )
        )

    download_sync_extract = dict(branch_config["download_sync_extract"])
    if download_sync_extract.get("enabled", True):
        threads.append(
            threading.Thread(
                target=branch_loop,
                kwargs={
                    "stop_event": stop_event,
                    "branch_name": "download_sync_extract",
                    "interval_seconds": int(download_sync_extract["interval_seconds"]),
                    "once": once,
                    "callback": run_download_sync_extract_once,
                },
                name="download_sync_extract",
                daemon=not once,
            )
        )

    library_audio_remux = dict(branch_config["library_audio_remux"])
    if library_audio_remux.get("enabled", True):
        threads.append(
            threading.Thread(
                target=branch_loop,
                kwargs={
                    "stop_event": stop_event,
                    "branch_name": "library_audio_remux",
                    "interval_seconds": int(library_audio_remux["interval_seconds"]),
                    "once": once,
                    "callback": lambda: run_library_audio_remux_once(int(library_audio_remux["max_per_run"])),
                },
                name="library_audio_remux",
                daemon=not once,
            )
        )

    for thread in threads:
        thread.start()
    return threads


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env(args.env_file)
    config = load_config(args.config)
    branch_config = orchestrator_config(config)
    threads = start_enabled_branches(config, branch_config, once=args.once)
    if not threads:
        LOGGER.warning("no enabled orchestrator branches configured")
        return 0
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        LOGGER.warning("received interrupt, waiting for branches to finish current iteration")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
