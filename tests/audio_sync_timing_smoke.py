#!/usr/bin/env python3
"""Smoke-test library audio mux timestamp offset handling with synthetic media."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import remux_library_video_with_download_audio as remux  # noqa: E402


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def stream_start_times(path: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,start_time",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return [float(stream.get("start_time") or 0.0) for stream in data["streams"]]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="assemblarr-audio-sync-") as tmp:
        tmp_path = Path(tmp)
        library_path = tmp_path / "library.mkv"
        source_audio_path = tmp_path / "source.mka"
        output_path = tmp_path / "out.mkv"

        run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x64:rate=25:duration=2",
                "-itsoffset",
                "0.125",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=2",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                str(library_path),
            ]
        )
        run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1200:duration=2",
                "-c:a",
                "aac",
                str(source_audio_path),
            ]
        )

        offset = remux.reference_audio_video_offset(library_path, 0)
        if offset <= 0.05:
            raise AssertionError(f"Expected positive reference audio offset, got {offset}")

        config = {
            "postprocess_import": {
                "library_audio_remux": {
                    "include_original_audio": True,
                    "set_preferred_audio_default": True,
                    "compat_stereo": {"enabled": False, "codec": "aac", "channels": 2, "bitrate": "192k"},
                }
            }
        }
        command, _plan = remux.build_ffmpeg_command(
            library_path,
            [
                {
                    "language": "cz",
                    "output_path": str(source_audio_path),
                    "prepared_output_path": str(source_audio_path),
                    "mux_input_offset_seconds": offset,
                }
            ],
            [],
            output_path,
            config,
        )
        run(command)

        start_times = stream_start_times(output_path)
        reference_audio_start = start_times[1]
        muxed_audio_start = start_times[2]
        if abs(reference_audio_start - muxed_audio_start) > 0.001:
            raise AssertionError(
                f"Muxed audio start {muxed_audio_start:.6f}s does not match reference {reference_audio_start:.6f}s"
            )

        print(f"reference_audio_video_offset_seconds: {offset:.6f}")
        print(f"reference_audio_start_seconds: {reference_audio_start:.6f}")
        print(f"muxed_audio_start_seconds: {muxed_audio_start:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
