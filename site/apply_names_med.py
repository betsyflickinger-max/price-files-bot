"""Patch the Price Finder data block: plain names, HCPCS Level II official descriptions, MS-DRG titles, Medicare for all codes."""
import json, re, sys, csv
page, medp, plainp, t5p = sys.argv[1:5]
s = open(page).read(); i = s.find('id="data"'); a = s.find('>', i) + 1; b = s.find('</script>', i)
D = json.loads(s[a:b]); M = json.load(open(medp))
def cat(code, t):
    if t == 'MS-DRG': return 'Inpatient stays'
    if t == 'HCPCS':
        c = code[0]
        return {'J': 'Drugs given by a provider', 'A': 'Supplies and transport', 'E': 'Medical equipment', 'L': 'Braces and artificial limbs',
                'G': 'Medicare-defined services', 'Q': 'Medicare-defined services', 'C': 'Hospital outpatient items', 'K': 'Medical equipment',
                'S': 'Commercial insurer services', 'T': 'Medicaid services', 'H': 'Behavioral health', 'V': 'Vision and hearing', 'P': 'Lab tests', 'R': 'Imaging', 'D': 'Dental', 'M': 'Medicare-defined services', 'B': 'Supplies and transport'}.get(c, 'Other services')
    if code.endswith('T'): return 'New or emerging services'
    if code.endswith('U'): return 'Lab tests'
    if code.endswith('F'): return 'Quality reporting'
    n = int(code) if code.isdigit() else -1
    for lo, hi, name in [(100, 1999, 'Anesthesia'), (10004, 19499, 'Skin and breast'), (20005, 29999, 'Orthopedics'), (30000, 32999, 'Lungs and nose'),
                         (33016, 37799, 'Heart and blood vessels'), (38100, 38999, 'Blood and lymph'), (39000, 39599, 'Chest'), (40490, 49999, 'Digestive system'),
                         (50010, 53899, 'Urology'), (54000, 55899, 'Urology'), (56405, 58999, 'Gynecology'), (59000, 59899, 'Maternity'), (60000, 60699, 'Endocrine'),
                         (61000, 64999, 'Brain, spine and nerves'), (65091, 68899, 'Eye'), (69000, 69979, 'Ear'), (70010, 76499, 'Imaging'), (76506, 76999, 'Ultrasound'),
                         (77001, 77799, 'Imaging and radiation therapy'), (78012, 79999, 'Nuclear medicine'), (80047, 89398, 'Lab tests'), (90281, 90756, 'Vaccines and shots'),
                         (90785, 90899, 'Behavioral health'), (90901, 99199, 'Other medical services'), (99202, 99499, 'Visits'), (99500, 99607, 'Home services')]:
        if lo <= n <= hi: return name
    return 'Other services'
plain = dict(l.rstrip('\n').split('\t') for l in open(plainp) if '\t' in l)
KEEP = {'MCC', 'CC', 'O.R.', 'MV', 'ECMO', 'HIV', 'AMI', 'CABG', 'PTCA', 'AICD', 'ECT', 'TIA', 'ENT', 'COPD', 'GI', 'CNS', 'DRG', 'W/O', 'CC/MCC', 'MCC/CC'}
def drg_case(t):
    w = t.split(' '); out = []
    for k, x in enumerate(w):
        u = x.strip('",()')
        out.append(x if u in KEEP else (x.lower() if k else x.capitalize()))
    r = ' '.join(out).replace('w/o', 'without')
    return r[0].upper() + r[1:]
drg = {}
for r in csv.reader(open(t5p, encoding='latin-1'), delimiter='\t'):
    if r and r[0].strip().isdigit(): drg[r[0].strip().lstrip('0')] = drg_case(r[5].strip().strip('"'))
n = {'plain': 0, 'hcpcs': 0, 'drg': 0}
for code, C in D['codes'].items():
    if not C.get('d'): continue
    if code in plain: C['name'] = plain[code]; C['cat'] = cat(code, C['type']); n['plain'] += 1; C.pop('d')
    elif C['type'] == 'HCPCS' and code in M['hcpcs_names']:
        nm = M['hcpcs_names'][code]; C['name'] = nm if len(nm) <= 140 else nm[:137].rsplit(' ', 1)[0] + '…'; C['cat'] = cat(code, 'HCPCS'); n['hcpcs'] += 1; C.pop('d')
    elif C['type'] == 'MS-DRG' and code in drg: C['name'] = drg[code]; C['cat'] = 'Inpatient stays'; n['drg'] += 1; C.pop('d')
    else: C['cat'] = cat(code, C['type']) + ' · name as written in the insurer file'
nm0 = len(D['med']['codes'])
for code, v in M['med'].items(): D['med']['codes'].setdefault(code, v)
print(n, 'medicare codes', nm0, '->', len(D['med']['codes']))
js = json.dumps(D, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')
s = s[:a] + js + s[b:]
# label drug/item lines per billing unit
s = s.replace("if (m.opps) L.push(['Hospital outpatient facility fee', 'national rate before local wage adjustment', m.opps[0], 'opps']);",
  "if (m.opps) L.push([/^[KGH]/.test(m.opps[1]) ? 'Hospital outpatient payment for this drug or item, per billing unit' : 'Hospital outpatient facility fee', 'national rate before local wage adjustment', m.opps[0], 'opps']);")
s = s.replace("if (m.asc) L.push(['Surgery center facility fee', 'national rate before local wage adjustment', m.asc[0], 'asc']);",
  "if (m.asc) L.push([/^[KH]/.test(m.asc[1]) ? 'Surgery center payment for this drug or item, per billing unit' : 'Surgery center facility fee', 'national rate before local wage adjustment', m.asc[0], 'asc']);")
open(page, 'w').write(s)
