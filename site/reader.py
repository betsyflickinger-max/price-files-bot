"""Rebuild hospital-file prices for the Price Finder's codes, straight from the archived originals in R2.

For each raw/hospitals/*.zst in the R2 manifest:
  download -> decompress to a temp file -> parse (CSV tall/wide or JSON, per CMS 45 CFR 180 schema)
  -> keep only rows whose codes include one of the target codes -> append to out/hospital_rows.csv
Every kept row carries raw_key (the R2 archive), source_url, sha256 and source_row (line number in
the original CSV, or item number in JSON) so it traces to one line of one fingerprinted file.
Safe to re-run: finished archives are listed in out/done.txt and skipped.

Run: python extract_codes.py <codes.csv> <manifest.csv> <out_dir>
"""
import csv, gzip, io, json, os, re, sys, time, zipfile
from pathlib import Path
import ijson, zstandard
client = None

csv.field_size_limit(sys.maxsize)
codes_path, man_path, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
out.mkdir(parents=True, exist_ok=True)
TARGET = {r["code"] for r in csv.DictReader(open(codes_path))}
DRGS = {c for c in TARGET if len(c) <= 3}
CPTS = TARGET - DRGS
LINE_RE = re.compile(r"(?<![0-9])(" + "|".join(sorted(TARGET)) + r")(?![0-9])")

FIELDS = ["raw_key", "source_url", "sha256", "source_row", "hospital_name", "location_name", "last_updated_on",
          "type_2_npi", "hospital_address", "description", "code", "code_type", "all_codes", "modifiers", "setting",
          "billing_class", "gross_charge", "discounted_cash", "min_charge", "max_charge", "payer_name", "plan_name",
          "negotiated_dollar", "negotiated_percentage", "negotiated_algorithm", "methodology", "median_amount",
          "notes", "payer_notes"]


def match(codes):
    """codes: list of (code, type). Return (code, type) of the target code found, else None."""
    for c, t in codes:
        c, t = str(c).strip(), str(t).strip().upper()
        if t in ("CPT", "HCPCS") and c[:5] in CPTS and (len(c) == 5 or not c[5:].isalnum()):
            return c[:5], t
        if t == "MS-DRG" and c.lstrip("0") in DRGS:
            return c.lstrip("0"), t
    return None


def norm(h):
    return [p.strip().lower() for p in str(h).split("|")]


def sniff_open(path):
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if head == b"PK\x03\x04":
        z = zipfile.ZipFile(path)
        inner = max((i for i in z.infolist() if not i.is_dir()), key=lambda i: i.file_size)
        return z.open(inner)
    return open(path, "rb")


class _NoBOM:
    """Wraps a binary stream and drops a leading UTF-8 byte-order mark (some vendors' JSON starts with one)."""
    def __init__(self, f): self.f, self.first = f, True
    def read(self, n=-1):
        b = self.f.read(n)
        if self.first:
            self.first = False
            while b[:3] == b"\xef\xbb\xbf" or b[:1] in (b" ", b"\r", b"\n", b"\t"):
                b = b[3:] if b[:3] == b"\xef\xbb\xbf" else b[1:]
                if not b: b = self.f.read(n)
        return b
    def __enter__(self): return self
    def __exit__(self, *a): self.f.close()


def is_json(path):
    with sniff_open(path) as fb:
        start = fb.read(4096).lstrip(b"\xef\xbb\xbf \r\n\t")
    return start[:1] in (b"{", b"[")


def parse_csv(path):
    f = io.TextIOWrapper(sniff_open(path), encoding="utf-8-sig", errors="replace", newline="")
    pos = {"n": 0, "hdr": False}

    def lines():
        # every line passes until the header row has been read (quoted fields such as the attestation can span
        # several physical lines); after that only lines naming a target code
        for line in f:
            pos["n"] += 1
            if not pos["hdr"] or LINE_RE.search(line):
                yield line
    rdr = csv.reader(lines())
    mk, mv = next(rdr), next(rdr)
    meta = {}
    for k, v in zip(mk, mv):
        key = norm(k)[0]
        if key and key not in meta:
            meta[key] = v.strip()
    header = next(rdr)
    pos["hdr"] = True
    parts = [norm(h) for h in header]
    idx = {"|".join(p): i for i, p in enumerate(parts)}
    base = dict(hospital_name=meta.get("hospital_name", ""), location_name=meta.get("location_name", ""),
                last_updated_on=meta.get("last_updated_on", ""), type_2_npi=meta.get("type_2_npi", ""),
                hospital_address=meta.get("hospital_address", ""))
    code_cols = [(i, idx.get("|".join(p + ["type"]))) for i, p in enumerate(parts) if len(p) == 2 and p[0] == "code"]
    tall = "payer_name" in idx
    payer_cols = {}
    if not tall:
        for i, p in enumerate(parts):
            raw = header[i].split("|")
            if len(p) == 4 and p[0] == "standard_charge":
                payer_cols.setdefault((raw[1].strip(), raw[2].strip()), {})[p[3]] = i
            elif len(p) == 3 and p[0] in ("median_amount", "estimated_amount", "additional_payer_notes"):
                payer_cols.setdefault((raw[1].strip(), raw[2].strip()), {})[p[0]] = i

    def col(row, *names):
        for n in names:
            i = idx.get(n)
            if i is not None and i < len(row) and row[i].strip():
                return row[i].strip()
        return ""

    for row in rdr:
        codes = [(row[ci] if ci < len(row) else "", row[ti] if ti is not None and ti < len(row) else "")
                 for ci, ti in code_cols]
        m = match(codes)
        if not m:
            continue
        common = dict(base, source_row=pos["n"], description=col(row, "description"), code=m[0], code_type=m[1],
                      all_codes=";".join(f"{t}:{c}" for c, t in codes if c), modifiers=col(row, "modifiers"),
                      setting=col(row, "setting"), billing_class=col(row, "billing_class"),
                      gross_charge=col(row, "standard_charge|gross"),
                      discounted_cash=col(row, "standard_charge|discounted_cash"),
                      min_charge=col(row, "standard_charge|min"), max_charge=col(row, "standard_charge|max"),
                      notes=col(row, "additional_generic_notes"))
        if tall:
            yield dict(common, payer_name=col(row, "payer_name"), plan_name=col(row, "plan_name"),
                       negotiated_dollar=col(row, "standard_charge|negotiated_dollar"),
                       negotiated_percentage=col(row, "standard_charge|negotiated_percentage"),
                       negotiated_algorithm=col(row, "standard_charge|negotiated_algorithm"),
                       methodology=col(row, "standard_charge|methodology"), median_amount=col(row, "median_amount"),
                       payer_notes=col(row, "additional_payer_notes"))
        else:
            emitted = False
            for (payer, plan), c in payer_cols.items():
                g = lambda k: row[c[k]].strip() if k in c and c[k] < len(row) else ""
                vals = dict(negotiated_dollar=g("negotiated_dollar"), negotiated_percentage=g("negotiated_percentage"),
                            negotiated_algorithm=g("negotiated_algorithm"), methodology=g("methodology"),
                            median_amount=g("median_amount"), payer_notes=g("additional_payer_notes"))
                if any(vals[k] for k in ("negotiated_dollar", "negotiated_percentage", "negotiated_algorithm")):
                    emitted = True
                    yield dict(common, payer_name=payer.replace("_", " "), plan_name=plan.replace("_", " "), **vals)
            if not emitted:
                yield common


def parse_json(path):
    meta = {}
    with _NoBOM(sniff_open(path)) as fb:
        for prefix, event, value in ijson.parse(fb):
            if prefix in ("hospital_name", "last_updated_on") and event in ("string", "number"):
                meta[prefix] = str(value)
            elif prefix in ("type_2_npi.item", "hospital_address.item", "location_name.item"):
                meta.setdefault(prefix.split(".")[0], []).append(str(value))
            elif prefix == "location_name" and event == "string":
                meta["location_name"] = [value]
            elif prefix == "standard_charge_information":
                break
    j = lambda k: " | ".join(meta.get(k, []))
    base = dict(hospital_name=meta.get("hospital_name", ""), location_name=j("location_name"),
                last_updated_on=meta.get("last_updated_on", ""), type_2_npi=j("type_2_npi"),
                hospital_address=j("hospital_address"))
    with _NoBOM(sniff_open(path)) as fb:
        for n, item in enumerate(ijson.items(fb, "standard_charge_information.item", use_float=True), 1):
            codes = [(c.get("code", ""), c.get("type", "")) for c in item.get("code_information", [])]
            m = match(codes)
            if not m:
                continue
            for sc in item.get("standard_charges", []):
                common = dict(base, source_row=n, description=item.get("description", ""), code=m[0], code_type=m[1],
                              all_codes=";".join(f"{t}:{c}" for c, t in codes), modifiers=";".join(item.get("modifier_information", []) and [str(x.get("code", "")) for x in item.get("modifier_information", [])] or []),
                              setting=sc.get("setting", ""), billing_class=sc.get("billing_class", ""),
                              gross_charge=sc.get("gross_charge", ""), discounted_cash=sc.get("discounted_cash", ""),
                              min_charge=sc.get("minimum", ""), max_charge=sc.get("maximum", ""),
                              notes=sc.get("additional_generic_notes", ""))
                payers = sc.get("payers_information") or []
                if not payers:
                    yield common
                for p in payers:
                    yield dict(common, payer_name=p.get("payer_name", ""), plan_name=p.get("plan_name", ""),
                               negotiated_dollar=p.get("standard_charge_dollar", ""),
                               negotiated_percentage=p.get("standard_charge_percentage", ""),
                               negotiated_algorithm=p.get("standard_charge_algorithm", ""),
                               methodology=p.get("methodology", ""), median_amount=p.get("median_amount", ""),
                               payer_notes=p.get("additional_payer_notes", ""))


def main():
    s3, bucket = client()
    todo = [r for r in csv.DictReader(open(man_path)) if r["key"].startswith("raw/hospitals/")]
    donef = out / "done.txt"
    done = set(donef.read_text().split("\n")) if donef.exists() else set()
    rows_path = out / "hospital_rows.csv"
    new = not rows_path.exists()
    fo = open(rows_path, "a", newline="")
    w = csv.DictWriter(fo, FIELDS, extrasaction="ignore")
    if new:
        w.writeheader()
    work = out / "_work"
    work.mkdir(exist_ok=True)
    if "--reverse" in sys.argv:
        todo = todo[::-1]
    claims = out.parent / "claims.txt"
    for i, r in enumerate(todo, 1):
        key = r["key"]
        taken = set(claims.read_text().split("\n")) if claims.exists() else set()
        for dp in out.parent.glob("out*/done.txt"):
            taken |= set(dp.read_text().split("\n"))
        if key in done or key in taken:
            continue
        with open(claims, "a") as c:
            c.write(key + "\n")
        t0 = time.time()
        zpath, rpath = work / "f.zst", work / "f.raw"
        try:
            s3.download_file(bucket, key, str(zpath))
            with open(zpath, "rb") as fi, open(rpath, "wb") as fr:
                zstandard.ZstdDecompressor().copy_stream(fi, fr)
            zpath.unlink()
            parser = parse_json if is_json(rpath) else parse_csv
            n = 0
            for row in parser(rpath):
                w.writerow(dict(row, raw_key=key, source_url=r["source_url"], sha256=r["sha256"]))
                n += 1
            fo.flush()
            status = f"ok {n} rows"
        except Exception as e:
            status = f"ERROR {type(e).__name__}: {str(e)[:200]}"
        for p in (zpath, rpath):
            if p.exists():
                p.unlink()
        print(f"[{i}/{len(todo)}] {status} {time.time()-t0:.0f}s {key}", flush=True)
        if status.startswith("ok"):
            with open(donef, "a") as d:
                d.write(key + "\n")
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
