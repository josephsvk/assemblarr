# scan_download_artifact.py

## Purpose

Scans the real files of a completed download path and stores observed metadata in Postgres.

## Important Factors

- The script inspects the downloaded files, not just the torrent title.
- It can scan either a single file or a whole directory tree.
- It records file lists, sizes, likely primary video file, archive presence, and detected language tokens.
- Czech and Slovak detection covers token variants such as `cz`, `cs`, `cze`, `ces`, `sk`, `svk`, `slk`, and `slo`.
- It does not import, rename, extract, move, or delete downloaded files.
- It does not yet inspect embedded audio streams with ffprobe or mediainfo.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `database.dsn_env`
  - `download_scan`
- Completed content path from qBittorrent or another client.

## Outputs

- Console report with observed file counts and language hits.
- Optional row in `download_artifact_scans`.

## Example

```bash
python3 scripts/scan_download_artifact.py --content-path /downloads/assemblarr/incoming/example.mkv --client-queue-id abc123
python3 scripts/scan_download_artifact.py --content-path /downloads/assemblarr/incoming/example --client-queue-id abc123 --apply
```
