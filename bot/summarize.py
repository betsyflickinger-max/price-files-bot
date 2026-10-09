"""After all state jobs: merge today's status rows, update coverage, and list NEW problems.

Writes to R2 (under bot/):
  status/latest.csv            newest status row for every hospital ever checked (feeds the coverage
                               spreadsheet and the Price File Watch)
  status/coverage_by_state.csv one row per state: hospitals, files saved, unchanged, problems, not yet checked
  status/<day>/alerts.csv      problems that are new today (or rows down by more than half)
Also writes out/summary.md (shown on the GitHub run page) and out/alerts.md (opened as a GitHub issue
when there are new problems).
  python bot/summarize.py [--local]
"""
import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store  # noqa: E402
from run import PROBLEM, ROOT, STATUS_FIELDS  # noqa: E402

GOOD = {"unchanged", "changed - new version saved", "new - first version saved"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    a = ap.parse_args()
    day = datetime.now(timezone.utc).date().isoformat()
    P = store.R2_PREFIX
    if a.local:
        base = ROOT / "local"
        rd = lambda k: list(csv.DictReader(open(base / k))) if (base / k).exists() else []  # noqa: E731

        def wr(k, rows, fields):
            (base / k).parent.mkdir(parents=True, exist_ok=True)
            with open(base / k, "w", newline="") as f:
                w = csv.DictWriter(f, fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
        today_keys = [str(p.relative_to(base)) for p in (base / P / "status" / day).glob("*.csv") if p.stem != "alerts"]
    else:
        s3, b = store.r2()
        rd = lambda k: store.read_csv(s3, b, k)  # noqa: E731
        wr = lambda k, rows, fields: store.write_csv(s3, b, k, rows, fields)  # noqa: E731
        today_keys = [o["Key"] for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=f"{P}status/{day}/")
                      for o in pg.get("Contents", []) if not o["Key"].endswith("alerts.csv")]

    latest = {r["ccn"]: r for r in rd(f"{P}status/latest.csv")}
    today = [r for k in today_keys for r in rd(k)]
    alerts = []
    for r in today:
        old = latest.get(r["ccn"], {})
        was_problem = old.get("status") in PROBLEM
        if (r["status"] in PROBLEM and not was_problem) or (r["alert"] == "yes" and r["status"] in GOOD) \
                or r["status"] == "unreadable file" and old.get("status") != "unreadable file":
            alerts.append(dict(r, previous_status=old.get("status", "(first check)")))
        latest[r["ccn"]] = r
    wr(f"{P}status/latest.csv", sorted(latest.values(), key=lambda r: (r["state"], r["ccn"])), STATUS_FIELDS)
    if alerts:
        wr(f"{P}status/{day}/alerts.csv", alerts, STATUS_FIELDS + ["previous_status"])

    hospitals = list(csv.DictReader(open(ROOT / "data" / "hospitals.csv")))
    by_state = defaultdict(Counter)
    for h in hospitals:
        by_state[h["state"]]["hospitals"] += 1
    for r in latest.values():
        c = by_state[r["state"]]
        c["checked"] += 1
        c["file saved" if r["status"] in GOOD else r["status"]] += 1
    cov_fields = ["state", "hospitals", "checked", "file saved", "coverage_pct", "link broken", "not a price file",
                  "unreadable file", "no price file found", "too large for free runner", "not yet checked"]
    cov = []
    for st in sorted(by_state):
        c = by_state[st]
        cov.append(dict({k: c.get(k, 0) for k in cov_fields[1:]}, state=st,
                        coverage_pct=round(100 * c["file saved"] / c["hospitals"], 1) if c["hospitals"] else 0,
                        **{"not yet checked": c["hospitals"] - c["checked"]}))
    wr(f"{P}status/coverage_by_state.csv", cov, cov_fields)

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    tc = Counter(r["status"] for r in today)
    tot = Counter()
    for c in cov:
        for k in cov_fields[1:]:
            if k != "coverage_pct":
                tot[k] += c[k]
    lines = [f"## Hospital price files, {day}", "",
             f"Checked today: **{len(today)}** hospitals in {len({r['state'] for r in today})} states.", ""]
    lines += [f"- {k}: {v}" for k, v in tc.most_common()]
    lines += ["", f"All time: {tot['file saved']:,} of {tot['hospitals']:,} hospitals have a saved file "
              f"({100 * tot['file saved'] / max(tot['hospitals'], 1):.1f}%); {tot['not yet checked']:,} not yet checked.",
              "", f"New problems today: **{len(alerts)}** "
              f"({sum(1 for r in alerts if r['previous_status'] in GOOD or r['status'] in GOOD)} were working before)"]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    # The GitHub issue only lists files that worked before and now don't, plus big row drops, so the
    # first week's ~2,500 already-known gaps (from the October runs) don't bury real breakages.
    urgent = [r for r in alerts if r["previous_status"] in GOOD or r["status"] in GOOD]
    if urgent:
        al = [f"{len(urgent)} price file(s) that worked before now have a problem ({day}). "
              f"All {len(alerts)} new problems, including first-time ones: R2 `{P}status/{day}/alerts.csv`.", "",
              "| State | Hospital | Problem | Detail | Link |", "|---|---|---|---|---|"]
        for r in urgent[:200]:
            d = (r["detail"] or "").replace("|", "/")[:120]
            al.append(f"| {r['state']} | {r['hospital']} | {r['status']} | {d} | {(r['mrf_url'] or r['index_url'])[:100]} |")
        if len(urgent) > 200:
            al.append(f"\n…and {len(urgent) - 200} more in the CSV.")
        (out / "alerts.md").write_text("\n".join(al) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
