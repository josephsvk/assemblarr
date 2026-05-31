# run_pipeline_orchestrator.py

## Purpose

Runs the Assemblarr pipeline as several coordinated background loops instead of one monolithic cycle.

## Important Factors

- It is intended to be the top-level automatic service.
- It runs separate loops for:
  - missing-language search and queue fill
  - `rss_waitlist` retry
  - qBittorrent state sync, scan, and audio extraction
  - library audio remux
- Queue-producing branches share a lock and re-check qBittorrent active slots before they add more work.
- It reuses existing scripts instead of reimplementing their business logic.
- It should run in the `audio-tools` image so the remux branch has `synaudio-cli`.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - qBittorrent credentials
  - Prowlarr credentials
- `config.yml`
  - `download_clients`
  - `download_worker`
  - `pipeline_orchestrator`
  - `postprocess_import`

## Outputs

- Continuous branch-prefixed logs.
- New queued downloads from either missing-language search or `rss_waitlist`.
- Updated `download_jobs`, `download_audio_extractions`, `download_artifact_scans`, and `library_audio_remux_jobs`.

## Main Config

- `pipeline_orchestrator.missing_search`
  - Searches current missing-language titles and queues new releases while torrent slots are free.
- `pipeline_orchestrator.rss_waitlist`
  - Retries backlog entries from `rss_waitlist`.
- `pipeline_orchestrator.download_sync_extract`
  - Runs the existing worker in `--once --no-fill-queue --no-library-audio-remux` mode.
- `pipeline_orchestrator.library_audio_remux`
  - Runs the standalone library-audio remux script on extracted jobs.

## Example

```bash
python3 scripts/run_pipeline_orchestrator.py --once
python3 scripts/run_pipeline_orchestrator.py
```
