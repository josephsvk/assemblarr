# remux_and_import_radarr_movie.py

## Purpose

Takes one completed Radarr movie download, remuxes the selected streams into a clean `.mkv`, imports that remuxed file back into Radarr through `manualimport`, then finalizes the movie with rescan, rename, and language tag refresh.

## Important Factors

- It only targets `download_jobs` that belong to `radarr` movies.
- It only considers jobs already scanned and ready for post-processing.
- It prefers configured CZ/SK audio streams first, then keeps remaining audio streams behind them.
- It stops if no configured preferred audio stream is found in the source file.
- It keeps video streams untouched and copies streams without re-encoding.
- It uses Radarr `manualimport` instead of writing directly into the Radarr library path.
- After import it asks Radarr to rescan and rename the movie.
- It refreshes the local sync state and replaces Assemblarr-managed language tags with the final tag from verified Radarr media info.
- When called for an already imported job id, it can run a finalize-only recovery path to repair rescan, rename, and final tag state.
- It verifies that the remux really contains preferred CZ/SK audio before import.
- It verifies that Radarr actually switched the movie to a file that exposes preferred CZ/SK audio after import, with a direct `ffprobe` check on the final library file.
- Dry-run prints the planned remux and import candidate details without changing files or Radarr.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `RADARR_URL`
  - `RADARR_API_KEY`
- `config.yml`
  - `download_scan`
  - `postprocess_import`
- Postgres rows from:
  - `download_jobs`
  - `download_artifact_scans`
  - `download_audio_extractions`
  - `media_items`

## Outputs

- A remuxed `.mkv` in the processing workspace when `--apply` is used.
- Optional Radarr manual import of that remuxed file.
- Optional row in `download_media_imports`.
- Updated `download_jobs.status = imported` only after remux and Radarr import verification pass.

## Example

```bash
python3 scripts/remux_and_import_radarr_movie.py
python3 scripts/remux_and_import_radarr_movie.py --download-job-id 4
python3 scripts/remux_and_import_radarr_movie.py --download-job-id 4 --apply
```
