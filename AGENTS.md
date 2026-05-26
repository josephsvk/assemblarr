# Codex Project Notes

- Treat `.env` as secret local state. Never print API keys or passwords.
- Prefer dry-run commands before any command with `--apply`.
- Keep Radarr/Sonarr subtitle tags disabled unless a file-to-item aggregation policy is explicitly chosen.
- Assemblarr library paths should come from `.env` where possible.
- Runtime data such as Postgres files, workspace downloads, caches, and artifacts must stay out of git.
- Add or update a matching English `.md` description for every script.
