# extract_download_audio.py

## Purpose

Extracts Czech and Slovak audio tracks from a completed download video file and stores the result in Postgres.

## Important Factors

- The script uses `ffprobe` to inspect real audio streams.
- It uses `ffmpeg` stream copy, so it does not re-encode audio.
- It matches multiple Czech and Slovak variants such as `cz`, `cs`, `cze`, `ces`, `sk`, `svk`, `slk`, and `slo`.
- It writes extracted tracks into the Assemblarr processing workspace.
- It keeps the original downloaded media file on disk.
- It does not yet select the final best audio automatically if multiple matching tracks exist.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `download_scan`
  - `assemblarr_library`
- A completed local video file path.

## Outputs

- Extracted `.mka` files in the processing workspace.
- Optional row in `download_audio_extractions`.

## Example

```bash
python3 scripts/extract_download_audio.py --video-path /downloads/assemblarr/incoming/example.mkv --client-queue-id abc123
python3 scripts/extract_download_audio.py --video-path /downloads/assemblarr/incoming/example.mkv --client-queue-id abc123 --apply
```
