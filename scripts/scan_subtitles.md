# scan_subtitles.py

## Purpose

Scans media files for sidecar subtitles and stores subtitle status tags in the local database.

## Important Factors

- The script is generic for any configured language.
- Language behavior is controlled by `subtitle_scan.languages` in `config.yml`.
- It scans sidecar subtitle files next to the media file.
- It also checks embedded subtitle language data from Radarr/Sonarr `mediaInfo.subtitles` when `subtitle_scan.check_embedded` is enabled.
- The default run is dry-run. Use `--apply` to write local database tags.
- Arr tag writing is intentionally disabled in `config.yml` by default because subtitle status is file-level, while Radarr/Sonarr tags are item-level.
- For subtitles, file-level local tags are safer than item-level Arr tags, especially for Sonarr episodes.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `subtitle_scan.source_priority`
  - `subtitle_scan.media_types`
  - `subtitle_scan.sidecar_extensions`
  - `subtitle_scan.no_subtitle_tag`
  - `subtitle_scan.languages`
- Postgres table `media_files`.
- Sidecar subtitle files on disk.

## Outputs

- Optional rows in `media_file_tags`.
- Console summary with subtitle tag counts.
- Example tags: `no-tit`, `cz-tit`, `sk-tit`, `en-tit`.

## Example

```bash
python3 scripts/scan_subtitles.py --limit 50
python3 scripts/scan_subtitles.py --limit 50 --apply
python3 scripts/scan_subtitles.py --apply
```
