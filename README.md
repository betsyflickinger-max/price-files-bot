# price-files-bot

Automatically collects and checks U.S. hospital and insurer price transparency files, and flags missing or broken ones.

## Version 1: hospital files (running on GitHub, free)

Every day at 4:17am Central, GitHub runs one job per state (20 at a time). Each hospital comes up about once a week; broken links are retried daily.

For each hospital due that day the bot:

1. **Finds the current file link** in the hospital's own `cms-hpt.txt` index, so moved or renamed files are followed. Link cleanup is the same as the Oct 9 retry run.
2. **Checks for changes without downloading** when it can: same ETag, or same Last-Modified and size, or the same size plus first and last 64 KB. Otherwise it downloads the whole file and compares its SHA-256. Every file is fully downloaded at least once a week anyway, because some servers send stale headers.
3. **Saves every new version**, untouched and zstd-compressed, to R2 at `raw/hospitals/<yyyy-mm>/<state>/<sha12>_<name>.zst` and to the state's archive.org item. Files already archived in October are recognized and not saved twice.
4. **Reads it with the fixed reader** (`bot/reader.py`, from `pipeline/2026-10-09/08_extract_hospital_codes.py`): counts every row and pulls the site's 89 target codes to `bot/rows/<day>/<state>/<sha12>.csv.zst`. Each row keeps `raw_key`, `source_url`, `sha256` and `source_row`. The whole original is kept, so any other code can be extracted later.
5. **Writes one status row per hospital** and flags problems: broken link, missing file, a web page instead of a file, an unreadable file, or rows down by more than half.

Hospitals that share one system-wide file are checked once. Files over 6 GB are flagged for the rented machine (version 2), because the free runner has about 14 GB of disk.

### Where results go (R2, under `bot/`)

| File | What it is |
|---|---|
| `status/latest.csv` | Newest status for every hospital checked. Feeds the coverage spreadsheet and the Price File Watch. |
| `status/coverage_by_state.csv` | Hospitals, files saved, problems and not-yet-checked, by state |
| `status/<day>/<state>.csv` | That day's checks |
| `status/<day>/alerts.csv` | Problems that are new that day |
| `manifest/files/<state>.csv` | Fingerprint of every file (ETag, dates, size, SHA-256, rows) |
| `manifest/hospitals/<state>.csv` | Each hospital's current link and last status |
| `rows/<day>/<state>/*.csv.zst` | Target-code rows from new versions |

If a file that used to work breaks, or its rows drop by more than half, the bot opens a GitHub issue labeled `price-file-alert`. Hospitals that never had a working file are listed in `alerts.csv` and the Price File Watch, but they don't open issues.

### Setup (one time)

Repo **Settings → Secrets and variables → Actions → New repository secret**, one at a time:

| Name | Value |
|---|---|
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | from `r2.env.txt` |
| `IA_ACCESS_KEY` | the `IA_ACCESS` value from `ia.env.txt` on the Mac |
| `IA_SECRET_KEY` | the `IA_SECRET` value from `ia.env.txt` on the Mac |

The bot runs without the archive.org keys and notes "archive.org keys not set" in each new file's status.

### Running it by hand

**Actions → Hospital price files → Run workflow.** To test, enter one small state (for example `DE`), tick "check every hospital", and set the limit to `3`.

On a computer: `pip install -r requirements.txt` then `python bot/run.py --state DE --all --limit 3 --local` (writes to `./local/`, no keys needed).

### Hospital list

`data/hospitals.csv` lists the 5,255 hospitals on CMS's Hospital General Information list (excluding VA and Defense Department hospitals). For each hospital it has the known index domain and file link, plus the last original archived in the October 3–9 runs. To add a found link, edit the hospital's row; the bot picks it up the next time that hospital is due.

## Version 2: insurer files (planned)

A monthly job checks every insurer file for changes on GitHub, then starts rented DigitalOcean machines only for the files that changed. The machines read the files, save the results, and delete themselves.

## Data rules

Every price comes from a real source file and links back to it. Nothing is estimated or averaged. Each record shows when it was checked.

Change-detection ideas adapted from [hospital-price-history](https://github.com/lkowalcz/hospital-price-history) (CC0).
