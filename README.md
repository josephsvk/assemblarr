# Assemblarr

Assemblarr is an AI-assisted media workflow prototype for scanning Radarr/Sonarr libraries, tagging language/subtitle state, searching Prowlarr, and staging selected download artifacts in its own managed workspace.

## Current Flow

Assemblarr currently works like this:

Radarr + Sonarr sync
- loads library items and media files into Postgres
- tags detected audio language state in Arr and/or database
- scans subtitle state and stores or tags subtitle findings
- finds titles missing the configured CZ/SK language tokens
- searches Prowlarr for better matching releases
- scores candidates and selects the best release
- rejects torrent candidates below the configured minimum peer count
- saves `.torrent`, `.nzb`, or `.magnet` into the staging workspace
- optionally submits the staged artifact to qBittorrent
- records the staged job in Postgres
- if no acceptable candidate exists yet, records the title into the RSS waitlist for later follow-up
- worker watches qBittorrent and keeps at most the configured number of active torrents
- when a download completes, worker scans the real downloaded files and stores file-level observations
- worker extracts matching CZ/SK audio tracks from completed video files
- when configured seeding ratio is `0`, the completed torrent is removed from qBittorrent without deleting downloaded files
- when a torrent stays stalled longer than the configured timeout, worker can remove it from qBittorrent and push the title back to the RSS waitlist
- downloaded files stay on disk for later extraction, audio verification, and import

Planned next flow:

staged artifact in `DOWNLOAD_ROOT/assemblarr/incoming`
-> send artifact to qBittorrent or SABnzbd
-> wait for completed download
-> inspect extracted files and match them to the correct movie or series item
-> import the correct file into `ASSEMBLARR_LIBRARY_ROOT`
-> continue with archive, cleanup, tagging, and final verification

Current state:

- sync from Radarr and Sonarr works
- language and subtitle tagging works
- Prowlarr search and candidate scoring works
- artifact staging into the download workspace works
- qBittorrent submission for staged `.torrent` and `.magnet` works
- minimum torrent peer filtering works
- obvious collection/pack release tokens can be rejected before queueing
- titles without a usable release are recorded into an RSS waitlist for future follow-up
- long-running download worker mode exists
- completed downloads are scanned from real files and stored in `download_artifact_scans`
- completed downloads can extract CZ/SK audio tracks into the processing workspace
- completed torrents can be removed from qBittorrent automatically while keeping files on disk
- stalled torrents can expire after a configured timeout and be returned to the RSS waitlist
- remuxed Radarr movie imports can be prepared and sent back through Radarr manual import
- imported Radarr movies can be finalized with rescan, rename, and final language tag refresh
- existing Radarr movie video can be remuxed in place with extracted CZ/SK audio from a download
- current library movie audio can be archived in bulk by a selected local tag such as `2160p`
- downloaded files are kept and not deleted automatically
- automatic archive extraction and full series import are not implemented yet; movie audio extraction and library remux post-processing are implemented for Radarr jobs with a matched movie file

## Warning

This project is AI-assisted and still experimental. Review every configuration value and dry-run output before using `--apply`. Do not point it at production library paths or download clients until the workflow is verified on a small sample.

## Setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
python3 -m pip install -r requirements.txt
cp .env.example .env
```

# venv 

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

cd ~/Stack-V2/assemblarr

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```


```bash
cd ~/Stack-V2/assemblarr
source .venv/bin/activate
python tvoj_script.py
```

Edit `.env`:

```bash
POSTGRES_DSN=postgresql://assemblarr:change_me@localhost:5432/assemblarr
MOVIES_LIBRARY_ROOT=/mnt/MediaPool/Movies
SERIES_LIBRARY_ROOT=/mnt/MediaPool/Serials
DOWNLOAD_ROOT=/mnt/MediaPool/Downloads
ASSEMBLARR_LIBRARY_ROOT=/home/joseph/assemblarr/assemblarr_library
RADARR_URL=http://localhost:7878
RADARR_API_KEY=...
SONARR_URL=http://localhost:8989
SONARR_API_KEY=...
PROWLARR_URL=http://localhost:9696
PROWLARR_API_KEY=...
```

Start Postgres:

```bash
docker compose -f "docker compose.yml" up -d postgres
```

## Docker

The repository includes a hardened `assemblarr` container for running the scripts in isolation:

```bash
cp .env.example .env
docker compose -f "docker compose.yml" build
docker compose -f "docker compose.yml" up -d
```

The default runtime image intentionally stays light and does not include `synaudio-cli`. If you need the manual library-remux workflow from `scripts/remux_library_video_with_download_audio.py`, build the separate `audio-tools` target from `Dockerfile` first.

Long-running worker service:

```bash
docker compose -f "docker compose.yml" up -d assemblarr-worker
docker compose -f "docker compose.yml" logs -f assemblarr-worker
```

Parallel orchestrator service:

```bash
docker compose -f "docker compose.yml" up -d assemblarr-orchestrator
docker compose -f "docker compose.yml" logs -f assemblarr-orchestrator
```

One-shot background audio-backup service:

```bash
docker compose -f "docker compose.yml" up -d assemblarr-audio-backup
docker compose -f "docker compose.yml" logs -f assemblarr-audio-backup
docker compose -f "docker compose.yml" ps assemblarr-audio-backup
```

`assemblarr-audio-backup` is configured with `restart: on-failure:5`, so a transient failure such as a temporary Postgres recovery event will be retried automatically. The job logs one progress line per movie in the form `progress: done/total status=... remaining=... title=...`.

Container safety defaults:

- runs as a non-root `assemblarr` user
- can be mapped to your host user with `PUID` and `PGID` for writable bind mounts
- drops Linux capabilities and enables `no-new-privileges`
- keeps the container filesystem read-only except for `/tmp`
- mounts the movie library read-write for manual in-place audio remux workflows
- mounts the series library read-only
- mounts `DOWNLOAD_ROOT` read-write for staged artifacts
- mounts only `ASSEMBLARR_LIBRARY_ROOT` read-write
- stores Postgres data in a named Docker volume, not in git

Run scripts inside the container:

```bash
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/manage_library_workspace.py
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/manage_library_workspace.py --apply
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/sync_library.py --init-db
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/sync_library.py --source radarr
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/sync_library.py --source sonarr
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/run_download_worker.py --once --no-fill-queue
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/run_download_worker.py
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/queue_prowlarr_download.py --target-source rss_waitlist --max-targets 25 --apply
docker compose -f "docker compose.yml" run --rm assemblarr-orchestrator python scripts/run_pipeline_orchestrator.py --once
```

The `POSTGRES_DSN` inside Docker is overridden to use the `postgres` service hostname automatically, so `.env` can keep the localhost DSN for local non-container runs.

If `ASSEMBLARR_LIBRARY_ROOT` is a host bind mount, create it before the first `--apply` run and ensure it is writable by the UID/GID used by the container:

```bash

sudo mkdir -p /mnt/MediaPool/Download/assemblarr
sudo chown -R 1000:1000 /mnt/MediaPool/Download
sudo chmod -R u+rwX,g+rwX /mnt/MediaPool/Download

```

Adjust `1000:1000` to the values used in `.env` for `PUID` and `PGID`.

## Background Worker

`assemblarr-worker` now runs the infinite download loop in Docker.

It currently:

- watches qBittorrent for `assemblarr` category jobs
- syncs torrent state back into `download_jobs`
- keeps at most `download_clients.qbittorrent.max_active_torrents`
- fills free slots with new acceptable Prowlarr candidates
- scans completed downloads and stores the results in `download_artifact_scans`
- extracts matching CZ/SK audio tracks from completed video files
- can expire stalled torrents after `download_worker.stalled.minutes`
- can remove expired stalled torrents from qBittorrent and return them to `rss_waitlist`
- keeps downloaded files on disk

It does not yet extract archives or import the selected media into the final library.

## Pipeline Orchestrator

`assemblarr-orchestrator` is the new top-level service for parallel background work. It uses the heavier `audio-tools` image so the remux branch can run real `synaudio-cli` sync instead of fallback passthrough.

It runs four loops in parallel:

- `missing_search`: searches desired or missing titles and queues new releases while free torrent slots exist
- `rss_waitlist`: retries titles stored in `rss_waitlist`
- `download_sync_extract`: syncs qBittorrent state, scans completed downloads, and extracts CZ/SK audio
- `library_audio_remux`: takes `audio_extracted` jobs and runs the library-audio sync/remux step

Recommended default service:

```bash
docker compose -f "docker compose.yml" up -d assemblarr-orchestrator
```

Worker-only mode still exists, but the orchestrator is the preferred path when you want search, retry, extraction, and remux to keep moving automatically.

Main knobs:

```yaml
pipeline_orchestrator:
  missing_search:
    interval_seconds: 180
    max_queue_additions_per_run: 2
  rss_waitlist:
    interval_seconds: 300
  download_sync_extract:
    interval_seconds: 60
  library_audio_remux:
    interval_seconds: 120
    max_per_run: 1
```

Initialize and sync:

```bash
python3 scripts/sync_library.py --init-db
python3 scripts/sync_library.py --source radarr
python3 scripts/sync_library.py --source sonarr
```

## Workspace

Assemblarr uses its own managed library workspace, separate from Radarr/Sonarr libraries:

```bash
python3 scripts/manage_library_workspace.py
python3 scripts/manage_library_workspace.py --apply
```

Default folders:

```text
incoming/
processing/
library/
archive/
failed/
```

## Common Commands

Dry-run language tag decisions:

```bash
python3 scripts/apply_language_tag.py --mode all --batch
```

Apply language tags:

```bash
python3 scripts/apply_language_tag.py --mode all --batch --apply
```

Dry-run resolution tags:

```bash
python3 scripts/apply_resolution_tag.py --batch --limit 20
```

Apply resolution tags across the synced Radarr library:

```bash
python3 scripts/apply_resolution_tag.py --batch --apply
```

Dry-run bulk library audio backup for 4K-tagged movies:

```bash
python3 scripts/archive_library_audio_by_tag.py --batch --limit 10
```

Apply bulk library audio backup for all `2160p`-tagged movies:

```bash
python3 scripts/archive_library_audio_by_tag.py --batch --apply
```

Run the same backup flow in Docker as a detached one-shot job:

```bash
docker compose -f "docker compose.yml" up -d assemblarr-audio-backup
docker compose -f "docker compose.yml" logs -f assemblarr-audio-backup
docker compose -f "docker compose.yml" ps assemblarr-audio-backup
```

Track backup progress in Postgres:

```bash
docker compose -f "docker compose.yml" exec -T postgres psql -U assemblarr -d assemblarr -c "
SELECT extraction_status, count(*)
FROM library_audio_backup_jobs
WHERE source = 'radarr' AND tag_label = '2160p'
GROUP BY extraction_status
ORDER BY extraction_status;
"
```

Scan subtitles into the local DB only:

```bash
python3 scripts/scan_subtitles.py
python3 scripts/scan_subtitles.py --apply
```

Find and stage one Prowlarr artifact into the configured staging workspace:

```bash
python3 scripts/queue_prowlarr_download.py --max-targets 50
python3 scripts/queue_prowlarr_download.py --max-targets 50 --apply
python3 scripts/find_audio_sync_test_candidate.py --max-size-gb 8 --limit 5
python3 scripts/remux_and_import_radarr_movie.py
python3 scripts/remux_and_import_radarr_movie.py --download-job-id 4 --apply
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28 --apply
python3 scripts/health_check_library_audio_remux.py --download-job-id 33
```

The library-audio remux keeps the existing library video master, appends preferred CZ/SK audio when found, and can also generate an extra AAC stereo compatibility track for weaker playback devices.

Inserted Assemblarr audio is currently treated as provisional. Injected track titles are labeled as possibly incorrect or incomplete, and the AAC stereo compatibility track is preferred as the default playback track.

Use `find_audio_sync_test_candidate.py` on the media server to find a small Radarr movie file that has extracted CZ/SK audio on disk. It prints the matching `remux_library_video_with_download_audio.py` dry-run and apply commands.

The long-running worker can now continue the movie pipeline automatically after `audio_extracted`: it will remux the preferred CZ/SK audio into the existing Radarr movie file, add the optional AAC stereo compatibility track, rescan/rename in Radarr, archive the extracted audio, and keep the language and resolution tags in sync.

Bulk library audio backup by tag is also available. It only extracts audio streams from the already synced library file and stores them into the Assemblarr archive workspace. It does not remux, rename, or retag the movie.

On CPU-only hosts, the movie video is stream-copied and should not be transcoded. The expensive stages are `synaudio-cli`, the synchronized E-AC-3 encode, and the optional AAC stereo compatibility encode. On a dual-socket Xeon E5-2650L v4 host, keep video remuxing as `-c copy`, start with one or two library-audio remux jobs per worker cycle, and increase only after checking disk I/O wait and Radarr scan time.

Timing fix and compatibility audio are configurable in `config.yml`:

```yaml
postprocess_import:
  library_audio_remux:
    track_title_suffix: (Assemblarr - possibly incorrect or incomplete)
    sync_audio:
      enabled: true
      reference_audio_stream_index:
      precision_scale: 0.25
      strategy:
        method: adaptive_anchor_sync
        single_anchor_max_duration_diff_seconds: 4.0
        prefer_silence_windows: true
        silence_window_status: planned
        center_trimmed_edges_probe:
          segment_duration_seconds: 45.0
          anchor_points: [0.5, 0.75]
          trim_tolerance_seconds: 2.0
          rate_tolerance: 0.002
          sample_length: 0.5
          sample_gap: 9999.0
          start_range: 12.0
          end_range: 12.0
        profiles:
          single_anchor:
            sample_length: 0.25
            sample_gap: 90.0
            start_range: 240.0
            end_range: 120.0
          multi_anchor:
            sample_length: 0.125
            sample_gap: 10.0
            start_range: 180.0
            end_range: 60.0
      preflight_duration_tolerance_seconds: 12.0
      duration_mismatch_policy: reject
      max_silence_padding_seconds: 0.0
      synced_codec: eac3
      synced_bitrate_2ch: 384k
      synced_bitrate_multichannel: 640k
      duration_tolerance_seconds: 0.25
      rate_tolerance: 0.0000001
      archive_subdir: synced_audio
    compat_stereo:
      enabled: true
      codec: aac
      bitrate: 192k
      channels: 2
      prefer_default: true
      title_suffix: Stereo (Assemblarr - possibly incorrect or incomplete)
```

- `reference_audio_stream_index`: optional explicit library audio stream index used as the sync reference.
- `track_title_suffix`: appended to injected CZ/SK track titles so the file clearly shows that the dubbing may still be incorrect or incomplete.
- `precision_scale`: multiplier for `synaudio-cli` correlation sample size; lower values are faster but less precise. `0.25` is the current faster setting to push through more films at the cost of weaker sync confidence.
- current conservative mode: keep only near-matching audio candidates, allow at most `12s` preflight length drift, and reject the rest so a later queue run fetches another release.
- `strategy.method: center_anchored_trimmed_edges`: experimental test mode for releases that may be shortened at the beginning and end. It probes the middle and 3/4 positions first; if both align, the shorter track is centered by padding the start and end.
- `strategy.method: adaptive_anchor_sync`: named sync method for testing. When the extracted audio is almost as long as the library reference, it uses a sparse single-anchor profile; when the durations diverge more, it switches to a denser multi-anchor profile.
- `strategy.single_anchor_max_duration_diff_seconds`: threshold for switching from `single_anchor` to `multi_anchor`.
- `strategy.prefer_silence_windows`: reserved switch for future silence-aware anchor placement. It is prepared in config/metadata, but not yet executed in the current sync implementation.
- `preflight_duration_tolerance_seconds`: rejects obviously mismatched audio before the expensive sync pass.
- `duration_mismatch_policy: reject`: current default for throughput. When the measured candidate still does not fit, Assemblarr stops that remux and relies on a later queue run to fetch another release.
- `max_silence_padding_seconds`: used only when `duration_mismatch_policy` is switched back to `pad_silence` for experimental recovery flows.
- `duration_tolerance_seconds`: maximum allowed length drift after sync.
- `synced_bitrate_2ch` and `synced_bitrate_multichannel`: target bitrate for the synchronized dubbing master.
- synchronized tracks keep the same audio-vs-video timestamp offset as the selected reference audio stream when they are muxed back into the library file.
- `compat_stereo`: creates a lightweight playback-friendly track after sync, so the stereo version is derived from the corrected timing and not from the unsynchronized source.
- `compat_stereo.prefer_default`: makes the AAC stereo compatibility track the default output audio, which is safer on weaker playback devices when the higher-quality synced track behaves badly.
- repeated `queue_prowlarr_download.py` runs now skip already attempted releases for the same target, so the next run is biased toward a different release; after download, the remux preflight still compares extracted audio length against the current library reference before muxing.

Library-audio backup by tag is configurable here:

```yaml
library_audio_backup:
  enabled: true
  source: radarr
  default_tag: 2160p
  archive_subdir: library_audio_backups
  output_extension: .mka
  language_groups:
    cz: [cz, cs, cze, ces, czech]
    sk: [sk, svk, slk, slo, slovak, slovakian]
  include_languages: []
  keep_existing: true
```

- `default_tag`: local `media_item_tags.tag_label` used when `--tag` is not passed.
- `language_groups`: configurable alias groups used to normalize stream language codes.
- `include_languages`: optional audio-language allowlist. Keep it empty to archive every audio stream. `cz` and `sk` are enough when the alias groups are defined above.
- `keep_existing`: skips already archived movies when the source file still matches the saved audit row.

## Download Clients

Prowlarr provides torrent/NZB/magnet artifacts. It does not manage the download queue. qBittorrent or SABnzbd will be needed for queue status, pause/resume, deletion, and cleanup.

Download client integration is enabled for qBittorrent staging downloads:

```yaml
download_clients:
  enabled: true
  preferred: qbittorrent
```

The qBittorrent worker keeps at most this many active torrents at a time:

```yaml
download_clients:
  qbittorrent:
    max_active_torrents: 5
    seed_ratio_limit: 0
```

## Script Documentation

Each script has a matching English `.md` description in `scripts/`.
