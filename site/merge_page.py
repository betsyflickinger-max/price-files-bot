"""Put a finished monthly build into the Price Finder page.
usage: merge_page.py PAGE_HTML EMBED_JSON HOSP_INDEX_JSON
- insurer part (embed.json from every_code.py) replaces codes/fac/nets/dates/urls/labs/scope; cash/med/shop stay;
  codes that already had a proper name keep it; new codes arrive with the insurer's wording (flag d) and are named
  afterwards by apply_names_med.py
- hospital part (hosp_index.json from h2_build.py) replaces D.hosp (per-code lines load from pf/h/<code>.txt)"""
import json, sys
page, embp, hosp = sys.argv[1:4]
s = open(page).read(); tag = '<script type="application/json" id="data">'
a = s.index(tag) + len(tag); b = s.index('</script>', a)
D = json.loads(s[a:b]); new = json.load(open(embp)); old = D['codes']
for c, o in old.items():
    if c in new['codes'] and not o.get('d'):
        new['codes'][c].update(name=o['name'], cat=o['cat'], type=o.get('type', new['codes'][c].get('type'))); new['codes'][c].pop('d', None)
for k in ('codes', 'fac', 'nets', 'dates', 'urls', 'labs', 'scope'): D[k] = new[k]
for c in D['codes'].values(): c['f'] = None
D['hosp'] = json.load(open(hosp))
print('codes', len(D['codes']), 'providers', len(D['fac']), 'networks', len(D['nets']), 'hospital files', len(D['hosp']['src']),
      'dropped codes', len([c for c in old if c not in D['codes']]))
open(page, 'w').write(s[:a] + json.dumps(D, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/') + s[b:])
