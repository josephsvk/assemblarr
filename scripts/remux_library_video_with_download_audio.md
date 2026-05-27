# remux_library_video_with_download_audio.py

## Purpose

Takes the current Radarr library movie file as the video master and muxes in extracted CZ/SK audio tracks from a completed Assemblarr download.

## Important Factors

- It targets Radarr movie-file jobs that already have extracted audio tracks.
- It prefers both CZ and SK when both are available.
- It keeps the original library video and, by default, keeps the original library audio too.
- It writes a temporary remux next to the library file, verifies it, then replaces the library file in place.
- After a successful replace it asks Radarr to rescan and rename the movie.
- It archives the extracted audio tracks into the Assemblarr archive workspace without deleting the originals.
- It verifies the final library file directly with `ffprobe`.
- It can synchronize the extracted CZ/SK audio against the existing library movie audio before muxing it back.
- It can generate extra AAC stereo compatibility tracks for the preferred CZ/SK audio, while still keeping the original higher-quality audio streams.
- If the library file already contains the preferred CZ/SK audio, a repeated `--apply` run switches into a recovery path and records the audit without remuxing the movie again.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `RADARR_URL`
  - `RADARR_API_KEY`
- `config.yml`
  - `postprocess_import`
  - `download_scan`
- Postgres rows from:
  - `download_jobs`
  - `download_audio_extractions`
  - `media_files`
  - `media_items`

## Outputs

- In-place replacement of the Radarr library movie file when `--apply` is used.
- Archived copies of the selected extracted audio files in the download workspace archive.
- Optional row in `library_audio_remux_jobs`.
- Updated `download_jobs.metadata.library_audio_remux`.

## Important Config

- `postprocess_import.library_audio_remux.sync_audio.enabled`
  - Enables the timing-fix stage before the final remux.
- `postprocess_import.library_audio_remux.sync_audio.reference_audio_stream_index`
  - Optional explicit library audio stream index to use as the sync reference.
  - When empty, the script prefers the default library audio stream and otherwise uses the first one.
- `postprocess_import.library_audio_remux.sync_audio.synced_codec`
  - Codec for the synchronized high-quality dubbing master before muxing.
- `postprocess_import.library_audio_remux.sync_audio.synced_bitrate_2ch`
  - Bitrate used when the synchronized dubbing track is stereo.
- `postprocess_import.library_audio_remux.sync_audio.synced_bitrate_multichannel`
  - Bitrate used when the synchronized dubbing track has more than 2 channels.
- `postprocess_import.library_audio_remux.sync_audio.duration_tolerance_seconds`
  - Maximum allowed duration drift between the reference audio and the synchronized dubbing output.
- `postprocess_import.library_audio_remux.sync_audio.rate_tolerance`
  - Minimum playback-rate delta required before `atempo` is applied.
- `postprocess_import.library_audio_remux.sync_audio.archive_subdir`
  - Archive subdirectory where synchronized dubbing masters and reference extracts are kept.
- `postprocess_import.library_audio_remux.compat_stereo.enabled`
  - Enables the extra lightweight AAC stereo compatibility track.
- `postprocess_import.library_audio_remux.compat_stereo.codec`
  - Codec used for the compatibility track.
- `postprocess_import.library_audio_remux.compat_stereo.bitrate`
  - Bitrate used for the compatibility track.
- `postprocess_import.library_audio_remux.compat_stereo.channels`
  - Channel count used for the compatibility track, typically `2`.

## Example

```bash
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28 --apply
```
