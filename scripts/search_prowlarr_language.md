# search_prowlarr_language.py

## Purpose

Searches Prowlarr for releases that contain the configured Czech or Slovak language tokens for the first movie currently missing those tokens locally.

## Important Factors

- The script is read-only.
- It does not download, grab, rename, tag, or delete anything.
- It uses `library_checks.missing_language` to choose the local target movie.
- It uses the separate `prowlarr_search.language` section to detect wanted language tokens in Prowlarr release titles.
- This separation avoids mixing local filename checks with remote release matching.
- Releases without any configured language token are ignored.
- Releases containing configured reject tokens such as `CAM`, `TS`, or `HDCAM` are ignored.
- Scoring prioritizes wanted language first, then audio format, video resolution, source type, and seeders.
- Prowlarr release metadata can vary by indexer, so title parsing should be treated as a ranking signal, not as final proof of audio or subtitles.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `PROWLARR_URL`
  - `PROWLARR_API_KEY`
- `config.yml`
  - `database.dsn_env`
  - `library_checks.missing_language`
  - `prowlarr_search.base_url_env`
  - `prowlarr_search.api_key_env`
  - `prowlarr_search.type`
  - `prowlarr_search.limit`
  - `prowlarr_search.categories`
  - `prowlarr_search.language.required_any_tokens`
  - `prowlarr_search.language.score`
  - `prowlarr_search.scoring`
- Postgres data from `media_items` and `media_files`.
- Prowlarr `/api/v1/search` results.

## Outputs

- Console report with the local target movie.
- Ranked Prowlarr candidates.
- Score reasons for each candidate.
- Whether a download or magnet URL is present.

## Example

```bash
python3 scripts/search_prowlarr_language.py
python3 scripts/search_prowlarr_language.py --top 10
```
