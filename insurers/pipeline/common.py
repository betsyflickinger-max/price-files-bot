"""Shared paths and helpers for the Upfront DFW data pipeline (test copy)."""
import csv
import gzip
import io
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = ROOT / "raw"
CODES_FILE = Path(__file__).resolve().parent / "codes.csv"
csv.field_size_limit(sys.maxsize)
USER_AGENT = "UpfrontDFW-research/0.1 (price transparency research)"


def load_codes():
    with open(CODES_FILE, newline="", encoding="utf-8") as f:
        return {r["code"].strip(): r["plain_name"].strip() for r in csv.DictReader(f)}


def open_binary(path):
    path = str(path)
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        inner = [n for n in z.namelist() if not n.endswith("/")][0]
        return z.open(inner)
    return open(path, "rb")


def write_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def to_num(v):
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s == "" or s.lower() in ("n/a", "na", "null", "none"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
