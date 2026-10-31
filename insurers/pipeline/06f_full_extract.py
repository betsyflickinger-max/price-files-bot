"""Step 6f (full scope): every billing code, every DFW provider, from one insurer in-network file.

Same idea as step 6, sized for the whole market:
  - Providers in scope: every active NPI with a DFW practice address (data/dfw_npis_all.parquet,
    built from the federal NPI Registry file), or any CSV/Parquet with an npi column (--scope).
  - Codes: all of them (or --codes FILE to limit).
  - Pass 1 (Python) reads only the roster at the top of the file and stops when the prices begin.
  - Pass 2 (Go, pipeline/mrfrows) streams the prices once and writes one row per provider x price.
  - Output is one Parquet file per source file, so a re-run replaces just that file:
      data/rates/<source>.parquet        billing code, provider, TIN, rate, type, class, setting, ...
      data/codes/<source>.parquet        each billing code's description, once per code (Oct 13, 2026)
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
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import duckdb

from common import DATA, USER_AGENT, download
import caresource  # noqa: E402

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("s6", HERE / "06_filter_payer_file.py")
s6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s6)
BIN = HERE / "mrfrows" / "mrfrows"
RATES, MEMB, CODES = DATA / "rates", DATA / "membership", DATA / "codes"
FIELDS = DATA / "fields"   # Oct 15, 2026: --fields-only output (see fields_only())


def duck():
    """DuckDB connection that spills to disk instead of running out of memory on billion-row tables
    (Cigna Texas files were killed at the default 80%-of-RAM limit while Python also held the roster)."""
    con = duckdb.connect()
    try:
        ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError):
        ram_gb = 8
    spill = DATA / "duckdb_spill"
    spill.mkdir(parents=True, exist_ok=True)
    con.execute(f"set memory_limit='{max(1, int(ram_gb * 0.4))}GB'")
    con.execute("set threads=2")
    con.execute("set preserve_insertion_order=false")
    con.execute(f"set temp_directory='{spill}'")
    return con


def load_scope(scope):
    p = Path(scope)
    if p.suffix == ".parquet":
        return {r[0] for r in duckdb.sql(f"select npi from '{p}'").fetchall()}
    with open(p, encoding="utf-8") as f:
        return {r["npi"].strip() for r in csv.DictReader(f) if r.get("npi")}


COLS = ["billing_code_type", "billing_code", "name", "negotiation_arrangement", "npi", "tin", "tin_type",
        "negotiated_type", "negotiated_rate", "billing_class", "setting", "service_code", "billing_code_modifier",
        "expiration_date",
        # kept since Oct 13, 2026 (appended; the site build doesn't use them yet)
        "billing_code_type_version", "description", "additional_information", "bundled_codes", "covered_services"]


# Oct 31, 2026 (Betsy: "store numbers as numbers", option A = new tables only): negotiated_rate is written as a number
# (DOUBLE) instead of text. It is DuckDB's own try_cast of the text, the same cast every reader already applies
# (try_cast(negotiated_rate as double)), so every build gives identical results from old (text) and new tables.
# Nothing is lost: negotiated_rate_text keeps the insurer's original text whenever the number can't stand in for it
# (not a number, e.g. "N/A", or more than 15 digits, past what a DOUBLE holds exactly); otherwise it is empty.
RATE_SQL = """select * replace (try_cast(negotiated_rate as double) as negotiated_rate),
  case when coalesce(negotiated_rate, '') <> '' and (try_cast(negotiated_rate as double) is null
            or length(regexp_replace(negotiated_rate, '[^0-9]', '', 'g')) > 15)
       then negotiated_rate end as negotiated_rate_text
  from t"""


def typed_rates(tbl, con):
    """One batch of mrfrows text columns -> the same batch with negotiated_rate as a number (+ negotiated_rate_text)."""
    import pyarrow as pa
    con.register("t", tbl)
    try:
        out = con.execute(RATE_SQL).arrow()
    finally:
        con.unregister("t")
    schema = pa.schema([(f.name, pa.float64() if f.name == "negotiated_rate" else pa.string()) for f in tbl.schema]
                       + [("negotiated_rate_text", pa.string())])
    return out.select(schema.names).cast(schema)


def stream_to_parquet(cmd, dest, consts):
    """Run mrfrows with CSV on stdout and write Parquet batch by batch: no big temporary file."""
    import pyarrow as pa
    import pyarrow.csv as pcsv
    import pyarrow.parquet as pq
    import queue
    import threading
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    reader = pcsv.open_csv(p.stdout, read_options=pcsv.ReadOptions(block_size=16 << 20),
                           # names like BCBSTX's sometimes contain a line break (quoted by mrfrows)
                           parse_options=pcsv.ParseOptions(newlines_in_values=True),
                           convert_options=pcsv.ConvertOptions(column_types={c: pa.string() for c in COLS},
                                                               strings_can_be_null=False))
    tmpdest = Path(str(dest) + ".part")
    # Parquet compression runs on its own thread so it overlaps with CSV parsing (pyarrow releases the GIL).
    q, err, state = queue.Queue(maxsize=4), [], {"writer": None}
    descs = {}  # (billing_code_type, billing_code) -> description

    def write_loop():
        try:
            while (tbl := q.get()) is not None:
                if state["writer"] is None:
                    state["writer"] = pq.ParquetWriter(tmpdest, tbl.schema, compression="zstd")
                state["writer"].write_table(tbl)
        except Exception as e:  # keep draining so the reader never blocks
            err.append(e)
            while q.get() is not None:
                pass

    th = threading.Thread(target=write_loop, daemon=True)
    th.start()
    # The six per-file values are the same on every row: build them with pa.repeat, not a Python list per batch.
    const = lambda k, n: pa.repeat(pa.scalar(str(consts[k] or ""), pa.string()), n)
    rcon = duckdb.connect()   # numbers as numbers (typed_rates): same DuckDB cast the readers use
    rcon.execute("set threads=2")   # small batches; leave the cores to mrfrows and the Parquet writer
    try:
        for batch in reader:
            if err:
                break
            n = batch.num_rows
            tbl = pa.Table.from_batches([batch]).cast(pa.schema([(f.name, pa.string()) for f in batch.schema]))
            for i, k in enumerate(("reporting_entity_name", "file_label", "last_updated_on")):
                tbl = tbl.add_column(i, k, const(k, n))
            for k in ("source_file", "source_url", "checked_on"):
                tbl = tbl.append_column(k, const(k, n))
            if "description" in tbl.column_names:  # one row per code in data/codes/, not repeated on every price row (+18% size)
                for r in tbl.select(["billing_code_type", "billing_code", "description"]).group_by(
                        ["billing_code_type", "billing_code", "description"]).aggregate([]).to_pylist():
                    descs.setdefault((r["billing_code_type"], r["billing_code"]), r["description"])
                tbl = tbl.drop_columns(["description"])
            q.put(typed_rates(tbl, rcon))
    finally:
        q.put(None)
        th.join()
    if err:
        p.kill()
        raise err[0]
    if p.wait() != 0:
        raise RuntimeError("mrfrows failed")
    if state["writer"]:
        state["writer"].close()
        tmpdest.replace(dest)
    else:  # Oct 15, 2026: a file with no prices for anyone in this area (e.g. a Houston-only network read for DFW) gets an
        # empty table with the usual columns, so it is recorded as read with 0 rows instead of failing three times
        names = ["reporting_entity_name", "file_label", "last_updated_on"] + [c for c in COLS if c != "description"] + \
                ["source_file", "source_url", "checked_on"]
        names.append("negotiated_rate_text")   # Oct 31, 2026: same columns and types as a table with prices
        pq.write_table(pa.table({c: pa.array([], pa.float64() if c == "negotiated_rate" else pa.string()) for c in names}),
                       dest, compression="zstd")
    codes = CODES / Path(dest).name
    codes.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"billing_code_type": [k[0] for k in descs], "billing_code": [k[1] for k in descs],
                             "description": list(descs.values()), "source_file": [str(consts["source_file"] or "")] * len(descs),
                             "checked_on": [str(consts["checked_on"])] * len(descs)}), codes, compression="zstd")


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
    with open(tmp / "refmap.tsv", "w") as rm:
        for pr in s6.roster_items_any(path):   # Oct 24: falls back to a parser for very long ids (Mountain Health)
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
    if ctx.get("fields_only"):
        return fields_only(path, ctx, meta, label, source_file, tmp)

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
    con = duck()
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
    # An inventory problem must never cost us the tables. Oct 29, 2026: the inventory now runs in its own process.
    # UHC Choice Plus (18 billion rows) and Anthem California (11 billion) for NY/CA were read and written, then the
    # in-process inventory ran out of memory and the kill (exit -9) took the finished tables with it, three times each.
    # If the child is killed, it is tried once more with less memory; if that fails too, only the inventory is skipped.
    con.close()
    args = (RATES / (st + ".parquet"), CENSUS / (st + ".json"), meta, label, source_file, ctx.get("source_url", ""),
            n_rows, n_refs, len({k[0] for k in members}))
    import multiprocessing as mp
    for low in (False, True):
        ch = mp.get_context("fork").Process(target=_census_child, args=(args, low))
        ch.start()
        ch.join()
        if ch.exitcode == 0:
            break
        print(f"  ! census {'skipped' if low else 'failed'} (exit {ch.exitcode}{', retrying with less memory' if not low else ''})", flush=True)


def _census_child(args, low):
    try:
        con = duck()
        if low:
            con.execute("set threads=1")
            con.execute("set memory_limit='" + str(max(1, int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30 * 0.25))) + "GB'")
        census(con, *args)
    except Exception as e:
        print(f"  ! census skipped: {e}", flush=True)


CENSUS, ENDS = DATA / "census", DATA / "ends"


def fields_only(path, ctx, meta, label, source_file, tmp):
    """Oct 15, 2026: for an unchanged file whose tables came from an older reader, write only the fields the reader has
    kept since Oct 13 to data/fields/<source>.parquet, without re-writing billions of price rows:
      rec='item'  billing code version, description, bundled_codes, covered_services, once per in-network item
      rec='note'  additional_information, once per price x provider group (TIN), only where the file has one
    Same items and providers in scope as a full read; the rates/membership tables are left as they are."""
    st = stem(source_file)
    cmd = [str(BIN), "-in", str(path), "-refmap", str(tmp / "refmap.tsv"), "-npis", str(tmp / "npis.txt"),
           "-out", str(tmp / "fields.csv.gz"), "-fields"]
    if ctx.get("codes"):
        cmd += ["-codes", ctx["codes"]]
    subprocess.run(cmd, check=True)
    FIELDS.mkdir(parents=True, exist_ok=True)
    con = duck()
    lit = lambda s: "'" + str(s or "").replace("'", "''") + "'"
    dest = FIELDS / (st + ".parquet")
    con.execute(f"""copy (select distinct {lit(meta.get('reporting_entity_name'))} as reporting_entity_name, {lit(label)} as file_label,
          {lit(meta.get('last_updated_on'))} as last_updated_on, coalesce(columns(*), ''), {lit(source_file)} as source_file,
          {lit(ctx.get('source_url'))} as source_url, {lit(date.today().isoformat())} as checked_on
        from read_csv('{tmp / 'fields.csv.gz'}', header=true, all_varchar=true, quote='"', escape='"', delim=',')
    ) to '{dest}' (format parquet, compression zstd)""")
    n = dict(con.execute(f"select rec, count(*) from '{dest}' group by 1").fetchall())
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    print(f"  fields only: {n.get('item', 0):,} items, {n.get('note', 0):,} price notes -> fields/{st}.parquet", flush=True)


def census(con, rates, dest, meta, label, source_file, source_url, n_rows, n_groups, n_npis):
    """'What's in the file' inventory + per-provider contract end dates (pipeline/inventory.py).
    Real counts, no sampling; research only, never shown as a price."""
    import inventory
    t = f"read_parquet('{rates}')"
    base = {"source_file": source_file, "source_url": source_url, "network": label, "checked_on": date.today().isoformat(),
            "header": {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))},
            "roster_provider_groups_in_scope": n_groups, "roster_npis_in_scope": n_npis}
    out = inventory.census(con, t, base)
    memb = MEMB / Path(rates).name
    if memb.exists():
        out.update(inventory.roster(con, f"read_parquet('{memb}')"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, default=str))
    ENDS.mkdir(parents=True, exist_ok=True)
    inventory.contract_ends(con, t, ENDS / Path(rates).name)
    print(f"  census + contract end dates -> {dest.stem}", flush=True)


def membership_only(path, ctx):
    """Rebuild data/membership/<source>.parquet from the roster plus the existing rates Parquet."""
    meta = s6.header(path)
    source_file = ctx.get("source_file") or Path(path).name
    label = ctx.get("network") or s6.network_label_from_name(source_file)
    st = stem(source_file)
    members = {}
    if True:
        for pr in s6.roster_items_any(path):
            nets = pr.get("network_name") or []
            nets = [nets] if isinstance(nets, str) else nets
            for n, tt, t, biz in s6.groups_in_scope(pr.get("provider_groups"), ctx["npis"]):
                m = members.setdefault((n, t.replace("-", "")), {"tin_type": tt, "biz": biz, "nets": set()})
                m["nets"].update(x for x in nets if x)
    con = duck()
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
        last = None
        for attempt in range(5):
            try:
                download(source, tmp)   # Oct 22, 2026: host swap, browser user agent, certificate chain (common.py)
                break
            except Exception as e:
                last = e
                print(f"  retry {attempt + 1}: {e}", flush=True)
                # Oct 29, 2026: some hosts (Geisinger's mrf.thehealthplan.com) block for a few minutes when many of our
                # machines ask at once. Five back-to-back tries all landed inside one block; now they wait 0.5-8 minutes.
                if attempt < 4:
                    time.sleep(30 * 2 ** attempt)
        if not tmp.exists():   # was a confusing "No such file or directory: dl_..." from the reader
            raise RuntimeError(f"download failed after 5 tries (last: {str(last)[:300]})")
        cms = None
        try:
            src = str(tmp)
            if caresource.is_caresource(source):   # Oct 29, 2026: CareSource's own layout -> CMS layout first
                cms = DATA / ("cs_" + stem(name) + ".json.gz")
                src = str(caresource.convert(str(tmp), str(cms)))
                tmp.unlink(missing_ok=True)
            extract(src, {**ctx, "source_url": source.split("?")[0], "source_file": name})
        finally:
            tmp.unlink(missing_ok=True)
            if cms:
                cms.unlink(missing_ok=True)
        return
    extract(source, {**ctx, "source_file": Path(source).name})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--scope", default=str(DATA / "dfw_npis_all.parquet"))
    ap.add_argument("--codes", default="", help="file of billing codes (default: all)")
    ap.add_argument("--network", default="")
    ap.add_argument("--source-url", default="")
    ap.add_argument("--fields-only", action="store_true", help="write only data/fields/<source>.parquet (see fields_only())")
    a = ap.parse_args()
    codes = ""
    if a.codes:
        cs = [r["code"] for r in csv.DictReader(open(a.codes))] if a.codes.endswith(".csv") else open(a.codes).read().split()
        codes = str(Path(tempfile.mkdtemp()) / "codes.txt")
        Path(codes).write_text("\n".join(cs))
    run(a.source, {"npis": load_scope(a.scope), "codes": codes, "network": a.network, "source_url": a.source_url,
                   "fields_only": a.fields_only})
