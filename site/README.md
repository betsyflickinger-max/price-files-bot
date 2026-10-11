# Monthly website refresh

`.github/workflows/site-build.yml` runs `site/orchestrate.py` every 6 hours. Once a month (from the 8th, after the
insurer bot has read the month's new files) it rebuilds the Price Finder and deploys it to the private preview site.

1. **build1** (rented machines): `every_code.py` reads every DFW insurer table (`data/full/rates/`) into one file per
   billing code; `hosp_every.py` reads only the DFW hospital price files that changed since last month.
2. **build2** (one machine): `h2_build.py` turns the hospital lines into one file per code.
3. **publish** (GitHub runner): `merge_page.py` + `apply_names_med.py` update the page, `check_build.py` compares it
   with the live page (networks, codes, providers, rate lines, hospital files must not drop), then R2
   `site/every/CURRENT` is pointed at the new build and the site is deployed with wrangler.
   Any failed check → nothing goes live and an issue opens.

Needs the `CLOUDFLARE_API_TOKEN` secret to deploy (without it the build stops at "ready" and opens an issue).
The site's files are kept in R2 (`site/deploy/current.tgz`), not in this public repo.
State: R2 `site/builds/<YYYY-MM>/state.json`. Start early: run the workflow by hand with "force".

Names: `plain_names_cpt.tsv` (plain-English names written for the most common codes), HCPCS Level II official
descriptions and MS-DRG titles (public CMS files), Medicare 2026 benchmarks (`build_med_all.py`, CMS fee schedules).
