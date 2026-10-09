"""Step 6f (full scope): every billing code, every DFW provider, from one insurer in-network file.

Same idea as step 6, sized for the whole market:
  - Providers in scope: every active NPI with a DFW practice address (data/dfw_npis_all.parquet,
    built from the federal NPI Registry file), or any CSV/Parquet with an npi column (--scope).
  - Codes: all of them (or --codes FILE to limit).
  - Pass 1 (Python) reads only the roster at the top of the file and stops when the prices begin.
  - Pass 2 (Go, pipeline/mrfrows) streams the prices once and writes one row per provider x price.
  - Output is one Parquet file per source file, so a re-run replaces just that file:
      data/rates/<source>.parquet        billing code, provider, TIN, rate, type, class, setting, ...
      data/membership/<source>.parquet   every in-scope NPI+TIN on the roster, its network name(s),
                                         and how many priced rows point at it (0 = listed, no prices)
    Both carry reporting entity, file date, source file, source URL and checked_on.

Run:
  python pipeline/06f_full_extract.py FILE_OR_URL [--network "BCBSTX Blue Choice PPO"] [--source-url URL]
  python pipeline/06f_full_extract.py data/payer_files_bcbstx.csv          # every file in a step-6a list
  python pipeline/06f_full_extract.py FILE --scope data/dfw_facilities_npi.csv --codes pipeline/codes.csv
Query later with DuckDB:  select * from 'data/rates/*.parquet' where billing_code='45378'
"""
import argparse
import csv
import importlib.util
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import duckdb

from common import DATA, USER_AGENT

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("s6", HERE / "06_filter_payer_file.py")
s6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s6)
BIN = HERE / "mrfrows" / "mrfrows"
RATES, MEMB = DATA / "rates", DATA / "membership"


def load_scope(scope):
    p = Path(scope)
    if p.suffix == ".parquet":
        return {r[0] for r in duckdb.sql(f"select npi from '{p}'").fetchall()}
    with open(p, encoding="utf-8") as f:
        return {r["npi"].strip() for r in csv.DictReader(f) if r.get("npi")}


COLS = ["billing_code_type", "billing_code", "name", "negotiation_arrangement", "npi", "tin", "tin_type",
        "negotiated_type", "negotiated_rate", "billing_class", "setting", "service_code", "billing_code_modifier",
        "expiration_date"]


def stream_to_parquet(cmd, dest, consts):
    """Run mrfrows with CSV on stdout and write Parquet batch by batch: no big temporary file."""
    import pyarrow as pa
    import pyarrow.csv as pcsv
    import pyarrow.parquet as pq
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    reader = pcsv.open_csv(p.stdout, read_options=pcsv.ReadOptions(block_size=16 << 20),
                           convert_options=pcsv.ConvertOptions(column_types={c: pa.string() for c in COLS},
                                                               strings_can_be_null=False))
    writer = None
    tmpdest = Path(str(dest) + ".part")
    for batch in reader:
        n = batch.num_rows
        cols = {k: pa.array([str(consts[k] or "")] * n, pa.string()) for k in ("reporting_entity_name", "file_label", "last_updated_on")}
        tbl = pa.Table.from_batches([batch]).cast(pa.schema([(f.name, pa.string()) for f in batch.schema]))
        for i, (k, v) in enumerate(cols.items()):
            tbl = tbl.add_column(i, k, v)
        for k in ("source_file", "source_url", "checked_on"):
            tbl = tbl.append_column(k, pa.array([str(consts[k] or "")] * n, pa.string()))
        if writer is None:
            writer = pq.ParquetWriter(tmpdest, tbl.schema, compression="zstd")
        writer.write_table(tbl)
    if p.wait() != 0:
        raise RuntimeError("mrfrows failed")
    if writer:
        writer.close()
        tmpdest.replace(dest)


def stem(name):
    for ext in (".gz", ".json", ".zip", ".7z"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name[:180]


def extract(path, ctx):
    meta = s6.header(path)
    source_file = ctx.get("source_file") or Path(path).name
    label = ctx.get("network") or s6.network_label_from_name(source_file)
    npis = ctx["npis"]
    print(f"{meta.get('reporting_entity_name', '?')} | {label} | updated {meta.get('last_updated_on', '?')}", flush=True)

    tmp = Path(tempfile.mkdtemp(dir=DATA))
    members = {}
    n_refs = 0
    with open(tmp / "refmap.tsv", "w") as rm, s6.open_binary(path) as fb:
        for pr in s6.roster_items(fb):
            groups = pr.get("provider_groups")
            if pr.get("location") and not groups:
                try:
                    groups = s6.fetch_remote_groups(pr["location"])
                except Exception:
                    continue
            nets = pr.get("network_name") or []
            nets = [nets] if isinstance(nets, str) else nets
            hit = False
            for n, tt, t, biz in s6.groups_in_scope(groups, npis):
                t = t.replace("-", "")
                rm.write(f"{pr.get('provider_group_id')}\t{n}\t{t}\t{tt}\n")
                m = members.setdefault((n, t), {"tin_type": tt, "biz": biz, "nets": set()})
                m["nets"].update(x for x in nets if x)
                hit = True
            n_refs += hit
    print(f"  roster: {n_refs:,} provider groups include in-scope providers; {len({k[0] for k in members}):,} NPIs", flush=True)
    (tmp / "npis.txt").write_text("\n".join(npis))

    RATES.mkdir(parents=True, exist_ok=True)
    MEMB.mkdir(parents=True, exist_ok=True)
    st = stem(source_file)
    today = date.today().isoformat()
    cmd = [str(BIN), "-in", str(path), "-refmap", str(tmp / "refmap.tsv"), "-npis", str(tmp / "npis.txt"), "-out", "-"]
    if ctx.get("codes"):
        cmd += ["-codes", ctx["codes"]]
    stream_to_parquet(cmd, RATES / (st + ".parquet"), {
        "reporting_entity_name": meta.get("reporting_entity_name", ""), "file_label": label,
        "last_updated_on": meta.get("last_updated_on", ""), "source_file": source_file,
        "source_url": ctx.get("source_url", ""), "checked_on": today})
    con = duckdb.connect()
    lit = lambda s: "'" + str(s or "").replace("'", "''") + "'"
    n_rows = con.execute(f"select count(*) from '{RATES / (st + '.parquet')}'").fetchone()[0]

    # Membership with priced-row counts
    counts = dict(((r[0], r[1]), r[2]) for r in con.execute(
        f"select npi, tin, count(*) from '{RATES / (st + '.parquet')}' group by 1,2").fetchall())
    with open(tmp / "memb.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["npi", "tin", "tin_type", "tin_business_name", "network_name", "priced_rows"])
        for (n, t), m in members.items():
            w.writerow([n, t, m["tin_type"], m["biz"], " | ".join(sorted(m["nets"])) or label, counts.get((n, t), 0)])
        for (n, t), c in counts.items():  # listed only inline next to a price
            if (n, t) not in members:
                w.writerow([n, t, "", "", label, c])
    con.execute(f"""copy (
        select {lit(meta.get('reporting_entity_name'))} as reporting_entity_name, {lit(meta.get('last_updated_on'))} as last_updated_on,
               *, {lit(source_file)} as source_file, {lit(ctx.get('source_url'))} as source_url, {lit(today)} as checked_on
        from read_csv('{tmp / 'memb.csv'}', header=true, all_varchar=true, quote='"', escape='"', delim=',')
    ) to '{MEMB / (st + '.parquet')}' (format parquet, compression zstd)""")
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    print(f"  wrote {n_rows:,} price rows and {len(members):,} roster rows -> {st}.parquet", flush=True)


def membership_only(path, ctx):
    """Rebuild data/membership/<source>.parquet from the roster plus the existing rates Parquet."""
    meta = s6.header(path)
    source_file = ctx.get("source_file") or Path(path).name
    label = ctx.get("network") or s6.network_label_from_name(source_file)
    st = stem(source_file)
    members = {}
    with s6.open_binary(path) as fb:
        for pr in s6.roster_items(fb):
            nets = pr.get("network_name") or []
            nets = [nets] if isinstance(nets, str) else nets
            for n, tt, t, biz in s6.groups_in_scope(pr.get("provider_groups"), ctx["npis"]):
                m = members.setdefault((n, t.replace("-", "")), {"tin_type": tt, "biz": biz, "nets": set()})
                m["nets"].update(x for x in nets if x)
    con = duckdb.connect()
    counts = dict(((r[0], r[1]), r[2]) for r in con.execute(
        f"select npi, tin, count(*) from '{RATES / (st + '.parquet')}' group by 1,2").fetchall())
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = [(n, t, m["tin_type"], m["biz"], " | ".join(sorted(m["nets"])) or label, counts.get((n, t), 0))
            for (n, t), m in members.items()]
    rows += [(n, t, "", "", label, c) for (n, t), c in counts.items() if (n, t) not in members]
    cols = list(zip(*rows)) if rows else [[]] * 6
    tbl = pa.table({"reporting_entity_name": [meta.get("reporting_entity_name", "")] * len(rows),
                    "last_updated_on": [meta.get("last_updated_on", "")] * len(rows),
                    "npi": list(cols[0]), "tin": list(cols[1]), "tin_type": list(cols[2]), "tin_business_name": list(cols[3]),
                    "network_name": list(cols[4]), "priced_rows": [str(x) for x in cols[5]],
                    "source_file": [source_file] * len(rows), "source_url": [ctx.get("source_url", "")] * len(rows),
                    "checked_on": [date.today().isoformat()] * len(rows)})
    MEMB.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, MEMB / (st + ".parquet"), compression="zstd")
    print(f"  membership rebuilt: {len(rows):,} rows -> {st}.parquet", flush=True)


def run(source, ctx):
    import requests
    if source.endswith(".csv"):
        with open(source, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("file_url")]
        for r in rows:
            try:
                run(r["file_url"], {**ctx, "network": ctx.get("network") or r.get("file_description", "")})
            except Exception as e:
                print(f"  ! {r['file_url'][:120]}: {e}", flush=True)
        return
    if source.startswith("http"):
        name = Path(source.split("?")[0]).name
        tmp = DATA / ("dl_" + name)
        print(f"Downloading {name} ...", flush=True)
        for attempt in range(5):
            try:
                with requests.get(source, stream=True, timeout=300, headers={"User-Agent": USER_AGENT}) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(8 << 20):
                            f.write(chunk)
                break
            except Exception as e:
                print(f"  retry {attempt + 1}: {e}", flush=True)
        try:
            extract(str(tmp), {**ctx, "source_url": source.split("?")[0], "source_file": name})
        finally:
            tmp.unlink(missing_ok=True)
        return
    extract(source, {**ctx, "source_file": Path(source).name})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--scope", default=str(DATA / "dfw_npis_all.parquet"))
    ap.add_argument("--codes", default="", help="file of billing codes (default: all)")
    ap.add_argument("--network", default="")
    ap.add_argument("--source-url", default="")
    a = ap.parse_args()
    codes = ""
    if a.codes:
        cs = [r["code"] for r in csv.DictReader(open(a.codes))] if a.codes.endswith(".csv") else open(a.codes).read().split()
        codes = str(Path(tempfile.mkdtemp()) / "codes.txt")
        Path(codes).write_text("\n".join(cs))
    run(a.source, {"npis": load_scope(a.scope), "codes": codes, "network": a.network, "source_url": a.source_url})
