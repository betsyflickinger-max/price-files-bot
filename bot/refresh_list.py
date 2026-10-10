"""Add hospitals to data/hospitals.csv from CMS's public lists. Never removes or changes existing rows.

Lists (CMS Provider Data Catalog):
  xubh-q36u  Hospital General Information (acute, critical access, psychiatric, children's, rural emergency)
  7t8x-u3ir  Inpatient Rehabilitation Facility - General Information
  azum-44iv  Long-Term Care Hospital - General Information

Skipped: VA and Department of Defense hospitals (federal; not covered by the price-file rule), and rehab
*units* inside another hospital (CCN with a letter, e.g. 45T123): their prices are in the parent hospital's file.
Freestanding rehab hospitals have CCN numbers xx3025-xx3099; long-term care hospitals xx2000-xx2299.

Usage: python bot/refresh_list.py   (rewrites data/hospitals.csv and prints what was added)
"""
import csv
import io
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "data" / "hospitals.csv"
API = "https://data.cms.gov/provider-data/api/1/datastore/query/{}/0/download?format=csv"
FEDERAL = {"Veterans Health Administration", "Department of Defense"}


def get(dataset):
    r = requests.get(API.format(dataset), timeout=300)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))


def ccn_num(c):
    return int(c[2:]) if len(c) == 6 and c[2:].isdigit() else -1


def main():
    rows = list(csv.DictReader(open(LIST, encoding="utf-8")))
    fields = list(rows[0].keys())
    have = {r["ccn"] for r in rows}
    new = []
    for r in get("xubh-q36u"):
        if r["Facility ID"] not in have and r["Hospital Ownership"] not in FEDERAL:
            new.append(dict(ccn=r["Facility ID"], name=r["Facility Name"], city=r["City/Town"], state=r["State"],
                            type=r["Hospital Type"]))
    for ds, typ, lo, hi in (("7t8x-u3ir", "Inpatient Rehabilitation", 3025, 3099),
                            ("azum-44iv", "Long-term", 2000, 2299)):
        for r in get(ds):
            c = r["CMS Certification Number (CCN)"]
            if lo <= ccn_num(c) <= hi and c not in have:
                new.append(dict(ccn=c, name=r["Provider Name"], city=r["City/Town"], state=r["State"], type=typ))
    seen = set()
    new = [n for n in new if not (n["ccn"] in seen or seen.add(n["ccn"]))]
    rows += [{f: n.get(f, "") for f in fields} for n in new]
    rows.sort(key=lambda r: (r["state"], r["ccn"]))
    with open(LIST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)
    by = {}
    for n in new:
        by[n["type"]] = by.get(n["type"], 0) + 1
    print(f"added {len(new)} hospitals ({by}); list now {len(rows)}")


if __name__ == "__main__":
    main()
