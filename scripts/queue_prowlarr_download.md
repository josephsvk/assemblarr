# queue_prowlarr_download.py

## Purpose

Selects the best configured Prowlarr candidate for one missing-language target and optionally saves the release artifact into a managed workspace.

## Important Factors

- The default run is dry-run.
- `--apply` writes only to the configured workspace and `download_jobs`.
- It does not import the release into Radarr/Sonarr.
- It does not move existing library files.
- It checks available disk space before writing.
- It can try multiple missing-language targets and select the first one with a scored Prowlarr candidate.
- It saves magnet links as `.magnet` files.
- It saves fetched release artifacts as `.torrent` or `.nzb` files.
- The workspace is intentionally separate from the final media library.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `PROWLARR_URL`
  - `PROWLARR_API_KEY`
- `config.yml`
  - `download_workspace`
  - `library_checks.missing_language`
  - `prowlarr_search`
- Postgres library tables.
- Prowlarr search results.

## Outputs

- Optional file in `assemblarr_library.folders.incoming`.
- Optional row in `download_jobs`.
- Console report with target, release, score, size, and workspace free space.

## Example

```bash
python3 scripts/queue_prowlarr_download.py
python3 scripts/queue_prowlarr_download.py --apply
python3 scripts/queue_prowlarr_download.py --max-targets 50
```
