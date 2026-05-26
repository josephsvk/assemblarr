2026-05-19

- Pripraveny prvy import kniznice z Radarr/Sonarr do Postgres.
- Pridany skript `scripts/sync_library.py`.
- Skript cita konfiguraciu z `.env`: `POSTGRES_DSN`, `RADARR_URL`, `RADARR_API_KEY`, `SONARR_URL`, `SONARR_API_KEY`.
- Pridana DB schema v `schema.sql` pre `media_items`, `media_files`, `sync_state`.
- Pridany `.env.example` a `requirements.txt`.
- Opraveny preklep `sevices` na `services` v `docker compose.yml`.

Spustenie:

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
# doplnit realne URL a API kluce
docker compose -f "docker compose.yml" up -d postgres
python3 scripts/sync_library.py --init-db
```

Test 2026-05-19:

- Postgres kontajner `assemblarr-postgres` bezi.
- Prvy pokus padol pocas inicializacneho restartu Postgresu / sandbox pristupu na localhost.
- Import spusteny mimo sandboxu presiel uspesne.
- Vysledok importu: Radarr 889 filmov, 676 movie files; Sonarr 300 serialov.
- Do skriptu doplneny retry na Postgres pripojenie po starte kontajnera.
- Opakovany import bez `--init-db` presiel uspesne a upsert neduplikuje zaznamy.
- Overene v DB:
  - `media_items`: `radarr/movie` 889, `sonarr/series` 300
  - `media_files`: `radarr/movie_file` 676
  - `sync_state`: zapisany `last_full_sync` pre `radarr` aj `sonarr`

Triedenie podla nazvu suboru:

- Pridany `config.yml` pre nastavovanie spravania skriptov.
- Pridany read-only skript `scripts/find_missing_language.py`.
- Ku skriptom pridane samostatne anglicke popisy:
  - `scripts/sync_library.md`
  - `scripts/find_missing_language.md`
- Realne Radarr nazvy pouzivaju tokeny ako `CS`, `SK`, `CZECH`, `SLOVAK`, preto su zahrnute v `config.yml`.
- Prvy najdeny Radarr film bez nakonfigurovaneho CZ/SK tokenu:
  - `20 Days in Mariupol` (2023), tmdb `1058616`
  - `/mnt/MediaPool/Movies/20 Days in Mariupol (2023) [tmdbid-1058616]/20 Days in Mariupol (2023) [tmdbid-1058616] - [WEBRip-1080p][AAC][2.0][HEVC].mkv`

Krok 3 - Prowlarr vyhladavanie:

- Pridany skript `scripts/search_prowlarr_language.py`.
- Pridany anglicky popis `scripts/search_prowlarr_language.md`.
- Do `.env.example` doplnene `PROWLARR_URL` a `PROWLARR_API_KEY`.
- Do `config.yml` doplnena samostatna sekcia `prowlarr_search.language`, aby sa lokalna kontrola nazvu suboru nemiesala s kontrolou release titulkov z Prowlarr.
- Scoring je konfigurovatelny:
  - hladany jazyk ma najvyssiu prioritu
  - potom audio format
  - potom video rozlisenie, zdroj a seeders
  - nekvalitne zdroje ako `CAM`, `TS`, `HDCAM` su odmietane
- Realny Prowlarr test zatial neprebehol, pretoze `.env` nema nastavene `PROWLARR_URL` a `PROWLARR_API_KEY`.

Krok 4 - Tagovanie v Arr a lokalnej DB:

- Pridany skript `scripts/apply_language_tag.py`.
- Pridany anglicky popis `scripts/apply_language_tag.md`.
- Do `schema.sql` pridana tabulka `media_item_tags`.
- Do `config.yml` pridana sekcia `tagging`.
- Tagovanie je idempotentne:
  - lokalna DB pouziva unique kluc `(source, media_type, source_id, tag_label)`
  - Radarr/Sonarr tag sa prida len ked este na iteme nie je
  - existujuce Arr tagy sa zachovaju
- Test vykonany na filme `20 Days in Mariupol`:
  - Radarr movie id `452`
  - vytvoreny/pouzity Radarr tag `nodab` s id `10`
  - zapisane do `media_item_tags`
  - opakovane spustenie `--apply` vratilo `arr_changed: False`
  - po `python3 scripts/sync_library.py --source radarr` zostal lokalny DB tag zachovany
  - synced `media_items.raw->tags` pre film obsahuje `[10]`

Postupne tagovanie dalsich poloziek:

- `scripts/apply_language_tag.py` rozsireny o `--mode missing` a `--mode detected`.
- Skript preskakuje polozky, ktore uz maju zaznam v `media_item_tags`.
- `--mode detected` najde dalsiu neoznacenu polozku s nakonfigurovanym CZ/SK tokenom.
- Otestovane na filme:
  - `Ant-Man and the Wasp: Quantumania` (2023), Radarr movie id `1028`
  - subor obsahuje `[EN+CS]`
  - priradeny tag `cz`
  - Radarr tag id `11`
  - zapisane do `media_item_tags`
- Dalsi dry-run po zapise preskocil tento film a nasiel:
  - `The Guardians of the Galaxy Holiday Special` (2022)
  - subor obsahuje `[EN+CS+SK]`
  - navrhnuty tag `cz+sk`

Celkovy scan a tagovanie Radarr kniznice:

- `scripts/apply_language_tag.py` rozsireny o batch rezim:
  - `--mode all --batch`
  - `--limit N`
  - `--apply`
- Dry-run celej kniznice pred zapisom:
  - 674 neoznacenych poloziek
  - `cz=369`, `cz+sk=68`, `nodab=235`, `sk=2`
- Malý testovaci batch odhalil, ze Radarr nepovoluje tag label `cz+sk`.
- Kombinovany tag bol najprv zmeneny na `cz-sk`, ale existujuci Radarr tag id `2` sa cez movie PUT neukladal na filmy.
- Finalne riesenie: kombinovany tag je `czsk`.
- Nekonzistentne lokalne `cz-sk` zaznamy boli odstranene a 68 poloziek bolo znovu otagovanych ako `czsk`.
- Finalny stav po plnom tagovani a naslednom `python3 scripts/sync_library.py --source radarr`:
  - `cz`: 370 lokalne / 370 v Radarr raw tags
  - `czsk`: 68 lokalne / 68 v Radarr raw tags
  - `nodab`: 236 lokalne / 236 v Radarr raw tags
  - `sk`: 2 lokalne / 2 v Radarr raw tags
  - spolu 676 otagovanych Radarr movie files
  - kontrolny dry-run: `No untagged all media item found.`

Sonarr episode files a subtitle scan:

- `scripts/sync_library.py` rozsireny o Sonarr `/api/v3/episodefile`.
- Sonarr API vyzaduje `seriesId`, preto sync prechadza jednotlive serie.
- Test Sonarr syncu:
  - 300 series
  - 4278 episode files
- Do `config.yml` pridana `library.source_priority`, default `radarr`, potom `sonarr`.
- `scripts/apply_language_tag.py` upraveny tak, aby bral prioritu zdrojov z konfiguracie.
- Pri Sonarr sa v dry-run spracuje kazda seria iba raz, pretoze Arr tag je series-level, nie episode-level.
- Pridany `scripts/scan_subtitles.py`.
- Pridany anglicky popis `scripts/scan_subtitles.md`.
- Do `schema.sql` pridana tabulka `media_file_tags`.
- Subtitle scan je genericky pre jazyky z `config.yml`.
- Kontroluje:
  - sidecar titulky vedla media suboru
  - embedded titulky z `mediaInfo.subtitles`
- Arr tagovanie titulkov je v `config.yml` zatial vypnute (`subtitle_scan.apply_to_arr: false`), lebo subtitle stav je file-level.
- Subtitle scan zapisany lokalne pre:
  - 676 Radarr movie files
  - 4278 Sonarr episode files
- Vysledne lokalne subtitle tagy:
  - `cz-tit`: 2452
  - `en-tit`: 3545
  - `no-tit`: 1031
  - `sk-tit`: 788

Konzistentne pravidla a Prowlarr download priprava:

- Potvrdene pravidlo: titulky ostavaju iba v lokalnej DB (`media_file_tags`), nie ako Arr tagy.
- Sonarr dabing tagovanie upravene na konzistentne series-level spravanie:
  - seria sa taguje iba vtedy, ked vsetky dostupne episode files daju rovnaky jazykovy tag
  - mixed serie sa netaguju ako celok
- Sonarr konzistentne serie otagovane v Sonarr aj lokalnej DB:
  - `nodab`: 243
  - `cz`: 14
  - `czsk`: 1
- Po Sonarr synce overene, ze lokalne tagy sedia s raw tags:
  - `sonarr/nodab`: 243 / 243
  - `sonarr/cz`: 14 / 14
  - `sonarr/czsk`: 1 / 1
- Pridany `download_workspace` do `config.yml`.
- Pridana tabulka `download_jobs` do `schema.sql`.
- Pridany skript `scripts/queue_prowlarr_download.py`.
- Pridany anglicky popis `scripts/queue_prowlarr_download.md`.
- Dry-run Prowlarr download workflow:
  - ciel: `20 Days in Mariupol`
  - vysledok: Prowlarr nenasiel kandidata s konfiguracnymi jazykovymi tokenmi
  - nezapisany ziadny download job ani subor

Prowlarr download test jedneho kandidata:

- `scripts/queue_prowlarr_download.py` upraveny tak, aby skusal viac missing-audio cielov cez `--max-targets`.
- Dry-run `--max-targets 50`:
  - preskocene bez kandidata: `20 Days in Mariupol`, `Aap Jaisa Koi`, `AEW All Out 2021`, `AEW Dynasty 2024`, `All of You`
  - najdeny ciel: `A Madea Family Funeral` (2019)
  - release: `A Madea Family Funeral (2019)(CZ)[1080p]`
  - indexer: `SkTorrent`
  - score: 1100
  - size: 3.2 GB
- Prvy apply zlyhal, pretoze `/mnt/MediaPool/Assemblarr` nebolo mozne vytvorit bez prav k `/mnt/MediaPool`.
- Test workspace docasne presunuty do `/home/joseph/assemblarr/download_workspace`.
- Apply uspesne ulozil:
  - `/home/joseph/assemblarr/download_workspace/incoming/assemblarr-A-Madea-Family-Funeral-2019-CZ-1080p.torrent`
  - velkost artifactu: 16780 bytes
  - `download_jobs.id = 1`
- Nebol spusteny download klient ani import do kniznice.

Assemblarr library a download clients:

- Potvrdene rozhodnutie: projekt je mimo Arr stacku a nema byt zavisly od priameho pristupu do Radarr/Sonarr kniznic.
- Pridana samostatna `assemblarr_library` konfiguracia po vzore Arr root folderu:
  - `incoming`
  - `processing`
  - `library`
  - `archive`
  - `failed`
- Pridany `scripts/manage_library_workspace.py`.
- Pridany anglicky popis `scripts/manage_library_workspace.md`.
- Workspace vytvoreny v projektovom test priestore:
  - `/home/joseph/assemblarr/assemblarr_library`
- `scripts/queue_prowlarr_download.py` prepnuty na `assemblarr_library`.
- Prowlarr vie poskytnut torrent/NZB/magnet artifact, ale nespravuje download queue.
- Pre stav fronty, mazanie z fronty a cleanup budu potrebne Download Clients:
  - qBittorrent pre torrent
  - SABnzbd pre NZB
- Do `config.yml` pridana sekcia `download_clients`, zatial `enabled: false`.
- Do `.env.example` doplnene placeholdery pre qBittorrent a SABnzbd.
- Overeny dry-run queue workflow po zmene workspace:
  - ciel: `A Madea Family Funeral` (2019)
  - release: `A Madea Family Funeral (2019)(CZ)[1080p]`
  - staging ostal `not-written`, pretoze dry-run

Projektove subory a Codex priprava:

- Pridany `.gitignore`.
- `.gitignore` chrani:
  - `.env`
  - Postgres data
  - Python cache
  - Assemblarr workspace
  - download artifacty
  - lokalne Codex/runtime scratch adresare
- Do `.env.example` pridane cesty:
  - `MOVIES_LIBRARY_ROOT`
  - `SERIES_LIBRARY_ROOT`
  - `DOWNLOAD_ROOT`
  - `ASSEMBLARR_LIBRARY_ROOT`
- Do `config.yml` pridane `library.paths` a `assemblarr_library.root_env`.
- `scripts/manage_library_workspace.py` a `scripts/queue_prowlarr_download.py` respektuju `ASSEMBLARR_LIBRARY_ROOT`.
- Pridany `README.md` s navodom a upozornenim, ze ide o AI-assisted experimental workflow.
- Pridany `AGENTS.md` pre Codex projektove pravidla.
- Overene:
  - `python3 -m py_compile scripts/manage_library_workspace.py scripts/queue_prowlarr_download.py`
  - `python3 scripts/manage_library_workspace.py`
- `git status` zlyhal, pretoze adresar nie je realny git repozitar.
