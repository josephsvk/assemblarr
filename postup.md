1 nacitanie kniznice z radarr/sonarr do postgres

Stav:
- pripraveny skript `scripts/sync_library.py`
- konfiguracia ide cez `.env`
- schema je v `schema.sql`
- prvy import uklada Radarr filmy a Sonarr serialy do `media_items`
- dostupne Radarr movieFile data uklada do `media_files`
- test importu presiel: Radarr 889 filmov / 676 suborov, Sonarr 300 serialov
- skript ma retry na Postgres pripojenie po starte kontajnera

Spustenie:
```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
docker compose -f "docker compose.yml" up -d postgres
python3 scripts/sync_library.py --init-db
```

Dalsi potrebny postup:
- pri kazdom dalsom skripte vytvorit samostatny anglicky popis s castami Purpose, Important Factors, Inputs, Outputs, Example
- rozvijat pravidla v `config.yml`, nie natvrdo v skriptoch
- doplnit do `.env` `PROWLARR_URL` a `PROWLARR_API_KEY`
- spustit `python3 scripts/search_prowlarr_language.py --top 10`
- overit, ci jazykovy token hladat iba v nazve suboru alebo aj v audio/subtitle metadatoch
- rozhodnut, co ma nasledovat po najdeni filmu bez CZ/SK: report, export zoznamu, presun, tag v Radarr, alebo iny workflow
- rozhodnut, ci Prowlarr vysledky len reportovat alebo neskor posielat grab do klienta/Radarr
- rozšírit tagovanie z prveho kandidata na davkovy rezim s limitom a reportom
- doplnit rozhodovanie tagov: `nodab`, `cz`, `sk`, `cz+sk`, pripadne `dab_available`
- postupne tagovanie uz podporuje `--mode missing` a `--mode detected`
- batch tagovanie Radarr movie kniznice hotove: `cz`, `sk`, `czsk`, `nodab`
- kombinovany CZ/SK tag je `czsk`, nepouzivat `cz+sk` ani `cz-sk`
- Sonarr episode files import hotovy: 4278 episode files
- subtitle scan hotovy lokalne v `media_file_tags`: `cz-tit`, `sk-tit`, `en-tit`, `no-tit`
- titulky netagovat do Arr, ponechat iba v lokalnej DB
- Sonarr series tagovat iba pri konzistentnych seriach, mixed serie ponechat bez Arr tagu a riesit cez DB/report
- Sonarr konzistentne series tagovanie hotove: `nodab=243`, `cz=14`, `czsk=1`
- pripravene `download_workspace` a `download_jobs`
- pripraveny Prowlarr download workflow cez `scripts/queue_prowlarr_download.py`
- prvy download dry-run pre `20 Days in Mariupol` nenasiel vhodneho Prowlarr kandidata
- jeden Prowlarr artifact test uspesny: `A Madea Family Funeral (2019)(CZ)[1080p]` ulozeny ako torrent do `download_workspace/incoming`
- vytvorena samostatna `assemblarr_library` mimo Arr kniznic
- `download_workspace` nahradeny cez `assemblarr_library`
- qBittorrent/SABnzbd zatial nenapojene; budu potrebne na frontu, stav, mazanie a cleanup
- pre produkcny workspace pripravit adresar na media poole s pravami pre pouzivatela, potom prepnout `assemblarr_library.root` z projektoveho test adresara na cielovy mount
- dalsi krok: napojit torrent/NZB artifact na download klienta alebo vytvorit vlastny queue worker, ktory bude spravovat `incoming`, `processing`, `archive`
- pridany `.gitignore`, `README.md`, `AGENTS.md`
- pred commitovanim inicializovat realny git repozitar, ak este neexistuje
- produkcne cesty nastavovat cez `.env`: `MOVIES_LIBRARY_ROOT`, `SERIES_LIBRARY_ROOT`, `DOWNLOAD_ROOT`, `ASSEMBLARR_LIBRARY_ROOT`
- pred Arr tagovanim titulkov rozhodnut politiku: file-level ostane len v DB, alebo agregovat na movie/series tagy
- pri Sonarr dabingu rozhodnut politiku pre mixed series: tagovat seriu podla vsetkych epizod, iba kompletne serie, alebo drzat len file-level DB stav
- doplnit inkrementalnu synchronizaciu cez history/eventy
- pridat kontrolny SQL/report pre pocty filmov, serialov a suborov


2. triedenie podla nazvu 
