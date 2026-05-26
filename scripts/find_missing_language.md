# find_missing_language.py

## Purpose

Finds media files whose filename does not contain any configured Czech or Slovak language token.

## Important Factors

- The behavior is controlled by `config.yml`.
- The first check is intentionally limited to one Radarr movie file.
- Matching is case-insensitive.
- By default, only the basename is checked, not the full directory path.
- Current real Radarr filenames use tokens such as `CS`, `SK`, `CZECH`, and `SLOVAK`.
- The script is read-only: it reports candidates and does not move, rename, delete, or update files.

## Inputs

- `.env`
  - `POSTGRES_DSN`
- `config.yml`
  - `database.dsn_env`
  - `library_checks.missing_language.source`
  - `library_checks.missing_language.media_type`
  - `library_checks.missing_language.limit`
  - `library_checks.missing_language.filename_only`
  - `library_checks.missing_language.required_any_tokens`
- Postgres table `media_files`.
- Postgres table `media_items` for movie title metadata.

## Outputs

- Console report for the first matching file.
- Exit code `1` when a missing-language candidate is found.
- Exit code `0` when no candidate is found.

## Example

```bash
python3 scripts/find_missing_language.py
python3 scripts/find_missing_language.py --config config.yml
```
