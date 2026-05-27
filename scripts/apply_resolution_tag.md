# apply_resolution_tag.py

## Purpose

Applies configured resolution tags such as `2160p`, `1080p`, and `720p` to Radarr parent movies based on the current synced movie file.

## Important Factors

- The script is idempotent.
- It removes only the managed resolution tags before adding the currently detected one.
- It keeps unrelated Radarr tags intact.
- It stores the local result in `media_item_tags`.
- Resolution detection prefers synced Arr quality metadata and falls back to filename tokens.
- The default run is dry-run. Use `--apply` to write changes.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `RADARR_URL`
  - `RADARR_API_KEY`
- `config.yml`
  - `quality_tagging`
  - `tagging.arr`
- Postgres tables `media_items` and `media_files`.

## Outputs

- Optional local DB row in `media_item_tags`.
- Optional Radarr tag creation.
- Optional Radarr movie update with the detected resolution tag.

## Example

```bash
python3 scripts/apply_resolution_tag.py
python3 scripts/apply_resolution_tag.py --apply
python3 scripts/apply_resolution_tag.py --batch --limit 20
python3 scripts/apply_resolution_tag.py --batch --apply
```
