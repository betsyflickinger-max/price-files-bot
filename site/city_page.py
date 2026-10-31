"""Price Finder page for one Texas city, built from the live DFW page and that city's finished insurer build.
Run by .github/workflows/city-page.yml (by hand, one city at a time).

  python site/city_page.py el-paso [--deploy]
  python site/city_page.py el-paso --template pf.html --embed embed.json --rvu rvu.zip --med-all med.json --t5 t5.txt --out /tmp/x   (offline test)

Reads (R2):
  site/deploy/price-finder.html                   the live DFW page: its layout and code is reused as is
  site/every-tx/<city>/<build>/embed.json         newest finished city build (written only when all 256 parts are in)
  reference/site/med_all.json, msdrg_table5.txt   the same names and Medicare inputs as the monthly refresh
  reference/2026-09-29/cms/rvu26d-*.zip           CMS 2026 physician fee schedule, for the city's Medicare doctor-fee locality
Writes the page to OUT/page.html. With --deploy: adds it to the preview site at /<city>/price-finder/ together with a small
function that serves the city's per-code rate files (R2 <build>/t/<code>.txt), deploys with wrangler, and saves the site
back to R2 site/deploy/current.tgz (the same steps as the monthly refresh). It also adds the city to site/cities.json, the list of
live pages that the area picker shows and that the monthly refresh rebuilds.

What changes from the DFW page: the city's providers, networks and rates; doctor fees in the Medicare benchmark use the
city's Medicare locality; DFW-only parts are left out (hospital price files, posted cash prices, the plan calculator link); the map gets the
city's own provider locations and background (geo_build.py, basemap_build.py); the words that name DFW. Every price shown still comes from an insurer file and links to it.
Any wording change that no longer matches the DFW page stops the build, so a city page never goes out half-edited."""
import argparse, csv, io, json, os, re, shutil, subprocess, sys, tarfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
def _in(*c): return 'in a ZIP code that lies mostly in ' + (', '.join(c[:-1]) + ' or ' if len(c) > 1 else '') + c[-1] + ' County'
# slug -> (name, where its providers are (shown on the page), Medicare locality number, locality name). Cities by 3-digit ZIP
# area as in tx_split.py; metro areas (Oct 12, 2026) by county, from reference/geo/tx_zip_metro.csv (Census ZIP -> county -> metro).
# Localities from CMS 2026 physician fee schedule GPCI2026.csv / 26LOCCO.csv (rvu26d): Beaumont 20 = Jefferson County;
# outside Austin, Houston and DFW the rest is 99 Rest of Texas.
CITIES = {'el-paso': ('El Paso', 'in a ZIP code starting 798–799', '99', 'Rest of Texas'),
          'austin': ('Austin', 'in a ZIP code starting 786–787', '31', 'Austin'),
          'san-antonio': ('San Antonio', 'in a ZIP code starting 780–782', '99', 'Rest of Texas'),
          'houston': ('Houston', 'in a ZIP code starting 770–775', '18', 'Houston'),
          'mcallen': ('McAllen–Edinburg–Mission', _in('Hidalgo'), '99', 'Rest of Texas'),
          'killeen-temple': ('Killeen–Temple', _in('Bell', 'Coryell', 'Lampasas'), '99', 'Rest of Texas'),
          'brownsville-harlingen': ('Brownsville–Harlingen', _in('Cameron'), '99', 'Rest of Texas'),
          'corpus-christi': ('Corpus Christi', _in('Aransas', 'Nueces', 'San Patricio'), '99', 'Rest of Texas'),
          'beaumont-port-arthur': ('Beaumont–Port Arthur', _in('Hardin', 'Jefferson', 'Orange'), '20', 'Beaumont'),
          'lubbock': ('Lubbock', _in('Cochran', 'Crosby', 'Garza', 'Hockley', 'Lubbock', 'Lynn'), '99', 'Rest of Texas'),
          'waco': ('Waco', _in('Bosque', 'Falls', 'McLennan'), '99', 'Rest of Texas'),
          'longview': ('Longview', _in('Gregg', 'Harrison', 'Rusk', 'Upshur'), '99', 'Rest of Texas'),
          'college-station-bryan': ('College Station–Bryan', _in('Brazos', 'Burleson', 'Robertson'), '99', 'Rest of Texas'),
          'amarillo': ('Amarillo', _in('Armstrong', 'Carson', 'Oldham', 'Potter', 'Randall'), '99', 'Rest of Texas'),
          'tyler': ('Tyler', _in('Smith'), '99', 'Rest of Texas'),
          'abilene': ('Abilene', _in('Callahan', 'Jones', 'Taylor'), '99', 'Rest of Texas'),
          'midland': ('Midland', _in('Martin', 'Midland'), '99', 'Rest of Texas'),
          'odessa': ('Odessa', _in('Ector'), '99', 'Rest of Texas'),
          'wichita-falls': ('Wichita Falls', _in('Archer', 'Clay', 'Wichita'), '99', 'Rest of Texas'),
          'texarkana': ('Texarkana', _in('Bowie'), '99', 'Rest of Texas'),
          'san-angelo': ('San Angelo', _in('Irion', 'Tom Green'), '99', 'Rest of Texas'),
          'victoria': ('Victoria', _in('Goliad', 'Victoria'), '99', 'Rest of Texas'),
          'small-towns': ('Small towns of Texas', 'in a Texas ZIP code not covered by another Texas Price Finder page', '99', 'Rest of Texas')}
# First national batch (Oct 13, 2026): one page per state, built from data/s7/ split by the provider's practice state.
# Localities from CMS 2026 GPCI2026.csv (checked at build time: the build stops if a state/locality pair is missing).
# GA and MO have more than one doctor-fee locality; the page uses the one with the most providers (Atlanta, St. Louis) and says so.
STATE_OF = {'tennessee': 'TN', 'colorado': 'CO', 'oklahoma': 'OK', 'alabama': 'AL', 'mississippi': 'MS', 'missouri': 'MO', 'georgia': 'GA',
            'north-carolina': 'NC', 'connecticut': 'CT', 'illinois': 'IL', 'arkansas': 'AR',
            'maryland': 'MD', 'new-mexico': 'NM', 'montana': 'MT', 'louisiana': 'LA',
            'indiana': 'IN', 'kentucky': 'KY', 'ohio': 'OH', 'arizona': 'AZ',
            'michigan': 'MI', 'north-dakota': 'ND', 'south-dakota': 'SD', 'iowa': 'IA', 'minnesota': 'MN', 'nebraska': 'NE', 'kansas': 'KS', 'pennsylvania': 'PA', 'delaware': 'DE', 'west-virginia': 'WV', 'new-jersey': 'NJ', 'massachusetts': 'MA', 'rhode-island': 'RI', 'vermont': 'VT', 'new-hampshire': 'NH', 'maine': 'ME', 'south-carolina': 'SC', 'washington': 'WA', 'oregon': 'OR', 'idaho': 'ID', 'utah': 'UT', 'alaska': 'AK', 'nevada': 'NV', 'hawaii': 'HI', 'wyoming': 'WY',
            'new-york': 'NY', 'california': 'CA', 'virginia': 'VA', 'district-of-columbia': 'DC', 'florida': 'FL', 'wisconsin': 'WI'}   # Oct 16, 2026: North Carolina (data/nc/) and Connecticut (data/ct/), each its own scope
SCOPE_OF = {'north-carolina': 'nc', 'connecticut': 'ct', 'illinois': 'il', 'arkansas': 'ar', 'maryland': 'b1', 'new-mexico': 'b1', 'montana': 'b1', 'louisiana': 'la', 'indiana': 'b4', 'kentucky': 'b4', 'ohio': 'b4', 'arizona': 'az',
            'michigan': 'c1', 'north-dakota': 'c1', 'south-dakota': 'c1', 'iowa': 'c1', 'minnesota': 'c1', 'nebraska': 'c1', 'kansas': 'c1', 'pennsylvania': 'c2', 'delaware': 'c2', 'west-virginia': 'c2', 'new-jersey': 'c2', 'massachusetts': 'c2', 'rhode-island': 'c2', 'vermont': 'c2', 'new-hampshire': 'c2', 'maine': 'c2', 'south-carolina': 'c2', 'washington': 'c3', 'oregon': 'c3', 'idaho': 'c3', 'utah': 'c3', 'alaska': 'c3', 'nevada': 'c3', 'hawaii': 'c3', 'wyoming': 'c3',
            'new-york': 'n5', 'california': 'n5', 'virginia': 'n5', 'district-of-columbia': 'n5', 'florida': 'n5', 'wisconsin': 'n5'}   # state pages not listed here are in the seven-state scope (data/s7/)
STATE_PAGES = {'tennessee': ('Tennessee', '35', 'Tennessee'), 'colorado': ('Colorado', '01', 'Colorado'),
               'oklahoma': ('Oklahoma', '00', 'Oklahoma'), 'alabama': ('Alabama', '00', 'Alabama'),
               'mississippi': ('Mississippi', '00', 'Mississippi'), 'missouri': ('Missouri', '01', 'Metropolitan St. Louis'),
               'georgia': ('Georgia', '01', 'Atlanta'), 'north-carolina': ('North Carolina', '00', 'North Carolina'),   # NC: one statewide locality (CMS locality file)
               'connecticut': ('Connecticut', '00', 'Connecticut'),
               'illinois': ('Illinois', '16', 'Chicago'), 'arkansas': ('Arkansas', '13', 'Arkansas'),
               'maryland': ('Maryland', '01', 'Baltimore and surrounding counties'), 'new-mexico': ('New Mexico', '05', 'New Mexico'),
               'montana': ('Montana', '01', 'Montana'), 'louisiana': ('Louisiana', '01', 'New Orleans'),
               'indiana': ('Indiana', '00', 'Indiana'), 'kentucky': ('Kentucky', '00', 'Kentucky'), 'ohio': ('Ohio', '00', 'Ohio'),
               'arizona': ('Arizona', '00', 'Arizona'),
               'michigan': ('Michigan', '01', 'Detroit'),
               'north-dakota': ('North Dakota', '01', 'North Dakota'),
               'south-dakota': ('South Dakota', '02', 'South Dakota'),
               'iowa': ('Iowa', '00', 'Iowa'),
               'minnesota': ('Minnesota', '00', 'Minnesota'),
               'nebraska': ('Nebraska', '00', 'Nebraska'),
               'kansas': ('Kansas', '00', 'Kansas'),
               'pennsylvania': ('Pennsylvania', '01', 'Metropolitan Philadelphia'),
               'delaware': ('Delaware', '01', 'Delaware'),
               'west-virginia': ('West Virginia', '16', 'West Virginia'),
               'new-jersey': ('New Jersey', '01', 'Northern New Jersey'),
               'massachusetts': ('Massachusetts', '01', 'Metropolitan Boston'),
               'rhode-island': ('Rhode Island', '01', 'Rhode Island'),
               'vermont': ('Vermont', '50', 'Vermont'),
               'new-hampshire': ('New Hampshire', '40', 'New Hampshire'),
               'maine': ('Maine', '03', 'Southern Maine'),
               'south-carolina': ('South Carolina', '01', 'South Carolina'),
               'washington': ('Washington', '02', 'Seattle (King County)'),
               'oregon': ('Oregon', '01', 'Portland'),
               'idaho': ('Idaho', '00', 'Idaho'),
               'utah': ('Utah', '09', 'Utah'),
               'alaska': ('Alaska', '01', 'Alaska'),
               'nevada': ('Nevada', '00', 'Nevada'),
               'hawaii': ('Hawaii', '01', 'Hawaii'),
               'wyoming': ('Wyoming', '21', 'Wyoming'),
               # Oct 30, 2026 (n5): localities from GPCI2026.csv (rvu26d). NY has 5, CA 29, FL 3: the page uses the metro one
               # (Manhattan, Los Angeles, Miami) and says so, same rule as MI/PA/NJ/MA. VA and WI are statewide (VA's Northern
               # Virginia suburbs are in DC's locality). DC's locality is "DC + MD/VA suburbs". Before this, the six n5 states
               # weren't listed here, so every publish failed with "unknown city" (VA, DC, FL, WI failed 3 times each).
               'new-york': ('New York', '01', 'Manhattan'),
               'california': ('California', '18', 'Los Angeles-Long Beach-Anaheim'),
               'virginia': ('Virginia', '00', 'Virginia'),
               'district-of-columbia': ('Washington, DC', '01', 'DC + MD/VA suburbs'),
               'florida': ('Florida', '04', 'Miami'),
               'wisconsin': ('Wisconsin', '00', 'Wisconsin')}   # Oct 23: localities checked against GPCI2026.csv (rvu26d). MI, PA, NJ, MA, ME, WA, OR have 2; the page uses the metro one (Detroit, Philadelphia, Northern NJ, Boston, Southern Maine, Seattle, Portland) and says so   # Oct 22: MD has 2 localities (01 Baltimore/surr. counties, 99 rest of MD); the page uses 01 and says so. NM 05, MT 01 statewide (GPCI2026.csv checked)   # IL has 4 doctor-fee localities; the page uses Chicago (16) and says so, like GA and MO   # CT: one statewide locality (the build stops if GPCI2026.csv disagrees)
for _k, (_n, _l, _ln) in STATE_PAGES.items(): CITIES[_k] = (_n, f'in {_n}', _l, _ln)
def root(slug): return 'site/every-st/' if slug in STATE_OF else 'site/every-tx/'   # where an area's insurer builds are
def npis_key(slug):
    slug = _regions.PARENT.get(slug, slug) if '_regions' in globals() else slug   # a region's providers are in its state's list (Oct 31)
    return f'data/{SCOPE_OF.get(slug, "s7")}/state/{slug}/npis.parquet' if slug in STATE_OF else f'data/tx/metro/{slug}/npis.parquet'
# how a sentence names the area ("every El Paso-area hospital", "no rate ... in the El Paso area"), when the name doesn't fit
AREA = {'small-towns': ('small-town Texas', 'small-town Texas'), **{k: (v[0], v[0]) for k, v in STATE_PAGES.items()}}
# map area per city: a location outside this box is treated as a bad match and left off the map (geo_build.py GEO_BBOX).
# Metro areas: their counties' extent (Census TIGERweb) plus 0.3 degrees; small towns: all of Texas.
GEO_BBOX = {'el-paso': '-106.7,29.2,-102.4,32.1', 'austin': '-99.2,29.4,-96.6,31.3',
            'san-antonio': '-100.6,27.9,-96.7,30.4', 'houston': '-96.9,28.4,-93.9,31.3',
            'mcallen': '-98.9,25.7,-97.5,27.1', 'killeen-temple': '-98.9,30.4,-96.7,32.1', 'brownsville-harlingen': '-98.2,25.5,-96.7,26.8',
            'corpus-christi': '-98.3,27.2,-96.4,28.7', 'beaumont-port-arthur': '-95.1,29.2,-93.3,30.9', 'lubbock': '-103.4,32.6,-100.7,34.2',
            'waco': '-98.4,30.6,-96.2,32.6', 'longview': '-95.5,31.5,-93.7,33.3', 'college-station-bryan': '-97.3,29.9,-95.7,31.7',
            'amarillo': '-103.4,34.4,-100.7,36.0', 'tyler': '-95.9,31.8,-94.6,33.0', 'abilene': '-100.5,31.7,-98.8,33.3',
            'midland': '-102.6,31.3,-101.3,32.9', 'odessa': '-103.1,31.3,-101.9,32.4', 'wichita-falls': '-99.3,33.0,-97.6,34.6',
            'texarkana': '-95.1,32.9,-93.7,34.1', 'san-angelo': '-101.6,30.7,-99.8,32.1', 'victoria': '-98.1,28.0,-96.3,29.5',
            'small-towns': '-106.8,25.7,-93.4,36.7',
            'tennessee': '-90.6,34.7,-81.4,36.9', 'colorado': '-109.3,36.7,-101.8,41.3', 'oklahoma': '-103.3,33.4,-94.2,37.3',
            'alabama': '-88.7,29.9,-84.7,35.3', 'mississippi': '-91.9,29.9,-87.9,35.3', 'missouri': '-96.0,35.7,-88.9,40.9',
            'georgia': '-85.9,30.1,-80.6,35.3', 'north-carolina': '-84.6,33.5,-75.2,36.9', 'connecticut': '-74.0,40.7,-71.5,42.3', 'illinois': '-91.7,36.8,-87.3,42.7', 'arkansas': '-94.7,32.9,-89.6,36.6',
            'maryland': '-79.5,37.9,-75.0,39.8', 'new-mexico': '-109.1,31.3,-103.0,37.0', 'montana': '-116.1,44.3,-104.0,49.0', 'louisiana': '-94.1,28.9,-88.8,33.1',
            'indiana': '-88.1,37.7,-84.7,41.8', 'kentucky': '-89.6,36.4,-81.9,39.2', 'ohio': '-84.9,38.4,-80.5,42.3', 'arizona': '-114.9,31.3,-109.0,37.0',
            'michigan': '-90.5,41.6,-82.1,48.4', 'north-dakota': '-104.1,45.9,-96.5,49.0', 'south-dakota': '-104.1,42.4,-96.4,45.95', 'iowa': '-96.7,40.3,-90.1,43.6', 'minnesota': '-97.3,43.4,-89.4,49.4', 'nebraska': '-104.1,39.9,-95.3,43.1', 'kansas': '-102.1,36.9,-94.5,40.1', 'pennsylvania': '-80.6,39.7,-74.6,42.3', 'delaware': '-75.8,38.4,-75.0,39.9', 'west-virginia': '-82.7,37.2,-77.7,40.7', 'new-jersey': '-75.6,38.9,-73.8,41.4', 'massachusetts': '-73.6,41.2,-69.9,42.9', 'rhode-island': '-71.9,41.1,-71.1,42.1', 'vermont': '-73.5,42.7,-71.4,45.1', 'new-hampshire': '-72.6,42.6,-70.6,45.4', 'maine': '-71.1,42.9,-66.9,47.5', 'south-carolina': '-83.4,32.0,-78.5,35.3', 'washington': '-124.8,45.5,-116.9,49.1', 'oregon': '-124.6,41.9,-116.4,46.3', 'idaho': '-117.3,41.9,-111.0,49.1', 'utah': '-114.1,36.9,-109.0,42.1', 'alaska': '-179.2,51.2,-129.9,71.5', 'nevada': '-120.1,35.0,-114.0,42.1', 'hawaii': '-160.3,18.9,-154.8,22.3', 'wyoming': '-111.1,40.9,-104.0,45.1',
            'new-york': '-80.1,40.2,-71.5,45.3', 'california': '-124.8,32.2,-113.8,42.3', 'virginia': '-84.0,36.2,-74.9,39.8',
            'district-of-columbia': '-77.4,38.5,-76.6,39.3', 'florida': '-87.9,24.2,-79.7,31.3', 'wisconsin': '-93.2,42.2,-86.0,47.4'}
# Oct 31, 2026 (Betsy chose regional pages): a state too big for one page file (California, 32.5 MB; the host takes 25 MB) is
# published as regional pages built from its own split builds (site/regions.py, site/region_split.py). Each region is a page like
# any other; the state's own address becomes a short page linking to them (landing_page below).
import regions as _regions
for _st, _R in _regions.REGIONS.items():
    for _r, (_n, _l, _ln, _box, _cs) in _R.items():
        CITIES[_r] = (_n, _regions.by_county_text(_cs), _l, _ln)
        STATE_OF[_r] = STATE_OF[_st]
        if _st in SCOPE_OF: SCOPE_OF[_r] = SCOPE_OF[_st]
        AREA[_r] = (_n, _n)
        GEO_BBOX[_r] = _box
PAGE_MAX = 24 * 1024 * 1024   # Cloudflare Pages takes files up to 25 MiB; a bigger page stops before the deploy, saying why
MAP_NOTE = ('<p><b>Map.</b> The map places each provider at the practice address it gives the federal NPI Registry, using the '
            'U.S. Census Bureau\'s geocoder. Roads, lakes, county lines and city names come from the Census Bureau\'s TIGERweb. '
            'Places we couldn\'t place are still listed, marked "not on map". A ZIP code or location you enter is used only on '
            'the page to measure distance and is never saved or sent. <a href="/methodology/#map">How the map works</a>.</p>\n  ')
TAG = '<script type="application/json" id="data">'
FEATURED_MIN = 45   # same broken-build test as the dashboard: a finished build needs rates and at least 45 of the 89 featured codes
FEATURED = {"00811", "00812", "19120", "216", "22551", "27130", "27447", "29826", "29827", "29881", "29888", "42820", "43235", "43239", "43644",
            "43775", "45378", "45380", "45385", "45391", "460", "470", "473", "47562", "49505", "55250", "55700", "55866", "59400", "59510", "59610",
            "62322", "62323", "63030", "64483", "64721", "66821", "66984", "70450", "70551", "70553", "72110", "72148", "72193", "73721", "74177",
            "743", "75571", "76700", "76805", "76830", "77065", "77066", "77067", "80048", "80053", "80055", "80061", "80069", "80076", "81000",
            "81001", "81002", "81003", "84153", "84154", "84443", "85025", "85027", "85610", "85730", "88305", "90832", "90834", "90837", "90846",
            "90847", "90853", "93000", "93452", "95810", "97110", "99203", "99204", "99205", "99243", "99244", "99385", "99386"}


def split(s):
    a = s.index(TAG) + len(TAG); b = s.index('</script>', a)
    return s[:a], json.loads(s[a:b]), s[b:]


def join(pre, D, post):
    return pre + json.dumps(D, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/') + post


class Words:
    """Wording changes on the page's own text (never inside the price data). Each must match, or the build stops."""
    def __init__(self, s): self.s = s
    def sub(self, old, new, n=1, regex=False):
        found = len(re.findall(old, self.s, flags=re.S)) if regex else self.s.count(old)
        if found != n: sys.exit(f'STOP: page wording changed: expected {n} x {old[:90]!r}, found {found}. Update city_page.py.')
        self.s = re.sub(old, new.replace('\\', '\\\\'), self.s, flags=re.S) if regex else self.s.replace(old, new)


def practice_and_list_price(page, s3, B, out):
    """Oct 19, 2026 (Betsy OK'd): the practice view (practice_view.py) and the dollar math for percent-of-charges contracts
    (list_price_math.py). Both are page code that does nothing without its data, so they go on every page. D.hlink links each
    insurer-file hospital to its own price file: CMS Hospital Enrollments NPIs + the NPIs each hospital file lists, via the
    CMS number the hospital step recorded for each file (site/builds/hosp-*/areas.json)."""
    import practice_view, list_price_math
    s = open(page, encoding='utf-8').read()
    pre, D, post = split(s)
    hl = {}
    try:
        if D.get('hosp', {}).get('src'):
            areas = {}
            for pg in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix='site/builds/hosp-'):
                for o in pg.get('Contents', []):
                    if o['Key'].endswith('/areas.json'):
                        for k_, v_ in json.loads(s3.get_object(Bucket=B, Key=o['Key'])['Body'].read()).items(): areas[o['Key'] + k_] = v_
            s3.download_file(B, list_price_math.K_ENROLL, f'{out}/hospital_enrollments.csv')
            names = json.loads(s3.get_object(Bucket=B, Key='reference/geo/names.json')['Body'].read())
            npis = json.load(open(f'{out}/npis.json'))
            hl = list_price_math.hlink_for(D['hosp']['src'], areas, f'{out}/hospital_enrollments.csv', npis, names)
    except Exception as e:
        print('hospital links for the list-price math not built (the page shows percentages alone):', repr(e)[:200])
    D['hlink'] = hl
    import practice_names   # Oct 21, 2026: one spelling per practice/organization name (punctuation, spacing, capitals only)
    if D.get('prac'): print('practice names respelled:', practice_names.same_spelling(D['prac']))
    if D.get('fac'): print('organization names respelled:', practice_names.same_spelling_fac(D['fac']))
    s = join(pre, D, post)
    s = list_price_math.patch(practice_view.patch(s))
    open(page, 'w', encoding='utf-8').write(s)
    print(f"practice view {'with' if D.get('prac') else 'without'} practices; list-price math links {len(hl)} providers to their hospital files")


def pfs_for(rvu_zip, loc, codes, st='TX'):
    """Medicare doctor fees for one locality: same formula and filters as build_med_all.py. code -> {'pfs'|'pfs26': [office, facility]}"""
    Z = zipfile.ZipFile(rvu_zip)
    G = {(r[1], r[2]): [float(r[4]), float(r[5]), float(r[6])] for r in csv.reader(io.TextIOWrapper(Z.open('GPCI2026.csv'), encoding='latin-1'))
         if len(r) > 6 and r[1] in ('TX', st)}
    if (st, loc) not in G: sys.exit(f'STOP: Medicare locality {st} {loc} is not in GPCI2026.csv; fix STATE_PAGES in city_page.py')
    def num(x):
        try: return float(str(x).replace('$', '').replace(',', '').strip())
        except ValueError: return None
    L = list(csv.reader(io.TextIOWrapper(Z.open('PPRRVU2026_Oct_nonQPP.csv'), encoding='latin-1')))
    k = next(j for j, r in enumerate(L) if r and r[0].strip() == 'HCPCS')
    out = {}
    for r in L[k + 1:]:
        if len(r) < 26 or r[0] not in codes or r[1] not in ('', '26') or r[3] not in ('A', 'R', 'T'): continue
        w, pen, nfna, pef, fna, mp, cf = num(r[5]), num(r[6]), r[7].strip(), num(r[8]), r[9].strip(), num(r[10]), num(r[25])
        def at(l, s_=st):
            gw, gp, gm = G[(s_, l)]
            nf = None if nfna == 'NA' else round((w * gw + pen * gp + mp * gm) * cf, 2)
            fa = None if fna == 'NA' else round((w * gw + pef * gp + mp * gm) * cf, 2)
            return [nf, fa if fa is not None else nf]
        if all(x is None or x == 0 for x in at('11', 'TX')): continue   # build_med_all.py skips these on the Dallas figures
        out.setdefault(r[0], {})['pfs26' if r[1] == '26' else 'pfs'] = {'11': at('11', 'TX'), loc: at(loc)}
    return out


def build(slug, template, embed, rvu, med_all, t5, out, hosp_index=None, arb_file=None, npis_parquet=None):
    name, where, loc, locname = CITIES[slug]
    adj, place = AREA.get(slug, (f'{name}-area', f'the {name} area'))
    os.makedirs(out, exist_ok=True)
    pre, T, post = split(open(template, encoding='utf-8').read())
    E = json.load(open(embed))
    lines = sum(c.get('n', 0) for c in E['codes'].values()); have = sum(c in E['codes'] for c in FEATURED)
    if lines == 0 or have < FEATURED_MIN:
        sys.exit(f'STOP: the {name} build looks broken: {len(E["codes"])} codes, {lines} rate lines, {have} of 89 featured codes')
    print(f'{name}: {len(E["codes"]):,} codes, {len(E["fac"]):,} providers, {lines:,} rate lines, {have} of 89 featured codes')

    # 1. price data: the city's insurer build in place of DFW's (merge_page.py, as in the monthly refresh); no DFW hospital
    #    price files and no DFW posted cash prices (empty, same shape, so the page shows "none")
    #    (Oct 13, 2026: with hosp_index, the area's own hospital price files from hosp_area.py take DFW's place)
    T['hosp'] = {k: ([] if isinstance(v, list) else {} if isinstance(v, dict) else v) for k, v in T['hosp'].items()}
    T['cash'] = []
    page, hosp = f'{out}/page.html', f'{out}/hosp_empty.json'
    open(page, 'w', encoding='utf-8').write(join(pre, T, post)); json.dump(T['hosp'], open(hosp, 'w'))
    if hosp_index:
        H = json.load(open(hosp_index))
        if set(H) != set(T['hosp']): sys.exit(f'STOP: hospital index keys {sorted(H)} differ from the page\'s {sorted(T["hosp"])}')
        hosp, nh = hosp_index, len(H['src'])
        print(f'{name}: {nh} hospital price files')
    py = sys.executable
    subprocess.run([py, f'{HERE}/merge_page.py', page, embed, hosp], check=True)
    names = f'{out}/plain_names_all.tsv'; rows = {}
    for f in (f'{HERE}/plain_names_auto.tsv', f'{HERE}/plain_names_cpt.tsv'):   # hand-written names win, as in orchestrate.py
        if os.path.exists(f):
            for l in open(f):
                if '\t' in l: c, n = l.rstrip('\n').split('\t', 1); rows[c] = n
    open(names, 'w').write(''.join(f'{c}\t{n}\n' for c, n in rows.items()))
    subprocess.run([py, f'{HERE}/apply_names_med.py', page, med_all, names, t5], check=True)
    pre, D, post = split(open(page, encoding='utf-8').read())

    # 1b. No Surprises Act arbitration figures for this metro area (nsa/arb_page.py), never the DFW template's;
    #     none when CMS reports no such area (small towns, other states)
    D.pop('arb', None)
    if arb_file and os.path.exists(arb_file):
        sys.path.insert(0, os.path.join(HERE, '..', 'nsa')); import arb_page
        npp = os.path.join(os.path.dirname(os.path.abspath(embed)), 'npis.json')
        a = arb_page.page_arb(json.load(open(arb_file)), slug, json.load(open(npp)) if os.path.exists(npp) else None, set(D['codes']))
        if a: D['arb'] = a
        print(f"{name}: arbitration figures for {a['region']}: {len(a['c']):,} codes, {len(a['p']):,} providers" if a else f'{name}: no arbitration area')

    # 2. Medicare doctor fees for the city's locality. Check first: the same formula must give the page's own Dallas figures.
    M = D['med']['codes']
    P = pfs_for(rvu, loc, set(M), STATE_OF.get(slug, 'TX'))
    checked = bad = 0
    for c, m in M.items():
        for k in ('pfs', 'pfs26'):
            if k in m:
                checked += 1
                if P.get(c, {}).get(k, {}).get('11') != m[k].get('dal'): bad += 1
    print(f'Medicare doctor fees: {checked:,} checked against the DFW page, {bad} differ')
    if not checked or bad > 0.001 * checked: sys.exit('STOP: Medicare doctor fees do not reproduce the DFW page; nothing written')
    dropped = 0
    for c, m in M.items():
        for k in ('pfs', 'pfs26'):
            if k not in m: continue
            v = P.get(c, {}).get(k, {}).get(loc)
            if v is None or bad and P.get(c, {}).get(k, {}).get('11') != m[k].get('dal'): m.pop(k); dropped += 1   # never leave a Dallas figure
            else: m[k] = {'dal': v}   # the page reads the 'dal' key; it now holds this city's locality
    M = {c: m for c, m in M.items() if m}; D['med']['codes'] = M
    print(f'Medicare doctor fees set to the {locname} locality ({loc}); {dropped} left out')

    # 2b. Oct 30, 2026 (Betsy: "as accurate as possible"): in a state with more than one Medicare doctor-fee area, each
    #     provider is compared with Medicare for the area of their own practice ZIP (site/med_areas.py). The page's own
    #     figures above stay as they are; D.med.ar adds the other areas. Without the ZIP list the page is built as before.
    import med_areas
    D['med'].pop('ar', None)   # never the DFW template's own areas
    npp = os.path.join(os.path.dirname(os.path.abspath(embed)), 'npis.json')
    if STATE_OF.get(slug, 'TX') in med_areas.AREAS:
        if npis_parquet and os.path.exists(npis_parquet) and os.path.exists(npp):
            import duckdb
            zip_of = dict(duckdb.connect().execute(f"select npi, zip5 from read_parquet('{npis_parquet}')").fetchall())
            try: print(med_areas.attach(D, STATE_OF.get(slug, 'TX'), json.load(open(npp)), zip_of, rvu, loc))
            except ValueError as e: D['med'].pop('ar', None); print(f'Medicare areas: NOT added ({e}); every provider is compared with {locname}')
        else:
            print(f'Medicare areas: NOT added ({"no provider ZIP list" if not (npis_parquet and os.path.exists(npis_parquet)) else "no npis.json"}); every provider is compared with {locname}')

    # 3. wording
    w = Words(pre + '\x00' + post)
    w.s = w.s.replace('What It Costs', 'The Healthcare Files')   # Oct 18 rename: layouts saved before the rename still carry the old name
    w.sub('<title>The Healthcare Files Price Finder</title>', f'<title>{name} Price Finder · The Healthcare Files</title>')
    w.sub('content="Price Finder · The Healthcare Files"', f'content="{name} Price Finder · The Healthcare Files"')
    # Oct 19, 2026: the live layout's share link (og:url) still named the old preview address, so this check stopped every
    # state/city page build ("expected 1 x content=...files.healthcare/price-finder/"). Both addresses become the page's own.
    w.s = w.s.replace('content="https://upfront-dfw-preview.pages.dev/price-finder/"', 'content="https://files.healthcare/price-finder/"')
    w.sub('content="https://files.healthcare/price-finder/"', f'content="https://files.healthcare/{slug}/price-finder/"')
    # the canonical link (added Oct 18) pointed every state/city page at the DFW page
    if '<link rel="canonical" href="https://files.healthcare/price-finder/">' in w.s:
        w.sub('<link rel="canonical" href="https://files.healthcare/price-finder/">', f'<link rel="canonical" href="https://files.healthcare/{slug}/price-finder/">')
    w.sub(r'<meta property="og:image[^>]*>', '', n=4, regex=True)   # the share image shows a DFW hospital
    w.sub('content="summary_large_image"', 'content="summary"')
    w.sub('every DFW hospital, surgery center, doctor, lab and imaging center,',
          f'every {adj} hospital, surgery center, doctor, lab and imaging center,', n=2)   # page description (Oct 12 wording)
    nb = ''
    if slug in _regions.PARENT:   # Oct 31, 2026 (Betsy): regional pages link to the regions next to them
        st_ = _regions.PARENT[slug]; R_ = _regions.REGIONS[st_]
        nb = (f'<p class="note" style="margin:6px 0 0">{STATE_PAGES[st_][0]} is split into {len(R_)} regional pages. Near a border? Also see '
              + ' · '.join(f'<a href="/{n}/price-finder/">{R_[n][0]}</a>' for n in _regions.NEIGHBORS.get(slug, []))
              + (f' · <a href="/{st_}/price-finder/">all of {STATE_PAGES[st_][0]}</a></p>' if st_ in _regions.ALONGSIDE
                 else f' · <a href="/{st_}/price-finder/">all {STATE_PAGES[st_][0]} regions</a></p>'))
    w.sub('<div class="eyebrow">Dallas–Fort Worth · published prices</div>', f'<div class="eyebrow">{name} · published prices</div>{nb}')
    w.sub(r'<div class="note" id="planNote">.*?</div>',
          f'<div class="note" id="planNote"><b>Have a HealthCare.gov plan?</b> Insurer prices below are not what you pay. Your share '
          f'depends on your plan\'s deductible, copay and coinsurance. Our plan calculator doesn\'t cover {adj} plans yet.</div>', regex=True)
    w.sub(' If you have a marketplace plan, the <a href="/plan-calculator/">plan calculator</a> works it out for you.', '')
    w.sub(' DFW providers have a published rate in this network.', f' {adj[0].upper() + adj[1:]} providers have a published rate in this network.')
    w.sub("'This network has no published rate for this code in DFW.'", f"'This network has no published rate for this code in {place}.'")
    dates = sorted(d for d in set(D['dates']) if d)
    ins = sorted({n.split('|', 1)[0] for n in D['nets'] if D.get('scope', {}).get(n)}) or sorted({n.split('|', 1)[0] for n in D['nets']})
    w.sub(r'<p><b>Sources\.</b>.*?</p>',
          f'<p><b>Sources.</b> Insurer Transparency in Coverage files dated {dates[0]} to {dates[-1]}: {", ".join(ins)}. Each file was read '
          f'for every {STATE_PAGES[_regions.PARENT.get(slug, slug)][0] + " provider" if slug in SCOPE_OF else "provider in Tennessee, Colorado, Oklahoma, Alabama, Mississippi, Missouri and Georgia" if slug in STATE_OF else "Texas provider"}, then narrowed to providers whose practice address in the federal NPI Registry is '
          f'{where}. Each rate links to the file it came from. Some insurer links expire and must be re-opened from the '
          f'insurer\'s own page. ' + (f'Hospital price files: {nh} files from hospitals {where}, each line linked to its file. Providers\' posted cash '
          f'prices are not on the {name} page yet.</p>' if hosp_index else f'Hospital price files and providers\' posted cash prices are not on the {name} page yet.</p>'), regex=True)
    # hospital files, cash prices and quality ratings aren't on city pages yet: say so instead of "none found"
    notyet = lambda what: f'{what} are not on the {name} page yet.'
    if not hosp_index:
        w.sub('No hospital price file lists a rate from this insurer for this code.', notyet('Hospital price files'))
        w.sub('No hospital price file lists this code for this insurer.', notyet('Hospital price files'))
        w.sub("None of the hospitals' price files lists a cash price for this code.", notyet('Hospital price files'))
    w.sub('No provider on our list posts a cash price for this code yet.', notyet("Providers' posted cash prices"))
    w.sub(r'<p><b>Quality ratings\.</b>.*?</p>', f'<p><b>Quality ratings.</b> {notyet("Public quality ratings")}</p>', regex=True)
    w.sub(r'<p><b>Map\.</b>.*?</p>', '', regex=True)
    w.sub('(October release, Dallas locality)', f'(October release, {locname} locality)')
    w.sub('Every DFW provider that appears in the insurer files we read is included',
          f'Every provider with a practice address {where} that appears in the insurer files we read is included')
    w.sub(' Fort Worth amounts differ by a few dollars.', '')
    w.sub("'Dallas area", f"'{locname} Medicare area", n=w.s.count("'Dallas area") or 1)
    w.sub('<button type="button" id="vMap" aria-pressed="false">Map</button>',
          '<button type="button" id="vMap" aria-pressed="false" hidden>Map</button>')   # the map's base layer and locations are DFW only
    left = [m.group(0) for m in re.finditer(r'.{40}(?:DFW|Dallas|Fort Worth).{40}', w.s)]
    if left: sys.exit('STOP: DFW wording left on the page:\n  ' + '\n  '.join(left[:10]))
    pre, post = w.s.split('\x00')
    open(page, 'w', encoding='utf-8').write(med_areas.patch_page(join(pre, D, post)) if D['med'].get('ar') else join(pre, D, post))
    print(f'wrote {page}: {len(D["codes"]):,} codes, {len(D["fac"]):,} providers, {len(D["nets"])} networks, insurers {", ".join(ins)}')
    return page


QUALITY_KEYS = {'hs', 'pm', 'PR', 'pper', 'nat', 'h', 'H', 'a', 'd', 'hn', 'src', 'checked'}
BY_PROVIDER = {'hs', 'pm', 'h', 'a', 'd'}   # keyed by the DFW page's provider or hospital-file positions


def empty_quality(dfw_quality_txt, out):
    """The quality ratings file is matched to providers by their position in the DFW page, so it must never be served
    with a city page. The city gets the same file with every provider match removed (no ratings shown, nothing wrong shown).
    dfw_quality_txt: the DFW file (base64 of gzip JSON) to copy the shape from, or None to use the known shape."""
    import base64, gzip
    if dfw_quality_txt:
        Q = json.loads(gzip.decompress(base64.b64decode(open(dfw_quality_txt).read().strip())))
        if set(Q) != QUALITY_KEYS: sys.exit(f'STOP: the quality file changed shape ({sorted(set(Q) ^ QUALITY_KEYS)}); update city_page.py')
    else:
        Q = {'hs': {}, 'pm': {}, 'PR': {}, 'pper': {}, 'nat': {}, 'h': {}, 'H': {}, 'a': {}, 'd': {}, 'hn': [], 'src': {}, 'checked': ''}
    for k in BY_PROVIDER: Q[k] = {}
    open(out, 'w').write(base64.b64encode(gzip.compress(json.dumps(Q, separators=(',', ':')).encode(), 6)).decode())
    return out


FUNC = """// {name} Price Finder: per-code insurer rate files from R2 (written by price-files-bot site/city_page.py)
const PREFIX = {prefix};
export async function onRequestGet({{ params, env }}) {{
  const code = String(params.code || '').replace(/\\.txt$/, '');
  if (!/^[A-Za-z0-9.-]{{1,12}}$/.test(code)) return new Response('not found', {{ status: 404 }});
  const obj = await env.PF.get(PREFIX + 't/' + code + '.txt');
  if (!obj) return new Response('not found', {{ status: 404 }});
  return new Response(obj.body, {{ headers: {{ 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'public, max-age=3600' }} }});
}}
"""


def live_list(d, slug, name):
    """site/cities.json: every live Price Finder page, read by the page's area picker and by the monthly refresh (orchestrate.py)."""
    f = f'{d}/site/cities.json'
    try: L = json.load(open(f, encoding='utf-8'))
    except Exception: L = []
    if not any(c['slug'] == 'dfw' for c in L): L.append({'slug': 'dfw', 'name': 'Dallas–Fort Worth', 'path': '/price-finder/'})
    L = [c for c in L if c['slug'] != slug] + [{'slug': slug, 'name': name, 'path': f'/{slug}/price-finder/'}]
    order = ['dfw'] + list(CITIES)
    L.sort(key=lambda c: order.index(c['slug']) if c['slug'] in order else len(order))
    json.dump(L, open(f, 'w', encoding='utf-8'), ensure_ascii=False)


BASEMAP_V = 2   # basemap_build.py output version: a cached background older than this is rebuilt (v2: city names fixed, Oct 15, 2026)


def make_map(A, E, s3, B, written, prefix, page):
    """Map files for one page: provider locations (cached per build) and the background (cached per area). Rebuilt when the
    cached copy is out of date: locations made before the page had its hospital price files or a ZIP list (Oct 15, 2026: every
    Texas city map showed every hospital price as "not on map" because its locations were cached before Oct 13), or a background
    older than BASEMAP_V. If either fails, the page goes out without its Map button, exactly as before maps existed for cities."""
    gkey, bkey, gpath, bpath = prefix + 'geo.json', f'site/deploy/cities/{A.city}/basemap.json', f'{A.out}/geo.json', f'{A.out}/basemap.json'
    env = dict(E, NPIS=npis_key(A.city), GEO_NPIS_JSON=prefix + 'npis.json', GEO_OUT=gkey, GEO_BBOX=GEO_BBOX[A.city],
               GEO_STATE=STATE_OF.get(A.city, 'TX'), GEO_STATE_NAME=CITIES[_regions.PARENT.get(A.city, A.city)][0] if A.city in STATE_OF else 'Texas')
    try:
        s = open(page, encoding='utf-8').read()
        nh = len(json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', s, re.S).group(1))['hosp']['src'])
        fresh = False
        if written(gkey):
            s3.download_file(B, gkey, gpath); g = json.load(open(gpath))
            fresh = g['stats'].get('hospital_files', 0) == nh and bool(g.get('zips')) and 'unplaced' in g and not E.get('GEO_REBUILD')   # 'unplaced': Medicare-address placing (Oct 15); GEO_REBUILD=1 forces
            if not fresh: print(f"map locations are out of date (hospital files {g['stats'].get('hospital_files', 0)} vs {nh} on the page, {len(g.get('zips') or {})} ZIP codes); rebuilding")
        if not fresh:
            r = subprocess.run([sys.executable, f'{HERE}/geo_build.py', page, prefix], env=env, capture_output=True, text=True, timeout=5400)
            print(r.stdout[-2000:], r.stderr[-2000:])
            if r.returncode: raise RuntimeError('geo_build.py failed')
            s3.download_file(B, gkey, gpath)
        ok = False
        if written(bkey):
            s3.download_file(B, bkey, bpath)
            try: ok = json.load(open(bpath)).get('v', 1) >= BASEMAP_V
            except Exception: ok = False
        if not ok:
            r = subprocess.run([sys.executable, f'{HERE}/basemap_build.py', gpath, page, bpath], env=env, capture_output=True, text=True, timeout=1800)
            print(r.stdout[-1500:], r.stderr[-1500:])
            if r.returncode: raise RuntimeError('basemap_build.py failed')
            s3.upload_file(bpath, B, bkey)
        g = json.load(open(gpath))
        print(f"map: {g['stats']['placed']:,} of {g['stats']['providers']:,} providers placed, {g['stats']['hospital_files_placed']} of {nh} hospital files, {len(g['zips'])} ZIP codes")
        on = '<button type="button" id="vMap" aria-pressed="false" hidden>Map</button>'
        if s.count(on.replace(' hidden>', '>')) == 1 and MAP_NOTE[:60] in s: return True   # already has its map (--map-only)
        if s.count(on) != 1 or s.count('<p>Published prices are not quotes.') != 1: raise RuntimeError('page wording for the map changed')
        s = s.replace(on, on.replace(' hidden>', '>')).replace('<p>Published prices are not quotes.', MAP_NOTE + '<p>Published prices are not quotes.')
        open(page, 'w', encoding='utf-8').write(s)
        return True
    except Exception as e: print('map left off this page:', e); return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('city'); ap.add_argument('--deploy', action='store_true'); ap.add_argument('--out', default='/tmp/city')
    ap.add_argument('--map-only', action='store_true', help='rebuild only the map files for the published page (no deploy)')
    ap.add_argument('--check', action='store_true', help='build the page only: no map, nothing saved to R2, no deploy (Oct 30)')
    for a in ('template', 'embed', 'rvu', 'med-all', 't5', 'quality'): ap.add_argument('--' + a)
    A = ap.parse_args()
    if A.city not in CITIES: sys.exit(f'unknown city {A.city!r}; choose one of {", ".join(CITIES)}')
    name = CITIES[A.city][0]
    os.makedirs(A.out, exist_ok=True)
    if A.city in _regions.REGIONS and A.city not in _regions.ALONGSIDE:   # Oct 31, 2026: California's address links to its regions
        return landing_page(A)
    if A.template:   # offline test: local files only
        empty_quality(A.quality, f'{A.out}/quality.txt')
        return build(A.city, A.template, A.embed, A.rvu, A.med_all, A.t5, A.out)
    import boto3
    E = os.environ
    for k in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET', 'CLOUDFLARE_API_TOKEN'):
        if k in E: E[k] = ''.join(E[k].split())
    B = E.get('R2_BUCKET', 'upfrontdfwdata')
    s3 = boto3.client('s3', endpoint_url=(os.environ.get("STORAGE_ENDPOINT") or f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"), aws_access_key_id=E['R2_ACCESS_KEY_ID'],
                      aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name=(os.environ.get('STORAGE_REGION') or 'auto'))
    def written(k):
        try: return s3.head_object(Bucket=B, Key=k)['LastModified']
        except Exception: return None
    dirs = [c['Prefix'] for pg in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix=f'{root(A.city)}{A.city}/', Delimiter='/')
            for c in pg.get('CommonPrefixes', [])]
    done = sorted((t, d) for d in dirs if (t := written(d + 'embed.json')))   # newest finished build by when its embed.json was written
    if not done: sys.exit(f'{name}: no finished build yet (no embed.json under {root(A.city)}{A.city}/); nothing to do')
    prefix = done[-1][1]
    print('using', prefix)
    f = {k: f'{A.out}/{n}' for k, n in [('template', 'dfw.html'), ('embed', 'embed.json'), ('rvu', 'rvu.zip'), ('med', 'med.json'), ('t5', 't5.txt')]}
    for k, key in [('template', 'site/deploy/price-finder.html'), ('embed', prefix + 'embed.json'), ('rvu', 'reference/2026-09-29/cms/rvu26d-updated-08-26-2026.zip'),
                   ('med', 'reference/site/med_all.json'), ('t5', 'reference/site/msdrg_table5.txt')]:
        if not A.map_only: s3.download_file(B, key, f[k])
    hidx, hpre = None, None
    for _, d_ in reversed(done):   # the area's hospital price files (site/hosp_area.py): newest insurer build that has them
        if written(d_ + 'hosp/hosp_index.json'):
            hpre = d_ + 'hosp/'; hidx = f'{A.out}/hosp_index.json'; s3.download_file(B, hpre + 'hosp_index.json', hidx); break
    if hidx: print('hospital files from', hpre)
    for k, dst in [(prefix + 'npis.json', f'{A.out}/npis.json'), ('data/idr/site/arb_tx.json', f'{A.out}/arb_tx.json'),
                   (npis_key(A.city), f'{A.out}/npis.parquet')]:   # npis.parquet: practice ZIPs for the Medicare areas (Oct 30)
        try: s3.download_file(B, k, dst)
        except Exception as e: print('not found:', k, e)
    if A.map_only:   # Oct 15, 2026: refresh only the map files for the page already published (saved in R2), no new page
        page = f'{A.out}/page.html'; s3.download_file(B, f'site/deploy/cities/{A.city}/price-finder.html', page)
    else: page = build(A.city, f['template'], f['embed'], f['rvu'], f['med'], f['t5'], A.out, hidx, f'{A.out}/arb_tx.json', f'{A.out}/npis.parquet')
    if not A.map_only: practice_and_list_price(page, s3, B, A.out)
    if A.check: print('check only: page built in', page, f'({os.path.getsize(page) / 2**20:.1f} MiB; not saved, not deployed)'); return
    if os.path.getsize(page) > PAGE_MAX:   # Oct 31, 2026: say why instead of failing inside the deploy
        sys.exit(f'STOP: the {name} page is {os.path.getsize(page) / 2**20:.1f} MiB; the website host takes files up to 25 MiB. '
                 f'Split it into regional pages (site/regions.py) like California.')
    maps = make_map(A, E, s3, B, written, prefix, page)
    s3.upload_file(page, B, f'site/deploy/cities/{A.city}/price-finder.html')
    if A.map_only: print('map files and page saved in', A.out, '(not deployed)'); return
    if not A.deploy: print('built, not deployed (run with deploy on to publish)'); return
    if not E.get('CLOUDFLARE_API_TOKEN'): sys.exit('CLOUDFLARE_API_TOKEN is not set; built but not deployed')
    # deploy: same steps as orchestrate.publish
    d = f'{A.out}/deploy'; shutil.rmtree(d, ignore_errors=True)
    s3.download_file(B, 'site/deploy/current.tgz', f'{A.out}/site.tgz')
    with tarfile.open(f'{A.out}/site.tgz') as t: t.extractall(d)
    if not os.path.isdir(f'{d}/functions/price-finder') or not os.path.isfile(f'{d}/site/price-finder/index.html'):
        sys.exit('STOP: site/deploy/current.tgz does not have the expected site/ and functions/ folders; nothing deployed')
    os.makedirs(f'{d}/site/{A.city}/price-finder', exist_ok=True)
    shutil.copy(page, f'{d}/site/{A.city}/price-finder/index.html')
    if maps:
        os.makedirs(f'{d}/site/{A.city}/price-finder/pf/g', exist_ok=True)
        shutil.copy(f'{A.out}/geo.json', f'{d}/site/{A.city}/price-finder/pf/g/geo.json')
        shutil.copy(f'{A.out}/basemap.json', f'{d}/site/{A.city}/price-finder/basemap.json')
    os.makedirs(f'{d}/site/{A.city}/price-finder/pf/q', exist_ok=True)
    dq = f'{d}/site/price-finder/pf/q/quality.txt'
    empty_quality(dq if os.path.isfile(dq) else None, f'{d}/site/{A.city}/price-finder/pf/q/quality.txt')
    live_list(d, A.city, f'{name}, {CITIES[_regions.PARENT[A.city]][0]}' if A.city in _regions.PARENT else name)   # region: 'Bay Area, California'
    fn = f'{d}/functions/{A.city}/price-finder/pf/t'; os.makedirs(fn, exist_ok=True)
    open(f'{fn}/[code].js', 'w').write(FUNC.format(name=name, prefix=json.dumps(prefix)))
    fh = f'{d}/functions/{A.city}/price-finder/pf/h'
    if hidx:   # per-code hospital lines, same function pointed at the area's hosp/h/ folder
        os.makedirs(fh, exist_ok=True)
        open(f'{fh}/[code].js', 'w').write(FUNC.format(name=name, prefix=json.dumps(hpre + 'h/')).replace("PREFIX + 't/' + code", "PREFIX + code"))
    else: shutil.rmtree(fh, ignore_errors=True)
    env = dict(E, CLOUDFLARE_ACCOUNT_ID=E['R2_ACCOUNT_ID'])
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sitewide.py'), d], check=True)   # name, Units look, share image (Oct 18)
    r = subprocess.run(['npx', '--yes', 'wrangler@3.114.0', 'pages', 'deploy', 'site', '--project-name', 'upfront-dfw-preview', '--branch', 'main',
                        '--commit-dirty=true'], cwd=d, env=env, capture_output=True, text=True)
    print(r.stdout[-2000:], r.stderr[-2000:])
    if r.returncode != 0: sys.exit('deploy failed; the preview site is unchanged')
    with tarfile.open(f'{A.out}/site.tgz', 'w:gz') as t: t.add(d, arcname='.')
    s3.upload_file(f'{A.out}/site.tgz', B, 'site/deploy/current.tgz')
    print(f'live: https://files.healthcare/{A.city}/price-finder/')


def landing_page(A):
    """Oct 31, 2026: the state's own address (/<state>/price-finder/) for a state published as regional pages (site/regions.py).
    Lists each region with its counties and provider count (from the split, site/region_split.py) and links to its Price Finder.
    Published by hosp_area.py after every regional page is live; the state is left out of cities.json (the regions are listed
    there instead), so the area picker and the procedure pages only see real Price Finder pages."""
    import boto3, html
    sys.path.insert(0, HERE); import procedure_pages as pp
    E = os.environ
    for k in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET', 'CLOUDFLARE_API_TOKEN'):
        if k in E: E[k] = ''.join(E[k].split())
    B = E.get('R2_BUCKET', 'upfrontdfwdata')
    s3 = boto3.client('s3', endpoint_url=(E.get('STORAGE_ENDPOINT') or f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"), aws_access_key_id=E['R2_ACCESS_KEY_ID'],
                      aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name=(E.get('STORAGE_REGION') or 'auto'))
    st, R = CITIES[A.city][0], _regions.REGIONS[A.city]
    d = f'{A.out}/deploy'; shutil.rmtree(d, ignore_errors=True)
    s3.download_file(B, 'site/deploy/current.tgz', f'{A.out}/site.tgz')
    with tarfile.open(f'{A.out}/site.tgz') as t: t.extractall(d)
    if not os.path.isfile(f'{d}/site/index.html'): sys.exit('STOP: site/deploy/current.tgz has no site/index.html; nothing deployed')
    live = {c['slug'] for c in json.load(open(f'{d}/site/cities.json', encoding='utf-8'))} if os.path.isfile(f'{d}/site/cities.json') else set()
    if set(R) - live: sys.exit(f'STOP: regional pages not live yet: {", ".join(sorted(set(R) - live))}; the {st} page waits for them')
    pp.site_settings(f'{d}/site')
    esc = html.escape; rows = []
    for r, (n, _l, _ln, _box, cs) in R.items():
        try:
            sp = json.loads(s3.get_object(Bucket=B, Key=f"{s3.list_objects_v2(Bucket=B, Prefix=f'site/every-st/{r}/', Delimiter='/')['CommonPrefixes'][-1]['Prefix']}split.json")['Body'].read())
            np_ = sp['regions'][r]['providers']
        except Exception: np_ = None
        rows.append(f'<section class="sec"><h2><a href="/{r}/price-finder/">{esc(n)}</a></h2><p class="lede">{esc(", ".join(cs))} '
                    f'{"county" if len(cs) == 1 else "counties"}{f" · {np_:,} providers" if np_ else ""}.</p>'
                    f'<p class="ctas"><a class="btn hi" href="/{r}/price-finder/">Open the {esc(n)} Price Finder</a></p></section>')
    title = f'{st} Price Finder · {pp.SITE["name"]}'
    desc = (f'What insurers have agreed to pay {st} hospitals, surgery centers, doctors, labs and imaging centers, from the price files '
            f'they have to publish. {st} is split into {len(R)} regional Price Finders.')
    body = f"""<main><section class="board"><div class="wrap"><p class="eyebrow">{esc(st)} · published prices</p>
<h1 class="letters">{esc(st)} Price Finder<em> by region</em></h1>
<p class="lead">{esc(st)} has too many providers for one page, so its prices are split into {len(R)} regional Price Finders. Each one has every
provider whose practice address is in that region's counties, with the same insurer price files, hospital price files and Medicare comparison
as the rest of the site. Pick the region where you're getting care.</p></div></section>
<div class="wrap">{''.join(rows)}</div></main><!-- written by city_page.py landing_page -->"""
    jsonld = {'@context': 'https://schema.org', '@type': 'CollectionPage', 'name': title, 'description': desc,
              'url': pp.SITE['origin'] + f'/{A.city}/price-finder/'}
    os.makedirs(f'{d}/site/{A.city}/price-finder', exist_ok=True)
    for sub in ('pf', 'basemap.json'):   # nothing left over from a statewide page
        x = f'{d}/site/{A.city}/price-finder/{sub}'
        shutil.rmtree(x, ignore_errors=True) if os.path.isdir(x) else (os.remove(x) if os.path.exists(x) else None)
    shutil.rmtree(f'{d}/functions/{A.city}/price-finder', ignore_errors=True)
    open(f'{d}/site/{A.city}/price-finder/index.html', 'w', encoding='utf-8').write(pp.shell(title, desc, f'/{A.city}/price-finder/', body, jsonld))
    L = json.load(open(f'{d}/site/cities.json', encoding='utf-8'))
    json.dump([c for c in L if c['slug'] != A.city], open(f'{d}/site/cities.json', 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'{st} landing page: {len(R)} regions')
    if not A.deploy: print('built, not deployed'); return
    env = dict(E, CLOUDFLARE_ACCOUNT_ID=E['R2_ACCOUNT_ID'])
    subprocess.run([sys.executable, os.path.join(HERE, 'sitewide.py'), d], check=True)
    r = subprocess.run(['npx', '--yes', 'wrangler@3.114.0', 'pages', 'deploy', 'site', '--project-name', 'upfront-dfw-preview', '--branch', 'main',
                        '--commit-dirty=true'], cwd=d, env=env, capture_output=True, text=True)
    print(r.stdout[-2000:], r.stderr[-2000:])
    if r.returncode != 0: sys.exit('deploy failed; the preview site is unchanged')
    with tarfile.open(f'{A.out}/site.tgz', 'w:gz') as t: t.add(d, arcname='.')
    s3.upload_file(f'{A.out}/site.tgz', B, 'site/deploy/current.tgz')
    print(f'live: https://files.healthcare/{A.city}/price-finder/')


if __name__ == '__main__':
    main()
