"""Medicare 2026 benchmark for every Price Finder code, same method as the 89-code build
(PFS RVU26D x Dallas/Fort Worth GPCI x CF; OPPS Addendum B July 2026; ASC Addendum AA July 2026; CLFS 2026 Q4),
plus public-domain HCPCS Level II long descriptions (CMS Oct 2026 alpha-numeric file).
usage: build_med_all.py MEDDIR PAGE_HTML OUT_JSON"""
import csv, json, re, sys
M, page, outp = sys.argv[1:4]
s = open(page).read(); i = s.find('id="data"'); D = json.loads(s[s.find('>', i) + 1:s.find('</script>', i)])
codes = set(D['codes'])
def num(x):
    try: return float(str(x).replace('$', '').replace(',', '').strip())
    except ValueError: return None
def rows(path, hdr_first):
    L = list(csv.reader(open(path, encoding='latin-1')))
    k = next(j for j, r in enumerate(L) if r and r[0].strip() == hdr_first)
    return L[k], L[k + 1:]
R = f'{M}/rvu26d-updated-08-26-2026'
G = {}
for r in csv.reader(open(f'{R}/GPCI2026.csv', encoding='latin-1')):
    if len(r) > 6 and r[1] == 'TX' and r[2] in ('11', '28'): G['dal' if r[2] == '11' else 'ftw'] = [float(r[4]), float(r[5]), float(r[6])]
h, body = rows(f'{R}/PPRRVU2026_Oct_nonQPP.csv', 'HCPCS')
out = {}
for r in body:
    if len(r) < 26 or r[0] not in codes or r[1] not in ('', '26') or r[3] not in ('A', 'R', 'T'): continue
    w, pen, nfna, pef, fna, mp, cf = num(r[5]), num(r[6]), r[7].strip(), num(r[8]), r[9].strip(), num(r[10]), num(r[25])
    v = {}
    for loc, (gw, gp, gm) in G.items():
        nf = None if nfna == 'NA' else round((w * gw + pen * gp + mp * gm) * cf, 2)
        fa = None if fna == 'NA' else round((w * gw + pef * gp + mp * gm) * cf, 2)
        v[loc] = [nf, fa if fa is not None else nf]
    if all(x is None or x == 0 for x in v['dal']): continue
    out.setdefault(r[0], {})['pfs26' if r[1] == '26' else 'pfs'] = v
h, body = rows(f'{M}/july-2026-opps-addendum-b/508 Version July Addendum B/2026 July Web Addendum B.07.13.26.csv', 'HCPCS Code')
for r in body:
    if r and r[0] in codes and num(r[5]): out.setdefault(r[0], {})['opps'] = [num(r[5]), r[2].strip(), r[3].strip()]
h, body = rows(f'{M}/july-2026-asc-addenda/508 Version of July 2026 ASC Addenda/July 2026 ASC Addenda - Addendum AA.07.08.26.csv', 'HCPCS Code')
for r in body:
    if r and r[0] in codes and len(r) > 6 and num(r[6]): out.setdefault(r[0], {})['asc'] = [num(r[6]), r[4].strip()]
h, body = rows(f'{M}/26clabq4/PUF_CLFS_CY2026_Q4V1.csv', 'YEAR')
for r in body:
    if len(r) > 5 and r[1] in codes and r[2] == '' and num(r[5]): out.setdefault(r[1], {})['lab'] = num(r[5])
# HCPCS Level II long descriptions (public domain)
names, cur = {}, None
for line in open(f'{M}/hcpcs/HCPC2026_OCT_ANWEB_09232026.txt', encoding='latin-1'):
    code, rid, txt = line[0:5], line[10:11], line[11:91].strip()
    if rid == '3': cur = code; names[code] = txt
    elif rid == '4' and cur == code: names[code] += ' ' + txt
names = {k: re.sub(r'\s+', ' ', v) for k, v in names.items() if k in codes}
# check against the 77 codes built on Sept 29
old = json.load(open(f'{M}/medicare_2026.json'))['codes']
bad = [(k, old[k], out.get(k)) for k in old if out.get(k) != old[k]]
print('codes with a Medicare figure:', len(out), '| HCPCS names:', len(names), '| mismatches vs Sept 29 build:', len(bad))
for b in bad[:8]: print('  ', b)
json.dump({'med': out, 'hcpcs_names': names}, open(outp, 'w'), separators=(',', ':'))
