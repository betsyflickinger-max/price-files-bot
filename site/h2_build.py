"""Step H2: per-code hospital files for the every-code Price Finder.

Same rules as 09_build_pricefinder_hosp.py (nothing averaged; every line keeps its hospital file + line number;
identical duplicate lines collapsed), but over every code and built in DuckDB.
Output:
  OUT/h/<code>.txt  gzip+base64 JSON {c:[[s,st,cash,gross,row,fee,d]], r:[[s,ins,prod,p,fee,pct,val,row,st,flat]], d:[desc], p:[plan]}
  OUT/hosp_index.json  {src, ins, cover, plans:[], desc:[], codes:{}}  (replaces D.hosp in the page embed)
usage: h2_build.py ROWS_GLOB MAPPING_CSV PAGE_HTML OUT_DIR
"""
import base64, csv, gzip, json, os, re, sys
from collections import defaultdict
import duckdb

rows_glob, map_path, page_path, out = sys.argv[1:5]
os.makedirs(f'{out}/h', exist_ok=True)

# ---- existing page: keep the 79 current hospital files at the same index (quality ratings are keyed by it)
s = open(page_path).read()
i = s.find('id="data"'); D = json.loads(s[s.find('>', i) + 1:s.find('</script>', i)])
codes_ok = set(D['codes'])
src = [list(x) for x in D['hosp']['src']]
src_idx = {x[5]: k for k, x in enumerate(src)}

DB = f'{out}/h2.duckdb'
if os.path.exists(DB): os.remove(DB)
con = duckdb.connect(DB)
NP = int(os.environ.get('NP', '2'))
con.execute(f"SET memory_limit='{os.environ.get('MEM','5GB')}'; SET temp_directory='{out}/duck'; SET preserve_insertion_order=false; SET threads={os.cpu_count()}")
con.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet('{rows_glob}', union_by_name=true)")

# ---- new hospital files: same name/city/unit logic as 09
def city_of(addr):
    m = re.search(r",\s*([A-Za-z .'-]+?),?\s+(TX|Texas)\b", addr)
    return m.group(1).strip().title() if m else ""
meta = con.execute("""SELECT left(sha256,16), any_value(hospital_name), any_value(location_name), any_value(hospital_address),
    any_value(last_updated_on), any_value(source_url), any_value(raw_key) FROM raw GROUP BY 1""").fetchall()
present = {m[0] for m in meta}
for sha, hn, ln, ad, lu, url, rk in meta:
    if sha in src_idx:
        continue
    # same hospital, new file this month: take over its old slot (quality ratings are keyed by slot)
    old = next((k for k, x in enumerate(src) if x[5] not in present and x[5] in src_idx and (x[4] == (url or '') or x[0].lower() == (hn or '').strip().lower())), None)
    locs = [x.strip() for x in (ln or '').split('|') if x.strip()]
    addrs = [x.strip() for x in (ad or '').split('|') if x.strip()]
    name = (hn or '').strip() or (locs[0] if locs else '')
    k = next((j for j, l in enumerate(locs) if l.lower() == name.lower()), 0)
    city = city_of(addrs[k] if k < len(addrs) else (addrs[0] if addrs else ''))
    others = [l for l in locs if l.lower() != name.lower()]
    k_ = (rk or '').lower()
    unit = 'entry' if '.json' in k_ else 'line' if ('.csv' in k_ or 'standardcharges.zip' in k_) else 'row'
    entry = [name, city, ' · '.join(others), lu or '', url or '', sha, unit]
    if old is not None:
        del src_idx[src[old][5]]; src[old] = entry; src_idx[sha] = old
    else:
        src_idx[sha] = len(src); src.append(entry)
print('hospital files', len(src), '(new:', len(src) - len(D['hosp']['src']), ')', flush=True)
con.execute("CREATE TABLE srcmap(sha16 VARCHAR, s INTEGER)")
con.executemany("INSERT INTO srcmap VALUES (?,?)", [[k, v] for k, v in src_idx.items()])

# ---- payer classification (09 rules) on distinct payer/plan pairs
PROD_CODE = {"Com": 0, "Ex": 1, "MA": 2, "Mcd": 3}
mapping, by_payer = {}, defaultdict(lambda: defaultdict(int))
for m in csv.DictReader(open(map_path, encoding='utf-8-sig')):
    ins = m["grid_column"] if m["grid_column"] != "(not in grid)" else ""
    mapping[(m["payer_name"], m["plan_name"])] = (ins, PROD_CODE.get(m["product_line"], 4))
    if ins:
        by_payer[m["payer_name"]][ins] += 1
NAME_RULES = [(("aetna",), "Aetna"), (("humana",), "Humana"), (("cigna",), "Cigna"),
              (("unitedhealth", "united health", "uhc", "optum"), "UnitedHealthcare"),
              (("blue cross", "bcbs", "blue shield"), "BCBSTX"), (("molina",), "Molina"),
              (("ambetter", "superior health", "wellcare", "centene"), "Superior/Ambetter/Wellcare (Centene)"),
              (("oscar",), "Oscar"), (("wellpoint", "amerigroup"), "Wellpoint/Amerigroup"),
              (("baylor scott", "bsw"), "BSW Health Plan"), (("curative",), "Curative")]
def classify(payer, plan):
    if (payer, plan) in mapping:
        return mapping[(payer, plan)]
    if payer in by_payer:
        return max(by_payer[payer], key=by_payer[payer].get), 4
    p = payer.lower()
    for words, ins in NAME_RULES:
        if any(w in p for w in words):
            return ins, 4
    return "", 4

NUM = lambda c: f"try_cast(replace(replace(trim({c}),'$',''),',','') AS DOUBLE)"
pairs = con.execute("SELECT DISTINCT coalesce(payer_name,''), coalesce(plan_name,'') FROM raw WHERE trim(coalesce(payer_name,''))<>''").fetchall()
cls = []
for pa, pl in pairs:
    ins, prod = classify(pa, pl)
    if not ins:
        ins = pa.strip()
    cls.append([pa, pl, ins, prod, f'{pa.strip()} · {pl.strip()}'.strip(' ·')])
con.execute("CREATE TABLE cls(payer VARCHAR, plan VARCHAR, ins VARCHAR, prod INTEGER, plabel VARCHAR)")
con.executemany("INSERT INTO cls VALUES (?,?,?,?,?)", cls)
print('payer/plan pairs', len(cls), flush=True)
con.execute("CREATE TABLE okc(code VARCHAR)")
con.executemany("INSERT INTO okc VALUES (?)", [[c] for c in codes_ok])

base = f"""SELECT m.s, upper(trim(r.code)) code,
  CASE WHEN lower(trim(coalesce(r.billing_class,'')))='professional' THEN 1 ELSE 0 END fee,
  CASE lower(trim(coalesce(r.setting,''))) WHEN 'outpatient' THEN 'o' WHEN 'inpatient' THEN 'i' WHEN 'both' THEN 'b' ELSE '' END st,
  coalesce(try_cast(r.source_row AS BIGINT),0) rw, r.* EXCLUDE (code)
  FROM raw r JOIN srcmap m ON left(r.sha256,16)=m.sha16 JOIN okc ON okc.code=upper(trim(r.code))"""
con.execute(f"""CREATE TABLE cash AS SELECT s, code, st, cash, coalesce(gross,0) gross, min(rw) rw, fee, arg_min(dsc, rw) dsc FROM (
  SELECT s, code, fee, st, rw, {NUM('discounted_cash')} cash, {NUM('gross_charge')} gross, left(trim(coalesce(description,'')),90) dsc FROM ({base}))
  WHERE cash>0 GROUP BY s, code, fee, st, cash, gross""")
print('cash lines', con.execute("SELECT count(*) FROM cash").fetchone()[0], flush=True)
con.execute(f"""CREATE TABLE rates AS SELECT s, code, ins, prod, plabel, fee, pct, val, min(rw) rw, st FROM (
  SELECT b.s, b.code, c.ins, c.prod, c.plabel, b.fee, b.st, b.rw,
    CASE WHEN dol>0 THEN 0 ELSE 1 END pct, round(CASE WHEN dol>0 THEN dol ELSE pc END, 2) val
  FROM (SELECT s, code, fee, st, rw, payer_name, plan_name, {NUM('negotiated_dollar')} dol, {NUM('negotiated_percentage')} pc FROM ({base})
        WHERE trim(coalesce(payer_name,''))<>'') b
  JOIN cls c ON c.payer=coalesce(b.payer_name,'') AND c.plan=coalesce(b.plan_name,'')
  WHERE dol>0 OR pc>0)
  GROUP BY s, code, ins, prod, plabel, fee, st, pct, val""")
print('payer lines', con.execute("SELECT count(*) FROM rates").fetchone()[0], flush=True)
# flat case rate: identical dollar amount, same file + insurer + plan + fee type, on 5+ codes
con.execute("""CREATE TABLE flat AS SELECT s, ins, plabel, fee, val FROM rates WHERE pct=0
  GROUP BY s, ins, plabel, fee, val HAVING count(DISTINCT code)>=5""")

ins_l = [x[0] for x in con.execute("SELECT ins FROM rates GROUP BY ins ORDER BY count(*) DESC").fetchall()]
ins_i = {v: k for k, v in enumerate(ins_l)}
cover = {str(ins_i[a]): n for a, n in con.execute("SELECT ins, count(DISTINCT s) FROM rates GROUP BY ins").fetchall()}

CAPR, CAPC = int(os.environ.get('CAPR', '500')), int(os.environ.get('CAPC', '100'))
json.dump({'src': src, 'ins': ins_l, 'cover': cover, 'plans': [], 'desc': [], 'codes': {}},
          open(f'{out}/hosp_index.json', 'w'), separators=(',', ':'))
# lines ranked within hospital file + code (lowest first) so a file listing thousands of lines for one code is capped
con.execute(f"""CREATE TABLE rout AS SELECT r.code, r.s, r.ins, r.prod, r.plabel, r.fee, r.pct, r.val, r.rw, r.st,
  CASE WHEN f.s IS NULL THEN 0 ELSE 1 END fl, row_number() OVER (PARTITION BY r.code, r.s ORDER BY r.pct, r.val, r.rw) k,
  count(*) OVER (PARTITION BY r.code, r.s) n FROM rates r
  LEFT JOIN flat f ON f.s=r.s AND f.ins=r.ins AND f.plabel=r.plabel AND f.fee=r.fee AND f.val=r.val AND r.pct=0""")
con.execute(f"""CREATE TABLE cout AS SELECT *, row_number() OVER (PARTITION BY code, s ORDER BY cash, rw) k,
  count(*) OVER (PARTITION BY code, s) n FROM cash""")
con.close()

def part(k):
    import boto3
    from concurrent.futures import ThreadPoolExecutor
    c2 = duckdb.connect(DB, read_only=True)
    c2.execute("SET threads=1; SET memory_limit='1500MB'")
    E = os.environ
    s3 = boto3.client('s3', endpoint_url=f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=E['R2_ACCESS_KEY_ID'],
                      aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name='auto') if E.get('UPLOAD') else None
    pool = ThreadPoolExecutor(8)
    cash_by, xc = defaultdict(list), defaultdict(dict)
    for row in c2.execute(f"SELECT code, s, st, cash, gross, rw, fee, dsc, k, n FROM cout WHERE hash(code)%{NP}={k} ORDER BY code, cash").fetchall():
        if row[8] <= CAPC: cash_by[row[0]].append(row[1:8])
        elif row[8] == CAPC + 1: xc[row[0]][str(row[1])] = row[9] - CAPC
    def write(code, c, r, xr):
        d, di, p, pi = [], {}, [], {}
        def ix(lst, dd, v):
            if v not in dd: dd[v] = len(lst); lst.append(v)
            return dd[v]
        C = [[s_, st, cash, gross, rw, fee, ix(d, di, dsc)] for s_, st, cash, gross, rw, fee, dsc in c]
        R = [[s_, ins_i[ins], prod, ix(p, pi, pl), fee, pct, (int(val) if val == int(val) else val), rw, st, fl]
             for s_, ins, prod, pl, fee, pct, val, rw, st, fl in r]
        o = {'c': C, 'r': R, 'd': d, 'p': p}
        if xr or xc.get(code): o['x'] = {'r': xr, 'c': xc.get(code, {})}
        b = base64.b64encode(gzip.compress(json.dumps(o, separators=(',', ':')).encode(), 9))
        open(f'{out}/h/{code}.txt', 'wb').write(b)
        if s3: pool.submit(s3.put_object, Bucket='upfrontdfwdata', Key=E['HKEY'] + code + '.txt', Body=b, ContentType='text/plain')
    cur = c2.execute(f"""SELECT code, s, ins, prod, plabel, fee, pct, val, rw, st, fl, k, n FROM rout WHERE hash(code)%{NP}={k}
      AND k<={CAPR + 1} ORDER BY code, s, k""")
    n, prev, buf, xr = 0, None, [], {}
    while True:
        batch = cur.fetchmany(100000)
        for row in batch:
            if row[0] != prev and prev is not None:
                write(prev, cash_by.pop(prev, []), buf, xr); n += 1; buf, xr = [], {}
            prev = row[0]
            if row[11] <= CAPR: buf.append(row[1:11])
            else: xr[str(row[1])] = row[12] - CAPR
        if not batch:
            break
    if prev is not None:
        write(prev, cash_by.pop(prev, []), buf, xr); n += 1
    for code, c in list(cash_by.items()):
        write(code, c, [], {}); n += 1
    pool.shutdown(wait=True)
    return n

from multiprocessing import Pool
with Pool(NP) as P:
    ns = P.map(part, range(NP))
print('code files', sum(ns), 'insurer names', len(ins_l), flush=True)
