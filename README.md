# Assemblarr

Assemblarr is an AI-assisted media workflow prototype for scanning Radarr/Sonarr libraries, tagging language/subtitle state, searching Prowlarr, and staging selected download artifacts in its own managed workspace.

## Warning

This project is AI-assisted and still experimental. Review every configuration value and dry-run output before using `--apply`. Do not point it at production library paths or download clients until the workflow is verified on a small sample.

## Setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
python3 -m pip install -r requirements.txt
cp .env.example .env
```

# venv 

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

cd ~/Stack-V2/assemblarr

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```


```bash
cd ~/Stack-V2/assemblarr
source .venv/bin/activate
python tvoj_script.py
```

Edit `.env`:

```bash
POSTGRES_DSN=postgresql://assemblarr:change_me@localhost:5432/assemblarr
MOVIES_LIBRARY_ROOT=/mnt/MediaPool/Movies
SERIES_LIBRARY_ROOT=/mnt/MediaPool/Serials
DOWNLOAD_ROOT=/mnt/MediaPool/Downloads
ASSEMBLARR_LIBRARY_ROOT=/home/joseph/assemblarr/assemblarr_library
RADARR_URL=http://localhost:7878
RADARR_API_KEY=...
SONARR_URL=http://localhost:8989
SONARR_API_KEY=...
PROWLARR_URL=http://localhost:9696
PROWLARR_API_KEY=...
```

Start Postgres:

```bash
docker compose -f "docker compose.yml" up -d postgres
```

## Docker

The repository includes a hardened `assemblarr` container for running the scripts in isolation:

```bash
cp .env.example .env
docker compose -f "docker compose.yml" build
docker compose -f "docker compose.yml" up -d
```

Container safety defaults:

- runs as a non-root `assemblarr` user
- drops Linux capabilities and enables `no-new-privileges`
- keeps the container filesystem read-only except for `/tmp`
- mounts movie, series, and download libraries read-only
- mounts only `ASSEMBLARR_LIBRARY_ROOT` read-write
- stores Postgres data in a named Docker volume, not in git

Run scripts inside the container:

```bash
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/manage_library_workspace.py
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/manage_library_workspace.py --apply
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/sync_library.py --init-db
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/sync_library.py --source radarr
docker compose -f "docker compose.yml" run --rm assemblarr python scripts/sync_library.py --source sonarr
```

The `POSTGRES_DSN` inside Docker is overridden to use the `postgres` service hostname automatically, so `.env` can keep the localhost DSN for local non-container runs.

Initialize and sync:

```bash
python3 scripts/sync_library.py --init-db
python3 scripts/sync_library.py --source radarr
python3 scripts/sync_library.py --source sonarr
```

## Workspace

Assemblarr uses its own managed library workspace, separate from Radarr/Sonarr libraries:

```bash
python3 scripts/manage_library_workspace.py
python3 scripts/manage_library_workspace.py --apply
```

Default folders:

```text
incoming/
processing/
library/
archive/
failed/
```

## Common Commands

Dry-run language tag decisions:

```bash
python3 scripts/apply_language_tag.py --mode all --batch
```

Apply language tags:

```bash
python3 scripts/apply_language_tag.py --mode all --batch --apply
```

Scan subtitles into the local DB only:

```bash
python3 scripts/scan_subtitles.py
python3 scripts/scan_subtitles.py --apply
```

Find and stage one Prowlarr artifact:

```bash
python3 scripts/queue_prowlarr_download.py --max-targets 50
python3 scripts/queue_prowlarr_download.py --max-targets 50 --apply
```

## Download Clients

Prowlarr provides torrent/NZB/magnet artifacts. It does not manage the download queue. qBittorrent or SABnzbd will be needed for queue status, pause/resume, deletion, and cleanup.

Download client integration is configured but disabled by default:

```yaml
download_clients:
  enabled: false
```

## Script Documentation

Each script has a matching English `.md` description in `scripts/`.
