# apply_language_tag.py

## Purpose

Applies a configured language status tag to one media item in the local database and, when enabled, in Radarr or Sonarr.

## Important Factors

- The script is idempotent.
- It stores local tag decisions in `media_item_tags`, which is separate from synced source payloads.
- A later Radarr/Sonarr library sync updates `media_items` and `media_files` but does not delete `media_item_tags`.
- The script preserves existing Radarr/Sonarr tags and appends only the configured tag when missing.
- The default run is dry-run. Use `--apply` to write changes.
- Use `--mode missing` for the next untagged item without configured language tokens.
- Use `--mode detected` for the next untagged item with configured language tokens.
- Use `--mode all --batch` to scan the full untagged library and tag both missing and detected language states.
- Combined language tags use compact labels, for example `czsk`, to avoid inconsistent behavior with pre-existing Arr tags.
- Already tagged items in `media_item_tags` are skipped, so review can move forward one item at a time.
- For Radarr movie files, the tag is applied to the parent movie.
- For Sonarr, the same design applies to the parent series once Sonarr episode files are imported.
- Current `nodab` means no configured Czech/Slovak dubbing token was found in the filename.

## Inputs

- `.env`
  - `POSTGRES_DSN`
  - `RADARR_URL`
  - `RADARR_API_KEY`
  - `SONARR_URL`
  - `SONARR_API_KEY`
- `config.yml`
  - `library_checks.missing_language`
  - `tagging.apply_to_arr`
  - `tagging.apply_to_database`
  - `tagging.missing_language_tag`
  - `tagging.detected_language_tags`
  - `tagging.arr`
- Postgres tables `media_items` and `media_files`.
- Radarr/Sonarr tag and item APIs.

## Outputs

- Optional local DB row in `media_item_tags`.
- Optional Radarr/Sonarr tag creation.
- Optional Radarr/Sonarr item update with the tag id appended.
- Console report showing the tag decision and whether the Arr item changed.

## Example

```bash
python3 scripts/apply_language_tag.py
python3 scripts/apply_language_tag.py --apply
python3 scripts/apply_language_tag.py --mode detected
python3 scripts/apply_language_tag.py --mode detected --apply
python3 scripts/apply_language_tag.py --mode all --batch
python3 scripts/apply_language_tag.py --mode all --batch --apply
python3 scripts/apply_language_tag.py --mode all --batch --limit 10
```
