"""Build the provider list ("scope") for a state from the federal NPI registry (NPPES) monthly file.

Same columns as data/2026-09-29/dfw_npis_all.parquet, so 06f reads it the same way:
  npi, entity_type, name, credential, taxonomy_1, address, city, zip5, phone, deactivated
Keeps every NPI whose practice location is in the state. Streams the ~1 GB zip without unpacking it.

  python insurers/build_scope.py TX            -> R2 data/scope/tx_npis_all.parquet
"""
import csv
import io
import re
import sys
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import store  # noqa: E402

INDEX = "https://download.cms.gov/nppes/NPI_Files.html"
COLS = dict(npi="NPI", entity_type="Entity Type Code", org="Provider Organization Name (Legal Business Name)",
            last="Provider Last Name (Legal Name)", first="Provider First Name", credential="Provider Credential Text",
            taxonomy_1="Healthcare Provider Taxonomy Code_1",
            address="Provider First Line Business Practice Location Address",
            city="Provider Business Practice Location Address City Name",
            state="Provider Business Practice Location Address State Name",
            zip="Provider Business Practice Location Address Postal Code",
            phone="Provider Business Practice Location Address Telephone Number",
            deactivated="NPI Deactivation Date")


def main():
    st = sys.argv[1].upper()
    page = requests.get(INDEX, timeout=60).text
    name = re.findall(r"NPPES_Data_Dissemination_[A-Za-z]+_\d{4}_V2\.zip", page)[0]
    url = f"https://download.cms.gov/nppes/{name}"
    zp = Path("nppes.zip")
    print("downloading", url, flush=True)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(zp, "wb") as f:
            for ch in r.iter_content(16 << 20):
                f.write(ch)
    z = zipfile.ZipFile(zp)
    main_csv = next(n for n in z.namelist() if re.match(r"npidata_pfile_\d+-\d+\.csv$", n))
    rows = {k: [] for k in ("npi", "entity_type", "name", "credential", "taxonomy_1", "address", "city", "zip5", "phone", "deactivated")}
    seen = kept = 0
    with z.open(main_csv) as fb:
        rd = csv.reader(io.TextIOWrapper(fb, encoding="utf-8", errors="replace", newline=""))
        hdr = next(rd)
        ix = {k: hdr.index(v) for k, v in COLS.items()}
        for row in rd:
            seen += 1
            if row[ix["state"]].strip().upper() != st:
                continue
            kept += 1
            org = row[ix["org"]].strip()
            rows["npi"].append(row[ix["npi"]])
            rows["entity_type"].append(row[ix["entity_type"]])
            rows["name"].append(org or f"{row[ix['first']].strip()} {row[ix['last']].strip()}".strip())
            rows["credential"].append(row[ix["credential"]].replace(".", "").strip() or None)
            rows["taxonomy_1"].append(row[ix["taxonomy_1"]] or None)
            rows["address"].append(row[ix["address"]])
            rows["city"].append(row[ix["city"]])
            rows["zip5"].append(row[ix["zip"]][:5])
            rows["phone"].append(row[ix["phone"]] or None)
            rows["deactivated"].append(row[ix["deactivated"]] or None)
            if seen % 1_000_000 == 0:
                print(f"  {seen:,} read, {kept:,} in {st}", flush=True)
    out = Path(f"{st.lower()}_npis_all.parquet")
    pq.write_table(pa.table({k: pa.array(v, pa.string()) for k, v in rows.items()}), out, compression="zstd")
    s3, b = store.r2()
    key = f"data/scope/{out.name}"
    s3.upload_file(str(out), b, key)
    s3.put_object(Bucket=b, Key=key + ".source.txt", Body=f"{url}\n{seen} NPIs read, {kept} in {st}\n".encode())
    print(f"done: {kept:,} {st} providers of {seen:,} -> R2 {key} (from {name})")


if __name__ == "__main__":
    main()
