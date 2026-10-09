"""Step 6 (advanced): read an insurer's "in-network rates" file and keep two things for DFW:
   (A) NETWORK MEMBERSHIP - which providers the insurer lists as in-network, and
   (B) NEGOTIATED RATES   - what the insurer pays them for the procedures in codes.csv.

Why both
--------
Insurers (UnitedHealthcare, BCBS of Texas, Aetna, Cigna, Curative, etc.) must publish what
they pay every in-network provider, as huge JSON files listed in a monthly "table of
contents" (index). Each rate file has two parts:

  provider_references  - the roster: "group #123 = these NPIs, billing under this tax ID (TIN)".
                         This is the insurer's own written list of who it has contracts with.
  in_network           - the prices: "for code 29881, group #123 is paid $X".

Step 6 used to read the roster only to find prices, then throw it away. It now also saves
the roster, one row per provider per file. Run it across several insurers and step 6b
turns those rows into a network-overlap map (which DFW providers are in-network with
which insurers).

What one membership row says
----------------------------
  "NPI 1234567890, billing under TIN 75-xxxxxxx, is listed in <insurer>'s <network> file
   dated <last_updated_on>, and has rates there for N billing codes (M of ours)."

  network_label         the network name the insurer gives (schema 2.0 files name it per roster
                        entry); otherwise --network, the step-6a description, or the file name.
  listed_via            reference         roster at the top of the file (the usual case)
                        remote_reference  roster kept in a separate linked file (Cigna does this);
                                          fetched and cached in raw/provider_refs/
                        inline            provider listed right next to a price, with no roster entry
  rate_items            how many priced services in the file point at this provider.
                        0 = on the roster but no prices anywhere in the file. NOTE: some insurers split
                        one network into numbered parts (Curative: 001-of-006) that repeat the full roster
                        but split the prices, so judge "no prices" across all parts (step 6b does this). That is worth
                        flagging: a contract on paper, or leftover roster data.
  our_codes_with_rates  how many of the procedures in codes.csv have a price for this provider.

What it does NOT prove
----------------------
A row means the insurer says there is a contract. It does not prove the provider is still
practicing at that address or taking new patients. Check that later against NPPES and the
insurers' directory APIs.

Which providers get recorded (--scope)
--------------------------------------
  dfw (default)   NPIs in data/dfw_facilities_npi.csv (surgery centers and hospitals)
  <file.csv>      any CSV with an "npi" column, e.g. every Texas doctor pulled from NPPES
  all             every NPI in the file. Very large for UHC/Aetna/BCBS; use for small insurers.
Rates are always kept only for NPIs in scope AND codes in codes.csv.

Outputs (rows from the same source file are replaced when re-run, never duplicated)
-------
  data/network_membership_dfw.csv   (A) one row per NPI + TIN + source file
  data/payer_rates_dfw.csv          (B) one row per NPI + code + price, as before, now with source_url

Run
---
  python pipeline/06_filter_payer_file.py path/to/in-network-file.json.gz
  python pipeline/06_filter_payer_file.py https://insurer.example/in-network.json.gz     # downloads, reads, deletes
  python pipeline/06_filter_payer_file.py data/payer_files_bcbstx.csv                    # every file listed by step 6a
  python pipeline/06_filter_payer_file.py data/payer_files_curative.csv --network "Curative PPO"
  python pipeline/06_filter_payer_file.py FILE --scope data/texas_npis.csv --membership-only
Then:  python pipeline/06b_build_network_overlap.py
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import ijson

from common import DATA, RAW, USER_AGENT, load_codes, open_binary, write_csv

RATE_FIELDS = ["reporting_entity_name", "plan_name", "last_updated_on", "billing_code_type", "billing_code", "plain_name",
               "name", "negotiation_arrangement", "npi", "tin", "tin_business_name", "negotiated_type", "negotiated_rate",
               "setting", "billing_class", "service_code", "expiration_date", "source_file", "source_url"]

MEMBER_FIELDS = ["reporting_entity_name", "reporting_entity_type", "network_label", "plan_names", "plan_ids",
                 "plan_market_types", "last_updated_on", "npi", "provider_name", "facility_type", "tin_type", "tin",
                 "tin_business_name", "listed_via", "rate_items", "our_codes_with_rates", "source_file", "source_url",
                 "checked_on"]

RATES_OUT = DATA / "payer_rates_dfw.csv"
MEMBERS_OUT = DATA / "network_membership_dfw.csv"
REMOTE_CACHE = RAW / "provider_refs"


# ---------------------------------------------------------------- who is in scope

def load_facilities():
    """{npi: (name, facility_type)} from the DFW facility list (used to label rows)."""
    p = DATA / "dfw_facilities_npi.csv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r["npi"].strip(): (r.get("name", ""), r.get("facility_type", "")) for r in csv.DictReader(f)}


def load_scope(scope, facilities):
    """Return a set of NPIs to record, or None meaning 'every NPI in the file'."""
    if scope == "all":
        return None
    if scope == "dfw":
        return set(facilities)
    with open(scope, encoding="utf-8") as f:
        return {r["npi"].strip() for r in csv.DictReader(f) if r.get("npi")}


def in_scope(npi, npis):
    return npis is None or npi in npis


# ---------------------------------------------------------------- reading the file

def header(path):
    """Top-of-file fields. Single-plan files also carry plan_name / plan_id here."""
    keys = ("reporting_entity_name", "reporting_entity_type", "plan_name", "plan_id", "plan_market_type",
            "last_updated_on")
    meta = {}
    with open_binary(path) as fb:
        for prefix, event, value in ijson.parse(fb):
            if prefix in keys and event in ("string", "number"):
                meta[prefix] = str(value)
            if prefix in ("provider_references", "in_network"):
                break
    return meta


def groups_in_scope(groups, npis):
    """Yield (npi, tin_type, tin, business_name) for in-scope NPIs inside a provider_groups list."""
    for g in groups or []:
        tin = g.get("tin") or {}
        for n in g.get("npi") or []:
            n = str(n).strip()
            if n and in_scope(n, npis):
                yield n, tin.get("type", ""), str(tin.get("value", "")), tin.get("business_name", "")


def roster_items(fb):
    """Yield provider_references entries, and STOP reading once the prices section begins.
    On big insurer files the roster is a small slice at the top, so this avoids a second
    full read of a file that can be hundreds of GB uncompressed. If a file puts the roster
    after the prices (allowed but rare), it still works; it just reads to the end."""
    builder, depth, seen = None, 0, False
    for prefix, event, value in ijson.parse(fb, use_float=True):
        if prefix == "in_network" and event == "start_array" and seen:
            return  # roster came first and is done; skip the prices
        if prefix == "provider_references" and event == "end_array":
            return
        if builder is None:
            if prefix == "provider_references.item" and event == "start_map":
                builder, depth, seen = ijson.ObjectBuilder(), 0, True
            else:
                continue
        builder.event(event, value)
        if event in ("start_map", "start_array"):
            depth += 1
        elif event in ("end_map", "end_array"):
            depth -= 1
            if depth == 0:
                yield builder.value
                builder = None


FAST_BIN = Path(__file__).resolve().parent / "mrffilter" / "mrffilter"


def use_fast(path, ctx):
    """Use the Go pre-filter for big files (over 1 GB) when it has been built."""
    if ctx.get("no_fast") or not FAST_BIN.exists():
        return False
    return ctx.get("fast") or os.path.getsize(path) > 1e9


def fast_items(path, codes, refs, npis):
    """Stream in_network items through mrffilter (pipeline/mrffilter, Go). It skips items for
    other billing codes without parsing them and keeps only rates that point at in-scope
    providers, so Python sees a tiny fraction of the file. Trade-off: rate_items in the
    membership output then counts only our codes, not every service in the file."""
    import subprocess
    tmp = Path(tempfile.mkdtemp())
    (tmp / "codes.txt").write_text("\n".join(codes))
    (tmp / "refs.txt").write_text("\n".join(str(k) for k in refs))
    (tmp / "npis.txt").write_text("\n".join(npis) if npis is not None else "")
    print("  using fast pre-filter (rate_items will count our procedure codes only)")
    p = subprocess.Popen([str(FAST_BIN), "-in", str(path), "-codes", str(tmp / "codes.txt"),
                          "-refs", str(tmp / "refs.txt"), "-npis", str(tmp / "npis.txt")],
                         stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        it = json.loads(line, parse_float=float)
        for nr in it.get("negotiated_rates", []):
            nr["provider_references"] = [int(x) if str(x).isdigit() else x for x in nr.get("provider_references") or []]
        yield it
    p.wait()


def fetch_remote_groups(url):
    """Some insurers (Cigna) keep a roster entry in its own small file. Fetch once and cache."""
    import requests

    REMOTE_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.split("?")[0].encode()).hexdigest()[:16]  # signed URLs change; cache on the path
    cached = REMOTE_CACHE / f"{key}.json"
    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
    elif url.startswith("http"):
        r = requests.get(url, timeout=120, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        cached.write_text(json.dumps(data), encoding="utf-8")
    else:  # a local path (handy for testing)
        data = json.loads(Path(url).read_text(encoding="utf-8"))
    return data.get("provider_groups", [])


def network_label_from_name(name):
    """'2026-09-01_Blue-Cross-and-Blue-Shield-of-Texas_BlueChoice-PPO_in-network-rates.json.gz'
    -> 'Blue Cross and Blue Shield of Texas BlueChoice PPO'. Best effort; override with --network."""
    s = re.sub(r"\.(json|gz|zip|7z)$", "", name, flags=re.I)
    s = re.sub(r"\.(json|gz|zip|7z)$", "", s, flags=re.I)
    s = re.sub(r"^\d{4}-\d{2}-\d{2}_?", "", s)
    s = re.sub(r"_?in[-_]?network([-_]rates)?.*$", "", s, flags=re.I)
    return re.sub(r"[-_]+", " ", s).strip()


# ---------------------------------------------------------------- main work

def main(path, ctx):
    """path: local file. ctx: source_url, network, plan info from step 6a, scope, flags."""
    facilities = ctx["facilities"]
    npis = ctx["npis"]
    codes = load_codes()
    meta = header(path)
    source_file = ctx.get("source_file") or Path(path).name
    network = ctx.get("network") or ctx.get("file_description") or network_label_from_name(source_file)
    print(f"{meta.get('reporting_entity_name', '?')} | {network} | updated {meta.get('last_updated_on', '?')}")

    # membership[(npi, tin)] -> row being built
    members = {}

    def note(npi, tin_type, tin, biz, via, nets=()):
        k = (npi, tin)
        if k not in members:
            members[k] = {"npi": npi, "tin_type": tin_type, "tin": tin, "tin_business_name": biz,
                          "via": set(), "nets": set(), "rate_items": 0, "our_codes": set()}
        members[k]["via"].add(via)
        members[k]["nets"].update(n for n in nets if n)
        if biz and not members[k]["tin_business_name"]:
            members[k]["tin_business_name"] = biz
        return k

    # Pass 1: the roster. ref id -> list of member keys.
    refs = {}
    remote_ok = remote_fail = 0
    with open_binary(path) as fb:
        for pr in roster_items(fb):
            gid = pr.get("provider_group_id")
            groups, via = pr.get("provider_groups"), "reference"
            if pr.get("location") and not groups:
                if ctx["skip_remote"]:
                    remote_fail += 1
                    continue
                try:
                    groups, via = fetch_remote_groups(pr["location"]), "remote_reference"
                    remote_ok += 1
                except Exception as e:  # expired link, 403, etc. Count it and keep going.
                    remote_fail += 1
                    if remote_fail <= 3:
                        print(f"  ! could not fetch roster file {pr['location'][:90]}...: {e}")
                    continue
            nets = pr.get("network_name") or []  # schema 2.0: the network(s) this roster entry belongs to
            if isinstance(nets, str):
                nets = [nets]
            keys = [note(n, tt, t, b, via, nets) for n, tt, t, b in groups_in_scope(groups, npis)]
            if keys:
                refs[gid] = keys
    print(f"  roster: {len(refs)} provider groups include an in-scope provider"
          + (f"; {remote_ok} separate roster files read" if remote_ok else "")
          + (f"; {remote_fail} separate roster files NOT read" if remote_fail else ""))

    # Pass 2: every priced service. Count rates per provider (all codes), keep full rates for our codes.
    out = []
    with open_binary(path) as fb:
        t0, n_items = time.time(), 0
        items = fast_items(path, codes, refs, npis) if use_fast(path, ctx) else \
            ijson.items(fb, "in_network.item", use_float=True)
        for item in items:
            n_items += 1
            if n_items % 200000 == 0:
                print(f"    ... {n_items:,} priced services read ({(time.time() - t0) / 60:.0f} min)", flush=True)
            code = str(item.get("billing_code", "")).strip()
            ours = code in codes
            touched = set()
            for nr in item.get("negotiated_rates", []):
                who = []
                for ref in nr.get("provider_references") or []:
                    who += refs.get(ref, [])
                for n, tt, t, b in groups_in_scope(nr.get("provider_groups"), npis):
                    who.append(note(n, tt, t, b, "inline"))
                if not who:
                    continue
                touched.update(who)
                if not ours or ctx["membership_only"]:
                    continue
                for price in nr.get("negotiated_prices", []):
                    for k in who:
                        m = members[k]
                        out.append({**meta, "billing_code_type": item.get("billing_code_type", ""),
                                    "billing_code": code, "plain_name": codes[code], "name": item.get("name", ""),
                                    "negotiation_arrangement": item.get("negotiation_arrangement", ""),
                                    "npi": m["npi"], "tin": m["tin"], "tin_business_name": m["tin_business_name"],
                                    "negotiated_type": price.get("negotiated_type", ""),
                                    "negotiated_rate": price.get("negotiated_rate", ""),
                                    "setting": price.get("setting", ""), "billing_class": price.get("billing_class", ""),
                                    "service_code": ",".join(price.get("service_code") or []),
                                    "expiration_date": price.get("expiration_date", ""),
                                    "source_file": source_file, "source_url": ctx.get("source_url", "")})
            for k in touched:
                members[k]["rate_items"] += 1
                if ours:
                    members[k]["our_codes"].add(code)

    # Membership rows
    today = date.today().isoformat()
    mrows = []
    for m in members.values():
        name, ftype = facilities.get(m["npi"], ("", ""))
        mrows.append({"reporting_entity_name": meta.get("reporting_entity_name", ""),
                      "reporting_entity_type": meta.get("reporting_entity_type", ""),
                      "network_label": " | ".join(sorted(m["nets"])) or network,
                      "plan_names": ctx.get("plan_names") or meta.get("plan_name", ""),
                      "plan_ids": ctx.get("plan_ids") or meta.get("plan_id", ""),
                      "plan_market_types": ctx.get("plan_market_types") or meta.get("plan_market_type", ""),
                      "last_updated_on": meta.get("last_updated_on", ""),
                      "npi": m["npi"], "provider_name": name, "facility_type": ftype,
                      "tin_type": m["tin_type"], "tin": m["tin"], "tin_business_name": m["tin_business_name"],
                      "listed_via": ",".join(sorted(m["via"])), "rate_items": m["rate_items"],
                      "our_codes_with_rates": len(m["our_codes"]),
                      "source_file": source_file, "source_url": ctx.get("source_url", ""), "checked_on": today})
    mrows.sort(key=lambda r: (r["npi"], r["tin"]))
    n_npi = len({r["npi"] for r in mrows})
    no_rates = sum(1 for r in mrows if r["rate_items"] == 0)
    replace_rows(MEMBERS_OUT, mrows, MEMBER_FIELDS, source_file)
    print(f"  membership: {n_npi} in-scope providers listed ({len(mrows)} NPI+TIN rows); "
          f"{no_rates} rows are on the roster with no prices")

    if not ctx["membership_only"]:
        total = replace_rows(RATES_OUT, out, RATE_FIELDS, source_file)
        print(f"  rates: kept {len(out)} rows for our codes. {RATES_OUT.name} now has {total} rows.")


def replace_rows(dest, new_rows, fields, source_file):
    """Drop earlier rows from this same source file, append the new ones, write. Returns total rows."""
    existing = []
    if dest.exists():
        with open(dest, encoding="utf-8") as f:
            existing = [r for r in csv.DictReader(f) if r.get("source_file") != source_file]
    write_csv(dest, existing + new_rows, fields)
    return len(existing) + len(new_rows)


# ---------------------------------------------------------------- inputs: file, URL, or a step-6a list

def run(source, ctx):
    import requests

    if source.endswith(".csv"):
        with open(source, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("file_url")]
        print(f"{len(rows)} files listed in {source}")
        for r in rows:
            sub = {**ctx, "plan_names": r.get("plan_names", ""), "plan_ids": r.get("plan_ids", ""),
                   "plan_market_types": r.get("plan_market_types", ""),
                   "file_description": ctx.get("network") or r.get("file_description", "")}
            try:
                run(r["file_url"], sub)
            except Exception as e:
                print(f"  ! {r['file_url'][:120]}: {e}")
        return
    if source.startswith("http"):
        name = Path(source.split("?")[0]).name or "payer_file.json"
        tmp = Path(tempfile.gettempdir()) / name
        print(f"Downloading {name} ...")
        with requests.get(source, stream=True, timeout=300, headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        try:
            main(str(tmp), {**ctx, "source_url": source.split("?")[0], "source_file": name})
        finally:
            os.remove(tmp)
        return
    main(source, {**ctx, "source_url": ctx.get("source_url", ""), "source_file": Path(source).name})


def cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="in-network file (path or URL), or a data/payer_files_<insurer>.csv list from step 6a")
    ap.add_argument("--scope", default="dfw", help="dfw (default), all, or a CSV with an npi column")
    ap.add_argument("--network", default="", help="label for the network, e.g. 'BCBSTX Blue Choice PPO'")
    ap.add_argument("--source-url", default="", help="for a local file: the insurer URL it was downloaded from")
    ap.add_argument("--membership-only", action="store_true", help="skip the rates output (faster to write)")
    ap.add_argument("--skip-remote", action="store_true", help="don't fetch roster entries kept in separate files")
    a = ap.parse_args()
    facilities = load_facilities()
    ctx = {"facilities": facilities, "npis": load_scope(a.scope, facilities), "network": a.network,
           "source_url": a.source_url, "membership_only": a.membership_only, "skip_remote": a.skip_remote}
    if a.scope == "all":
        print("Scope: every NPI in the file. This can use a lot of memory on the big insurers.")
    run(a.source, ctx)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cli()
