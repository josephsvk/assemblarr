#!/usr/bin/env python3
"""Create and inspect the Assemblarr managed library workspace."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from sync_library import load_env


ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("assemblarr_library"), dict):
        raise SystemExit("Missing config: assemblarr_library")
    return config


def workspace_paths(config: dict[str, Any]) -> dict[str, Path]:
    library = config["assemblarr_library"]
    folders = library.get("folders", {})
    root_env = library.get("root_env")
    root = Path(os.getenv(str(root_env), str(library["root"])) if root_env else str(library["root"]))
    return {
        "root": root,
        "incoming": root / str(folders.get("incoming", "incoming")),
        "processing": root / str(folders.get("processing", "processing")),
        "library": root / str(folders.get("library", "library")),
        "archive": root / str(folders.get("archive", "archive")),
        "failed": root / str(folders.get("failed", "failed")),
    }


def free_gb(path: Path) -> float:
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    return shutil.disk_usage(existing).free / 1024 / 1024 / 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and inspect the Assemblarr managed library workspace.")
    parser.add_argument("--config", default=ROOT / "config.yml", type=Path)
    parser.add_argument("--env-file", default=ROOT / ".env", type=Path)
    parser.add_argument("--apply", action="store_true", help="Create workspace directories.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    config = load_config(args.config)
    paths = workspace_paths(config)

    if args.apply:
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)

    print("Assemblarr library workspace")
    print(f"dry_run: {not args.apply}")
    print(f"min_free_gb: {config['assemblarr_library'].get('min_free_gb')}")
    for name, path in paths.items():
        print(f"{name}: {path} exists={path.exists()}")
    print(f"available_gb: {free_gb(paths['root']):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
