"""Find the website (and so the cms-hpt.txt price-file index) for hospitals the bot has no link for.

Hospitals with no index_domain and no mrf_url in data/hospitals.csv are skipped by the daily run as
"no price file found" without anything being tried. This script tries three sources of candidate websites:

  osm    OpenStreetMap hospitals with a website tag, matched on name AND (ZIP or city)
  chain  the corporate site of a hospital chain named in the hospital's name (Encompass, Select, Kindred...)
  guess  domains built from the hospital's name (e.g. "Mat-Su Regional" -> matsuregional.com)

A candidate is accepted ONLY if https://<domain>/cms-hpt.txt exists and lists a location whose name matches
the hospital (score >= 85, or a single-location index for an OSM match, which is already place-checked).
For chain/guess matches, a name shared by hospitals in other states is not accepted automatically
(same-named hospitals in different states were the known failure mode of the Oct 5 domain guessing).

Writes index_domain + found_by into data/hospitals.csv and every result into data/found_sites.csv.
Usage: python bot/find_sites.py [--limit N]
"""
import argparse
import csv
import io
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LIST, REPORT = ROOT / "data" / "hospitals.csv", ROOT / "data" / "found_sites.csv"
CMS = "https://data.cms.gov/provider-data/api/1/datastore/query/{}/0/download?format=csv"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
OSM_Q = """[out:json][timeout:900];area["ISO3166-1"="US"][admin_level=2]->.us;
(nwr["amenity"="hospital"](area.us);nwr["healthcare"="hospital"](area.us););out tags center;"""

CHAINS = {  # words in the hospital name -> corporate sites to try (wrong ones simply fail the index check)
    "encompass": ["encompasshealth.com"],
    "select specialty": ["selectmedical.com", "selectspecialtyhospitals.com"],
    "regency hospital": ["selectmedical.com", "regencyhospital.com"],
    "kindred": ["kindredhospitals.com", "scionhealth.com"],
    "vibra": ["vibrahealthcare.com"],
    "pam ": ["pamhealth.com", "postacutemedical.com"],
    "post acute": ["pamhealth.com", "postacutemedical.com"],
    "ernest": ["ernesthealth.com"],
    "advanced care hospital": ["ernesthealth.com"],
    "nobis": ["nobisrehab.com"],
    "cornerstone": ["chghospitals.com"],
    "lifepoint": ["lifepointhealth.net", "lifepointrehab.com"],
    "kessler": ["kessler-rehab.com", "selectmedical.com"],
    "lifecare": ["lifecarehospitals.com"],
    "trustpoint": ["trustpointhospital.com"],
    "acadia": ["acadiahealthcare.com"],
    "uhs": ["uhs.com"],
    "shriners": ["shrinerschildrens.org"],
}
GENERIC = {"hospital", "hospitals", "medical", "center", "centre", "centers", "health", "healthcare", "system",
           "regional", "community", "memorial", "general", "county", "campus", "clinic", "llc", "inc", "the",
           "of", "and", "at", "for", "rehabilitation", "rehab", "specialty", "long", "term", "acute", "care",
           "behavioral", "psychiatric", "institute", "services", "facility", "dba"}


def words(name):
    s = name.lower().replace("&", " and ").replace("'", "").replace("saint ", "st ").replace("st. ", "st ")
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", s).split() if w]


def guesses(name):
    w = words(name)
    core = [x for x in w if x not in GENERIC]
    out = []
    for stem in ("".join(core), "".join(core[:2]), "".join(w[:3]), "".join(core) + "hospital",
                 "".join(core) + "health", "".join(core) + "medical", "".join(x[0] for x in w if x not in {"of", "and", "the"})):
        if len(stem) >= 4:
            out += [stem + ".org", stem + ".com"]
    return list(dict.fromkeys(out))[:10]


def zips():
    """ccn -> ZIP5, from the three CMS lists (hospitals.csv has no ZIP column)."""
    z = {}
    for ds, idk in (("xubh-q36u", "Facility ID"), ("7t8x-u3ir", "CMS Certification Number (CCN)"),
                    ("azum-44iv", "CMS Certification Number (CCN)")):
        try:
            r = requests.get(CMS.format(ds), timeout=300)
            for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
                z[row[idk]] = (row.get("ZIP Code") or "")[:5]
        except Exception as e:
            print("  ! CMS list", ds, e, flush=True)
    return z


def osm():
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": OSM_Q}, timeout=1000, headers={"User-Agent": "price-files-bot"})
            r.raise_for_status()
            out = []
            for e in r.json().get("elements", []):
                t = e.get("tags", {})
                site = t.get("website") or t.get("contact:website") or t.get("url")
                if t.get("name") and site:
                    out.append(dict(name=t["name"], site=site, zip=(t.get("addr:postcode") or "")[:5],
                                    city=(t.get("addr:city") or "").lower()))
            print(f"  OSM: {len(out):,} US hospitals with a website", flush=True)
            return out
        except Exception as e:
            print("  ! Overpass", url, e, flush=True)
    return []


def osm_candidates(h, z, places):
    found = []
    near = (places["zip"].get(z, []) if z else []) + places["city"].get(h["city"].lower(), [])
    for p in near:
        if discover.score(h["name"], p["name"]) >= 85:
            d = discover.host(p["site"])
            if d and not d.endswith(discover.SHARED_HOSTS):
                found.append(d)
    return list(dict.fromkeys(found))


def try_domain(h, dom, src, dup_names):
    idx, ents = discover.fetch_index(dom)
    if not ents:
        return None
    best = max(((discover.score(h["name"], e.get("location-name", "")), e) for e in ents), key=lambda x: x[0])
    sc, e = best
    ok = sc >= 85 or (src == "osm" and len(ents) == 1)
    if ok and src != "osm" and h["_norm"] in dup_names:
        return dict(domain=dom, found_by=src, score=round(sc), location=e.get("location-name", ""), index_url=idx,
                    accepted="no - same name in another state, check by hand")
    if not ok:
        return None
    return dict(domain=dom, found_by=src, score=round(sc), location=e.get("location-name", ""), index_url=idx,
                accepted="yes")


def search(h, z, places, dup_names):
    tried = set()
    lname = " " + h["name"].lower() + " "
    plan = [(d, "osm") for d in osm_candidates(h, z, places)]
    plan += [(d, "chain") for k, ds in CHAINS.items() if k in lname for d in ds]
    plan += [(d, "guess") for d in guesses(h["name"])]
    fallback = None
    for dom, src in plan:
        if dom in tried:
            continue
        tried.add(dom)
        try:
            res = try_domain(h, dom, src, dup_names)
        except Exception:
            res = None
        if res and res["accepted"] == "yes":
            return res
        fallback = fallback or res
    return fallback


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=48)
    a = ap.parse_args()
    rows = list(csv.DictReader(open(LIST, encoding="utf-8")))
    fields = list(rows[0].keys()) + (["found_by"] if "found_by" not in rows[0] else [])
    norm = lambda s: " ".join(discover._toks(s))
    states_by_name = {}
    for r in rows:
        r["_norm"] = norm(r["name"])
        states_by_name.setdefault(r["_norm"], set()).add(r["state"])
    dup_names = {n for n, s in states_by_name.items() if len(s) > 1}
    todo = [r for r in rows if not r.get("index_domain") and not r.get("mrf_url")]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo):,} hospitals have no website or file link", flush=True)
    z, pl = zips(), osm()
    places = {"zip": {}, "city": {}}
    for p in pl:
        if p["zip"]:
            places["zip"].setdefault(p["zip"], []).append(p)
        if p["city"]:
            places["city"].setdefault(p["city"], []).append(p)

    def one(h):
        return h, search(h, z.get(h["ccn"], ""), places, dup_names)

    report, n_ok = [], 0
    with ThreadPoolExecutor(a.workers) as ex:
        for i, (h, res) in enumerate(ex.map(one, todo), 1):
            if res:
                report.append(dict(ccn=h["ccn"], name=h["name"], city=h["city"], state=h["state"], type=h["type"], **res))
                if res["accepted"] == "yes":
                    h["index_domain"], h["found_by"] = res["domain"], f"{res['found_by']} {date.today()}"
                    n_ok += 1
            if i % 200 == 0:
                print(f"  {i:,}/{len(todo):,} searched, {n_ok:,} found", flush=True)
    for r in rows:
        r.pop("_norm", None)
    with open(LIST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)
    rf = ["ccn", "name", "city", "state", "type", "domain", "found_by", "score", "location", "index_url", "accepted"]
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, rf)
        w.writeheader()
        w.writerows(sorted(report, key=lambda r: (r["state"], r["ccn"])))
    by = {}
    for r in report:
        if r["accepted"] == "yes":
            by[r["found_by"]] = by.get(r["found_by"], 0) + 1
    held = sum(r["accepted"] != "yes" for r in report)
    print(f"done: {n_ok:,} of {len(todo):,} hospitals now have a website ({by}); {held} held for a hand check")


if __name__ == "__main__":
    main()
