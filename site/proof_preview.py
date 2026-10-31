"""Before/after preview of card #2 (proof a provider does the procedure) on one area's price pages. Deploys nothing.

Reads the live site bundle and the DFW Price Finder's per-code files exactly as site/procedure_pages.py does, runs summarize()
without and with proof (site/proof.py), and writes reports/proof-preview/<area>.md: for each sample procedure, the lowest 10
before and after, the proof line on each ranked provider, and who would be listed as "also listed, may not offer this procedure".

  python site/proof_preview.py [--area illinois] [--codes 29881,66984,...]
      (also renders the area's gallbladder page: reports/proof-preview/<area>-gallbladder.html)
Oct 31, 2026 (all states): a non-Texas area reads its own state's proof file (by_state/evidence_<st>.parquet). The report also
lists every area with the state the page code gives it, to catch a state page treated as Texas.
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import procedure_pages as pp   # noqa: E402
from proof import Proof, line, load_taxonomy, load_taxonomy_state, for_page, STATE_NAME  # noqa: E402

SAMPLE = ['29881', '66984', '47562', '64721', '45378', '27447', '73721', '59400', '99213']


def money(v):
    return f"${v:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes')
    ap.add_argument('--area', default=os.environ.get('PROOF_PREVIEW_AREA') or 'illinois')
    A = ap.parse_args()
    d = '/tmp/proofprev'
    os.makedirs(d, exist_ok=True)
    store = pp.Store(d)
    s3, B = store.s3, store.B
    s3.download_file(B, 'site/deploy/current.tgz', f'{d}/site.tgz')
    with tarfile.open(f'{d}/site.tgz') as t:
        mem = {m.name.lstrip('./'): m for m in t.getmembers() if m.isfile()}
        areas = json.load(t.extractfile(mem['site/cities.json']))
        a = next(x for x in areas if x['slug'] == A.area)
        want = f"site/{a['path'].strip('/')}/index.html"
        os.makedirs(os.path.dirname(f'{d}/{want}'), exist_ok=True)
        open(f'{d}/{want}', 'wb').write(t.extractfile(mem[want]).read())
        for name, m_ in mem.items():   # the area's Pages Functions say where its per-code files are (Store.prefix)
            if name.startswith(f"functions/{a['slug']}/"):
                os.makedirs(os.path.dirname(f"{d}/{name}"), exist_ok=True)
                open(f"{d}/{name}", "wb").write(t.extractfile(m_).read())
        open(f'{d}/site/index.html', 'wb').write(t.extractfile(mem['site/index.html']).read())
    D = pp.page_data(f'{d}/{want}')[0]
    loc = store.local_medicare(a, len(D['fac']))
    pst = store.provider_states(a, D)
    pre = store.prefix(a, 't')
    npis = json.loads(s3.get_object(Bucket=B, Key=pre[:-2] + 'npis.json')['Body'].read())
    assert len(npis) == len(D['fac']), 'npis.json is not aligned with the page providers'
    area_st = pp.area_state(a)
    if area_st == 'TX':     # exactly what the live Texas pages use
        P = Proof(s3, B, tax=load_taxonomy(s3, B))
    else:
        P = Proof(s3, B, state=area_st, tax=load_taxonomy_state(s3, B, area_st))
    codes = [c for c in (A.codes.split(',') if A.codes else SAMPLE) if c in D['codes']]
    out = [f"# Card #2 preview: {a['name']} price pages, before and after", "",
           f"Built {dt.date.today().isoformat()} by site/proof_preview.py. Nothing deployed. Proof data: Medicare 2024 + Medicaid 2023-2024 "
           f"({STATE_NAME.get(area_st, area_st)}). Order stays by price; proof only decides who is in the lowest-10 list. The highest 10 is unchanged.", ""]
    tot = collections.Counter()
    for c in codes:
        C = D['codes'][c]
        t, h = store.get(a, 't', c), store.get(a, 'h', c)
        drg = 'DRG' in (C.get('type') or '')
        f = (t or {}).get('f') or C.get('f') or {}
        prf, why = {}, collections.Counter()
        for fi in f:
            i = int(fi)
            x = D['fac'][i]
            kind = pp.kind_of(x)
            ccn = (loc.get(i) or [None, None, None])[2]
            st, info = P.status(kind, npis[i], ccn, c, C['name'], C.get('cat', ''), drg, x[0])
            prf[i] = (st, {'text': line(info), 'url': info['url']} if st == 'proved' else info)
            why[(kind, st)] += 1
            tot[(kind, st)] += 1
        S0 = pp.summarize(D, c, t, h, loc, area_st, pst)
        S1 = pp.summarize(D, c, t, h, loc, area_st, pst, prf)
        out += [f"## {C['name']} ({c})", "",
                "Providers on the page by kind and proof: " + "; ".join(f"{pp.KIND.get(k, k)} {s}: {n}" for (k, s), n in sorted(why.items())), ""]
        out += [f"Check: per-code insurer file {'found' if t else 'NOT found'} ({store.prefix(a, 't')}); providers with rates {len(f)}; "
                f"left out as out of state {S0.get('n_out_of_state')}; ranked providers {S0.get('providers')}; kind {S0.get('kind')}; "
                f"states seen {dict(collections.Counter(pst.values()).most_common(4)) if pst else 'none'}", ""]
        out += ["| # | Before (today) | After | Proof shown |", "|---|---|---|---|"]
        for j in range(max(len(S0['lowest']), len(S1['lowest']))):
            b = S0['lowest'][j] if j < len(S0['lowest']) else None
            n = S1['lowest'][j] if j < len(S1['lowest']) else None
            out.append(f"| {j + 1} | {(b['name'] + ' ' + money(b['v'])) if b else ''} | {(n['name'] + ' ' + money(n['v'])) if n else ''} | "
                       f"{(n.get('proof') or {}).get('text', '') if n else ''} |")
        al = S1['also_listed']
        for e in al:
            cc = (loc.get(e['fi']) or [None, None, None])[2]
            e['diag'] = f"CCN {cc}, {P.ccn_groups.get(cc, 0)} Medicare outpatient groups on record" if cc else 'no CCN'
        out += ["", f"Left out of the figures (clinicians whose specialty doesn't perform this code): {S1['n_wrong_specialty']}; lowest before {money(S0['lo']['v']) if S0['lo'] else '-'}, after {money(S1['lo']['v']) if S1['lo'] else '-'}"]
        out += ["", f"Also listed, may not offer this procedure: {len(al)}" + (" (" + "; ".join(f"{e['name']} {money(e['v'])} [{e.get('diag', '')}]" for e in al[:8])
                                                                               + (" ..." if len(al) > 8 else "") + ")" if al else ""), ""]
        if c == '47562':   # one full page, rendered exactly as the live build would, to check the HTML
            pp.site_settings(f'{d}/site')
            slug = pp.load_slugs([c], {c: C['name']})[c]
            html_ = pp.procedure_page(a, S1, [], [], pp.page_data(f'{d}/{want}')[1], a['path'], pp.area_url_slug(a['slug']), slug)
            (HERE.parent / 'reports' / 'proof-preview').mkdir(parents=True, exist_ok=True)
            (HERE.parent / 'reports' / 'proof-preview' / f"{a['slug']}-gallbladder.html").write_text(html_)
    out += ["## All sample procedures together", "", "| Kind | Proved | Ranked as before (no file covers them) | Held out (no record) |",
            "|---|---|---|---|"]
    for k in sorted({k for k, _ in tot}):
        out.append(f"| {pp.KIND.get(k, k)} | {tot[(k, 'proved')]} | {tot[(k, 'not_covered')]} | {tot[(k, 'no_record')]} |")
    rp = HERE.parent / 'reports' / 'proof-preview'
    rp.mkdir(parents=True, exist_ok=True)
    # every area and the state the page code gives it (a state page read as Texas would get Texas proof and Texas benchmarks)
    try:
        sc = json.load(open(HERE / 'state_scopes.json'))
        known = {slug: code for k, v in sc.items() if not k.startswith('_') for code, slug in v.items()}
    except Exception:
        known = {}
    out += ["", "## Areas and the state the page code gives them", "", "| Area | Slug | State in page code | State it should be |", "|---|---|---|---|"]
    for x in areas:
        should = known.get(x['slug'], pp.area_state(x))
        flag = " **wrong**" if should != pp.area_state(x) else ""
        out.append(f"| {x.get('name', '')} | {x['slug']} | {pp.area_state(x)} | {should}{flag} |")
    (rp / f"{a['slug']}.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == '__main__':
    try:
        main()
    except BaseException:     # write the error where it can be read from the repo, then fail
        import traceback
        rp = HERE.parent / 'reports' / 'proof-preview'
        rp.mkdir(parents=True, exist_ok=True)
        (rp / 'preview-error.txt').write_text(traceback.format_exc())
        raise
