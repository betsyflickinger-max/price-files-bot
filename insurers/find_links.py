"""Find this month's file for each insurer network.

Insurers re-post their rate files every month under a new dated name, e.g.
  .../2026-09-05/inNetworkRates/2026-09-05_pl-22z-hr23_Aetna-Health-Inc.---Texas.json.gz
networks.csv keeps each network's link as a template ({date}=YYYY-MM-DD, {dc}=YYYYMMDD, {ym}=YYYY-MM).
We try the dates from newest to oldest, from today back to the date we already have, and take the
first link that answers. Networks with no date in the link (Molina, Wellpoint) keep the same link.
A network whose new file can't be found keeps its current file and is flagged "no new file yet".
"""
import re
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


_CACHE = {}


def _get(url, **kw):
    import requests
    if url not in _CACHE:
        r = requests.get(url, headers={"User-Agent": check.UA}, timeout=600, **kw)
        r.raise_for_status()
        _CACHE[url] = r.text
    return _CACHE[url]


def cigna_toc(arg):
    """Cigna serves files only through signed links. Its public latest.json points to this month's
    table of contents, which lists a signed link for every rate file. arg = network part of the name."""
    import json
    latest = json.loads(_get("https://www.cigna.com/static/mrf/latest.json"))
    toc_url = next(f["url"] for m in latest["mrfs"] if m.get("kind") == "TOC" for f in m["files"])
    toc = _get(toc_url)
    for u in re.findall(r'"location"\s*:\s*"([^"]+)"', toc):
        if f"_{arg}_in-network-rates" in u.split("?")[0]:
            d = re.search(r"(\d{4}-\d{2}-\d{2})_cigna", u)
            return dict(url=u, how=f"from Cigna's table of contents ({d.group(1) if d else '?'})", http="200", error="", found=True)
    return dict(url="", how="not in Cigna's table of contents", http="", error="missing from table of contents", found=False)


def azure_list(arg):
    """Insurers that keep files in public Azure storage (BSW): list the folder, newest matching file.
    arg = '<container list URL>|<regex for the file name>'."""
    base, pat = arg.split("|", 1)
    names, marker = [], ""
    while True:
        x = _get(base + (f"&marker={marker}" if marker else ""))
        names += re.findall(r"<Name>([^<]+)</Name>", x)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            break
        marker = m.group(1)
    hits = sorted(n for n in names if re.search(pat, n.rsplit("/", 1)[-1]))
    if not hits:
        return dict(url="", how="no matching file in the insurer's folder", http="", error="no match", found=False)
    root = base.split("?")[0]
    return dict(url=f"{root}/{hits[-1]}", how=f"newest in the insurer's folder ({hits[-1].rsplit('/', 1)[-1][:10]})",
                http="200", error="", found=True)


def find(row, today, since):
    """Returns dict(url, how, http, error). since = date of the file we already have."""
    finder = row.get("finder", "")
    try:
        if finder == "cigna_toc":
            return cigna_toc(row["finder_arg"])
        if finder == "azure_list":
            return azure_list(row["finder_arg"])
    except Exception as e:
        return dict(url=row["current_url"], how=f"could not read the file list: {type(e).__name__}", http="",
                    error=str(e)[:200], found=False)
    t = row["url_template"]
    if "{" not in t:
        h = check.headers(t)
        if h["ok"] and h.get("html"):
            return dict(url=t, how="link now opens a web page, not a file", http=h["http"], error="web page", found=False)
        return dict(url=t, how="same link every month" if h["ok"] else f"link broken ({h['error']})",
                    http=h["http"], error=h["error"], found=h["ok"])
    cands = {}
    for d in candidate_dates(since, today):  # newest first; {ym}-only templates repeat, keep the newest day
        cands.setdefault(fill(t, d), d)
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda u: (u, check.headers(u)), cands))
    for u, h in results:  # still newest first
        if h["ok"] and not h.get("html"):
            return dict(url=u, how=f"new file dated {cands[u].isoformat()}", http=h["http"], error="", found=True)
    return dict(url=row["current_url"], how="no new file yet", http="", error="", found=False)
