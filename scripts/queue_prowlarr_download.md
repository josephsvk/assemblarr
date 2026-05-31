# queue_prowlarr_download.py

## Purpose

Selects the best configured Prowlarr candidate for one missing-language target and optionally saves the release artifact into a configured staging workspace.

## Important Factors

- The default run is dry-run.
- `--apply` writes only to the configured staging workspace and `download_jobs`.
- It does not import the release into Radarr/Sonarr.
- It does not move existing library files.
- It checks available disk space before writing.
- It can try multiple missing-language targets and select the first one with a scored Prowlarr candidate.
- It saves magnet links as `.magnet` files.
- It saves fetched release artifacts as `.torrent` or `.nzb` files.
- When `download_clients.enabled` is true and `preferred` is `qbittorrent`, it also submits the staged artifact to qBittorrent.
- The staging workspace can be the managed library workspace or a dedicated subdirectory under `DOWNLOAD_ROOT`.
- When no acceptable candidate exists yet, it records the title into `rss_waitlist` for later RSS-driven retry logic.
- It can also target `rss_waitlist` directly for retry runs instead of only scanning current missing-language items.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `PROWLARR_URL`
  - `PROWLARR_API_KEY`
  - `DOWNLOAD_ROOT` when `download_workspace.root_env` points there
- `config.yml`
  - `download_workspace`
  - `download_clients`
  - `library_checks.missing_language`
  - `prowlarr_search`
- CLI
  - `--target-source missing_language|rss_waitlist`
- Postgres library tables.
- Prowlarr search results.

## Outputs

- Optional file in the configured staging `incoming` folder.
- Optional row in `download_jobs`.
- Optional row in `rss_waitlist` when no acceptable candidate exists yet.
- Optional qBittorrent queue submission.
- Console report with target, release, score, size, staging root, client status, and staging free space.

## Example

```bash
python3 scripts/queue_prowlarr_download.py
python3 scripts/queue_prowlarr_download.py --apply
python3 scripts/queue_prowlarr_download.py --max-targets 50
python3 scripts/queue_prowlarr_download.py --target-source rss_waitlist --max-targets 25 --apply
```
