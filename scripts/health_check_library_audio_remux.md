# health_check_library_audio_remux.py

## Purpose

Runs a post-remux health check against a final library movie file and reports whether the result looks safe to test manually.

## Important Factors

- It can resolve the final file from `download_job_id` through `library_audio_remux_jobs`.
- It verifies preferred CZ/SK language presence and compatibility stereo presence with the same `ffprobe`-based logic used by the remux flow.
- It checks the default audio track profile.
- It runs sampled `ffmpeg` decode windows for the default audio+video path and for every audio stream.
- If remux metadata is available, it reports whether the track used true sync output or only provisional passthrough/fallback mode.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `database`
  - `postprocess_import`
- Either:
  - `--download-job-id`
  - or `--library-path`

## Outputs

- Plain-text `pass/warn/fail` report by default.
- Optional JSON report with `--json`.
- Exit code `0` for `pass` or `warn`, `1` for `fail`.

## Example

```bash
python3 scripts/health_check_library_audio_remux.py --download-job-id 33
python3 scripts/health_check_library_audio_remux.py --download-job-id 33 --json
python3 scripts/health_check_library_audio_remux.py --library-path "/mnt/MediaPool/Movies/.../movie.mkv"
```
