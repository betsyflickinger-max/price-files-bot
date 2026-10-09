"""Runs on a rented machine: read each assigned insurer file and replace its tables in R2, then delete itself.

Job list: R2 bot/insurers/runs/<RUN_ID>/<GROUP>.csv  (slot, url, network_label, prev_table)
For each file:
  1. 06f_full_extract.py (unchanged pipeline code): download, read the roster, stream every price for
     every in-scope provider into data/rates/<name>.parquet and data/membership/<name>.parquet.
  2. Upload both to R2 data/full/rates/ and data/full/membership/.
  3. Double-count fix: if this network's previous table had a different name (insurers date their file
     names), move the old tables to work/retired/<yyyy-mm>/ so a build over data/full/rates/*.parquet
     counts each network once. The old table is only moved after the new one is safely uploaded.
  4. Write a result marker: bot/insurers/runs/<RUN_ID>/done/<slot>.json
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import boto3
import duckdb
import requests

HERE = Path(__file__).resolve().parent
PIPE, DATA = HERE / "pipeline", HERE / "data"
E = {k: "".join(os.environ.get(k, "").split()) for k in
     ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "DIGITALOCEAN_TOKEN", "RUN_ID", "GROUP")}
s3 = boto3.client("s3", endpoint_url=f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                  aws_access_key_id=E["R2_ACCESS_KEY_ID"], aws_secret_access_key=E["R2_SECRET_ACCESS_KEY"],
                  region_name="auto")
B = E["R2_BUCKET"]
RUN = f"bot/insurers/runs/{E['RUN_ID']}/"
SCOPE_KEY = os.environ.get("SCOPE_KEY", "data/2026-09-29/dfw_npis_all.parquet")
LOG = []


def log(*a):
    m = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(m, flush=True)
    LOG.append(m)
    s3.put_object(Bucket=B, Key=RUN + f"log_{E['GROUP']}.txt", Body="\n".join(LOG).encode())


def stem(name):  # same naming as 06f
    for ext in (".gz", ".json", ".zip", ".7z"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name[:180]


def exists(key):
    try:
        s3.head_object(Bucket=B, Key=key)
        return True
    except Exception:
        return False


def move(src, dst):
    s3.copy_object(Bucket=B, Key=dst, CopySource={"Bucket": B, "Key": src})
    s3.delete_object(Bucket=B, Key=src)


def one(job):
    t0 = time.time()
    url, label = job["url"], job["network_label"]
    new = stem(Path(url.split("?")[0]).name)
    res = dict(slot=job["slot"], url=url, table=new, prev_table=job.get("prev_table", ""), ok=False)
    for sub in ("rates", "membership"):
        (DATA / sub / f"{new}.parquet").unlink(missing_ok=True)
    p = subprocess.run([sys.executable, str(PIPE / "06f_full_extract.py"), url, "--network", label,
                        "--scope", str(DATA / "scope.parquet")], cwd=PIPE, capture_output=True, text=True)
    tail = (p.stdout + p.stderr).strip().splitlines()[-6:]
    log(f"[{job['slot']}]", *tail)
    rates, memb = DATA / "rates" / f"{new}.parquet", DATA / "membership" / f"{new}.parquet"
    if p.returncode != 0 or not rates.exists() or not memb.exists():
        res["error"] = " | ".join(tail)[-500:] or f"06f exit {p.returncode}"
        return res
    con = duckdb.connect()
    res["rows"] = con.execute(f"select count(*) from '{rates}'").fetchone()[0]
    res["roster_rows"] = con.execute(f"select count(*) from '{memb}'").fetchone()[0]
    meta = con.execute(f"select any_value(reporting_entity_name), any_value(last_updated_on) from '{memb}'").fetchone()
    res["reporting_entity_name"], res["file_last_updated_on"] = meta[0] or "", meta[1] or ""
    s3.upload_file(str(rates), B, f"data/full/rates/{new}.parquet")
    s3.upload_file(str(memb), B, f"data/full/membership/{new}.parquet")
    rates.unlink(); memb.unlink()
    old = job.get("prev_table", "")
    if old and old != new:
        month = date.today().strftime("%Y-%m")
        for sub in ("rates", "membership"):
            k = f"data/full/{sub}/{old}.parquet"
            if exists(k):
                move(k, f"work/retired/{month}/{sub}/{old}.parquet")
        res["retired"] = old
    res.update(ok=True, secs=round(time.time() - t0))
    return res


def self_delete():
    try:
        did = requests.get("http://169.254.169.254/metadata/v1/id", timeout=5).text.strip()
        requests.delete(f"https://api.digitalocean.com/v2/droplets/{did}",
                        headers={"Authorization": f"Bearer {E['DIGITALOCEAN_TOKEN']}"}, timeout=30)
    except Exception as e:
        log("self-delete failed (the GitHub side deletes leftover machines after 2 days):", e)


def main():
    body = s3.get_object(Bucket=B, Key=RUN + f"{E['GROUP']}.csv")["Body"].read().decode()
    jobs = list(csv.DictReader(io.StringIO(body)))
    (DATA / "rates").mkdir(parents=True, exist_ok=True)
    (DATA / "membership").mkdir(parents=True, exist_ok=True)
    s3.download_file(B, SCOPE_KEY, str(DATA / "scope.parquet"))
    log(f"group {E['GROUP']}: {len(jobs)} files")
    for job in jobs:
        key = RUN + f"done/{job['slot']}.json"
        if exists(key):  # finished before a restart
            continue
        try:
            res = one(job)
        except Exception as e:
            res = dict(slot=job["slot"], url=job["url"], ok=False, error=f"{type(e).__name__}: {str(e)[:400]}")
        res["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        s3.put_object(Bucket=B, Key=key, Body=json.dumps(res).encode())
        log(f"[{job['slot']}] {'ok' if res['ok'] else 'FAILED'} {res.get('rows', '')} rows {res.get('error', '')[:200]}")
    log("ALL DONE")


if __name__ == "__main__":
    try:
        main()
    finally:
        if os.environ.get("NO_SELF_DELETE") != "1":
            self_delete()
