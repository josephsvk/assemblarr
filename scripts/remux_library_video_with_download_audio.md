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
- Synchronized audio is muxed back with the same audio-vs-video timestamp offset as the selected reference library audio stream.
- It can generate extra AAC stereo compatibility tracks for the preferred CZ/SK audio, while still keeping the original higher-quality audio streams.
- Injected Assemblarr tracks are labeled as possibly incorrect or incomplete, because the current priority is throughput over claiming final-perfect sync.
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
  - The selected reference stream also supplies the final mux input offset for synchronized external audio.
- `postprocess_import.library_audio_remux.track_title_suffix`
  - Appended to injected CZ/SK track titles to flag them as provisional.
- `postprocess_import.library_audio_remux.sync_audio.strategy.method`
  - Named sync method. `adaptive_anchor_sync` uses a sparse single-anchor profile for near-equal durations and a denser multi-anchor profile when the durations diverge more.
  - `center_anchored_trimmed_edges` is the current experimental mode for releases that may be shortened at the start and end. It probes the middle and 3/4 points first; when both probes align, the shorter dubbing is centered into the reference duration with padding.
- `postprocess_import.library_audio_remux.sync_audio.strategy.single_anchor_max_duration_diff_seconds`
  - Threshold for deciding whether the current audio pair is close enough for the `single_anchor` profile.
- `postprocess_import.library_audio_remux.sync_audio.strategy.prefer_silence_windows`
  - Prepared switch for future silence-aware anchor selection. The current code records the intent but does not yet scan silence windows.
- `postprocess_import.library_audio_remux.sync_audio.synced_codec`
  - Codec for the synchronized high-quality dubbing master before muxing.
- `postprocess_import.library_audio_remux.sync_audio.synced_bitrate_2ch`
  - Bitrate used when the synchronized dubbing track is stereo.
- `postprocess_import.library_audio_remux.sync_audio.synced_bitrate_multichannel`
  - Bitrate used when the synchronized dubbing track has more than 2 channels.
- `postprocess_import.library_audio_remux.sync_audio.preflight_duration_tolerance_seconds`
  - Early length check between the extracted dubbing and the current library reference before the expensive sync pass.
  - The current production-oriented setup uses a conservative small window so only near-matching releases proceed to real sync; the rest should be retried with another release.
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
- `postprocess_import.library_audio_remux.compat_stereo.prefer_default`
  - Makes the playback-friendlier AAC stereo compatibility track the default output audio stream.

## Retry Behavior

- `queue_prowlarr_download.py` now skips already attempted releases for the same title, so the next queue run is pushed toward another release instead of downloading the same one again.
- After that next release is downloaded and audio is extracted, this script compares its duration against the current library reference before muxing.
- The active default is to reject mismatched candidates instead of padding silence, because throughput is currently better when the next run searches for a cleaner release.

## Example

```bash
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28 --apply
```
