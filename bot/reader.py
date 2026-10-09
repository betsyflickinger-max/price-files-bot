"""Hospital price-file reader (CMS 45 CFR 180 schema: CSV tall/wide or JSON).

This is the fixed reader from pipeline/2026-10-09/08_extract_hospital_codes.py, unchanged except that
it is importable: the target codes are loaded with load_codes() instead of from the command line, and
the R2 batch loop (main) is replaced by count_records(), which the bot uses to check every new file.
Fixes kept from Oct 9: skips a leading byte-order mark in JSON; handles line breaks inside quoted CSV fields.

parse_csv / parse_json yield one row per (code, payer/plan) for the target codes only. The bot keeps the
whole original file in R2 and archive.org, so any other code can be extracted later from the originals.
"""
import csv, gzip, io, json, os, re, sys, time, zipfile
from pathlib import Path
import ijson, zstandard

csv.field_size_limit(sys.maxsize)
TARGET, DRGS, CPTS, LINE_RE = set(), set(), set(), re.compile(r"(?!x)x")


def load_codes(codes_path):
    global TARGET, DRGS, CPTS, LINE_RE
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


class _SkipBOM:
    """Wraps a byte stream and drops a leading UTF-8 byte-order mark, which ijson rejects."""
    def __init__(self, fb):
        self.fb, self.first = fb, True
    def read(self, n=-1):
        b = self.fb.read(n)
        if self.first and b:  # ijson probes with read(0) first, so wait for real bytes
            self.first = False
            if b.startswith(b"\xef\xbb\xbf"):
                b = b[3:] or self.fb.read(n)
        return b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        self.fb.close()


def json_open(path):
    return _SkipBOM(sniff_open(path))


def is_json(path):
    with sniff_open(path) as fb:
        start = fb.read(4096).lstrip(b"\xef\xbb\xbf \r\n\t")
    return start[:1] in (b"{", b"[")


def parse_csv(path):
    f = io.TextIOWrapper(sniff_open(path), encoding="utf-8-sig", errors="replace", newline="")
    pos = {"n": 0}

    def lines():
        # Group physical lines into CSV records (a quoted field can contain line breaks), then
        # pass the first 3 records (meta keys, meta values, header) and any later record naming a target code.
        rec, q, nrec = [], 0, 0
        for line in f:
            pos["n"] += 1
            rec.append(line); q += line.count('"')
            if q % 2:
                continue
            text = "".join(rec); rec, q = [], 0; nrec += 1
            if nrec <= 3 or LINE_RE.search(text):
                yield text
        if rec:
            text = "".join(rec); nrec += 1
            if nrec <= 3 or LINE_RE.search(text):
                yield text
    rdr = csv.reader(lines())
    mk, mv = next(rdr), next(rdr)
    meta = {}
    for k, v in zip(mk, mv):
        key = norm(k)[0]
        if key and key not in meta:
            meta[key] = v.strip()
    header = next(rdr)
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
    with json_open(path) as fb:
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
    with json_open(path) as fb:
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


def _csv_records(path):
    """Count CSV records (quote-aware, so a line break inside a quoted field doesn't count twice)."""
    f = io.TextIOWrapper(sniff_open(path), encoding="utf-8-sig", errors="replace", newline="")
    n, q, rec = 0, 0, False
    first = []
    for line in f:
        q += line.count('"'); rec = True
        if q % 2:
            continue
        n += 1; q = 0; rec = False
        if n <= 2:
            first.append(line)
    if rec:
        n += 1
    meta = {}
    if len(first) == 2:
        mk, mv = next(csv.reader([first[0]])), next(csv.reader([first[1]]))
        for k, v in zip(mk, mv):
            key = norm(k)[0]
            if key and key not in meta:
                meta[key] = v.strip()
    return max(n - 3, 0), meta.get("hospital_name", ""), meta.get("last_updated_on", "")


def _json_records(path):
    n, meta = 0, {}
    with json_open(path) as fb:
        for prefix, event, value in ijson.parse(fb):
            if prefix in ("hospital_name", "last_updated_on") and event in ("string", "number"):
                meta[prefix] = str(value)
            elif prefix == "standard_charge_information.item" and event == "start_map":
                n += 1
    return n, meta.get("hospital_name", ""), meta.get("last_updated_on", "")


def count_records(path):
    """Read the whole file once. Returns (format, records, hospital_name, last_updated_on).
    records = data rows after the 3 header rows (CSV) or standard_charge_information items (JSON).
    Raises if the file can't be read, which the bot reports as an unreadable file."""
    if is_json(path):
        return ("json",) + _json_records(path)
    return ("csv",) + _csv_records(path)
