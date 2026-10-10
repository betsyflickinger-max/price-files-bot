"""Hospital-side data for the Price Transparency Metrics dashboard, built nationally from the hospital bot.

Reads (R2, under bot/):
  status/latest.csv                newest bot check for every hospital (see bot/summarize.py)
  dashboard/baseline/*.json        one-time copies of the Oct 9 coverage-spreadsheet results (states, hospitals,
                                   states_history) used for hospitals the bot hasn't checked yet, and for history
  dashboard/history/<day>.json     this script's own daily state totals
and data/hospitals.csv (the CMS hospital list) from this repo.
Writes out/dashboard/{states,hospitals,systems,states_history}.json (one array of rows each) and today's
totals to R2 dashboard/history/<day>.json. The dashboard's other files (insurer rates) are built on Betsy's Mac.

Status per hospital: the bot's latest check wins; if the bot hasn't checked it yet, a file archived in an
October run (data/hospitals.csv last_key) counts as "Have file"; then the baseline status (detail says so);
otherwise "Not checked yet". Counts only, nothing estimated.
  python dashboard/build.py [--local DIR]   (--local reads baseline/latest from DIR instead of R2)
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
ROOT = Path(__file__).resolve().parent.parent
GOOD = {"unchanged", "changed - new version saved", "new - first version saved"}
FAILED = {"link broken", "not a price file", "unreadable file", "too large for free runner"}
NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
         "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
         "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
         "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
         "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
         "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
         "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
         "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
         "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
         "WI": "Wisconsin", "WY": "Wyoming"}


def site(u):
    try:
        h = urlparse(u if "://" in u else "https://" + u).hostname or ""
    except ValueError:
        return ""
    return re.sub(r"^www\.", "", h.lower())


def pdate(x):
    x = str(x or "").strip()[:10]
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(x, f).date()
        except ValueError:
            pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local")
    a = ap.parse_args()
    today = datetime.now(timezone.utc).date()
    if a.local:
        L = Path(a.local)
        get_json = lambda k: json.loads((L / k).read_text()) if (L / k).exists() else None  # noqa: E731
        latest = list(csv.DictReader(open(L / "latest.csv"))) if (L / "latest.csv").exists() else []
        hist_days = sorted(p.stem for p in (L / "history").glob("*.json")) if (L / "history").exists() else []
        put_json = lambda k, obj: ((L / k).parent.mkdir(parents=True, exist_ok=True), (L / k).write_text(json.dumps(obj)))  # noqa: E731
        get_hist = lambda d: get_json(f"history/{d}.json")  # noqa: E731
    else:
        import store
        s3, b = store.r2()
        P = store.R2_PREFIX + "dashboard/"

        def get_json(k):
            try:
                return json.loads(s3.get_object(Bucket=b, Key=P + k)["Body"].read())
            except Exception:
                return None
        latest = store.read_csv(s3, b, store.R2_PREFIX + "status/latest.csv")
        hist_days = sorted(o["Key"].rsplit("/", 1)[1][:-5] for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=P + "history/")
                           for o in pg.get("Contents", []) if o["Key"].endswith(".json"))
        put_json = lambda k, obj: s3.put_object(Bucket=b, Key=P + k, Body=json.dumps(obj).encode(), ContentType="application/json")  # noqa: E731
        get_hist = lambda d: get_json(f"history/{d}.json")  # noqa: E731

    base_h = {h["ccn"]: h for h in (get_json("baseline/hospitals.json") or [])}
    base_s = {s["state"]: s for s in (get_json("baseline/states.json") or [])}
    base_hist = [r for r in (get_json("baseline/states_history.json") or []) if r["state"] != "ALL"]
    bot = {r["ccn"]: r for r in latest}
    cms = [h for h in csv.DictReader(open(ROOT / "data" / "hospitals.csv")) if h["state"] in NAMES]

    hospitals = []
    for h in cms:
        ccn, r, bh = h["ccn"], bot.get(h["ccn"]), base_h.get(h["ccn"])
        row = {"id": ccn, "ccn": ccn, "state": h["state"], "hospital": h["name"].title(), "city": h["city"].title(),
               "type": h["type"], "status": "Not checked yet", "detail": "", "website": site(h.get("mrf_url") or "") or h.get("index_domain", ""),
               "file_date": None, "stale": "", "format": "", "checked": None, "source": ""}
        if r:
            st = r["status"]
            row.update(status="Have file" if st in GOOD else "Download failed" if st in FAILED else "No file found",
                       detail=st + (f": {r['detail']}" if r.get("detail") else ""), checked=(r.get("checked_at") or "")[:10],
                       website=site(r.get("mrf_url") or "") or site(r.get("index_url") or "") or row["website"], source="Bot")
            d = pdate(r.get("file_last_updated_on"))
            row["file_date"] = d.isoformat() if d else None
        elif h.get("last_key"):
            row.update(status="Have file", detail=f"File saved in the {h.get('last_archived_run') or 'October'} run", source="Saved file")
        elif bh:
            row.update(status=bh.get("status") or "Not checked yet", detail="Oct 9 list: " + str(bh.get("detail") or ""),
                       website=bh.get("website") or row["website"], file_date=bh.get("file_date"), source="Oct 9 list")
        d = pdate(row["file_date"])
        if row["status"] == "Have file" and d and (today - d).days > 183:
            row["stale"] = "Over 6 months old"
        hospitals.append(row)

    by = defaultdict(lambda: defaultdict(int))
    for h in hospitals:
        c = by[h["state"]]
        c["hospitals"] += 1
        c[{"Have file": "have", "Download failed": "failed", "No file found": "nofile", "Known file, not downloaded": "pending",
           "Not checked yet": "unchecked"}.get(h["status"], "nofile")] += 1
        c["stale"] += bool(h["stale"])
        c["bot"] += h["source"] == "Bot"
    states = []
    for ab, nm in sorted(NAMES.items(), key=lambda x: x[1]):
        c, bs = by[ab], base_s.get(ab, {})
        started = c["hospitals"] > c["unchecked"]
        states.append({"state": ab, "name": nm, "status": "Started" if started else "Not started",
                       "hospitals": c["hospitals"] or None, "have": c["have"], "pending": c["pending"], "failed": c["failed"],
                       "nofile": c["nofile"], "unchecked": c["unchecked"], "stale": c["stale"], "checked_by_bot": c["bot"],
                       "coverage": round(c["have"] / c["hospitals"], 4) if c["hospitals"] and started else None,
                       "insurer_files": bs.get("insurer_files") or "None yet", "insurer_markets": bs.get("insurer_markets") or "None yet"})

    systems = {}
    for h in hospitals:
        k = (h["state"], h["website"] or "(no file website known)")
        r = systems.setdefault(k, {"id": f"{k[0]}|{k[1]}", "state": k[0], "website": k[1], "hospitals": 0, "have": 0,
                                   "failed": 0, "nofile": 0, "unchecked": 0, "stale": 0})
        r["hospitals"] += 1
        r["have"] += h["status"] == "Have file"
        r["failed"] += h["status"] == "Download failed"
        r["nofile"] += h["status"] in ("No file found", "Known file, not downloaded")
        r["unchecked"] += h["status"] == "Not checked yet"
        r["stale"] += bool(h["stale"])

    day = today.isoformat()
    snap = [{"id": f"{day}|{s['state']}", "date": day, "state": s["state"], "hospitals": s["hospitals"], "have": s["have"],
             "failed": s["failed"], "nofile": s["nofile"]} for s in states if s["hospitals"]]
    snap.append({"id": f"{day}|ALL", "date": day, "state": "ALL", "hospitals": sum(s["hospitals"] or 0 for s in states),
                 "have": sum(s["have"] for s in states), "failed": sum(s["failed"] for s in states), "nofile": sum(s["nofile"] for s in states)})
    put_json(f"history/{day}.json", snap)
    hist = list(base_hist)
    seen = {r["date"] for r in hist}
    for d in sorted(set(hist_days) | {day}):
        if d in seen:
            continue
        hist += snap if d == day else (get_hist(d) or [])
    hist.sort(key=lambda r: (r["date"], r["state"]))

    out = ROOT / "out" / "dashboard"
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("states", states), ("hospitals", hospitals),
                       ("systems", sorted(systems.values(), key=lambda r: (r["state"], -r["hospitals"]))), ("states_history", hist)):
        (out / f"{name}.json").write_text(json.dumps(rows, separators=(",", ":")))
    tot = snap[-1]
    print(f"{day}: {tot['have']:,} of {tot['hospitals']:,} hospitals have a file; bot has checked "
          f"{sum(s['checked_by_bot'] for s in states):,}; {sum(s['unchecked'] for s in states):,} not checked yet.")


if __name__ == "__main__":
    main()
