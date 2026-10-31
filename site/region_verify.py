"""Oct 31, 2026: check a regional split against its state build (read-only). For each sample code and region: every row of a
regional provider is the state's row for the same provider (positions mapped back through npis.json); every state provider
placed in the region with rows for the code is in the regional file; practice rows keep exactly the region's doctors.
Usage: python site/region_verify.py california [codes...]"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import regions, region_split as rs
from hosp_area import newest_build
st = sys.argv[1]; codes = sys.argv[2:] or ['27447', '70553', '99213', '45378', '93000', '80053']
s3 = rs.s3c(); B = rs.B
src = newest_build(s3, B, st); month = src.rstrip('/').rsplit('/', 1)[1]
g = lambda k: s3.get_object(Bucket=B, Key=k)['Body'].read()
npis = json.loads(g(src + 'npis.json')); pos_of = {n: i for i, n in enumerate(npis)}
bad = 0
for r in regions.REGIONS[st]:
    pre = f'site/every-st/{r}/{month}/'
    rn = json.loads(g(pre + 'npis.json')); back = [pos_of[n] for n in rn]; inr = set(back)
    emb = json.loads(g(pre + 'embed.json'))
    assert len(emb['fac']) == len(rn), 'fac and npis.json differ'
    S = json.loads(g(src + 'embed.json')) if r == list(regions.REGIONS[st])[0] else S
    assert all(emb['fac'][j] == S['fac'][back[j]] for j in range(len(rn))), 'provider names differ'
    for c in codes:
        # Oct 31, 2026: a test split (SPLIT_TEST) covers only the 89 featured codes, and 99213 isn't one of them, so every
        # region "missed" it (NY, FL, IL). Check only codes the regional page lists.
        if c not in emb['codes']:
            print(f'{r} {c}: not in this split (test splits cover the featured codes only); skipped', flush=True); continue
        try: so = rs.dec(g(f'{src}t/{c}.txt'))
        except Exception: continue
        try: ro = rs.dec(g(f'{pre}t/{c}.txt'))
        except Exception as ex:   # Oct 31: say why (99213 came back empty in every region of NY, FL and IL)
            ro = {'f': {}, 'p': {}}
            info = lambda k: (lambda h: f"{h['ContentLength']:,} bytes, {h['LastModified']:%m-%d %H:%M}")(s3.head_object(Bucket=B, Key=k))
            try: rinfo = info(f'{pre}t/{c}.txt')
            except Exception as ex2: rinfo = 'not there: ' + repr(ex2)[:80]
            print(f'  {r} {c}: regional file unreadable ({repr(ex)[:120]}); regional {rinfo}; state {info(f"{src}t/{c}.txt")}', flush=True)
        e = 0
        for k, rows in ro['f'].items():
            if so['f'].get(str(back[int(k)])) != rows: e += 1
        want = {k for k in so['f'] if int(k) in inr}
        miss = len(want) - len(ro['f'])
        pe = 0; pn = 0
        for pid, lst in so.get('p', {}).items():
            exp = [[row, [x for x in docs if x in inr]] for row, docs in lst]
            exp = [[row, d] for row, d in exp if d]
            got = [[row, [back[x] for x in docs]] for row, docs in ro.get('p', {}).get(pid, [])]
            if exp != got: pe += 1
            pn += 1
        n_ok = emb['codes'].get(c, {}).get('n') == sum(len(v) for v in ro['f'].values()) + sum(len(v) for v in ro.get('p', {}).values())
        print(f'{r} {c}: providers {len(ro["f"]):,} (rows differ {e}, missing {miss}); practices checked {pn:,}, differ {pe}; count ok {n_ok}', flush=True)
        bad += e + abs(miss) + pe + (not n_ok)
    print(f'{r}: {len(rn):,} providers, {len(emb["codes"]):,} codes, {len(emb.get("prac", {})):,} practices', flush=True)
print('ALL MATCH' if not bad else f'PROBLEMS: {bad}')
sys.exit(1 if bad else 0)
