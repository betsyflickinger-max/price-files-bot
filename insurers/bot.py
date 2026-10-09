"""Insurer side (version 2), run daily on GitHub:  python insurers/bot.py [--dry-run] [--only SLOT,...]

  1. Collect: read results from rented machines; mark each network processed or failed;
     delete any bot machine older than 2 days (cost safety net).
  2. Check: for every network in networks.csv, find this month's file (find_links.py) and decide,
     without downloading it, whether it changed (same ETag, or same size + same internal
     last_updated_on = unchanged; the same rules as 06g_check_changed.py).
  3. Launch: rent one machine per insurer group that has changed files (worker.py reads them and
     deletes itself). Nothing is launched for a network that is already being read.

State lives in R2: bot/insurers/manifest.csv (one row per network), runs/<run_id>/ (job lists,
results, logs). Unchanged networks get last_checked = today, which the site uses as "checked on".
"""
import argparse
import csv
import io
import json
import re
import sys
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "bot")]
import check  # noqa: E402
import droplets  # noqa: E402
import find_links  # noqa: E402
import store  # noqa: E402

P = "bot/insurers/"
REPO = "https://github.com/betsyflickinger-max/price-files-bot"
FIELDS = ["slot", "insurer", "network_label", "machine_group", "url", "url_date", "table", "etag", "size",
          "file_last_updated_on", "rows", "status", "detail", "last_checked", "last_changed", "processed_on",
          "run_id", "droplet_id", "launched_at", "fails"]
SIZES = {"uhc": "s-4vcpu-16gb-amd", "aetna": "s-4vcpu-16gb-amd"}  # biggest files; anything else: 4 vCPU / 8 GB
MAX_MACHINES = 5
STUCK_HOURS = 48
STALE_DAYS = 45      # flag a network whose file hasn't changed in this long
PEEK = 256 * 1024


def url_date(u):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", u) or re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", u)
    if not m:
        return ""
    return m.group(1) if len(m.groups()) == 1 else f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def peek(url):
    """Headers plus last_updated_on from the first 256 KB. Never downloads the whole file."""
    h = {"User-Agent": check.UA, "Range": f"bytes=0-{PEEK - 1}"}
    with requests.get(url, headers=h, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        head = r.raw.read(PEEK, decode_content=False)
        size = ""
        cr = r.headers.get("Content-Range", "")
        if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            size = cr.rsplit("/", 1)[1]
        elif r.status_code == 200:
            size = r.headers.get("Content-Length", "")
        etag = r.headers.get("ETag", "").replace("W/", "").strip('"')
    if head[:2] == b"\x1f\x8b":
        try:
            head = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(head)
        except zlib.error:
            head = b""
    m = re.search(rb'"last_updated_on"\s*:\s*"([^"]*)"', head)
    return dict(etag=etag, size=size, file_last_updated_on=m.group(1).decode() if m else "")


def load(s3, b):
    man = {r["slot"]: r for r in store.read_csv(s3, b, P + "manifest.csv")}
    for n in csv.DictReader(open(HERE / "networks.csv")):
        if n["slot"] not in man:  # first run: start from the tables already in R2
            man[n["slot"]] = dict(slot=n["slot"], insurer=n["insurer"], network_label=n["network_label"],
                                  machine_group=n["machine_group"], url=n["current_url"],
                                  url_date=url_date(n["current_url"]), table=n["current_table"],
                                  file_last_updated_on=n["last_updated_on"], status="processed",
                                  processed_on="(before the bot)", fails="0")
        man[n["slot"]]["_net"] = n
    return man


def collect(s3, b, man, now, dry):
    notes = []
    for m in man.values():
        if m.get("status") != "running":
            continue
        key = f"{P}runs/{m['run_id']}/done/{m['slot']}.json"
        try:
            res = json.loads(s3.get_object(Bucket=b, Key=key)["Body"].read())
        except Exception:
            age = (now - datetime.fromisoformat(m["launched_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            if age > STUCK_HOURS:
                m.update(status="failed", detail=f"machine gave no result in {STUCK_HOURS} hours", fails=str(int(m.get("fails") or 0) + 1))
                notes.append(("failed", m))
            continue
        if res.get("ok"):
            old_rows = m.get("rows", "")
            m.update(status="processed", table=res["table"], rows=str(res.get("rows", "")),
                     file_last_updated_on=res.get("file_last_updated_on", m.get("file_last_updated_on", "")),
                     processed_on=res.get("finished_at", "")[:10], last_changed=res.get("finished_at", "")[:10],
                     fails="0", detail=f"read in {res.get('secs', '?')}s" + (f"; retired {res['retired']}" if res.get("retired") else ""))
            if old_rows.isdigit() and int(old_rows) > 0 and int(m["rows"] or 0) < int(old_rows) / 2:
                m["detail"] += f"; rows fell from {int(old_rows):,} to {int(m['rows']):,}"
                notes.append(("rows dropped", m))
        else:
            m.update(status="failed", detail=res.get("error", "unknown error")[:300], fails=str(int(m.get("fails") or 0) + 1))
            notes.append(("failed", m))
    # cost safety net: any bot machine older than STUCK_HOURS is deleted
    try:
        for did, name, created in droplets.mine():
            age = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
            if age > STUCK_HOURS and not dry:
                droplets.delete(did)
                print(f"deleted leftover machine {name} ({age:.0f} h old)")
    except Exception as e:
        print("could not list machines:", e)
    return notes


def check_all(man, today, only):
    notes = []
    for m in man.values():
        n = m["_net"]
        if n.get("enabled", "yes") != "yes" or (only and m["slot"] not in only) or m.get("status") == "running":
            continue
        since = date.fromisoformat(m["url_date"]) if m.get("url_date") else today - timedelta(days=40)
        f = find_links.find(dict(n, current_url=m["url"]), today, since)
        url = f["url"]
        try:
            info = peek(url)
        except Exception as e:
            m["last_checked"] = today.isoformat()
            if url == m["url"]:
                m["detail"] = f"could not check the file: {type(e).__name__}: {str(e)[:150]}"
                notes.append(("check failed", m))
                continue
            info = dict(etag="", size="", file_last_updated_on="")
        same = False
        if m.get("status") == "processed":
            if info["etag"] and info["etag"] == m.get("etag"):
                same, why = True, "same ETag"
            elif info["file_last_updated_on"] and info["file_last_updated_on"] == m.get("file_last_updated_on") \
                    and (not m.get("size") or info["size"] == m.get("size")):
                same, why = True, "same internal date" + (" and size" if m.get("size") else "")
        m["last_checked"] = today.isoformat()
        if same:
            m.update(etag=info["etag"] or m.get("etag", ""), size=info["size"] or m.get("size", ""),
                     url=url, url_date=url_date(url) or m.get("url_date", ""), detail=f"unchanged ({why})")
            if m.get("last_changed") and (today - date.fromisoformat(m["last_changed"])).days > STALE_DAYS:
                notes.append(("stale", m))
            continue
        if m.get("status") == "failed" and int(m.get("fails") or 0) >= 3 and url == m["url"]:
            continue  # gave up on this file; it's in the issue already
        m.update(status="queued", next_url=url, etag=info["etag"], size=info["size"],
                 detail=f"{f['how']}; internal date {info['file_last_updated_on'] or '?'}")
    return notes


def user_data(run_id, group, env):
    exports = "\n".join(f"export {k}='{v}'" for k, v in env.items())
    return f"""#!/bin/bash
exec > /var/log/job.log 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive HOME=/root
{exports}
export RUN_ID='{run_id}' GROUP='{group}'
apt-get update -y && apt-get install -y python3-venv git curl
curl -sL https://go.dev/dl/go1.26.9.linux-amd64.tar.gz | tar -C /usr/local -xz
export PATH=$PATH:/usr/local/go/bin
git clone --depth 1 {REPO} /opt/bot
cd /opt/bot/insurers/pipeline/mrfrows && go build -o mrfrows . && cd /opt/bot
python3 -m venv /opt/venv && /opt/venv/bin/pip install -q -r insurers/requirements-worker.txt
/opt/venv/bin/python insurers/worker.py
# if the worker died before deleting the machine, delete it now
ID=$(curl -s http://169.254.169.254/metadata/v1/id)
curl -s -X DELETE -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" https://api.digitalocean.com/v2/droplets/$ID
"""


def launch(s3, b, man, now, dry):
    queued = [m for m in man.values() if m.get("status") == "queued"]
    groups = {}
    for m in queued:
        groups.setdefault(m["machine_group"], []).append(m)
    running = {m["machine_group"] for m in man.values() if m.get("status") == "running"}
    try:
        live = len(droplets.mine()) if not dry else 0
    except Exception:
        live = 0
    run_id = now.strftime("%Y%m%d-%H%M")
    env = {k: store.env(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "DIGITALOCEAN_TOKEN")}
    launched = []
    for g, ms in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if g in running or live >= MAX_MACHINES:
            continue
        jobs = [dict(slot=m["slot"], url=m["next_url"], network_label=m["network_label"], prev_table=m.get("table", "")) for m in ms]
        buf = io.StringIO()
        w = csv.DictWriter(buf, ["slot", "url", "network_label", "prev_table"])
        w.writeheader(); w.writerows(jobs)
        if dry:
            print(f"[dry run] would rent a {SIZES.get(g, droplets.DEFAULT_SIZE)} machine for {g}: {len(jobs)} files")
            continue
        s3.put_object(Bucket=b, Key=f"{P}runs/{run_id}/{g}.csv", Body=buf.getvalue().encode())
        try:
            d = droplets.create(f"upfront-bot-{g}-{run_id}", user_data(run_id, g, env), SIZES.get(g, droplets.DEFAULT_SIZE))
        except Exception as e:
            for m in ms:
                m["detail"] = f"could not rent a machine: {str(e)[:200]}"
            continue
        live += 1
        for m in ms:
            m.update(status="running", run_id=run_id, droplet_id=str(d["id"]), launched_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     url=m.pop("next_url"), url_date=url_date(m["url"]) or m.get("url_date", ""))
        launched.append((g, len(ms)))
    return launched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="check only; rent nothing, change nothing in R2")
    ap.add_argument("--only", default="", help="comma-separated slots")
    a = ap.parse_args()
    s3, b = store.r2()
    now = datetime.now(timezone.utc)
    today = now.date()
    man = load(s3, b)
    notes = collect(s3, b, man, now, a.dry_run)
    notes += check_all(man, today, {s.strip() for s in a.only.split(",") if s.strip()})
    launched = launch(s3, b, man, now, a.dry_run)
    rows = sorted(man.values(), key=lambda m: (m["insurer"], m["slot"]))
    if not a.dry_run:
        store.write_csv(s3, b, P + "manifest.csv", rows, FIELDS)

    out = HERE.parent / "out"
    out.mkdir(exist_ok=True)
    from collections import Counter
    c = Counter(m.get("status") for m in rows)
    lines = [f"## Insurer files, {today}", "", f"{len(rows)} networks: " + ", ".join(f"{v} {k}" for k, v in c.most_common())]
    if launched:
        lines.append("Rented machines: " + ", ".join(f"{g} ({n} files)" for g, n in launched))
    lines += ["", "| Insurer | Network | Status | Detail |", "|---|---|---|---|"]
    lines += [f"| {m['insurer']} | {m['network_label']} | {m.get('status', '')} | {(m.get('detail') or '').replace('|', '/')[:120]} |" for m in rows]
    (out / "insurer_summary.md").write_text("\n".join(lines) + "\n")
    bad = [(k, m) for k, m in notes if k in ("failed", "rows dropped") or (k == "stale" and today.day == 20)]
    if bad and not a.dry_run:
        al = [f"{len(bad)} insurer file problem(s) on {today}:", "", "| Problem | Insurer | Network | Detail |", "|---|---|---|---|"]
        al += [f"| {k} | {m['insurer']} | {m['network_label']} | {(m.get('detail') or '').replace('|', '/')[:150]} |" for k, m in bad]
        (out / "insurer_alerts.md").write_text("\n".join(al) + "\n")
    print("\n".join(lines[:4]))


if __name__ == "__main__":
    main()
