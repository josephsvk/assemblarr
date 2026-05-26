# sync_library.py

## Purpose

Loads the current Radarr and Sonarr library state into Postgres.

## Important Factors

- Uses Radarr `/api/v3/movie`.
- Uses Sonarr `/api/v3/series`.
- Uses Sonarr `/api/v3/episodefile`.
- Stores full source API payloads in JSONB for later parsing.
- Uses upsert behavior, so repeated runs update existing rows instead of duplicating them.
- Can initialize the required database tables with `--init-db`.
- Retries the Postgres connection after container startup.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `RADARR_URL`
  - `RADARR_API_KEY`
  - `SONARR_URL`
  - `SONARR_API_KEY`
- `schema.sql`
- Radarr and Sonarr API responses.

## Outputs

- `media_items`
  - Radarr movies.
  - Sonarr series.
- `media_files`
  - Radarr movie files available inside the movie response.
  - Sonarr episode files from the episodefile endpoint.
- `sync_state`
  - Last full sync timestamp per source.
- Console summary with synced item counts.

## Example

```bash
python3 scripts/sync_library.py --init-db
python3 scripts/sync_library.py --source radarr
```
