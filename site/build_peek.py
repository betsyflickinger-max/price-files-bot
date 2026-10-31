"""Oct 30, 2026: read-only look at one state build in R2: which files it has and what each kind holds (shapes and a sample),
for planning the California regional split. Changes nothing. Usage: python site/build_peek.py california"""
import base64, gzip, json, os, sys, collections
import boto3
slug = sys.argv[1]
E = {k: ''.join(os.environ.get(k, '').split()) for k in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET')}
s3 = boto3.client('s3', endpoint_url=(os.environ.get('STORAGE_ENDPOINT') or f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"),
                  aws_access_key_id=E['R2_ACCESS_KEY_ID'], aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name=(os.environ.get('STORAGE_REGION') or 'auto'))
B = E['R2_BUCKET']
def ls(p):
    for pg in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix=p):
        yield from pg.get('Contents', [])
def get(k): return s3.get_object(Bucket=B, Key=k)['Body'].read()
def shape(v, d=0):
    if isinstance(v, dict):
        ks = list(v)[:6]; return '{' + ', '.join(f'{k!r}: {shape(v[k], d+1)}' for k in ks) + (f', …{len(v)} keys' if len(v) > 6 else '') + '}' if d < 3 else f'dict({len(v)})'
    if isinstance(v, list): return f'[{len(v)}: ' + (shape(v[0], d+1) if v else '') + ']' if d < 4 else f'list({len(v)})'
    return repr(v)[:60]
def hosp_check(slug, sp):
    """Oct 31: the state's hospital jobs without a ZIP, next to the state's hospital index entries (by id and by name)"""
    plan = json.loads(get(sp + 'areas.json')); jobs = plan[slug]['jobs']
    src = json.loads(get(plan[slug]['prefix'] + 'hosp/hosp_index.json'))['src']
    by_id = {x[5]: x for x in src if len(x) > 5}; by_nm = {}
    for x in src: by_nm.setdefault(x[0].strip().lower(), []).append(x)
    nz = [j for j in jobs if not j.get('zip')]
    print(len(jobs), 'jobs,', len(nz), 'without zip;', len(src), 'index entries; job keys:', sorted(jobs[0]))
    for j in nz:
        print(' ', j.get('name'), '| ccn', j.get('ccn'), '| id', j.get('id'), '| by id:', (by_id.get(j.get('id')) or [None, None])[:2],
              '| by name:', [x[1] for x in by_nm.get((j.get('name') or '').strip().lower(), [])], '|', (j.get('url') or '')[:90])
if len(sys.argv) > 3 and sys.argv[2] == 'hosp': hosp_check(slug, sys.argv[3]); sys.exit(0)
if len(sys.argv) > 2 and sys.argv[2] == 'state':   # Oct 31: what each page job has published for this state and its regions
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import regions
    names = [slug] + list(regions.REGIONS.get(slug, {}))
    for o in ls('site/builds/'):
        if not o['Key'].endswith('/state.json'): continue
        st = json.loads(get(o['Key'])); pub = st.get('published', {})
        hit = {n: pub[n] for n in names if n in pub}
        if hit: print(o['Key'], o['LastModified']); [print(f'  {n}: {v}') for n, v in hit.items()]
        for n in names:
            try: d = json.loads(get(o['Key'][:-10] + f'h2/{n}.done')); print(f'  h2 done {n}: {len(d.get("jobs", []))} jobs')
            except Exception: pass
    sys.exit(0)

root = f'site/every-st/{slug}/'
months = sorted({o['Key'][len(root):].split('/')[0] for o in ls(root)})
print('builds:', months)
pre = root + months[-1] + '/'
groups = collections.Counter(); sizes = collections.Counter()
for o in ls(pre):
    rel = o['Key'][len(pre):]; g = rel.split('/')[0] + ('/' if '/' in rel else '') + ('/'.join(rel.split('/')[1:2]) + '/' if rel.count('/') > 1 else '')
    groups[g] += 1; sizes[g] += o['Size']
for g, n in sorted(groups.items()): print(f'  {g:40} {n:7,} files {sizes[g]/1e6:10.1f} MB')
emb = json.loads(get(pre + 'embed.json'))
print('embed keys:', {k: len(json.dumps(v)) for k, v in emb.items()})
for k, v in emb.items():
    if isinstance(v, dict) and v: kk = next(iter(v)); print(f'  embed.{k}[{kk!r}] =', json.dumps(v[kk])[:400])
    elif isinstance(v, list) and v: print(f'  embed.{k}[0] =', json.dumps(v[0])[:400], f'(len {len(v)})')
    else: print(f'  embed.{k} =', json.dumps(v)[:200])
np_ = json.loads(get(pre + 'npis.json')); print('npis.json:', shape(np_))
def dec(k): return json.loads(gzip.decompress(base64.b64decode(get(k))))
t = dec(pre + 't/27447.txt'); print('t/27447 f keys', len(t.get('f', {})), 'p keys', len(t.get('p', {})))
for k in ('f', 'p'):
    if t.get(k): kk = next(iter(t[k])); print(f'  t.{k}[{kk}] =', json.dumps(t[k][kk])[:500])
for o in ls(pre + 'hosp/'):
    print('hosp file:', o['Key'][len(pre):], o['Size']); break
try:
    H = json.loads(get(pre + 'hosp/hosp_index.json')); print('hosp_index:', shape(H))
    for k, v in H.items():
        if isinstance(v, list) and v: print(f'  hosp.{k}[0] =', json.dumps(v[0])[:400], len(v))
        elif isinstance(v, dict) and v: kk = next(iter(v)); print(f'  hosp.{k}[{kk}] =', json.dumps(v[kk])[:400], len(v))
    hk = [o['Key'] for o in ls(pre + 'hosp/h/')][:1]
    if hk:
        raw = get(hk[0])
        try: h = json.loads(gzip.decompress(base64.b64decode(raw)))
        except Exception: h = json.loads(raw)
        print(hk[0][len(pre):], shape(h)); print('  sample', json.dumps(h)[:600])
except Exception as e: print('hosp:', e)
for k in ('site/builds/hosp-stn5-' + months[-1] + '/areas.json',):
    try:
        A = json.loads(get(k)); a = A.get(slug); print('areas.json', slug, shape(a))
        if a and a.get('jobs'): print('  job sample', json.dumps(a['jobs'][0])[:500])
    except Exception as e: print(k, e)
