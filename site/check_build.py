"""Sanity checks before a monthly build goes live. Compares the new page data with the live one; exits 1 on failure.
usage: check_build.py OLD_PAGE NEW_PAGE"""
import json, sys
def data(p):
    s = open(p).read(); i = s.find('id="data"'); return json.loads(s[s.find('>', i) + 1:s.find('</script>', i)])
O, N = data(sys.argv[1]), data(sys.argv[2])
problems = []
def ratio(k, f, lim):
    o, n = f(O), f(N)
    print(f'{k}: {o:,} -> {n:,}')
    if o and n < lim * o: problems.append(f'{k} fell from {o:,} to {n:,} (more than {round((1 - lim) * 100)}%)')
ratio('codes', lambda d: len(d['codes']), 0.9)
ratio('providers', lambda d: len(d['fac']), 0.9)
ratio('networks', lambda d: len(d['nets']), 1.0)
ratio('rate lines', lambda d: sum(c.get('n', 0) for c in d['codes'].values()), 0.8)
ratio('hospital files', lambda d: len(d['hosp']['src']), 0.95)
lost = sorted(set(O['nets']) - set(N['nets']))
if lost: problems.append('networks missing: ' + '; '.join(lost))
for c in ('45378', '99213', '470', '70553'):
    o, n = O['codes'].get(c, {}).get('n', 0), N['codes'].get(c, {}).get('n', 0)
    if o and n < 0.7 * o: problems.append(f'code {c} rate lines fell from {o:,} to {n:,}')
print('\n'.join(['PROBLEMS:'] + problems) if problems else 'all checks passed')
sys.exit(1 if problems else 0)
