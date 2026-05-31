# run_download_worker.py

## Purpose

Runs a long-lived worker that watches qBittorrent jobs, scans completed downloads, and refills the queue up to the configured limit.

## Important Factors

- The worker is intended to run continuously.
- It logs every worker cycle with torrent counts and job state changes.
- It syncs qBittorrent states back into `download_jobs`.
- It fills the queue only up to `download_clients.qbittorrent.max_active_torrents`.
- It scans completed downloads and stores real file observations in `download_artifact_scans`.
- It extracts matching CZ/SK audio tracks from completed video files.
- It can automatically remux extracted CZ/SK audio into the existing Radarr library movie file after extraction succeeds.
- It can skip the remux stage when the top-level orchestrator wants this worker to focus only on torrent state, scan, and extraction.
- It can remove completed torrents from qBittorrent while keeping downloaded files on disk.
- It can expire stalled torrents after a configurable timeout and move them back to `rss_waitlist`.
- It keeps downloaded files in place and does not delete them.
- It does not yet extract archives or retry RSS automatically.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - qBittorrent credentials from `QBITTORRENT_*`
- `config.yml`
  - `download_clients`
  - `download_worker`
  - `download_scan`
  - `postprocess_import`
- Postgres data from `download_jobs` and `rss_waitlist`
- qBittorrent Web API

## Outputs

- Continuous console logs.
- Updated rows in `download_jobs`.
- Updated rows in `download_artifact_scans`.
- Updated rows in `library_audio_remux_jobs`.
- Optional new queued downloads when free torrent slots exist.

## Example

```bash
python3 scripts/run_download_worker.py --once --no-fill-queue
python3 scripts/run_download_worker.py --once --no-fill-queue --no-library-audio-remux
python3 scripts/run_download_worker.py --once
python3 scripts/run_download_worker.py
```
