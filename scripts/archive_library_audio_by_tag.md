# archive_library_audio_by_tag.py

## Purpose

Archives audio streams from existing library movie files selected by a local tag such as `2160p`, without changing the library file itself.

## Important Factors

- The default run is dry-run. Use `--apply` to extract and record the audit.
- It selects items from `media_item_tags`, so it depends on the resolution-tag pass already being up to date.
- It targets the current synced library movie file, not a download artifact.
- By default it archives all audio streams from the selected movie file.
- If `library_audio_backup.include_languages` is set, only those stream languages are extracted.
- Repeated `--apply` runs can skip already archived items when the source file path, size, and mtime still match the saved audit row.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `library_audio_backup`
  - `download_workspace`
- Postgres tables:
  - `media_item_tags`
  - `media_items`
  - `media_files`

## Outputs

- Archived audio files in `DOWNLOAD_ROOT/assemblarr/archive/...`
- Audit rows in `library_audio_backup_jobs`

## Important Config

- `library_audio_backup.source`
  - Source to scan. Currently expected to be `radarr`.
- `library_audio_backup.default_tag`
  - Default item tag used when `--tag` is not provided.
- `library_audio_backup.archive_subdir`
  - Archive subdirectory created under the Assemblarr download workspace archive root.
- `library_audio_backup.output_extension`
  - Container extension used for extracted audio streams.
- `library_audio_backup.language_groups`
  - Defines which aliases are normalized into each canonical language group.
  - The same mapping is used for both stream detection and `include_languages`.
- `library_audio_backup.include_languages`
  - Optional language allowlist. Leave empty to archive every audio stream.
  - You can keep this list simple, for example `cz` and `sk`, and let `language_groups` define the accepted aliases.
- `library_audio_backup.keep_existing`
  - When enabled, a later run skips items that were already archived from the same source file.

## Example

```bash
python3 scripts/archive_library_audio_by_tag.py
python3 scripts/archive_library_audio_by_tag.py --batch --limit 10
python3 scripts/archive_library_audio_by_tag.py --batch --apply
python3 scripts/archive_library_audio_by_tag.py --tag 2160p --movie-id 542 --apply
```
