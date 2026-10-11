"""Monthly site build (from the Oct 8, 2026 every-code build): build Price Finder files for EVERY CPT/HCPCS/MS-DRG code in the DFW insurer full tables.
Runs on a rented machine. Same row format and rules as 09r_build_site_v2.py (+ trim.py caps), but:
- reads data/full/rates/*.parquet (all codes) instead of the 89-code slices,
- CPT/HCPCS codes keep leading zeros (anesthesia 00811 etc.); DRGs are ltrimmed ('470'),
- works bucket by bucket (hash(code) % NB) so it never holds everything in memory,
- writes the insurer's own description per code (most common wording) for codes without a plain-English name.
Outputs to R2: TOUT/t/<code>.txt, TOUT/embed.json, TOUT/npis.json, OUTP/log.txt.
Monthly changes: input prefix RATES, provider list NPIS, stage A saved per insurer table (not per list position) in
ACACHE so unchanged tables are reused next month; several machines can run it at once (AORDER/BORDER=desc for
the second one); embed.json is written only when all NB buckets are done."""
import hashlib
import os, sys, csv, json, gzip, base64, time, duckdb, boto3, glob, shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
E = os.environ
s3 = boto3.client('s3', endpoint_url=f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=E['R2_ACCESS_KEY_ID'],
                  aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name='auto')
B = E['R2_BUCKET']; OUTP = E.get('OUTP', 'site/every/2026-10-08/'); D = Path(E.get('WORK', '/d')); NB = int(E.get('NB', '256')); TOUT = E.get('TOUT', 'site/every/2026-10-08/')
LOG = []
def log(*a):
    m = time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a); print(m, flush=True); LOG.append(m)
    if len(LOG) % 3 == 1: s3.put_object(Bucket=B, Key=OUTP + 'log.txt', Body='\n'.join(LOG).encode())
def ls(pfx):
    out = []
    for pg in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix=pfx): out += [(o['Key'], o['Size']) for o in pg.get('Contents', [])]
    return out
for sub in ('xpart', 'stage', 'aux', 'flat', 'names', 'tmp', 'in'): (D / sub).mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
con.execute(f"set enable_progress_bar=false; set memory_limit='{E.get('MEM','12GB')}'; set threads={E.get('THREADS','8')}; set preserve_insertion_order=false; set temp_directory='{D}/tmp'; set partitioned_write_max_open_files=8")
SHORT = """case when reporting_entity_name ilike '%blue cross%' then 'BCBSTX'
 when reporting_entity_name ilike '%uhic%' or reporting_entity_name ilike '%united%' then 'UnitedHealthcare'
 when reporting_entity_name ilike '%aetna%' then 'Aetna' when reporting_entity_name ilike '%cigna%' then 'Cigna'
 when reporting_entity_name ilike '%curative%' then 'Curative' when reporting_entity_name ilike '%baylor%' then 'BSW Health Plan'
 when reporting_entity_name ilike '%centene%' then 'Ambetter' when reporting_entity_name ilike '%oscar%' then 'Oscar'
 when reporting_entity_name ilike '%molina%' then 'Molina' when reporting_entity_name ilike '%wellpoint%' or reporting_entity_name ilike '%elevance%' or reporting_entity_name ilike '%anthem%' or source_file ilike 'TX_JBLX%' then 'Wellpoint'
 else reporting_entity_name end"""
NET = """case when reporting_entity_name ilike '%aetna%' then replace(replace(file_label, 'Aetna ', ''), 'Health Inc. - Texas ', '')
 when reporting_entity_name ilike '%centene%' then 'Texas exchange plans' when reporting_entity_name ilike '%molina%' then 'Marketplace (Texas)'
 when reporting_entity_name ilike '%oscar%' then replace(network_name, 'Individual Texas ', 'Individual ')
 when network_name = 'IFP TEXAS CONNECT' then 'IFP Texas Connect (marketplace)'
 when network_name = 'NATIONAL OAP' then 'Open Access Plus' when network_name = 'PATHWELL OAP' then 'Pathwell OAP'
 when network_name = 'LOCALPLUS' then 'LocalPlus' when network_name ilike 'BCBSTX %' then substr(network_name, 8)
 when source_file ilike 'TX_JBLX%' then 'Texas Essential (marketplace)' else network_name end"""
KEY = """case when billing_code_type ilike '%drg%' then ltrim(trim(billing_code), '0') else upper(trim(billing_code)) end"""
TYPE = """case when billing_code_type ilike '%drg%' then 'MS-DRG' when billing_code_type ilike 'hcpcs%' then 'HCPCS' else 'CPT' end"""

# ---------- Stage A: one pass per insurer file ----------
files = sorted(ls(E.get('RATES', 'data/full/rates/')), key=lambda x: x[1])
if E.get('AORDER') == 'desc': files = files[::-1]
if E.get('ONLY'): files = [f for f in files if E['ONLY'] in f[0]]
log('stage A', len(files), 'files', round(sum(s for _, s in files) / 1e9, 1), 'GB')
WK = E.get('WK', 'work/every/2026-10-08/')
AC = E.get('ACACHE', WK + 'A/')   # stage A results, shared across monthly builds
TAG = {key: 't' + hashlib.sha1(f'{key}|{size}'.encode()).hexdigest()[:12] for key, size in files}   # same table + size -> same results
TAGS = set(TAG.values())
doneA = {k.rsplit('/', 1)[1][:-5] for k, _ in ls(AC) if k.endswith('.done')}
def isdone(tag): 
    try: s3.head_object(Bucket=B, Key=AC + f'{tag}.done'); return True
    except Exception: return False
def mine(fn):  # a cached A file belongs to this build's tables
    return fn.split('_')[0] in TAGS
import math
def save_a(i):
    outs = glob.glob(f'{D}/xpart/bucket=*/{i}.parquet') + [p for p in (f'{D}/flat/{i}.parquet', f'{D}/names/{i}.parquet', f'{D}/aux/{i}.parquet') if os.path.exists(p)]
    for p in outs: s3.upload_file(p, B, AC + os.path.relpath(p, D))
    s3.put_object(Bucket=B, Key=AC + f'{i}.done', Body=b'1')
_LA = None
def restore_a(i):
    global _LA
    if _LA is None: _LA = ls(AC)
    for k, _ in _LA:
        rel = k[len(AC):]
        fn = rel.rsplit('/', 1)[-1][:-8] if rel.endswith('.parquet') else None
        if E.get('NOXPART') and rel.startswith('xpart/'): continue
        if fn is not None and (fn == str(i) or ('_' not in str(i) and fn.startswith(f'{i}_'))):
            dest = D / rel; dest.parent.mkdir(parents=True, exist_ok=True); s3.download_file(B, k, str(dest))
NEWP = [0]
def LOGFLUSH(): s3.put_object(Bucket=B, Key=OUTP + 'log.txt', Body='\n'.join(LOG).encode())
PIECE_GB = float(E.get('PIECE_GB', '0.4'))
for ii, (key, size) in enumerate(files):
    i = TAG[key]
    if E.get('ONLYIDX') and ii != int(E['ONLYIDX']): continue
    if str(i) in doneA or isdone(i):
        restore_a(i); log(f'A {ii+1}/{len(files)} restored from saved work', key.rsplit('/', 1)[1]); continue
    K = max(1, math.ceil(size / (PIECE_GB * 1e9))) if size > float(E.get('MINSPLIT', '0.8e9')) else 1
    loc, locm = D / 'in/r.parquet', D / 'in/m.parquet'
    s3.download_file(B, key, str(loc))
    try: s3.download_file(B, key.replace('/full/rates/', '/full/membership/'), str(locm)); mj = True
    except Exception: mj = False
    total = 0
    for p in range(K):
        tag = f'{i}' if K == 1 else f'{i}_{p}'
        if K > 1 and (tag in doneA or isdone(tag)):
            restore_a(tag); log(f'A {ii+1}/{len(files)} piece {p+1}/{K} restored'); continue
        pf = f"hash(npi) % {K} = {p}" if K > 1 else "true"
        mem = f"(select npi, tin, any_value(network_name) network_name from read_parquet('{locm}') where {pf} group by all)" if mj else "(select null::varchar npi, null::varchar tin, null::varchar network_name where false)"
        con.execute(f"""create or replace temp view r0 as
          select r.*, coalesce(m.network_name, r.file_label) network_name from (select * from read_parquet('{loc}') where {pf}) r left join {mem} m using (npi, tin)
          where (billing_code_type ilike 'cpt%' or billing_code_type ilike 'hcpcs%' or billing_code_type ilike 'ms-drg%' or billing_code_type ilike 'drg%')
            and lower(negotiated_type) in ('negotiated','fee schedule','derived','percentage') and try_cast(negotiated_rate as double) >= 0""")
        con.execute(f"""create or replace temp view r1 as select {SHORT} ins, {NET} net, {KEY} code, {TYPE} ctype, name, npi, tin,
          case when billing_class ilike 'inst%' then 0 else 1 end fee, case when negotiated_type ilike 'percent%' then 1 else 0 end t,
          round(try_cast(negotiated_rate as double), 2) v, coalesce(billing_code_modifier,'') modi, coalesce(setting,'') st,
          lower(negotiated_type) nt, last_updated_on dt, source_url url from r0""")
        con.execute(f"""create or replace temp view r2 as select * from r1
          where (ctype <> 'MS-DRG' and regexp_matches(code, '^[0-9A-Z][0-9]{{3}}[0-9A-Z]$')) or (ctype = 'MS-DRG' and regexp_matches(code, '^[0-9]{{1,3}}$'))""")
        sd = D / f'stage/src={tag}'
        shutil.rmtree(sd, ignore_errors=True)
        con.execute(f"copy (select ins, net, code, ctype, name, npi, tin, fee, t, v, modi, st, nt, dt, url, hash(code) % {NB} bucket from r2) to '{sd}' (format parquet, compression zstd, partition_by (bucket), overwrite_or_ignore)")
        if not glob.glob(f'{sd}/*/*.parquet'):
            if K > 1: s3.put_object(Bucket=B, Key=AC + f'{tag}.done', Body=b'1')
            continue
        src = f"read_parquet('{sd}/*/*.parquet')"
        con.execute(f"copy (select code, ctype, name, count(*) n from {src} where name is not null and name <> '' group by all) to '{D}/names/{tag}.parquet' (format parquet)")
        con.execute(f"copy (select npi, ins, net, v from {src} where fee=0 and t=0 and v>0 and modi='' group by all having count(distinct code) >= 5) to '{D}/flat/{tag}.parquet' (format parquet)")
        con.execute(f"copy (select distinct 'n' k, ins||'|'||net val from {src} union select distinct 'd', dt from {src} union select distinct 'u', url from {src} union select distinct 'p', npi from {src} union select distinct 'm', modi||'~'||st||'~'||nt from {src}) to '{D}/aux/{tag}.parquet' (format parquet)")
        for bdir in glob.glob(f'{sd}/bucket=*'):
            b = bdir.rsplit('=', 1)[1]; (D / f'xpart/bucket={b}').mkdir(parents=True, exist_ok=True)
            con.execute(f"copy (select ins, net, code, npi, tin, fee, t, v, modi, st, nt, dt, url from read_parquet('{bdir}/*.parquet')) to '{D}/xpart/bucket={b}/{tag}.parquet' (format parquet, compression zstd)")
        shutil.rmtree(sd)
        if K > 1:
            save_a(tag); log(f'A {ii+1}/{len(files)} piece {p+1}/{K} done')
            NEWP[0] += 1
            if E.get('MAXPIECES') and NEWP[0] >= int(E['MAXPIECES']): log('MAXPIECES reached, restarting fresh'); LOGFLUSH(); os._exit(3)
    loc.unlink(); locm.unlink(missing_ok=True)
    gl = f'{D}/xpart/bucket=*/{i}.parquet' if K == 1 else f'{D}/xpart/bucket=*/{i}_*.parquet'
    n = con.execute(f"select sum(num_rows) from (select distinct file_name, row_group_id, row_group_num_rows num_rows from parquet_metadata('{gl}'))").fetchone()[0] if glob.glob(gl) else 0
    if K == 1: save_a(i)
    else: s3.put_object(Bucket=B, Key=AC + f'{i}.done', Body=b'1')
    log(f'A {ii+1}/{len(files)}', f'{(n or 0):,} rows' + (f' in {K} pieces' if K > 1 else ''), key.rsplit('/', 1)[1], f'disk {shutil.disk_usage(str(D)).used/1e9:.0f} GB')

if E.get('STOPA'): log('STOPA'); sys.exit(0)
# ---------- lookups ----------
s3.download_file(B, E.get('NPIS', 'data/2026-09-29/dfw_npis_all.parquet'), str(D / 'npis.parquet'))
s3.download_file(B, 'site/site_codes.csv', str(D / 'site_codes.csv'))
NP = {r[0]: r[1:] for r in con.execute(f"select npi, entity_type, name, credential, taxonomy_1, city from '{D}/npis.parquet'").fetchall()}
FEAT = {r['code']: r for r in csv.DictReader(open(D / 'site_codes.csv'))}
NAMES = {c: (t, nm) for c, t, nm in con.execute(f"""select code, arg_max(ctype, n), arg_max(name, n) from
  (select code, ctype, name, sum(n) n from read_parquet('{D}/names/*.parquet') group by all) group by code""").fetchall()}
log('lookups', len(NP), 'npis', len(NAMES), 'codes')

MOD = {'26': 'professional part only', 'TC': 'technical part only', '52': 'reduced service', '53': 'discontinued procedure',
       '54': 'surgery only', '55': 'after-surgery care only', '56': 'pre-surgery care only', '78': 'return to operating room',
       '50': 'both sides', '51': 'multiple procedures', '59': 'separate procedure', '80': 'assistant surgeon', '82': 'assistant surgeon',
       'AS': 'assistant at surgery', 'LT': 'left side', 'RT': 'right side', 'QK': 'anesthesia, supervised', 'QX': 'nurse anesthetist, supervised',
       'QY': 'nurse anesthetist, supervised', 'QZ': 'nurse anesthetist alone', 'AA': 'anesthesiologist personally', 'GT': 'telehealth', '95': 'telehealth', 'GP': 'physical therapy plan'}
def label_str(modi, st, nt, other):
    p = [MOD.get(m, 'modifier ' + m) for m in modi.split('|') if m] if modi else []
    if st in ('inpatient', 'outpatient'): p.append(st)
    if nt in ('fee schedule', 'derived'): p.append(nt)
    if other: p.append('billed under another tax ID')
    return ', '.join(p)
def provider(npi):
    et, name, cred, tax, city = NP.get(npi, ('2', None, None, '', '')); tax = tax or ''
    kind = 'P' if et == '1' else 'A' if tax.startswith('261QA') else 'H' if tax[:3] in ('282', '283', '284', '286', '273', '275', '276') else 'G'
    nm = (name or f'NPI {npi}').strip()
    if nm.isupper():
        nm = nm.title()
        for a, b in ((' Llc', ' LLC'), (' Pllc', ' PLLC'), (' Pa', ' PA'), (' Lp', ' LP'), (' Md', ' MD'), (' Dds', ' DDS')): nm = nm.replace(a, b)
    if cred and kind == 'P': nm = f"{nm}, {cred.replace('.', '')}"
    return [nm, (city or '').title(), kind]
# ---------- global lookup tables (fixed before stage B so workers can run in parallel) ----------
AUX = {}
for k_, v_ in con.execute(f"select k, val from read_parquet('{D}/aux/*.parquet') group by all order by k, val").fetchall(): AUX.setdefault(k_, []).append(v_ if v_ is not None else '')
nets, dates, urls, npis = AUX.get('n', []), AUX.get('d', []), AUX.get('u', []), AUX.get('p', [])
ptab = [provider(n_) for n_ in npis]
labs = sorted({x for m in AUX.get('m', []) for o in (0, 1) for x in [label_str(*(m.split('~') + [''] * 3)[:3], o)] if x})
log('index', len(nets), 'networks', len(dates), 'dates', len(urls), 'urls', len(ptab), 'providers', len(labs), 'labels')
nidx = {k_: i_ for i_, k_ in enumerate(nets)}; didx = {k_: i_ for i_, k_ in enumerate(dates)}; uidx = {k_: i_ for i_, k_ in enumerate(urls)}
pidx = {k_: i_ for i_, k_ in enumerate(npis)}; lidx = {k_: i_ for i_, k_ in enumerate(labs)}; KIND = [p_[2] for p_ in ptab]
def lab(modi, st, nt, tin, tin0):
    x = label_str(modi or '', st or '', nt or '', tin != tin0)
    return lidx.get(x, -1) if x else -1
Q = """with md as (select code, fee, median(v) m, count(*) n from x where t=0 and v>0 and modi='' group by code, fee),
  y as (select x.*, fl.v is not null and x.fee=0 and x.t=0 and x.modi='' isflat,
          coalesce(md.n >= 5 and x.t=0 and x.v>0 and x.v < 0.2*md.m, false) islow
        from x left join flat fl on fl.npi=x.npi and fl.ins=x.ins and fl.net=x.net and fl.v=x.v left join md on md.code=x.code and md.fee=x.fee),
  g as (select code, npi, ins, net, fee, t, count(*) filter (where v=0) zeros,
    arg_min([v::varchar, tin, modi, st, nt, dt, url, isflat::varchar, islow::varchar], (modi<>'')::int*1e12 + v) filter (where v>0) h,
    count(*) filter (where v>0) npos, any_value(dt) dt0, any_value(url) url0 from y group by all),
  o as (select code, npi, ins, net, fee, t, list([v::varchar, tin, modi, st, nt] order by v) ot from
    (select *, row_number() over (partition by code, npi, ins, net, fee, t order by v) rn from x where v>0) where rn<=9 group by all)
  select g.*, o.ot from g left join o using (code, npi, ins, net, fee, t) order by code"""
FLAT = None
def work(k):
    """One bucket. DuckDB only sorts (spills to disk, so memory stays bounded); Python walks the sorted rows and
    applies the same rules as 09r: headline = lowest positive rate with no modifier (else lowest positive), up to 8
    other rates, $0 count, flat-case-rate and unusually-low flags (median via approx_quantile)."""
    try:
        return tuple(json.loads(s3.get_object(Bucket=B, Key=f'{WK}B/{k}.json')['Body'].read()))
    except Exception: pass
    global FLAT
    if E.get('NOXPART'):
        xd = D / f'xpart/bucket={k}'; xd.mkdir(parents=True, exist_ok=True)
        for kk_, _ in ls(f'{AC}xpart/bucket={k}/'):
            if not mine(kk_.rsplit('/', 1)[1]): continue
            dst = xd / kk_.rsplit('/', 1)[1]
            if not dst.exists(): s3.download_file(B, kk_, str(dst))
    c = duckdb.connect()
    c.execute(f"set enable_progress_bar=false; set memory_limit='{E.get('WMEM','3GB')}'; set threads={E.get('WTHREADS','2')}; set preserve_insertion_order=false; set temp_directory='{D}/tmp/w{k}'")
    if FLAT is None:
        FLAT = set(c.execute(f"select distinct npi, ins, net, v from read_parquet('{D}/flat/*.parquet')").fetchall())
    src = f"read_parquet('{D}/xpart/bucket={k}/*.parquet')"
    MD = {(cd, fe): (m, n) for cd, fe, m, n in c.execute(f"select code, fee, approx_quantile(v, 0.5), count(*) from {src} where t=0 and v>0 and modi='' group by all").fetchall()}
    cur = c.execute(f"""select code, npi, ins, net, fee, t, v, tin, modi, st, nt, dt, url from {src}
                       order by code, npi, ins, net, fee, t, v, modi, tin, st, nt, dt, url""")
    summ, scp = {}, {}; nx = 0
    pool = ThreadPoolExecutor(8); futs = []
    def put(code, f):
        for kk, rows in f.items():
            cap = 0 if KIND[int(kk)] == 'P' else 4
            for r in rows: r[8] = r[8][:cap]
        summ[code] = sum(len(v) for v in f.values())
        body = base64.b64encode(gzip.compress(json.dumps({'f': f}, separators=(',', ':')).encode(), 6))
        futs.append(pool.submit(s3.put_object, Bucket=B, Key=f'{TOUT}t/{code}.txt', Body=body, ContentType='text/plain'))
    def emit(f, g, rows):
        code, npi, ins, net, fee, t = g
        nk = f'{ins}|{net}'; n = nidx[nk]; scp.setdefault(nk, set()).add(code)
        zeros = sum(1 for r in rows if r[0] == 0); pos = [r for r in rows if r[0] > 0]
        if pos:
            h = next((r for r in pos if not r[2]), pos[0])
            v, tin, modi, st, nt, dt, url = h
            isflat = fee == 0 and t == 0 and not modi and (npi, ins, net, v) in FLAT
            m = MD.get((code, fee)); islow = bool(m and m[1] >= 5 and t == 0 and v < 0.2 * m[0])
            rest = [r for r in pos[:9] if r is not h]
            others = [[float(r[0]), lab(r[2], r[3], r[4], r[1], tin)] for r in rest[:8]]
            row = [n, fee, t, float(v), (1 if isflat else 0) | (2 if islow else 0), didx.get(dt or '', 0), uidx.get(url or '', 0), len(pos) - 1, others, zeros, lab(modi, st, nt, tin, tin)]
        else:
            r0 = rows[0]
            row = [n, fee, t, 0, 4, didx.get(r0[5] or '', 0), uidx.get(r0[6] or '', 0), 0, [], zeros, -1]
        f.setdefault(str(pidx[npi]), []).append(row)
    curcode, f, g0, grp = None, {}, None, []; prev = None
    while True:
        batch = cur.fetchmany(200000)
        if not batch: break
        nx += len(batch)
        for rw in batch:
            if rw == prev: continue
            prev = rw; code, npi, ins, net, fee, t, v, tin, modi, st, nt, dt, url = rw
            g = (code, npi, ins, net, fee, t)
            if g != g0:
                if g0: emit(f, g0, grp)
                if code != curcode:
                    if curcode: put(curcode, f)
                    curcode, f = code, {}
                g0, grp = g, []
            grp.append((v, tin, modi or '', st or '', nt or '', dt, url))
    if g0: emit(f, g0, grp)
    if curcode: put(curcode, f)
    for fu in futs: fu.result()
    c.close(); shutil.rmtree(D / f'xpart/bucket={k}', ignore_errors=True); shutil.rmtree(D / f'tmp/w{k}', ignore_errors=True)
    res = (k, nx, summ, {kk: sorted(v) for kk, v in scp.items()})
    s3.put_object(Bucket=B, Key=f'{WK}B/{k}.json', Body=json.dumps(res).encode())
    return res
# ---------- Stage B: buckets in parallel worker processes ----------
import multiprocessing as mp
summary, scope = {}, {}
buckets = sorted(int(p_.rsplit('=', 1)[1]) for p_ in glob.glob(f'{D}/xpart/bucket=*'))
if E.get('NOXPART'): buckets = list(range(NB))
for k_, _ in ls(WK + 'B/'):
    kb, nx_, summ_, scp_ = json.loads(s3.get_object(Bucket=B, Key=k_)['Body'].read())
    if kb in buckets: buckets.remove(kb)
    summary.update(summ_)
    for kk, v in scp_.items(): scope.setdefault(kk, set()).update(v)
if E.get('BORDER') == 'desc': buckets = buckets[::-1]
if not buckets: log('all buckets already done')
log('stage B', len(buckets), 'buckets to do', len(summary), 'codes already done')
con.close()
with mp.get_context('fork').Pool(int(E.get('WORKERS', '2')), maxtasksperchild=int(E.get('MAXTASKS', '4'))) as P:
    for done, (k, nx, summ, scp) in enumerate(P.imap_unordered(work, buckets), 1):
        summary.update(summ)
        for kk, v in scp.items(): scope.setdefault(kk, set()).update(v)
        log(f'B {done}/{len(buckets)}', f'bucket {k}', f'{nx:,} rows', len(summary), 'codes so far')
# another machine may have finished buckets meanwhile: reload them all, and write embed only when every bucket is in
summary, scope, nb = {}, {}, 0
for k_, _ in ls(WK + 'B/'):
    kb, nx_, summ_, scp_ = json.loads(s3.get_object(Bucket=B, Key=k_)['Body'].read()); nb += 1
    summary.update(summ_)
    for kk, v in scp_.items(): scope.setdefault(kk, set()).update(v)
if nb < NB: log(f'{nb}/{NB} buckets in; another machine will write embed.json'); LOGFLUSH(); sys.exit(0)
codes = {}
for c_, n_ in summary.items():
    t_, nm = NAMES.get(c_, ('CPT', ''))
    if c_ in FEAT: codes[c_] = {'name': FEAT[c_]['plain_name'], 'cat': FEAT[c_]['category'], 'type': FEAT[c_]['code_type'], 'n': n_, 'f': None}
    else: codes[c_] = {'name': (nm or f'{t_} {c_}').strip(), 'cat': 'description as written in the insurer file', 'type': t_, 'n': n_, 'f': None, 'd': 1}
embed = {'codes': codes, 'fac': ptab, 'nets': nets, 'dates': dates, 'urls': urls, 'labs': labs,
         'scope': {k_: sorted(v) for k_, v in scope.items() if len(v) < len(codes) - 20}}
s3.put_object(Bucket=B, Key=TOUT + 'embed.json', Body=json.dumps(embed, separators=(',', ':')).encode())
s3.put_object(Bucket=B, Key=TOUT + 'npis.json', Body=json.dumps(npis).encode())
log('DONE', len(codes), 'codes', len(ptab), 'providers', len(nets), 'networks', sum(summary.values()), 'lines')
s3.put_object(Bucket=B, Key=OUTP + 'log.txt', Body='\n'.join(LOG).encode())
