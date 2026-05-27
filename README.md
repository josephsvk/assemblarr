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
- automatic extraction, matching, and import into the correct movie is not implemented yet

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

Long-running worker service:

```bash
docker compose -f "docker compose.yml" up -d assemblarr-worker
docker compose -f "docker compose.yml" logs -f assemblarr-worker
```

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

Scan subtitles into the local DB only:

```bash
python3 scripts/scan_subtitles.py
python3 scripts/scan_subtitles.py --apply
```

Find and stage one Prowlarr artifact into the configured staging workspace:

```bash
python3 scripts/queue_prowlarr_download.py --max-targets 50
python3 scripts/queue_prowlarr_download.py --max-targets 50 --apply
python3 scripts/remux_and_import_radarr_movie.py
python3 scripts/remux_and_import_radarr_movie.py --download-job-id 4 --apply
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28
python3 scripts/remux_library_video_with_download_audio.py --download-job-id 28 --apply
```

The library-audio remux keeps the existing library video master, appends preferred CZ/SK audio when found, and can also generate an extra AAC stereo compatibility track for weaker playback devices.

The long-running worker can now continue the movie pipeline automatically after `audio_extracted`: it will remux the preferred CZ/SK audio into the existing Radarr movie file, add the optional AAC stereo compatibility track, rescan/rename in Radarr, archive the extracted audio, and keep the language and resolution tags in sync.

Bulk library audio backup by tag is also available. It only extracts audio streams from the already synced library file and stores them into the Assemblarr archive workspace. It does not remux, rename, or retag the movie.

Timing fix and compatibility audio are configurable in `config.yml`:

```yaml
postprocess_import:
  library_audio_remux:
    sync_audio:
      enabled: true
      reference_audio_stream_index:
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
```

- `reference_audio_stream_index`: optional explicit library audio stream index used as the sync reference.
- `duration_tolerance_seconds`: maximum allowed length drift after sync.
- `synced_bitrate_2ch` and `synced_bitrate_multichannel`: target bitrate for the synchronized dubbing master.
- `compat_stereo`: creates a lightweight playback-friendly track after sync, so the stereo version is derived from the corrected timing and not from the unsynchronized source.

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
