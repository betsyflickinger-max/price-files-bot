"""Find this month's file for each insurer network.

Insurers re-post their rate files every month under a new dated name, e.g.
  .../2026-09-05/inNetworkRates/2026-09-05_pl-22z-hr23_Aetna-Health-Inc.---Texas.json.gz
networks.csv keeps each network's link as a template ({date}=YYYY-MM-DD, {dc}=YYYYMMDD, {ym}=YYYY-MM).
We try the dates from newest to oldest, from today back to the date we already have, and take the
first link that answers. Networks with no date in the link (Molina, Wellpoint) keep the same link.
A network whose new file can't be found keeps its current file and is flagged "no new file yet".
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import check  # noqa: E402  (same header check as the hospital bot: HEAD, then a 1-byte GET)


def fill(template, d):
    return template.replace("{date}", d.isoformat()).replace("{dc}", d.strftime("%Y%m%d")).replace("{ym}", d.strftime("%Y-%m"))


def candidate_dates(since, today):
    d = today
    while d > since:
        yield d
        d -= timedelta(days=1)


def find(row, today, since):
    """Returns dict(url, how, http, error). since = date of the file we already have."""
    t = row["url_template"]
    if "{" not in t:
        h = check.headers(t)
        return dict(url=t, how="same link every month", http=h["http"], error=h["error"], found=h["ok"])
    cands = {}
    for d in candidate_dates(since, today):  # newest first; {ym}-only templates repeat, keep the newest day
        cands.setdefault(fill(t, d), d)
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda u: (u, check.headers(u)), cands))
    for u, h in results:  # still newest first
        if h["ok"] and not h.get("html"):
            return dict(url=u, how=f"new file dated {cands[u].isoformat()}", http=h["http"], error="", found=True)
    return dict(url=row["current_url"], how="no new file yet", http="", error="", found=False)
