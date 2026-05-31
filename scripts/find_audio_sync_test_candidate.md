# find_audio_sync_test_candidate.py

## Purpose

Finds a small visible Radarr movie file that already has extracted CZ/SK audio on disk, so the library-audio synchronization/remux path can be tested on a low-risk sample.

## Important Factors

- It reads candidate rows from Postgres.
- It requires the library video path and extracted audio paths to exist on the current host/container.
- It sorts visible candidates by library video file size.
- It does not modify files, Radarr, qBittorrent, or the database.
- It prints the exact dry-run and `--apply` commands for `remux_library_video_with_download_audio.py`.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `database`
- Postgres rows from:
  - `download_jobs`
  - `download_audio_extractions`
  - `media_files`
  - `library_audio_remux_jobs`

## Example

```bash
python3 scripts/find_audio_sync_test_candidate.py --max-size-gb 8 --limit 5
```
