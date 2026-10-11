"""Every-code extraction of DFW hospital price files (monthly site build; first run Oct 10, 2026). For each job: download raw .zst from R2,
decompress (strip BOM), parse with reader.py (CMS 45 CFR 180 CSV tall/wide + JSON) keeping EVERY CPT/HCPCS/MS-DRG
row, write rows to parquet and upload to R2 data/2026-10-10/hosp_every/rows/<sha16>.parquet. Resumable via done markers."""
import sys, os, re, json, csv, time, hashlib, boto3, zstandard, duckdb
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(f'{HERE}/reader.py').read().split('def main():')[0]
sys.argv = ['x', f'{HERE}/codes89.csv', 'x', f'{HERE}/out']
exec(src)
LINE_RE = re.compile(r'')   # pass every line
CODE5 = re.compile(r'^[0-9A-Z]{5}$')
def match(codes):
    for c, t in codes:
        c, t = str(c).strip().upper(), str(t).strip().upper()
        if t in ('CPT', 'HCPCS') and CODE5.match(c[:5]) and (len(c) == 5 or not c[5:].isalnum()):
            return c[:5], t
        if t in ('MS-DRG', 'MSDRG') and c.lstrip('0').isdigit() and len(c.lstrip('0')) <= 3:
            return c.lstrip('0'), 'MS-DRG'
    return None
E = os.environ
def parse_codecols(path):
    """CSV with columns description, CPT, HCPCS, MS-DRG, ..., payer_name, plan_name, standard_charge, standard_charge_percent
    and no CMS header rows (Children's Health, Sept 2026). Line numbers count the header as line 1."""
    with open(path, encoding='utf-8', errors='replace', newline='') as fh:
        for n, r in enumerate(csv.DictReader(fh), 2):
            code = ct = None
            for col in ('CPT', 'HCPCS'):
                c = (r.get(col) or '').strip().upper()
                if CODE5.match(c[:5]) and (len(c) == 5 or not c[5:].isalnum()): code, ct = c[:5], col; break
            if not code:
                c = (r.get('MS-DRG') or '').strip().lstrip('0')
                if c.isdigit() and len(c) <= 3: code, ct = c, 'MS-DRG'
            if not code: continue
            yield dict(source_row=str(n), description=r.get('description', ''), code=code, code_type=ct, modifiers=r.get('modifiers', ''),
                       setting=r.get('setting', ''), billing_class=r.get('billing_class', ''), gross_charge=r.get('gross_charge', ''),
                       discounted_cash=r.get('discounted_cash', ''), min_charge=r.get('minimum', ''), max_charge=r.get('maximum', ''),
                       payer_name=r.get('payer_name', ''), plan_name=r.get('plan_name', ''), negotiated_dollar=r.get('standard_charge', ''),
                       negotiated_percentage=r.get('standard_charge_percent', ''), negotiated_algorithm=r.get('standard_charge_algorithm', ''),
                       methodology=r.get('methodology', ''), median_amount=r.get('50th_percentile', ''), notes=r.get('additional_generic_notes', ''),
                       payer_notes=r.get('additional_payer_notes', ''), hospital_name=E.get('HNAME_' + os.path.basename(path), ''))
s3 = boto3.client('s3', endpoint_url=f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=E['R2_ACCESS_KEY_ID'], aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name='auto')
B = E['R2_BUCKET']; PFX = E.get('HPFX', 'data/hosp_every/')   # rows/<sha16>.parquet is a cache shared across months
jobs = json.loads(s3.get_object(Bucket=E['R2_BUCKET'], Key=E['JOBS_KEY'])['Body'].read()) if E.get('JOBS_KEY') else json.load(open(E.get('JOBS', f'{HERE}/jobs.json')))
if E.get('SHARDS'): jobs = jobs[int(E.get('SHARD', '0'))::int(E['SHARDS'])]
os.makedirs(f'{HERE}/out', exist_ok=True)
done = {o['Key'].rsplit('/', 1)[1][:-5] for pg in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix=PFX + 'done/') for o in pg.get('Contents', [])}
logf = open(f'{HERE}/out/log.jsonl', 'a')
for k, j in enumerate(jobs, 1):
    tag = j['id']
    if tag in done: continue
    t0 = time.time(); raw = f'{HERE}/out/f.raw'; rec = dict(j)
    try:
        s3.download_file(B, j['key'], raw + '.zst')
        h = hashlib.sha256()
        with open(raw + '.zst', 'rb') as fi, open(raw, 'wb') as fo2:
            r = zstandard.ZstdDecompressor().stream_reader(fi)
            while True:
                ch = r.read(1 << 24)
                if not ch: break
                h.update(ch); fo2.write(ch)
        os.remove(raw + '.zst'); sha = h.hexdigest()
        with open(raw, 'rb') as fh: head = fh.read(3)
        if head == b'\xef\xbb\xbf':
            with open(raw, 'rb') as fi, open(raw + '.nb', 'wb') as fo3:
                fi.seek(3)
                while True:
                    ch = fi.read(1 << 24)
                    if not ch: break
                    fo3.write(ch)
            os.replace(raw + '.nb', raw)
        parser = parse_json if is_json(raw) else parse_csv
        with open(raw, encoding='utf-8', errors='replace') as fh: first = fh.readline()
        cols = [x.strip().strip('"') for x in first.split(',')]
        if not is_json(raw) and 'CPT' in cols and 'payer_name' in cols and 'standard_charge' in cols: parser = parse_codecols
        rows_csv = f'{HERE}/out/rows.csv'; c = 0
        with open(rows_csv, 'w', newline='') as fo:
            w = csv.DictWriter(fo, FIELDS, extrasaction='ignore'); w.writeheader()
            for row in parser(raw):
                w.writerow(dict(row, raw_key=j['key'], source_url=j['url'], sha256=sha, hospital_name=row.get('hospital_name') or j.get('name', ''))); c += 1
        os.remove(raw)
        pq = f'{HERE}/out/{tag}.parquet'
        duckdb.sql(f"copy (select * from read_csv('{rows_csv}', all_varchar=true, header=true, max_line_size=20000000)) to '{pq}' (format parquet, compression zstd)")
        s3.upload_file(pq, B, PFX + f'rows/{tag}.parquet'); os.remove(pq); os.remove(rows_csv)
        s3.put_object(Bucket=B, Key=PFX + f'done/{tag}.done', Body=b'1')
        rec.update(ok=True, rows=c, secs=round(time.time() - t0), sha256=sha)
    except Exception as e:
        rec.update(ok=False, err=f'{type(e).__name__}: {str(e)[:300]}', secs=round(time.time() - t0))
        for p in (raw, raw + '.zst', raw + '.nb'):
            if os.path.exists(p): os.remove(p)
    logf.write(json.dumps(rec) + '\n'); logf.flush()
    s3.upload_file(f'{HERE}/out/log.jsonl', B, PFX + f"log_{E.get('SHARD','0')}.jsonl")
    print(k, len(jobs), rec.get('ok'), rec.get('rows'), rec.get('secs'), j['name'], flush=True)
print('ALL DONE', flush=True)
