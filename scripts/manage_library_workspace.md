# manage_library_workspace.py

## Purpose

Creates and inspects the Assemblarr managed library workspace.

## Important Factors

- The workspace is intentionally separate from Radarr and Sonarr libraries.
- It follows an Arr-like structure with `incoming`, `processing`, `library`, `archive`, and `failed`.
- The default run is dry-run.
- `--apply` creates directories but does not move or delete media.
- This script does not communicate with Prowlarr, qBittorrent, SABnzbd, Radarr, or Sonarr.

## Inputs

- `config.yml`
  - `assemblarr_library.root`
  - `assemblarr_library.folders`
  - `assemblarr_library.min_free_gb`

## Outputs

- Optional workspace directories.
- Console report with paths and free disk space.

## Example

```bash
python3 scripts/manage_library_workspace.py
python3 scripts/manage_library_workspace.py --apply
```
