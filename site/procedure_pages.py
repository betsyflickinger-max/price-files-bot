"""Static "what it costs" pages: one page per featured procedure per Price Finder area, plus index pages and the national
front page section of the homepage.

  python site/procedure_pages.py build SITE_DIR [--areas dfw,houston] [--codes 45378,73721] [--offline DEV_DIR]
  python site/procedure_pages.py deploy [--force]   # pull the live site from R2, rebuild what changed, deploy, save back

What it writes (all inside SITE_DIR/site):
  costs/<area>/<procedure>/index.html   one page per area x featured procedure (codes89.csv), e.g. costs/houston/colonoscopy-diagnostic/
  costs/<area>/index.html               every featured procedure in that area, lowest to highest
  costs/<procedure>/index.html          one procedure across every area
  costs/index.html, costs/costs.css     the index of indexes and the shared stylesheet
  costs/sitemap.xml                     every page that has at least one price (pages without one carry noindex)
  index.html                            only the part between <!-- NATIONAL:START --> and <!-- NATIONAL:END --> is replaced

Every figure comes from the same files the area's Price Finder page reads: the page's embedded data, and the per-code insurer
(pf/t) and hospital (pf/h) files in R2 named by the page's own functions. Nothing is estimated or averaged: ranges are the lowest
and highest published figure, each linked to the file it came from. Rates an insurer file marks as unusually low, $0 lines and
percent-of-charges rates are left out of the ranges and counted on the page. Pages follow the site's robots setting: while the
homepage says noindex, so do these pages.

Deploy runs only when an area's Price Finder page changed since the last run (fingerprints in R2
site/deploy/costs_state.json), so it is cheap to run daily. URLs never change once made: site/procedure_slugs.csv keeps them.
"""
import home_bits   # homepage hero pieces shared with site/ux_polish.py
import visuals     # price line, bill slips, word cards on every price page
import proof as proof_mod   # card #2: proof a provider does the procedure (Texas)
import argparse, base64, csv, datetime as dt, gzip, hashlib, html, json, os, re, shutil, subprocess, sys, tarfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TODAY = dt.date.today()
# DFW cities outside the Census Dallas-Fort Worth-Arlington metro (site/dfw_metro.py). Kept on the pages, tagged.
try:
    _M = json.load(open(os.path.join(HERE, 'dfw_metro.json')))
    OUTSIDE_DFW = set(_M['outside'])
except Exception:
    OUTSIDE_DFW = set()
AREA_SLUG = ['']   # set per area while rendering


def ctag(city):
    """' · Gainesville (outside the DFW metro)' style city text for DFW pages."""
    if not city:
        return ''
    out = AREA_SLUG[0] == 'dfw' and re.sub(r'[^a-z]+', ' ', city.lower()).strip() in OUTSIDE_DFW
    return esc(city) + (" <span class='flag'>outside the DFW metro</span>" if out else '')
STALE_DAYS = 183
esc = lambda s: html.escape(str(s if s is not None else ''), quote=True)


def parse_date(d):
    """File dates as the files write them: 2026-04-01, 5/29/2026, 2026/05/29 ... None if unreadable."""
    if not d:
        return None
    d = str(d).strip()[:10]
    for f in ('%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d', '%m-%d-%Y', '%Y%m%d'):
        try:
            return dt.datetime.strptime(d, f).date()
        except ValueError:
            pass
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', d)
    if m:
        y = int(m.group(3)); y += 2000 if y < 100 else 0
        try:
            return dt.date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def is_stale(d):
    x = parse_date(d)
    return bool(x) and (TODAY - x).days > STALE_DAYS


def money(v):
    return '$' + f'{v:,.2f}'


def short_money(v):
    return '$' + (f'{v:,.0f}' if v >= 100 else f'{v:,.2f}')


# ---------------------------------------------------------------- data access
class Store:
    """Reads per-code files for one area, from R2 (live) or a local folder (offline test)."""

    def __init__(self, site_dir, offline=None):
        self.site_dir, self.offline, self.s3 = site_dir, offline, None
        if not offline:
            import boto3
            E = {k: "".join(os.environ.get(k, "").split()) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
            self.s3 = boto3.client("s3", endpoint_url=(os.environ.get("STORAGE_ENDPOINT") or f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"),
                                   aws_access_key_id=E["R2_ACCESS_KEY_ID"], aws_secret_access_key=E["R2_SECRET_ACCESS_KEY"], region_name=(os.environ.get("STORAGE_REGION") or "auto"))
            self.B = E["R2_BUCKET"]
        self._cur = None

    def prefix(self, area, kind):
        """R2 prefix for an area's per-code files, read from the area's own Pages Function (so it follows each rebuild)."""
        if area['slug'] == 'dfw':
            if self._cur is None:
                self._cur = self.s3.get_object(Bucket=self.B, Key='site/every/CURRENT')['Body'].read().decode().strip()
            return f'site/every/{self._cur}/{kind}/'
        fn = os.path.join(self.site_dir, 'functions', area['slug'], 'price-finder', 'pf', kind, '[code].js')
        if not os.path.isfile(fn):
            return None
        src = open(fn, encoding='utf-8').read()
        m = re.search(r'PREFIX = "([^"]+)"', src)
        if not m:
            return None
        p = m.group(1)
        # the t/ function adds 't/' itself; the h/ function's prefix already ends in .../h/
        return p + 't/' if kind == 't' and "PREFIX + 't/'" in src else p

    def local_medicare(self, area, n_fac):
        """fac index -> [kind, wage index, CCN, CBSA] for this area's hospitals and surgery centers (site/med_local.py), or {}."""
        if self.offline or not self.s3:
            return {}
        try:
            if not hasattr(self, '_ml'):
                self._ml = json.loads(self.s3.get_object(Bucket=self.B, Key='site/med_local/2026.json')['Body'].read())['npi']
            pre = self.prefix(area, 't')
            if not pre or not pre.endswith('t/'):
                return {}
            npis = json.loads(self.s3.get_object(Bucket=self.B, Key=pre[:-2] + 'npis.json')['Body'].read())
        except Exception as e:
            print(f"  {area['slug']}: no local Medicare figures ({type(e).__name__})", flush=True)
            return {}
        if len(npis) != n_fac:                 # the provider list must line up with the page's, or nothing is shown
            print(f"  {area['slug']}: npis.json ({len(npis)}) and page providers ({n_fac}) differ; no local Medicare figures", flush=True)
            return {}
        return {i: self._ml[n] for i, n in enumerate(npis) if n in self._ml}

    def provider_states(self, area, D):
        """fac index -> state of the provider's practice ZIP (site/zip_state.py), for the out-of-state check in summarize().
        Oct 19, 2026. Builds from then on carry it as the 4th field of each D['fac'] row; for older builds it is read here from
        the area's provider list (the NPIS file its build used, by NPI via the build's npis.json). {} when it can't be read:
        the page is then built as before, with nothing left out."""
        fac = D['fac']
        if fac and all(len(x) > 3 for x in fac):
            return {i: x[3] for i, x in enumerate(fac) if x[3]}
        if self.offline:
            p = os.path.join(self.offline, area['slug'], 'states.json')   # offline test: {fac index: state}
            return {int(k): v for k, v in json.load(open(p)).items()} if os.path.isfile(p) else {}
        if not self.s3:
            return {}
        try:
            import pyarrow.parquet as pq
            from zip_state import zip_state
            pre = self.prefix(area, 't')
            if not pre or not pre.endswith('t/'):
                return {}
            npis = json.loads(self.s3.get_object(Bucket=self.B, Key=pre[:-2] + 'npis.json')['Body'].read())
            if len(npis) != len(fac):
                print(f"  {area['slug']}: npis.json ({len(npis)}) and page providers ({len(fac)}) differ; no out-of-state check", flush=True)
                return {}
            slug = area['slug']
            if slug == 'dfw':
                keys = ['data/2026-09-29/dfw_npis_all.parquet']
            elif slug in STATES:
                scopes = ['s7', 'nc', 'ct']
                try:
                    scopes += [k for k in json.load(open(os.path.join(HERE, 'state_scopes.json'))) if not k.startswith('_')]
                except Exception:
                    pass
                keys = [f'data/{sc}/state/{slug}/npis.parquet' for sc in scopes]
            else:
                keys = [f'data/tx/metro/{slug}/npis.parquet']
            body = None
            for k in keys:
                try:
                    body = self.s3.get_object(Bucket=self.B, Key=k)['Body'].read()
                    break
                except Exception:
                    continue
            if body is None:
                print(f"  {slug}: no provider list found ({keys[0]}...); no out-of-state check", flush=True)
                return {}
            import io
            tb = pq.read_table(io.BytesIO(body), columns=['npi', 'zip5'], use_threads=False).to_pydict()   # no thread pool: one stuck at exit in tests
            z = dict(zip(tb['npi'], tb['zip5']))
            out = {i: zip_state(z.get(n)) for i, n in enumerate(npis)}
            return {i: v for i, v in out.items() if v}
        except Exception as e:
            print(f"  {area['slug']}: no out-of-state check ({type(e).__name__}: {e})", flush=True)
            return {}

    def proof(self, area, n_fac):
        """(Proof, npis) for Texas areas (card #2, Oct 31, 2026; site/proof.py), or (None, None): the page then builds exactly as
        before. The proof table is Texas only for now."""
        if self.offline or not self.s3 or area_state(area) != 'TX':
            return None, None
        try:
            if not hasattr(self, '_proof'):
                from proof import Proof, load_taxonomy
                self._proof = Proof(self.s3, self.B, tax=load_taxonomy(self.s3, self.B))
            pre = self.prefix(area, 't')
            if not pre or not pre.endswith('t/'):
                return None, None
            npis = json.loads(self.s3.get_object(Bucket=self.B, Key=pre[:-2] + 'npis.json')['Body'].read())
            if len(npis) != n_fac:
                print(f"  {area['slug']}: npis.json ({len(npis)}) and page providers ({n_fac}) differ; no proof", flush=True)
                return None, None
            return self._proof, npis
        except Exception as e:
            print(f"  {area['slug']}: no proof ({type(e).__name__}: {e})", flush=True)
            self._proof = None
            return None, None

    def get(self, area, kind, code):
        if self.offline:
            p = os.path.join(self.offline, area['slug'], kind, code + '.txt')
            return decode(open(p).read()) if os.path.isfile(p) else None
        pre = self.prefix(area, kind)
        if not pre:
            return None
        try:
            body = self.s3.get_object(Bucket=self.B, Key=pre + code + '.txt')['Body'].read().decode()
        except Exception:
            return None
        return decode(body)


def decode(text):
    """Per-code files are gzip, base64-encoded as .txt (same as the Price Finder's getB64)."""
    raw = base64.b64decode(text.strip())
    try:
        return json.loads(gzip.decompress(raw))
    except OSError:
        return json.loads(raw)


def page_data(path):
    s = open(path, encoding='utf-8').read()
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', s, re.S)
    D = json.loads(m.group(1))
    a = re.search(r"var MED_AREA = '([^']*)'", s)
    return D, (a.group(1) if a else ''), hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- names, slugs, areas
def slugify(s):
    s = s.lower().replace('&', ' and ').replace('+', ' plus ')
    s = re.sub(r"['’]", '', s)
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


SLUG_OVERRIDE = {'73721': 'knee-mri', '70551': 'brain-mri', '70553': 'brain-mri-with-and-without-contrast', '72148': 'lower-back-mri',
                 '74177': 'ct-scan-abdomen-and-pelvis', '70450': 'head-ct-scan', '72193': 'ct-scan-pelvis', '85025': 'cbc-blood-test',
                 '80061': 'cholesterol-test', '93000': 'ekg', '84443': 'thyroid-tsh-test', '59400': 'vaginal-birth', '59510': 'c-section',
                 '59610': 'vbac-vaginal-birth-after-c-section', '43235': 'upper-endoscopy-egd', '43239': 'upper-endoscopy-egd-with-biopsy'}


def load_slugs(codes, names):
    """Stable procedure URLs: once a code has a slug it keeps it, even if its plain-English name is reworded later.
    New slugs: the plain name without its (parenthesis), unless two codes would share it; then the full name."""
    path = os.path.join(HERE, 'procedure_slugs.csv')
    have = {r['code']: r['slug'] for r in csv.DictReader(open(path))} if os.path.isfile(path) else {}
    short = {c: slugify(re.sub(r'\([^)]*\)', '', names[c])) for c in codes if c in names}
    dup = {v for v in short.values() if list(short.values()).count(v) > 1}
    used = set(have.values())
    for c in codes:
        if c not in have and c in names:
            s = SLUG_OVERRIDE.get(c) or (slugify(names[c]) if short[c] in dup else short[c]) or c.lower()
            if s in used:
                s = f'{s}-{c.lower()}'
            have[c] = s
            used.add(s)
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['code', 'slug'])
        for c in sorted(have):
            w.writerow([c, have[c]])
    return have


def area_url_slug(slug):
    return 'dallas-fort-worth' if slug == 'dfw' else slug


STATES = {'tennessee': 'TN', 'colorado': 'CO', 'oklahoma': 'OK', 'alabama': 'AL', 'mississippi': 'MS', 'missouri': 'MO', 'georgia': 'GA',
          'north-carolina': 'NC', 'connecticut': 'CT', 'illinois': 'IL', 'new-york': 'NY', 'california': 'CA', 'louisiana': 'LA',
          'florida': 'FL', 'kentucky': 'KY', 'arizona': 'AZ', 'new-mexico': 'NM', 'north-dakota': 'ND', 'south-dakota': 'SD',
          'michigan': 'MI', 'indiana': 'IN', 'washington-dc': 'DC'}


def _scope_states():
    """State pages built by the shared schedule (site/state_scopes.json): page slug -> state code."""
    try:
        sc = json.load(open(os.path.join(HERE, 'state_scopes.json'), encoding='utf-8'))
        return {slug: code for k, v in sc.items() if not k.startswith('_') for code, slug in v.items()}
    except Exception:
        return {}


sys.path.insert(0, HERE)
import regions as _regions   # Oct 31, 2026: regional pages (California's 5; New York, Florida, Illinois, Pennsylvania beside their state page)
CA_REGIONS = {r: _regions.STATE_CODE[st] for r, st in _regions.PARENT.items()}   # every regional page -> its state (name kept from CA)
STATES = {**_scope_states(), **STATES}   # regions stay out: like Texas metros, they get their own Medicare localities
STATE_NAME = {code: ('Texas' if code == 'TX' else slug.replace('-', ' ').title().replace(' Of ', ' of '))
              for slug, code in STATES.items() if slug != 'washington-dc'}
STATE_NAME.update({'CA': 'California', 'DC': 'Washington, DC'})


def area_state(a):
    """Oct 31, 2026 (Betsy OK'd): 14 live state pages (Virginia, Wisconsin, Arkansas ...) and California's 5 regions were missing
    here and read as Texas: Texas proof lines, Texas anesthesia rates, left off the homepage map. STATES now includes every
    state_scopes.json page; California's regions return CA."""
    return STATES.get(a['slug']) or CA_REGIONS.get(a['slug'], 'TX')


page_state = area_state   # the homepage map and place lists used this display-only copy earlier on Oct 31


def area_group(a):
    return 'Texas' if area_state(a) == 'TX' else 'States'


# Everyday names people search for, and other names for the same thing (page titles, headings and "also called").
SEARCH_NAMES = {
    '00811': ('Anesthesia for a colonoscopy', ['colonoscopy sedation']), '00812': ('Anesthesia for a screening colonoscopy', ['screening colonoscopy sedation']),
    '19120': ('Breast lump removal', ['excisional breast biopsy', 'fibroadenoma removal']),
    '216': ('Heart valve surgery (hospital stay)', ['valve replacement with cardiac catheterization']),
    '22551': ('Neck fusion (ACDF)', ['anterior cervical discectomy and fusion', 'cervical spine surgery']),
    '27130': ('Hip replacement', ['total hip arthroplasty']), '27447': ('Knee replacement', ['total knee arthroplasty', 'TKA']),
    '29826': ('Shoulder decompression surgery', ['arthroscopic acromioplasty', 'subacromial decompression']),
    '29827': ('Rotator cuff repair', ['arthroscopic rotator cuff surgery']), '29881': ('Knee arthroscopy (meniscus)', ['meniscectomy', 'knee scope']),
    '29888': ('ACL reconstruction', ['ACL surgery']), '42820': ('Tonsillectomy (child under 12)', ['tonsils and adenoids removal', 'T&A']),
    '43235': ('Upper endoscopy (EGD)', ['EGD', 'upper GI endoscopy']), '43239': ('Upper endoscopy with biopsy', ['EGD with biopsy']),
    '43644': ('Gastric bypass', ['Roux-en-Y gastric bypass', 'bariatric surgery']), '43775': ('Gastric sleeve', ['sleeve gastrectomy', 'bariatric surgery']),
    '45378': ('Colonoscopy', ['diagnostic colonoscopy']), '45380': ('Colonoscopy with biopsy', []),
    '45385': ('Colonoscopy with polyp removal', ['polypectomy']), '45391': ('Colonoscopy with ultrasound', ['endoscopic ultrasound of the colon']),
    '460': ('Spinal fusion (hospital stay)', ['lumbar fusion', 'back fusion']), '470': ('Hip or knee replacement (hospital stay)', ['joint replacement']),
    '473': ('Neck fusion (hospital stay)', ['cervical fusion']), '47562': ('Gallbladder removal', ['laparoscopic cholecystectomy']),
    '49505': ('Hernia repair', ['inguinal hernia surgery']), '55250': ('Vasectomy', []), '55700': ('Prostate biopsy', []),
    '55866': ('Robotic prostatectomy', ['prostate removal surgery']),
    '59400': ('Vaginal birth', ['having a baby', 'labor and delivery', 'childbirth']), '59510': ('C-section', ['cesarean delivery']),
    '59610': ('VBAC', ['vaginal birth after C-section']), '62322': ('Lower back epidural injection', ['epidural steroid injection']),
    '62323': ('Lower back epidural injection with imaging', ['epidural steroid injection with fluoroscopy']),
    '63030': ('Lumbar discectomy', ['laminotomy', 'herniated disc surgery']), '64483': ('Lower back nerve root injection', ['transforaminal epidural steroid injection']),
    '64721': ('Carpal tunnel surgery', ['carpal tunnel release']), '66821': ('YAG laser after cataract surgery', ['posterior capsulotomy']),
    '66984': ('Cataract surgery', []), '70450': ('Head CT scan', ['CT head without contrast']), '70551': ('Brain MRI', ['MRI brain without contrast']),
    '70553': ('Brain MRI with contrast', ['MRI brain with and without contrast']), '72110': ('Lower back X-ray', ['lumbar spine X-ray']),
    '72148': ('Lower back MRI', ['lumbar MRI']), '72193': ('Pelvic CT scan', ['CT pelvis with contrast']), '73721': ('Knee MRI', ['MRI knee']),
    '74177': ('CT scan of the abdomen and pelvis', ['CT abdomen and pelvis with contrast']),
    '743': ('Uterine surgery, non-cancer (hospital stay)', ['hysterectomy']), '75571': ('Heart calcium score', ['coronary calcium scan', 'CAC score']),
    '76700': ('Abdominal ultrasound', []), '76805': ('Pregnancy ultrasound', ['anatomy scan']), '76830': ('Transvaginal ultrasound', ['pelvic ultrasound']),
    '77065': ('Diagnostic mammogram (one breast)', []), '77066': ('Diagnostic mammogram', ['diagnostic mammogram of both breasts']),
    '77067': ('Mammogram', ['screening mammogram']), '80048': ('Basic metabolic panel', ['BMP blood test']),
    '80053': ('Comprehensive metabolic panel', ['CMP blood test', 'blood panel']), '80055': ('Prenatal blood panel', ['obstetric panel']),
    '80061': ('Cholesterol test', ['lipid panel']), '80069': ('Kidney function test', ['renal panel']), '80076': ('Liver function test', ['liver panel', 'LFT']),
    '81000': ('Urinalysis (manual, with microscope)', []), '81001': ('Urinalysis with microscope', []), '81002': ('Urinalysis (manual)', []),
    '81003': ('Urinalysis (automated)', ['urine test']), '84153': ('PSA test', ['prostate-specific antigen test']), '84154': ('Free PSA test', []),
    '84443': ('Thyroid (TSH) test', ['TSH blood test']), '85025': ('CBC with differential', ['complete blood count']), '85027': ('Complete blood count (CBC)', []),
    '85610': ('PT/INR blood clotting test', ['prothrombin time']), '85730': ('PTT blood clotting test', []), '88305': ('Pathology tissue exam', ['biopsy pathology']),
    '90832': ('Therapy session (30 minutes)', ['psychotherapy']), '90834': ('Therapy session (45 minutes)', ['psychotherapy']),
    '90837': ('Therapy session (60 minutes)', ['psychotherapy']), '90846': ('Family therapy (without the patient)', []),
    '90847': ('Family therapy', ['family psychotherapy']), '90853': ('Group therapy', []), '93000': ('EKG', ['ECG', 'electrocardiogram']),
    '93452': ('Heart catheterization', ['cardiac cath']), '95810': ('Sleep study', ['polysomnography']), '97110': ('Physical therapy (15 minutes)', ['therapeutic exercise']),
    '99203': ('New patient doctor visit (30 minutes)', ['office visit']), '99204': ('New patient doctor visit (45 minutes)', ['office visit']),
    '99205': ('New patient doctor visit (60 minutes)', ['office visit']), '99243': ('Specialist consultation (40 minutes)', []),
    '99244': ('Specialist consultation (60 minutes)', []), '99385': ('Annual physical, new patient (ages 18–39)', ['preventive visit']),
    '99386': ('Annual physical, new patient (ages 40–64)', ['preventive visit']),
}

# The person behind the site, for the byline, the About page and the pages' structured data. Change here; every page follows.
AUTHOR = {'name': 'Betsy Flickinger', 'role': 'founder of The Healthcare Files',
          'bio': ('Betsy Flickinger works on the economics of healthcare and work, alongside everyone it touches: executives, employers, '
                  'employees and gig workers. The Healthcare Files reads the price files hospitals and insurers are required to publish and puts every price in plain view, with a link to the file it came from.')}


def common(S):
    return SEARCH_NAMES.get(S['code'], (re.sub(r'\s*\([^)]*\)', '', S['name']).strip() or S['name'], []))[0]


def aliases(S):
    return SEARCH_NAMES.get(S['code'], ('', []))[1]


def updated():
    return TODAY.strftime('%B %-d, %Y')

def looks_placeholder(v, median):
    """A filler number, not a real price: far above the code's median AND shaped like filler (59,999.99, 99,999, 9,999.99, a round
    $100,000+), or so extreme no real price fits (over 100x the median). Real but outrageous prices (a $33,148 hospital carpal
    tunnel fee) are never caught: they stay on the pages."""
    if not median or v <= 5 * median:
        return False
    t = f'{v:.2f}'.rstrip('0').rstrip('.')
    nines = '9999' in t or bool(re.fullmatch(r'\d*999(\.99?)?', t))
    roundbig = v >= 100000 and v % 10000 == 0
    return nines or (roundbig and v > 20 * median) or v > 100 * median


NOT_HERE = re.compile(r'rehab|behavioral|psychiatr|hospice|nursing|long[- ]?term|\bltac\b|kindred|select specialty|\bopco\b|real estate|'
                      r'skilled|senior living|assisted living|home health|dialysis|\bdental|\bdds\b|pharmacy|'
                      # long-term acute care hospitals (NPPES 282E00000X), named by chain (Oct 18 fact-check: LifeCare at the top of DFW ACL)
                      r'lifecare|\bvibra\b|\bltach?\b|nexus specialty|acuity hospital|post[- ]?acute|cornerstone (?:hospital|specialty)|'
                      r'texas health specialty hospital|transitional hospitals')
SPECIALTY = [(re.compile(r'\beye\b|eyecare|surgeye|vision|retina|ophthalm|lasik|cataract'), ('Eye',)),
             (re.compile(r'cardiac|cardio|\bheart\b|\bcath\b|vascular'), ('Heart', 'Imaging', 'Inpatient stays')),
             (re.compile(r'nephrolog|kidney'), ('Lab tests',)),
             (re.compile(r'dermatolog|plastic surg|cosmetic|aesthetic'), ()),
             (re.compile(r'\bpain\b'), ('Pain management', 'Spine')),
             (re.compile(r'endoscop|gastro|\bgi\b|digestive|colon'), ('GI', 'Anesthesia')),
             (re.compile(r'orthop|\bspine\b|sports med'), ('Orthopedics', 'Spine', 'Pain management', 'Imaging', 'Physical therapy'))]
FERTILITY = re.compile(r'reproduct|fertility|\bivf\b|\bccrm\b')
IMAGING = re.compile(r'imag|radiolog|\bmri\b|diagnostic|\bscan|x-?ray|ultrasound|mammo|breast')
LAB = re.compile(r'\blab\b|labs\b|laborator|diagnostic|patholog|quest|labcorp|bioreference|sonic|clinical ref')


# ---------------------------------------------------------------- the numbers for one area x code
KIND = {'A': 'surgery center', 'H': 'hospital', 'G': 'practice, clinic, lab or imaging center', 'P': 'clinician'}
PROD = ['Employer and individual plans', 'Marketplace (ACA) plans', 'Medicare Advantage', 'Medicaid and CHIP', 'Plan type not stated',
        'Out-of-network']   # 5: Oct 30, 2026, hospital rows labelled out-of-network (h2_build.py OON_RE)
SHOWN = {'Superior/Ambetter/Wellcare (Centene)': 'Ambetter / Superior / Wellcare'}


def nice(t):
    """Readable provider names: ALL-CAPS names to title case, then fix the usual title-case slips the files carry
    ("Children'S", "West PArk", "Mckinney", "Medical Center At Dallas", "Llc")."""
    if not t:
        return t
    if t == t.upper():
        t = re.sub(r'\b([a-z])', lambda m: m.group(1).upper(), t.lower()).replace(' Er ', ' ER ')
    t = re.sub(r"(?<=[A-Za-z])'S\b", "'s", t)
    t = re.sub(r'\b([A-Z])([A-Z])([a-z]{2,})\b', lambda m: m.group(1) + m.group(2).lower() + m.group(3), t)
    t = re.sub(r'\bMc([a-z])', lambda m: 'Mc' + m.group(1).upper(), t)
    t = re.sub(r'(?<=\S )(Of|And|At|For|The)(?= \S)', lambda m: m.group(1).lower(), t)
    t = re.sub(r'\b(Llc|Llp|Lp|Pllc|Pa|Pc|Vamc|Va|Uhs|Ii|Iii|Iv|Usmd|Jps|Hma|Fw|Mca|Chi|Utsw|Ut|Bsw|Hca|Ltac|Asc|Er)\b', lambda m: m.group(1).upper(), t)
    t = re.sub(r'\.{2,}$', '.', t)
    t = re.sub(r'\bMuesnter\b', 'Muenster', re.sub(r'\bSouhtlake\b', 'Southlake', t))
    return CITY_FIX.get(t, t)


DOC_MDDO = re.compile(r'(^|[ ,])(M\.?D\.?|D\.?O\.?)($|[ ,])')
CITY_FIX = {'Souhtlake': 'Southlake', 'Muesnter': 'Muenster'}   # spelling slips seen in the files (Oct 2026)


def kind_of(x):
    """A hospital's own outpatient-surgery NPI is often registered as a surgery center; a place named "... Hospital" is shown
    as a hospital (Oct 18 fact-check: Texas Health Presbyterian Hospital Dallas was labeled "surgery center")."""
    return 'H' if x[2] == 'A' and HOSP_NAME.search(x[0]) else x[2]


HOSP_NAME = re.compile(r'\bhospital\b', re.I)
PARPLAN = re.compile(r'par\s*plan', re.I)
# Hospital price files filed under the wrong area by name alone (Oct 18 audit: "Dallas County Hospital" in Perry, Iowa sat in
# Dallas-Fort Worth). Left out of every page's figures here; the real fix is a state/address check where files are assigned to areas.
WRONG_AREA = re.compile(r'dallascohospital\.org', re.I)


def mostly_one(es):
    """(providers, providers at the top amount, top amount) when a network pays most of its places one top amount, e.g. a flat case
    rate (Curative's $11,500 colonoscopy at 32 of 43 DFW places, Oct 18). Each provider counted once, at its lowest rate in the
    network. None otherwise. A network that pays everyone the same is the case where all places share it."""
    provs = {}
    for e in es:
        k = (e['name'], e['city'])
        if k not in provs or e['v'] < provs[k]:
            provs[k] = e['v']
    if len(provs) < 5:
        return None
    top = max(provs.values())
    k = sum(1 for v in provs.values() if v == top)
    return (len(provs), k, top) if k >= 5 and 2 * k >= len(provs) else None


PUBLIC_PAYER = re.compile(r'^(?:medicare\b(?!.*advantage)|medicaid\b|tmhp\b|texas medicaid\b)', re.I)


def file_date(e):
    """The file's own date when its web address carries one (UnitedHealthcare's index dated Oct 1 lists the 2026-09-01 file);
    otherwise the date the site has for it."""
    m = re.search(r'(20\d\d)-(\d\d)-(\d\d)', e.get('url') or '')
    return '-'.join(m.groups()) if m else e.get('date')


def summarize(D, code, t, h, loc=None, st=None, pst=None, proof=None):
    """Everything a procedure page shows, as plain data. Mirrors the Price Finder's own rules (see the page's main script):
    one line per provider per network = that provider's lowest standard rate (no modifier) in that network; $0 lines,
    percent-of-charges lines and lines the file flags as unusually low are kept out of the low/high figures.
    Oct 19, 2026: st = the area's state, pst = {fac index: state of the provider's practice ZIP} (Store.provider_states).
    A provider whose state is known and isn't the area's is left out of the figures (counted in 'n_out_of_state'); the
    Alabama pages showed an Atlanta clinic whose registry entry says "Atlanta, AL 30329". Its rates stay in the files.
    Oct 31, 2026 (card #2): proof = {fac index: (state, info)} from site/proof.py, or None (pages exactly as before). With it,
    providers whose state is 'no_record' stay listed but are held out of the lowest 10 only ('also_listed'; the highest 10 is
    unchanged so overcharges stay visible), ranked providers with a Medicare or Medicaid record carry a proof line, and
    'wrong_specialty' lines (anesthesia clinicians under a delivery/surgery/scan/lab code) are left out of the figures and
    counted ('n_wrong_specialty'). Counts never change the order, which stays by price."""
    C = D['codes'][code]
    f = (t or {}).get('f') or C.get('f') or {}
    n_away = 0
    if st and pst:
        away = {fi for fi in f if pst.get(int(fi), st) != st}
        if away:
            n_away = len(away)
            f = {fi: rs for fi, rs in f.items() if fi not in away}
    nets = [n.split('|', 1) for n in D['nets']]
    drg = 'DRG' in (C.get('type') or '')

    def fac(i):
        x = D['fac'][int(i)]
        o = {'name': nice(x[0]), 'city': nice(x[1]), 'kind': kind_of(x), 'fi': int(i)}
        m = (loc or {}).get(int(i))
        if m and m[0] == o['kind']:           # [kind, wage index, CCN, CBSA] from site/med_local.py (hospitals, surgery centers)
            o['ml'] = m
        return o

    def entry(fi, r):
        x = fac(fi)
        x.update(v=r[3], net=r[0], ins=nets[r[0]][0], plan=nets[r[0]][1], date=D['dates'][r[5]], url=D['urls'][r[6]], low=bool(r[4] & 2),
                 lab=(D['labs'][r[10]] if len(r) > 10 and r[10] is not None and 0 <= r[10] < len(D.get('labs') or []) else ''))
        return x

    # Facility fees count only for the kinds of places that do this service: hospitals and surgery centers, plus imaging
    # centers, labs and clinics for scans, tests and office services (a "facility fee" filed under an individual or a
    # fertility lab for a colonoscopy is counted on the page but left out of the figures). Inpatient groups: hospitals only.
    cat = C.get('cat', '')
    # Scans and lab tests: hospitals plus imaging centers, labs and clinics (surgery centers' MRI or lab contract lines are
    # rates on paper for services they don't offer). Visits, heart tests, therapy: any facility. Surgeries and inpatient stays:
    # hospitals and surgery centers (inpatient: hospitals).
    fac_kinds = ({'H'} if drg else {'H', 'G'} if cat in ('Imaging', 'Lab tests')
                 else {'H', 'A', 'G'} if cat in ('Heart', 'Other', 'Physical therapy', 'Office visits', 'Behavioral health') else {'H', 'A'})
    anes = code.startswith('00')
    # Insurer files list contract rates for places that never do the service ("ghost rates"). Beyond the kind of place, keep out
    # places whose names say they don't (rehab, psychiatric, nursing...), and for scans and lab tests count clinics only when the
    # name says imaging or lab. A name test can only leave a place out, never put one in; the Price Finder still shows them all.
    def fits(fi):
        x = fit_cache.get(fi)
        if x is None:
            nm, kd = D['fac'][int(fi)][0].lower(), kind_of(D['fac'][int(fi)])
            x = kd in fac_kinds and not NOT_HERE.search(nm) and not (FERTILITY.search(nm) and cat not in ('Imaging', 'Lab tests', 'Maternity'))
            # single-specialty places named for another specialty (an eye surgery center for a colonoscopy)
            for rx, ok in SPECIALTY:
                if x and rx.search(nm) and cat not in ok:
                    x = False
            if x and kd == 'G' and cat == 'Imaging':
                x = bool(IMAGING.search(nm))
            elif x and kd == 'G' and cat == 'Lab tests':
                x = bool(LAB.search(nm)) and 'cath' not in nm
            fit_cache[fi] = x
        return x
    fit_cache = {}
    # Unusually high = placeholder-looking (see looks_placeholder): measured against the code's median published dollar rate
    # (5 or more rates; facility fees against the places counted below). Bit 8 from insurer builds after Oct 17, 2026; computed
    # here for older builds. The median is only a screening line; it is never shown as a price.
    def _med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if len(xs) >= 5 else None
    meds = {k: _med([r[3] for fi_, rs in f.items() for r in rs if (1 if (r[1] or anes) else 0) == k and not r[2] and not r[4] & 6 and r[3] > 0
                     and (k == 1 or fits(fi_))]) for k in (0, 1)}
    per = {'fac': {}, 'doc': {}}        # (fi, net) -> lowest dollar entry
    pct, flagged = {'fac': set(), 'doc': set()}, {'fac': set(), 'doc': set()}
    high = {'fac': set(), 'doc': set()}
    high_e = {'fac': [], 'doc': []}
    other_kind = set()
    wrong_spec = set()
    for fi, rs in f.items():
        if proof and (proof.get(int(fi)) or ('',))[0] == 'wrong_specialty':
            wrong_spec.add(fi)
            continue
        for r in rs:
            if anes:                     # anesthesia: every published dollar line is the anesthesia fee, however the file classes it
                r = [r[0], 1] + list(r[2:])
            if not r[1] and not fits(fi):
                if not r[2] and not r[4] & 6:
                    other_kind.add((fi, r[0]))
                continue
            if r[4] & 4:                 # $0 listed: paid inside another charge
                continue
            k = 'doc' if r[1] else 'fac'
            if r[2]:                     # percent of billed charges
                pct[k].add((fi, r[0]))
                continue
            if r[4] & 2:
                flagged[k].add((fi, r[0]))
                continue
            m_ = meds[1 if r[1] else 0]
            if m_ and r[3] < 0.2 * m_:   # the Price Finder's "unusually low" rule: under 20% of the typical published rate
                flagged[k].add((fi, r[0]))
                continue
            if r[4] & 8 or (m_ and looks_placeholder(r[3], m_)):
                high[k].add((fi, r[0]))
                high_e[k].append(entry(fi, r))
                continue
            key = (fi, r[0])
            if key not in per[k] or r[3] < per[k][key]['v']:
                per[k][key] = entry(fi, r)
    # what the page leads with: the clinician's fee for visits and therapy, otherwise the facility fee when there is one
    kind = 'doc' if (cat in ('Office visits', 'Behavioral health', 'Physical therapy', 'Maternity') or anes) and per['doc'] else ('fac' if per['fac'] else 'doc')
    allines = sorted(per[kind].values(), key=lambda e: (e['v'], e['name']))
    by_net = {}
    for e in allines:
        by_net.setdefault(e['net'], []).append(e)
    # each provider counted once per network (a provider can have several NPIs), so a network never shows more than the page total
    docs_by_net = {}
    if kind == 'fac':
        for (fi_, n_), e in per['doc'].items():
            nm_ = D['fac'][int(fi_)][0]
            ok_ = (e['kind'] == 'P' and DOC_MDDO.search(nm_)) or (e['kind'] == 'G' and not LAB.search(nm_.lower()) and not IMAGING.search(nm_.lower()) and not NOT_HERE.search(nm_.lower()))
            if ok_ and 'derived' not in (e.get('lab') or ''):
                docs_by_net.setdefault(n_, []).append(e['v'])
    doc_mid = {n_: sorted(v)[len(v) // 2] for n_, v in docs_by_net.items() if len(v) >= 5}
    networks = [{'ins': nets[n][0], 'plan': nets[n][1], 'doc_mid': doc_mid.get(n), 'n': len({(e['name'], e['city']) for e in es}), 'lo': es[0], 'hi': es[-1],
                 'flat': bool(mostly_one(es)), 'one': mostly_one(es), 'par': bool(PARPLAN.search(nets[n][1] or ''))} for n, es in by_net.items()]
    # BCBSTX ParPlan: listed in the networks table, labeled, but kept out of the headline figures (Oct 18: what it represents is
    # under review; the same amount repeats across hospitals)
    lines = [e for e in allines if not PARPLAN.search(e['plan'] or '')]
    by_net = {n: es for n, es in by_net.items() if not PARPLAN.search(nets[n][1] or '')}
    networks.sort(key=lambda x: (x['lo']['v'], x['ins'], x['plan']))
    flat = {n for n, es in by_net.items() if mostly_one(es)}
    # Networks that pay most places one amount stay in the networks table, labeled, but out of the headline low/high and the
    # comparisons below (Oct 18: a flat case rate was the page's "highest" and drove its "same network" and "same place" lines).
    # Real high prices elsewhere stay in the figures.
    kept = [e for e in lines if e['net'] not in flat]
    flat_out = bool(kept) and len(kept) < len(lines)
    if kept:
        lines = kept
    # lowest 10 providers (each provider once, at its lowest network). Networks that pay every provider the same amount
    # stay in the network table, labeled, but are left out of this ranking (same rule as the procedure packages).
    held = {}                            # card #2: listed, but no Medicare/Medicaid record where a volume file covers them
    def ranked(e):
        st_ = (proof or {}).get(e['fi'])
        if st_ and st_[0] == 'no_record':
            # Option B (Betsy, Oct 31, 2026): label, don't move. Everyone stays in price order; the label says no record was found.
            e['noproof'] = True
            return True
        if st_ and st_[0] == 'proved':
            e['proof'] = st_[1]
        return True
    seen, lowest = set(), []
    for e in lines:
        if e['net'] in flat:
            continue
        k = (e['name'], e['city'])
        if k not in seen:
            seen.add(k)
            if ranked(e):
                lowest.append(e)
        if len(lowest) == 10:
            break
    doc = sorted(per['doc'].values(), key=lambda e: e['v']) if kind == 'fac' else []
    # highest 10 providers (each provider once, at its highest network; same-rate-everywhere networks left out, like the lowest 10)
    seen, highest = set(), []
    for e in reversed(lines):
        if e['net'] in flat:
            continue
        k = (e['name'], e['city'])
        if k not in seen:
            seen.add(k)
            highest.append(e)   # proof applies to the lowest 10 only: overcharges stay visible here (Betsy, Oct 31)
        if len(highest) == 10:
            break
    # the biggest gap inside one network (what a patient on that plan could save by choosing where to go)
    gap = None
    for n, es in by_net.items():
        if n in flat or len({(e['name'], e['city']) for e in es}) < 5:
            continue
        if gap is None or es[-1]['v'] - es[0]['v'] > gap['hi']['v'] - gap['lo']['v']:
            gap = {'ins': nets[n][0], 'plan': nets[n][1], 'lo': es[0], 'hi': es[-1]}
    # placeholder-looking rates, for the Price File Watch notes (counts by network, a few examples)
    hx = high_e[kind]
    high_counts = {}
    for e in hx:
        k = f"{e['ins']}|{e['plan']}"
        high_counts[k] = high_counts.get(k, 0) + 1
    high_ex = [{'ins': e['ins'], 'plan': e['plan'], 'name': e['name'], 'city': e['city'], 'v': e['v'], 'date': e['date'], 'url': e['url']}
               for e in sorted(hx, key=lambda e: -e['v'])[:5]]

    # hospital price files (45 CFR 180)
    H = D.get('hosp') or {}
    hc = h or {'c': [], 'r': []}
    def hsrc(s):
        x = H['src'][s]
        return {'name': nice(x[0]), 'city': x[1] or HOSP_CITY.get(x[0], ''), 'date': x[3], 'url': x[4], 'unit': x[6] or 'row'}
    nsrc, nins = len(H.get('src') or []), len(H.get('ins') or [])
    wrong = {i for i, x in enumerate(H.get('src') or []) if WRONG_AREA.search(str(x[4] or ''))}
    hc = {'c': [r for r in hc.get('c', []) if r[0] < nsrc and r[0] not in wrong], 'p': hc.get('p'),
          'r': [r for r in hc.get('r', []) if r[0] < nsrc and r[1] < nins and r[0] not in wrong]}   # a per-code file newer than the page's lists: skip unknown entries
    hcash = {}
    for r in hc.get('c', []):
        if r[5] == 0 and (r[0] not in hcash or r[2] < hcash[r[0]]['v']):
            hcash[r[0]] = dict(hsrc(r[0]), v=r[2], row=r[4], list=r[3])
    hcash = sorted(hcash.values(), key=lambda e: e['v'])
    hrate, hoon, hfiles = {}, {}, set()
    plans = hc.get('p') or H.get('plans') or []
    for r in hc.get('r', []):
        hfiles.add(r[0])
        if r[4] or r[5]:                 # doctor's line or percent of charges
            continue
        # Oct 30, 2026 (Betsy, Oct 29): out-of-network amounts (type 5) are kept but never counted as an insurer's
        # in-network rate; they get their own short list on the page
        into = hoon if r[2] == 5 else hrate
        k = (r[0], r[1])
        if k not in into or r[6] < into[k]['v']:
            ins = H['ins'][r[1]]
            if PUBLIC_PAYER.search(ins):   # traditional Medicare / Medicaid lines aren't insurer rates
                continue
            into[k] = dict(hsrc(r[0]), v=r[6], row=r[7], ins=SHOWN.get(ins, ins), plan=plans[r[3]] if r[3] < len(plans) else '',
                           prod=PROD[r[2]] if r[2] < len(PROD) else '')
    hrate = sorted(hrate.values(), key=lambda e: e['v'])
    hoon = sorted(hoon.values(), key=lambda e: e['v'])
    hfiles |= {r[0] for r in hc.get('c', [])}

    cash = sorted([c for c in D.get('cash', []) if c.get('code') == code], key=lambda c: c['v'])
    dates = sorted({file_date(e) for e in allines})

    # facts that make each page its own (all real published figures, nothing averaged)
    byp = {}
    for e in lines:
        byp.setdefault((e['name'], e['city']), []).append(e)
    spread = None
    for (nm, ct), es in byp.items():
        if len({x['net'] for x in es}) >= 3 and es[0]['v'] >= 10:
            r = es[-1]['v'] / es[0]['v']
            if spread is None or r > spread['x']:
                spread = {'name': nm, 'city': ct, 'x': r, 'n': len(es), 'lo': es[0], 'hi': es[-1]}
    sc_lo = next((e for e in lines if e['kind'] == 'A'), None) if kind == 'fac' else None
    h_lo = next((e for e in lines if e['kind'] == 'H'), None) if kind == 'fac' else None
    cities = {}
    for nm, ct in byp:
        if ct:
            cities[ct] = cities.get(ct, 0) + 1
    top_cities = [c for c, _ in sorted(cities.items(), key=lambda x: (-x[1], x[0]))[:15]]
    hdates = {s_: H['src'][s_][3] for s_ in hfiles} if H.get('src') else {}
    h_stale = sum(1 for d in hdates.values() if is_stale(d) or not d)
    rows = None
    if len(lines) <= 5000:            # a downloadable copy of every figure behind the page (kept off very large lists)
        rows = [['insurer rate', e['name'], e['city'], KIND.get(e['kind'], ''), e['ins'], e['plan'], e['v'], file_date(e), e['url']] for e in allines]
        rows += [['hospital cash price', e['name'], e['city'], 'hospital', '', '', e['v'], e['date'], e['url']] for e in hcash]
        rows += [['posted cash price', c['name'], c['city'], '', '', c.get('label', ''), c['v'], c.get('chk', ''), c['src']] for c in cash]
    mlr = {}
    if kind == 'fac' and loc:
        for e in allines:
            if e.get('ml'):
                k_, w_ = e['ml'][0], e['ml'][1]
                r_ = mlr.setdefault(k_, [w_, w_, set()])
                r_[0], r_[1] = min(r_[0], w_), max(r_[1], w_)
                r_[2].add((e['name'], e['city']))
        mlr = {k_: [a_, b_, len(c_)] for k_, (a_, b_, c_) in mlr.items()}
    return {
        'mlr': mlr,
        'code': code, 'name': C['name'], 'common': SEARCH_NAMES.get(code, (None,))[0] or re.sub(r'\s*\([^)]*\)', '', C['name']).strip(), 'cat': C.get('cat', ''), 'type': C.get('type', ''), 'drg': drg, 'kind': kind,
        'lines': len(lines), 'providers': len({(e['name'], e['city']) for e in allines}), 'networks': networks,
        'lo': lines[0] if lines else None, 'hi': lines[-1] if lines else None, 'lowest': lowest,
        'n_out_of_state': n_away,
        'n_pct': len(pct[kind]), 'n_flag': len(flagged[kind]), 'n_high': len(high[kind]), 'n_other_kind': len(other_kind) if kind == 'fac' else 0,
        'fac_kinds': sorted(fac_kinds) if kind == 'fac' else [],
        'doc_lo': doc[0] if doc else None, 'doc_hi': doc[-1] if doc else None, 'doc_n': len(doc),
        'hcash': hcash, 'hrate': hrate, 'hoon': hoon, 'hfiles': len(hfiles), 'cash': cash, 'dates': dates,
        'med': (D.get('med') or {}).get('codes', {}).get(code), 'med_src': (D.get('med') or {}).get('src', {}),
        'med_ar': med_areas_of(D, code),
        'any': bool(lines or hcash or hrate or hoon or cash),
        'highest': highest, 'also_listed': sorted(held.values(), key=lambda e: e['v']), 'proof_on': proof is not None, 'n_wrong_specialty': len(wrong_spec), 'gap': gap, 'flat_out': flat_out, 'high_counts': high_counts, 'high_ex': high_ex,
        'index': len(byp) >= 5 or len(cash) + len(hcash) >= 3,     # thin pages stay out of search until they have more data
        'spread': spread, 'sc_lo': sc_lo, 'h_lo': h_lo, 'cities': top_cities, 'h_stale': h_stale, 'h_total': len(hdates), 'csv': rows,
    }


# ---------------------------------------------------------------- wording
def fee_words(S):
    if S['code'].startswith('00'):
        return 'rates', 'rate'
    if S['drg']:
        return 'hospital-stay rates', 'hospital-stay rate'
    return ('facility fees', 'facility fee') if S['kind'] == 'fac' else ("clinicians' fees", "clinician's fee")


def kinds_words(S):
    k = set(S.get('fac_kinds') or [])
    if 'G' in k:
        return 'hospitals, surgery centers, and imaging centers, labs and clinics' if 'A' in k else 'hospitals, imaging centers, labs and clinics'
    return 'hospitals' if S['drg'] else 'hospitals and surgery centers'


def prov_noun(S):
    if S['kind'] != 'fac':
        return 'provider' if S['providers'] == 1 else 'providers'
    if S['drg']:
        return 'hospital' if S['providers'] == 1 else 'hospitals'
    return 'facility' if S['providers'] == 1 else ('facilities' if 'G' in (S.get('fac_kinds') or []) else 'hospitals and surgery centers')


def netname(ins, plan):
    return plan if plan.lower().startswith(ins.lower()) else f'{ins} {plan}'


# Oct 24 usability test: a few hospital files give no address, so their cash rows showed no city. Filled from our hospital index.
HOSP_CITY = {r['name']: r['city'] for r in csv.DictReader(open(os.path.join(HERE, 'hospital_city_manual.csv'), encoding='utf-8'))}


def who(e):
    return f"{e['name']}{', ' + e['city'] if e.get('city') else ''}"


def whoh(e):
    """who() as HTML, flagging places outside the DFW metro."""
    return f"{esc(e['name'])}{', ' + ctag(e['city']) if e.get('city') else ''}"


def lede(S, area):
    pl, sg = fee_words(S)
    out = []
    if S['lo']:
        if S['lines'] == 1:
            out.append(f"One insurer-negotiated {sg} for {lower_name(S)} is published in {area['name']}: {money(S['lo']['v'])} at {who(S['lo'])}.")
        else:
            out.append(f"Insurers' negotiated {pl} for {lower_name(S, True)} in {area['name']} run from {money(S['lo']['v'])} to "
                       f"{money(S['hi']['v'])}, across {S['providers']:,} {prov_noun(S)} "
                       f"and {len(S['networks'])} {'network' if len(S['networks']) == 1 else 'networks'}.")
        if S['code'].startswith('00'):
            out.append('Many insurers publish anesthesia as a rate per time unit, not a total for the case.')
    cheapest = cheapest_cash(S)
    if cheapest:
        e, src = cheapest
        out.append(f"The lowest cash price we found is {money(e['v'])} at {who(e)}" +
                   ((" (the hospital's own fee only; the hospital's file gives no date)." if not e.get('date') else " (the hospital's own fee only).")
                    if src == 'hospital' else '.'))
    if not out:
        out.append(f"We haven't found a published price for {lower_name(S)} in {area['name']}'s files yet.")
    return ' '.join(out)


def cheapest_cash(S):
    c = [(x, 'posted') for x in S['cash'][:1]] + [(x, 'hospital') for x in S['hcash'][:1]]
    return min(c, key=lambda p: p[0]['v']) if c else None


def lower_name(S, article=False):
    """The everyday name inside a sentence; with article=True, "a colonoscopy" / "an EKG" where English wants one."""
    n = S.get('common') or S['name']
    if article and not re.match(r'(Anesthesia|Physical therapy|Family therapy|Group therapy|YAG)', n) and 'surgery' not in n.lower():
        low = n if re.match(r'^(MRI|CT|EKG|PSA|TSH|ACL|EGD|PT|VBAC|CBC)\b', n) or n[:2].isupper() else n[0].lower() + n[1:]
        return ('an ' if re.match(r'^(EKG|MRI|[aeiouAEIOU])', low) and not low.lower().startswith(('uri', 'ult')) else 'a ') + low
    return n if re.match(r'^(MRI|CT|EKG|PSA|TSH|ACL|EGD|PT)\b', n) or n[:2].isupper() else n[0].lower() + n[1:]


def includes(S):
    """(what the main figure covers, what it leaves out) in plain words, by kind of service."""
    cat, code = S['cat'], S['code']
    if code.startswith('00'):
        return ("The anesthesia professional's fee, as the insurer publishes it. Insurers often pay anesthesia as base units plus "
                "time units, so the total depends on how long the procedure takes.",
                "The procedure itself, the facility fee and the doctor doing the procedure all bill separately.")
    if S['drg']:
        return ("One payment to the hospital for the whole stay in this group: room, nursing, operating room, supplies and drugs.",
                "Doctors' fees (surgeon, anesthesia, other specialists) bill separately, and so can care after you leave.")
    if cat == 'Maternity':
        return ("A global code covers the doctor's care: prenatal visits, the delivery and the check-up after.",
                "The hospital stay, anesthesia (such as an epidural), lab work and ultrasounds bill separately.")
    if cat == 'Lab tests':
        return ("The lab test itself, as the lab or hospital bills it.",
                "The blood draw often bills as its own small charge, and so does the visit that ordered the test.")
    if cat == 'Imaging':
        if S['kind'] == 'fac':
            return ("The facility's fee for doing the scan: the machine, the technologist and the room.",
                    "The radiologist's fee for reading the scan bills separately. If contrast is not part of this code, it can add a charge.")
        return ("The amount for the scan as billed by the clinician or imaging center.",
                "If done at a hospital, the hospital can add its own facility fee.")
    if cat in ('Office visits', 'Behavioral health', 'Physical therapy', 'Heart', 'Other') and S['kind'] == 'doc':
        return ("The clinician's fee for the visit or service.",
                "If the clinic is owned by a hospital, the hospital can bill a separate facility fee. Tests and procedures done "
                "at the visit bill separately.")
    if S['kind'] == 'fac':
        return ("The facility fee at the hospital or surgery center: the room, equipment, nursing staff and supplies.",
                "The doctor's fee, anesthesia and pathology (if tissue goes to a lab) bill separately, and so do follow-up visits. "
                "If more is done during the procedure, a different code can be billed.")
    return ("The clinician's fee for this service.", "Any facility fee, anesthesia and lab work bill separately.")


# ---------------------------------------------------------------- HTML
SITE = {'name': 'The Healthcare Files', 'robots': 'noindex,nofollow', 'origin': 'https://files.healthcare', 'nav': ''}


def site_settings(site):
    """Name, robots setting and navigation come from the live homepage, so a rename or going public carries over."""
    home = open(os.path.join(site, 'index.html'), encoding='utf-8').read()
    m = re.search(r'<meta property="og:site_name" content="([^"]+)"', home)
    if m:
        SITE['name'] = html.unescape(m.group(1))
    m = re.search(r'<meta name="robots" content="([^"]+)"', home)
    SITE['robots'] = m.group(1) if m else ''
    m = re.search(r'<meta property="og:url" content="(https://[^/"]+)', home)
    if m:
        SITE['origin'] = m.group(1)
    m = re.search(r'<nav class="nav" id="nav"[^>]*>(.*?)</nav>', home, re.S)
    nav = m.group(1) if m else ''
    nav = re.sub(r'<button[^>]*data-open[^>]*>.*?</button>', '', nav, flags=re.S)
    nav = nav.replace('href="#/', 'href="/#/')
    nav = re.sub(r'\s+data-route="[^"]*"', '', nav)
    if '/costs/' not in nav:
        nav = nav.replace('<a href="/price-finder/">', '<a href="/costs/">Prices</a><a href="/price-finder/">', 1)
    SITE['nav'] = nav.strip()


def logo():
    n = SITE['name'].split(' ')
    return esc(' '.join(n[:-1])) + (' <span>' + esc(n[-1]) + '</span>' if len(n) > 1 else esc(n[0]))


def shell(title, desc, canon, body, jsonld, noindex=False):
    robots = SITE['robots'] or ''
    if noindex and 'noindex' not in robots:
        robots = 'noindex' + (',' + robots if robots else '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{f'<meta name="robots" content="{esc(robots)}">' if robots else ''}
<title>{esc(title)}</title><meta name="description" content="{esc(desc)}"><link rel="canonical" href="{esc(SITE['origin'] + canon)}">
<meta property="og:site_name" content="{esc(SITE['name'])}"><meta property="og:type" content="article"><meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}"><meta property="og:url" content="{esc(SITE['origin'] + canon)}"><meta property="og:image" content="{esc(SITE['origin'])}/og.jpg"><meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#4A1C1C"><link rel="icon" href="/favicon-32.png" sizes="32x32"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;900&family=Courier+Prime:wght@400;700&family=Public+Sans:ital,wght@0,400;0,600;0,800;1,400&display=swap">
<link rel="stylesheet" href="/costs/costs.css">
<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False).replace('</', '<\\/')}</script>
</head><body>
<header class="bar"><div class="wrap"><a class="logo" href="/">{logo()}</a>
<a class="start-pill" href="/start-here/">Start here</a><button class="menu-btn" id="menuBtn" aria-expanded="false" aria-controls="nav">Menu</button>
<nav class="nav" id="nav" aria-label="Main">{SITE['nav']}</nav></div></header>
{body}
<footer><div class="wrap"><span><b>{esc(SITE['name'])}</b><br>Every price links to the file it came from.</span>
<span><a href="/about/">About</a> · <a href="/methodology/">How we read the files</a> · <a href="/corrections/">Corrections</a> · <a href="/disclaimers/">Disclaimers</a> · <a href="/privacy/">Privacy</a> · <a href="/terms/">Terms</a> · <a href="/accessibility/">Accessibility</a></span></div></footer>
<script>(()=>{{const b=document.getElementById('menuBtn'),n=document.getElementById('nav');b.addEventListener('click',()=>{{const o=n.classList.toggle('open');b.setAttribute('aria-expanded',String(o));}});}})();</script>
</body></html>'''


def src_link(e, label=None):
    fd = file_date(e)
    stale = is_stale(fd)
    text = label or ('file dated ' + fd if fd else 'file has no date')
    return (f'<a class="src" href="{esc(e["url"])}" target="_blank" rel="noopener">{esc(text)} ↗</a>'
            + (' <span class="flag">over 6 months old</span>' if stale else ''))


def hosp_link(e):
    d = e['date'] or 'undated'
    stale = is_stale(e.get('date'))
    return (f'<a class="src" href="{esc(e["url"])}" target="_blank" rel="noopener">{esc(d)} · {esc(e["unit"])} {e["row"]:,} ↗</a>'
            + (' <span class="flag">over 6 months old</span>' if stale else ('' if e.get('date') else ' <span class="flag">no date in file</span>')))


def tile(label, v, sub):
    return f'<div class="tile"><p class="tl">{esc(label)}</p><p class="tv">{v}</p><div class="ts">{sub}</div></div>'


def med_areas_of(D, code):
    """Oct 30, 2026: the page's other Medicare doctor-fee areas for this code (site/med_areas.py, D.med.ar), the same figures
    the Price Finder works out: {'s': state, 'p': provider areas, 'd': default index, 'a': [[name, office, facility, providers]]}."""
    A = (D.get('med') or {}).get('ar')
    m = ((D.get('med') or {}).get('codes') or {}).get(code) or {}
    if not A or 'pfs' not in m or code not in A['r']:
        return None
    import med_areas
    out = []
    for a, nm in enumerate(A['n']):
        x = ((A['x'].get(code) or {}).get('pfs') or {}).get(str(a)) or ((A['x'].get(code) or {}).get('pfs') or {}).get(a)
        f = x or med_areas.fee_py(A['r'][code], A['g'][a])
        out.append([nm, f[0], f[1], A['c'][a]])
    return {'s': A['s'], 'p': A['p'], 'd': A['d'], 'a': out}


def med_lines(S, med_area):
    m, src = S['med'], S['med_src']
    if not m:
        return []
    L = []
    lk = lambda k: f'<a class="src" href="{esc(src[k][1])}" target="_blank" rel="noopener">CMS {esc(src[k][2])} ↗</a>' if k in src else ''
    if m.get('opps'):
        L.append(('Hospital outpatient facility fee', 'national rate before local adjustment', m['opps'][0], lk('opps')))
    if m.get('asc'):
        L.append(('Surgery center facility fee', 'national rate before local adjustment', m['asc'][0], lk('asc')))
    if m.get('pfs'):
        d = m['pfs'].get('dal') or [None, None]
        if d[1] is not None:
            L.append(('Doctor, when done in a hospital or surgery center', med_area, d[1], lk('pfs')))
        if d[0] is not None and d[0] != d[1]:
            L.append(('Doctor or clinic, when done in an office', med_area + ', includes office costs', d[0], lk('pfs')))
        if d[0] is not None and d[0] == d[1]:
            L.append(('Total when billed by an office or imaging center', med_area, d[0], lk('pfs')))
    if m.get('pfs26'):
        L.append(('Doctor reading the results only', med_area, m['pfs26']['dal'][1], lk('pfs')))
    MA = S.get('med_ar')
    if MA and m.get('pfs'):   # Oct 30, 2026: the doctor's fee in the state's other Medicare areas that have providers here
        for a, (nm, nf, fa, cnt) in enumerate(MA['a']):
            if a == MA['d'] or not cnt or (nf is None and fa is None):
                continue
            L.append((f"Doctor, in a hospital or surgery center: {nm}" if fa is not None else f"Doctor or clinic, in an office: {nm}",
                      f"{nm} Medicare area · {cnt:,} provider{'s' if cnt != 1 else ''} on this page" + (f" · office {money(nf)}" if fa is not None and nf is not None and nf != fa else ''),
                      fa if fa is not None else nf, lk('pfs')))
    if m.get('lab'):
        L.append(('Lab test', 'national payment limit', m['lab'], lk('lab')))
    return L


_ANES = None
def anes_med(S, area):
    """Medicare anesthesia yardstick (Oct 19): the code's base units and the area's CY2026 conversion factor(s), straight from CMS
    files in reference/medicare/. Never assumes a case length. None when the code has no CMS base units."""
    global _ANES
    code = S.get('code') or ''
    if _ANES is None:
        ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'reference', 'medicare')
        _ANES = ({r['code']: int(r['base_units']) for r in csv.DictReader(open(os.path.join(ref, 'anes_base_units.csv')))},
                 list(csv.DictReader(open(os.path.join(ref, 'anes_cf_2026.csv')))))
    bu, cf = _ANES
    if code not in bu:
        return None
    st = ('TEXAS' if area_state(area) == 'TX' else STATE_NAME[area_state(area)].upper() if area['slug'] in CA_REGIONS
          else area['slug'].replace('-', ' ').upper())
    if area['slug'] in ('washington-dc', 'district-of-columbia'):
        locs = [r for r in cf if r['locality_name'].startswith('DC')]
    elif area['slug'] in CA_REGIONS:   # Oct 31, 2026: the Medicare localities of the region's counties (site/regions.py)
        code_ = CA_REGIONS[area['slug']]
        cty = set(_regions.REGIONS[_regions.PARENT[area['slug']]][area['slug']][4])
        nums = {z['area'].split('-')[1] for z in csv.DictReader(open(os.path.join(HERE, '..', 'data', 'medicare_areas_zip.csv')))
                if z['state'] == code_ and z['county'] in cty}
        # a state's Medicare contractor numbers share their first 3 digits (California 01112/01182, New York 13202/13282/13292)
        pre3 = {r['contractor'][:3] for r in cf if r['locality_name'] in (st, 'REST OF ' + st)}
        cons = {r['contractor'] for r in cf if r['contractor'][:3] in pre3}
        locs = [r for r in cf if r['contractor'] in cons and r['locality'] in nums]
    else:
        cons = {r['contractor'] for r in cf if r['locality_name'] in (st, 'REST OF ' + st) or r['locality_name'].startswith(st + ', ')}   # 'HAWAII, GUAM'
        locs = [r for r in cf if r['contractor'] in cons]
        if area['slug'] not in STATES:          # a metro or town: its own Medicare localities, else the rest of the state
            words = ' ' + re.sub(r'[^A-Z]+', ' ', area['name'].upper()) + ' '
            locs = [r for r in locs if f" {r['locality_name']} " in words] or [r for r in locs if r['locality_name'] == 'REST OF ' + st]
    if not locs:
        return None
    v = sorted(float(r['cf_nonqualifying_apm']) for r in locs)
    where = ', '.join(sorted(r['locality_name'].title().replace('Dc ', 'DC ').replace('Md/Va', 'MD/VA').replace('Nyc ', 'NYC ').replace(' Of ', ' of ') for r in locs))
    return {'bu': bu[code], 'lo': v[0], 'hi': v[-1], 'where': where if len(locs) <= 3 else f"{len(locs)} Medicare areas in the {'region' if area['slug'] in CA_REGIONS else 'state'}"}


LABOR = {'H': 0.60, 'A': 0.50}    # labor-related share: OPPS 60% (CY2026 OPPS final rule), ASC 50% (MedPAC ASC Payment Basics, Oct 2026)


def med_here(S, e):
    """Medicare's facility rate for this hospital or surgery center after CMS's local wage adjustment (our calculation), or None."""
    m = S.get('med') or {}
    ml = (e or {}).get('ml')
    if S.get('drg') or S.get('kind') != 'fac' or not ml:
        return None
    nat = (m.get('opps') or [None])[0] if ml[0] == 'H' else (m.get('asc') or [None])[0] if ml[0] == 'A' else None
    if not nat:
        return None
    return round(nat * (LABOR[ml[0]] * ml[1] + 1 - LABOR[ml[0]]), 2)


def med_here_line(S, e, short=False):
    m = S.get('med') or {}
    if S.get('drg') or S.get('kind') != 'fac' or not e or e.get('kind') not in ('H', 'A'):
        return ''
    if not (m.get('opps') if e['kind'] == 'H' else m.get('asc')):
        return ''
    x = med_here(S, e)
    place = 'hospital' if e['kind'] == 'H' else 'surgery center'
    if x is None:
        return '' if short else (f"<br><span class='m'>Medicare's rate for this {place}: not calculated (we couldn't match it to its Medicare record; "
                                 f"national rate shown below)</span>")
    if short:
        return f" · Medicare here {money(x)}"
    return (f"<br>Medicare's rate for this {place}: <b>{money(x)}</b> <span class='m'>(our calculation from CMS's national rate and "
            f"its local wage index, CY2026 · <a href='/methodology/#medicare-local'>how</a>)</span>")


def local_range(S):
    """One line per facility type: Medicare's rate after the local wage adjustment, lowest and highest across places on this page."""
    m, out = S.get('med') or {}, []
    if S.get('drg') or S.get('kind') != 'fac':
        return ''
    for k, key, place in (('H', 'opps', 'hospitals'), ('A', 'asc', 'surgery centers')):
        r = (S.get('mlr') or {}).get(k)
        if not r or not m.get(key):
            continue
        nat, sh = m[key][0], LABOR[k]
        lo, hi = round(nat * (sh * r[0] + 1 - sh), 2), round(nat * (sh * r[1] + 1 - sh), 2)
        rng = money(lo) if lo == hi else f"{money(lo)} to {money(hi)}"
        out.append(f"<p class='cap'>After CMS's local wage adjustment, Medicare's {'hospital outpatient' if k == 'H' else 'surgery center'} rate at the "
                   f"{r[2]:,} {place if r[2] != 1 else place[:-1]} on this page we could match to their Medicare records is {rng} "
                   f"(our calculation from the national rate and each place's FY2026 wage index; "
                   f"<a href='/methodology/#medicare-local'>how</a>, <a href='https://www.cms.gov/files/zip/fy2026-ipps-fr-impact-file.zip'>CMS wage index file</a>).</p>")
    return ''.join(out)


def allin_html(S):
    """One all-in line under the tiles (Oct 19, first-visitor test): the cheapest place fee plus the typical doctor's fee in the same network."""
    best = None
    for n in S.get('networks') or []:
        if n.get('doc_mid') and not n.get('flat') and not n.get('par'):
            t = n['lo']['v'] + n['doc_mid']
            if best is None or t < best[0]:
                best = (t, n)
    if not best:
        return ''
    t, n = best
    return (f"<p class='allin'><b>All in, place plus doctor: from {money(t)}</b> in {esc(netname(n['ins'], n['plan']))} "
            f"({money(n['lo']['v'])} at {esc(n['lo']['name'])} + {money(n['doc_mid'])}, the typical doctor's fee in that network). "
            f"Anesthesia and any lab work bill separately. <a href='#by-network'>Your network's total</a></p>")


def jump_html(S, cash_sec, hosp_sec, med_sec):
    L = [('#by-network', 'Your insurance')] if S.get('networks') else []
    L += [('#cash', 'Cash prices')] if cash_sec else []
    L += [('#hospital-files', "Hospitals' own files")] if hosp_sec else []
    L += [('#medicare', 'Medicare')] if med_sec else []
    return ("<nav class='jump' aria-label='On this page'><span>Jump to:</span>" + ''.join(f"<a href='{h}'>{esc(t)}</a>" for h, t in L) + '</nav>') if len(L) > 1 else ''


def kind_ok_both(S, m):
    return S.get('kind') == 'fac' and bool(m.get('asc')) and bool(m.get('opps')) and m['asc'][0] < m['opps'][0]


def med_headline(S):
    m = S['med']
    if not m:
        return None
    if S['kind'] == 'fac' and m.get('opps'):
        return m['opps'][0], 'hospital outpatient facility fee, national rate before local adjustment'
    if m.get('lab'):
        return m['lab'], 'lab payment limit'
    d = (m.get('pfs') or {}).get('dal') or [None, None]
    if S['kind'] == 'doc' and d[0] is not None:
        return d[0], 'office rate, local'
    if d[1] is not None:
        return d[1], "doctor's fee, local"
    return None


def covers(S):
    if S['drg']:
        return "Both are the hospital's payment for the whole stay; doctors bill separately."
    if S['code'].startswith('00'):
        return 'Both are the anesthesia rate as published.'
    if S['kind'] == 'fac':
        cat = S.get('cat') or ''
        if cat == 'Imaging':
            return "Both are the facility fee only; the radiologist who reads the scan bills separately."
        if cat == 'Lab tests':
            return "Both are the lab's fee for the test; drawing the blood may be billed separately."
        if cat in ('Office visits', 'Behavioral health', 'Physical therapy', 'Heart'):
            return "Both are the facility fee only; the clinician's fee is billed separately."
        return "Both are the facility fee only; the doctor and anesthesia bill separately."
    return "Both are the clinician's fee only; any facility fee is extra."


def savings(S):
    """Same network, same procedure: the biggest gap a patient on that plan could avoid by choosing where to go."""
    g = S.get('gap')
    if not g or g['hi']['v'] - g['lo']['v'] < 1:
        return ''
    d = g['hi']['v'] - g['lo']['v']
    return (f"Same procedure, same network ({esc(netname(g['ins'], g['plan']))}): {money(g['hi']['v'])} at {esc(g['hi']['name'])}, "
            f"{money(g['lo']['v'])} at {esc(g['lo']['name'])}. That's {money(d)} more for the same {esc(fee_words(S)[1])}, and before you meet "
            f"your deductible, much of that difference can be yours to pay. {esc(covers(S))}")


def stands_out(S, area):
    """Two to four sentences written from this page's own figures (so no two pages read alike)."""
    out, pl = [], fee_words(S)[0]
    sv = savings(S)
    if sv:
        out.append(sv)
    sc, h = S.get('sc_lo'), S.get('h_lo')
    if sc and h:
        lo, hi = (sc, h) if sc['v'] <= h['v'] else (h, sc)
        out.append(f"The lowest {pl[:-1] if pl.endswith('s') else pl} at a surgery center is {money(sc['v'])} ({esc(sc['name'])}); at a hospital, "
                   f"{money(h['v'])} ({esc(h['name'])})." + (f" Going to the {'surgery center' if lo is sc else 'hospital'} saves "
                   f"{money(hi['v'] - lo['v'])} at the lowest published rate." if hi['v'] > lo['v'] else ''))
    sp = S.get('spread')
    if sp and sp['x'] >= 1.5:
        out.append(f"Same place, different insurer: at {esc(sp['name'])}{', ' + esc(sp['city']) if sp['city'] else ''}, the published rate runs from "
                   f"{money(sp['lo']['v'])} ({esc(netname(sp['lo']['ins'], sp['lo']['plan']))}) to {money(sp['hi']['v'])} "
                   f"({esc(netname(sp['hi']['ins'], sp['hi']['plan']))}), {sp['x']:.1f} times as much.")
    nets = [n for n in S['networks'] if not n['flat']]
    if len(nets) >= 3:
        a, b = nets[0], max(nets, key=lambda n: n['lo']['v'])
        if b['lo']['v'] > a['lo']['v']:
            out.append(f"Network matters: the lowest rate in {esc(netname(a['ins'], a['plan']))} is {money(a['lo']['v'])}; in "
                       f"{esc(netname(b['ins'], b['plan']))} even the lowest is {money(b['lo']['v'])}.")
    mh = med_headline(S)
    if mh and S['lo'] and S['kind'] == 'fac' and S['lo'].get('kind') == 'A' and (S['med'] or {}).get('asc'):
        mh = S['med']['asc'][0], 'surgery center facility fee'   # compare like with like: the lowest is at a surgery center
    MA = S.get('med_ar')
    if mh and MA and S['lo'] and S['lo'].get('fi') is not None and 'local' in mh[1]:   # Oct 30: the lowest provider's own Medicare area
        c = MA['p'][S['lo']['fi']] if S['lo']['fi'] < len(MA['p']) else '-'
        if c != '-':
            nm, nf, fa, _ = MA['a'][int(c, 36)]
            v = nf if 'office' in mh[1] else fa
            if v:
                mh = v, ('office rate' if 'office' in mh[1] else "doctor's fee") + f", {nm} Medicare area"
    if mh and S['lo'] and mh[0] > 0:
        x = S['lo']['v'] / mh[0]
        out.append(f"The lowest negotiated rate here is {x:.1f} times Medicare's {money(mh[0])} ({esc(mh[1])})."
                   if x >= 1.05 else f"The lowest negotiated rate here, {money(S['lo']['v'])}, is {'about the same as' if x >= 0.95 else 'below'} Medicare's {money(mh[0])} ({esc(mh[1])}).")
    if S.get('h_total'):
        out.append(f"{S['h_stale']} of the {S['h_total']} hospital price files that list this code are over six months old or undated."
                   if S['h_stale'] else f"All {S['h_total']} hospital price files that list this code are dated within the last six months.")
    return out[:6]


def faq(S, area, med_area):
    """Questions people search, answered only from this page's figures."""
    cm, an, pl = lower_name(S, True), area['name'], fee_words(S)[0]
    qa = []
    cc = cheapest_cash(S)
    ncash = len(S['cash']) + len(S['hcash'])
    if cc:
        e, k = cc
        a = (f"The lowest cash price we found in {an} is {money(e['v'])} at {e['name']}"
             + (", from the hospital's own price file; that covers the hospital's fee only, and the doctor and anesthesia bill separately." if k == 'hospital' else ", posted on its own website.")
             + f" We found {ncash} cash {'price' if ncash == 1 else 'prices'} in all. If you pay cash, ask for a written Good Faith Estimate when you schedule; "
               "you can dispute a bill that comes in $400 or more above it.")
    else:
        a = (f"We haven't found a posted cash price for {cm} in {an} yet. Ask the provider for its self-pay price and a written Good Faith "
             "Estimate before you schedule; providers must give one to patients paying cash.")
    qa.append((f"How much does {cm} cost without insurance in {an}?", a))
    if S['lo']:
        a = (f"It depends on your insurer's network and where you go. Published negotiated {pl} in {an} run from {money(S['lo']['v'])} to "
             f"{money(S['hi']['v'])}. That rate is the total; what you owe depends on your plan. Before you meet your deductible you may pay "
             "the whole negotiated rate; after that, your coinsurance or copay.")
        qa.append((f"How much does {cm} cost with insurance in {an}?", a))
        if S['lines'] > 1 and S['lo']['v'] > 0:
            a = (f"The highest published rate is {S['hi']['v'] / S['lo']['v']:.0f} times the lowest. Each insurer negotiates its own contract with each "
                 "hospital, surgery center and doctor, so the same service has many prices.")
            sc, h = S.get('sc_lo'), S.get('h_lo')
            if sc and h and h['v'] > sc['v']:
                a += f" Here, the lowest hospital rate ({money(h['v'])}) is above the lowest surgery center rate ({money(sc['v'])})."
            sp = S.get('spread')
            if sp and sp['x'] >= 1.5:
                a += f" Even at one place, {sp['name']}, rates range {sp['x']:.1f}-fold depending on the insurer."
            qa.append((f"Why do {lower_name(S)} prices vary so much?", a))
    inc, exc = includes(S)
    qa.append((f"What does the price of {cm} include?", f"{inc} Not included: {exc[0].lower() + exc[1:]}"))
    mh = med_headline(S)
    if mh:
        qa.append((f"What does Medicare pay for {cm}?", f"Traditional Medicare's 2026 rate is {money(mh[0])} ({mh[1]}{', ' + med_area if 'local' in mh[1] else ''}). "
                   "It's a benchmark; people with private insurance or no insurance can't get Medicare's rate."))
    qa.append(("Where do these prices come from?", "From the files insurers must publish under the federal Transparency in Coverage rule, hospitals' "
               f"standard charges files, and prices providers post themselves. Each figure on this page links to its file and date. Updated {updated()}."))
    return qa


def procedure_page(area, S, others, related, med_area, pf_path, au, ps):
    AREA_SLUG[0] = area['slug']
    S['anes'] = anes_med(S, area)
    pl, sg = fee_words(S)
    url = f'/costs/{au}/{ps}/'
    cm, yr = common(S), TODAY.year
    small = {'a', 'an', 'the', 'of', 'with', 'and', 'for', 'in', 'or', 'after', 'without', 'to'}
    tc = ' '.join(w if (i and w.lower() in small) or w[:1].isupper() or not w[:1].isalpha() else w[:1].upper() + w[1:]
                  for i, w in enumerate(cm.split(' ')))
    tc = re.sub(r'\((\w)', lambda m: '(' + m.group(1).upper(), tc)
    if S['lo'] and S['lines'] > 1:
        title = f"{tc} Cost in {area['name']}: {short_money(S['lo']['v'])} to {short_money(S['hi']['v'])} ({yr})"
    elif S['lo']:
        title = f"{tc} Cost in {area['name']}: {short_money(S['lo']['v'])} ({yr})"
    else:
        title = f"{tc} Cost in {area['name']} ({yr})"
    title += f" | {SITE['name']}"
    desc = lede(S, area)
    csv_url = f'{url}prices.csv' if S.get('csv') is not None or S.get('has_csv') else None
    finder = f"{pf_path}?code={S['code']}"
    tiles = []
    if S['lo']:
        tiles.append(tile(f'Lowest negotiated {sg}', money(S['lo']['v']), f"{esc(S['lo']['name'])}{', ' + ctag(S['lo']['city']) if S['lo'].get('city') else ''}<br>{esc(netname(S['lo']['ins'], S['lo']['plan']))} · {src_link(S['lo'])}{med_here_line(S, S['lo'])}"))
        if S['lines'] > 1:
            tiles.append(tile(f'Highest negotiated {sg}', money(S['hi']['v']), f"{esc(S['hi']['name'])}{', ' + ctag(S['hi']['city']) if S['hi'].get('city') else ''}<br>{esc(netname(S['hi']['ins'], S['hi']['plan']))} · {src_link(S['hi'])}{med_here_line(S, S['hi'])}"))
    cc = cheapest_cash(S)
    if cc:
        e, kind = cc
        sub = (f"{esc(e['name'])}{', ' + ctag(e['city']) if e.get('city') else ''}<br>"
               + (f"posted on its site, checked {esc(e['chk'])} · <a class='src' href='{esc(e['src'])}' target='_blank' rel='noopener'>source ↗</a>"
                  if kind == 'posted' else f"hospital's own fee only · {hosp_link(e)}"))
        tiles.append(tile('Lowest cash price', money(e['v']) + (('–' + money(e['hi'])) if kind == 'posted' and e.get('hi') else ''), sub))
    mh = med_headline(S)
    m2 = S.get('med') or {}
    if mh and kind_ok_both(S, m2):   # one tile, both kinds of place: surgery center and hospital (Oct 19)
        tiles.append(tile("Medicare's national rate", f"{money(m2['asc'][0])} – {money(m2['opps'][0])}",
                          "surgery center – hospital outpatient facility fee, 2026, before the local adjustment · a benchmark, not a price you can get"
                          " · <a href='/methodology/#medicare-local'>how Medicare adjusts for local costs</a>"))
    elif mh:
        nat = 'national rate' in mh[1]
        tiles.append(tile("Medicare's national rate" if nat else 'Medicare pays', money(mh[0]),
                          f"{esc(mh[1])}, 2026 · a benchmark, not a price you can get" + (" · <a href='/methodology/#medicare-local'>how Medicare adjusts for local costs</a>" if nat else '')))

    notes = []
    if S['n_flag']:
        notes.append(f"{S['n_flag']:,} {'rate is' if S['n_flag'] == 1 else 'rates are'} flagged in the insurer files as unusually low and left out of these figures.")
    if S.get('n_high'):
        notes.append(f"{S['n_high']:,} {'rate looks' if S['n_high'] == 1 else 'rates look'} like filler numbers in the insurer files "
                     "(such as 59,999.99, far above every real rate for this code) and {'is' if S['n_high'] == 1 else 'are'} left out of these figures. They're labeled \"unusually high\" in the Price Finder.")
    if S.get('n_other_kind'):
        notes.append(f"Counting {kinds_words(S)} only. {S['n_other_kind']:,} more facility fees are listed for places unlikely to offer this "
                     "service (individual clinicians, rehab, psychiatric or nursing facilities, clinics that don't do it). Insurer files often list "
                     "rates for every contracted place, whether or not it does the procedure. They're all in the Price Finder.")
    if any(n.get('par') for n in S['networks']):
        notes.append("BCBSTX ParPlan isn't a network a member picks. It's BCBSTX's contract with providers, in or out of its networks, who agree to accept "
                     "BCBSTX's allowed amount, file the claim and not bill the member above it (BCBSTX: bcbstx.com/provider/network/parplan.html). "
                     "ParPlan rates are listed in the networks table but left out of this page's low and high figures.")
    if any(n['flat'] for n in S['networks']):
        notes.append("Networks marked \"same rate everywhere\" or \"same $… at N of M places\" pay all or most of their listed places one amount. "
                     "They're shown in the networks table but left out of the rankings"
                     + (" and this page's low and high figures." if S.get('flat_out') else "."))
    if S['n_pct']:
        notes.append(f"{S['n_pct']:,} {'rate is' if S['n_pct'] == 1 else 'rates are'} published as a percent of billed charges, with no dollar figure.")

    # by network
    both_cols = S['kind'] == 'fac' and any(n.get('doc_mid') for n in S['networks'])
    if both_cols:
        notes.insert(0, "Both bills = the lowest place fee in that network plus the middle fee that network lists for doctors (MD or DO) and "
                        "practices for this code. Anesthesia and any lab work bill separately, and your share depends on your plan.")
    net_rows = ''.join(
        f"<tr><td><b>{esc(n['ins'])}</b><div class='m'>{esc(n['plan'])}{(' · <span class=flag>same rate everywhere</span>' if n['one'][1] == n['one'][0] else f" · <span class=flag>same {money(n['one'][2])} at {n['one'][1]} of {n['one'][0]} places</span>") if n.get('one') else ''}{' · <span class=flag>not in this page&#39;s low and high figures</span>' if n.get('par') else ''}</div></td><td class='num' data-l='Providers'>{n['n']:,}</td>"
        f"<td class='num' data-l='Lowest'>{money(n['lo']['v'])}<div class='m'>{whoh(n['lo'])} · {src_link(n['lo'], n['lo']['date'])}</div></td>"
        f"<td class='num' data-l='Highest'>{money(n['hi']['v']) if n['n'] > 1 else '—'}"
        f"{'<div class=m>' + whoh(n['hi']) + ' · ' + src_link(n['hi'], n['hi']['date']) + '</div>' if n['n'] > 1 else ''}</td>"
        + ((f"<td class='num' data-l='+ Doctor (typical)'>{money(n['doc_mid'])}</td><td class='num tot' data-l='= Both bills, from'>{money(n['lo']['v'] + n['doc_mid'])}</td>"
            if n.get('doc_mid') else "<td class='num' data-l='+ Doctor (typical)'>—</td><td class='num' data-l='= Both bills, from'>—</td>") if both_cols else '')
        + "</tr>"
        for n in S['networks'])
    net_sec = (f"<section class='sec' id='by-network'><p class='kicker'>What it costs, by insurer network</p><h2>Who pays what</h2>"
               f"<p class='lede'>Each line counts the providers with a published {sg} in that network and shows the lowest and highest. "
               f"Your plan's network decides which line applies to you. <a href='{esc(finder)}'>See every place and every insurer side by side, "
               f"with Medicare beside each place →</a></p>"
               f"<div class='tbl'><table class='rates stack'><thead><tr><th>Network</th><th class='num'>Providers</th><th class='num'>Lowest</th><th class='num'>Highest</th>"
               f"{'<th class=num>+ Doctor (typical)</th><th class=num>= Both bills, from</th>' if both_cols else ''}</tr></thead>"
               f"<tbody>{net_rows}</tbody></table></div>{''.join(f'<p class=cap>{esc(x)}</p>' for x in notes)}</section>") if S['networks'] else ''

    def proof_bit(e):
        x = e.get('proof')
        if isinstance(x, dict):
            return f" · <a href='{esc(x['url'])}' target='_blank' rel='noopener'>{esc(x['text'])}</a>"
        return " · <a href='/methodology/#proof'>No Medicare or Medicaid record found</a>" if e.get('noproof') else ''
    low_li = ''.join(f"<li><span><b>{esc(e['name'])}</b>{' · ' + ctag(e['city']) if e['city'] else ''}<span class='m'>{esc(KIND.get(e['kind'], ''))} · "
                     f"{esc(netname(e['ins'], e['plan']))} · {src_link(e, e['date'])}{proof_bit(e)}{med_here_line(S, e, True)}</span></span><span class='n'>{money(e['v'])}</span></li>" for e in S['lowest'])
    al = S.get('also_listed') or []
    also = (f"<details class='cap'><summary>Also listed, may not offer this procedure ({len(al)})</summary><p>{esc(al[0]['note'])} "
            f"<a href='/methodology/#proof'>How we decide</a></p><ul>"
            + ''.join(f"<li>{esc(e['name'])}{' · ' + esc(e['city']) if e['city'] else ''} · {esc(netname(e['ins'], e['plan']))} · "
                      f"{money(e['v'])} · {src_link(e, e['date'])}</li>" for e in al) + "</ul></details>") if al else ''
    pcap = (f"<p class='cap'>Order is by price only. Where Medicare or Medicaid billing records show a provider does this procedure, "
            f"we say so and link the record. \"No record found\" doesn't mean they don't do it: Medicare is mostly people 65+, and small "
            f"counts are hidden. <a href='/methodology/#proof'>About these records</a></p>") if S.get('proof_on') else ''
    who_ = ("clinician{s} whose federal provider listing names another specialty (nurses or nurse anesthetists, for example)" if proof_mod.DELIVERY_CODE.match(str(S.get('code') or ''))
            else "anesthesia clinician{s}").format(s='s' if S['n_wrong_specialty'] != 1 else '')
    wcap = (f"<p class='cap'>{S['n_wrong_specialty']:,} {who_} listed under this "
            f"code by an insurer {'are' if S['n_wrong_specialty'] != 1 else 'is'} left out of this page's figures (including the doctor's fee): they don't perform it. "
            f"Their rates stay in the Price Finder.</p>") if S.get('n_wrong_specialty') else ''
    low_sec = (f"<div><p class='kicker'>Lowest published</p><h3 class='h3'>The 10 lowest {pl}</h3><ol class='word'>{low_li}</ol>{pcap}{also}{wcap}</div>") if low_li else ''
    hi_li = ''.join(f"<li><span><b>{esc(e['name'])}</b>{' · ' + ctag(e['city']) if e['city'] else ''}<span class='m'>{esc(KIND.get(e['kind'], ''))} · "
                    f"{esc(netname(e['ins'], e['plan']))} · {src_link(e, e['date'])}{med_here_line(S, e, True)}</span></span><span class='n'>{money(e['v'])}</span></li>"
                    for e in (S.get('highest') or []))
    sv = savings(S)
    high_sec = (f"<div><p class='kicker'>Highest published</p><h3 class='h3'>The 10 highest {pl}</h3>"
                + (f"<p class='save'>{sv}</p>" if sv else '') + f"<ol class='word'>{hi_li}</ol></div>") if hi_li and S['lines'] >= 12 else ''

    cash_items = [(c['v'], f"<li><span><b>{esc(c['name'])}</b> · {ctag(c['city'])}<span class='m'>posted cash price{' · ' + esc(c['label']) if c.get('label') else ''}"
                           f"{' · ' + esc(c['note']) if c.get('note') else ''} · checked {esc(c['chk'])} · <a class='src' href='{esc(c['src'])}' target='_blank' rel='noopener'>source ↗</a></span></span>"
                           f"<span class='n'>{money(c['v'])}{('–' + money(c['hi'])) if c.get('hi') else ''}</span></li>") for c in S['cash']]
    cash_items += [(e['v'], f"<li><span><b>{esc(e['name'])}</b>{' · ' + ctag(e['city']) if e['city'] else ''}<span class='m'>hospital price file, hospital fee only"
                            f"{' · list price ' + money(e['list']) if e.get('list') else ''} · {hosp_link(e)}</span></span><span class='n'>{money(e['v'])}</span></li>")
                   for e in S['hcash'][:10]]
    cash_items.sort(key=lambda x: x[0])
    more_h = len(S['hcash']) - 10
    cash_sec = (f"<div id='cash'><p class='kicker'>Paying without insurance</p><h3 class='h3'>Cash prices</h3><ol class='word'>{''.join(x[1] for x in cash_items)}</ol>"
                + (f"<p class='cap'>{more_h} more hospital cash prices in the <a href='{esc(finder)}'>Price Finder</a>.</p>" if more_h > 0 else '')
                + "<p class='cap'>Ask for a written <b>Good Faith Estimate</b> when you schedule. Providers must give one to patients paying "
                  "cash, and you can dispute a bill that comes in $400 or more above it.</p></div>") if cash_items else ''

    hosp_sec = ''
    oon_sec = ''
    if S.get('hoon'):   # Oct 30, 2026: out-of-network amounts hospitals list, shown on their own and never compared
        ho = S['hoon']
        orow = ''.join(f"<li><span><b>{esc(e['name'])}</b><span class='m'>{esc(e['ins'])} · {esc(e['plan'])} · {hosp_link(e)}</span></span>"
                       f"<span class='n'>{money(e['v'])}</span></li>" for e in ho[:3])
        oon_sec = (f"<p class='cap'>Hospital files also list {len(ho):,} out-of-network {'amount' if len(ho) == 1 else 'amounts'} for this code "
                   f"({money(ho[0]['v'])}{'' if len(ho) == 1 else ' to ' + money(ho[-1]['v'])}). These are what a hospital lists for an insurer "
                   f"it has no contract with, so they aren't in-network rates and aren't counted above. The lowest:</p><ol class='word'>{orow}</ol>")
    if S['hrate'] or oon_sec:
        hr = S['hrate']
        rows = ''.join(f"<li><span><b>{esc(e['name'])}</b><span class='m'>{esc(e['ins'])} · {esc(e['plan'])} · {hosp_link(e)}</span></span>"
                       f"<span class='n'>{money(e['v'])}</span></li>" for e in hr[:5])
        hlede = (f"{S['hfiles']:,} hospital {'file lists' if S['hfiles'] == 1 else 'files list'} this code; their insurer rates run from "
                f"{money(hr[0]['v'])} to {money(hr[-1]['v'])} (hospital fee only, one line per hospital per insurer). The five lowest:</p>"
                f"<ol class='word'>{rows}</ol>") if hr else "None of the hospital files that list this code give an in-network insurer rate for it.</p>"
        hosp_sec = (f"<section class='sec' id='hospital-files'><p class='kicker'>In hospitals' own price files</p><h2>What hospitals say they're paid</h2>"
                    f"<p class='lede'>Hospitals must post every rate they've agreed with each insurer. {hlede}{oon_sec}</section>")

    inc, exc = includes(S)
    doc_line = ''
    if S['doc_lo'] and S['doc_n'] > 1:
        doc_line = (f"<p class='cap'>The doctor's own fee for this code, billed separately: {money(S['doc_lo']['v'])} to {money(S['doc_hi']['v'])} "
                    f"across {S['doc_n']:,} published rates.</p>")
    inc_sec = (f"<section class='sec'><p class='kicker'>Read the fine print</p><h2>What the price covers</h2><div class='doors'>"
               f"<div class='door'><h3>Included</h3><span>{esc(inc)}</span></div><div class='door'><h3>Not included</h3><span>{esc(exc)}{doc_line}</span></div>"
               f"<div class='door'><h3>What you'd pay</h3><span>A negotiated rate is the total the insurer and provider agreed. Until you meet your "
               f"deductible you may owe all of it; after that, your coinsurance or copay.{' The <a href=/plan-calculator/>Plan Calculator</a> does the math for DFW marketplace plans.' if area['slug'] == 'dfw' else ''}</span></div>"
               f"</div></section>")

    ml = med_lines(S, med_area)
    an = S.get('anes')
    if not ml and an:
        alk = '<a class="src" href="https://www.cms.gov/files/zip/2026-anesthesia-conversion-factors.zip" target="_blank" rel="noopener">CMS 2026 anesthesia conversion factors ↗</a>'
        ml = [('Anesthesia, per unit', an['where'] + ('' if an['lo'] == an['hi'] else f", up to {money(an['hi'])} in another part of this area"), an['lo'], alk),
              (f"Anesthesia base amount ({an['bu']} base units)", 'before time units; Medicare adds 1 unit per 15 minutes', an['lo'] * an['bu'], alk)]
    med_sec = (f"<section class='sec' id='medicare'><p class='kicker'>For comparison</p><h2>What Medicare pays</h2><p class='lede'>From CMS's 2026 fee schedules. "
               f"A reference point only: you can't buy care at Medicare rates unless you're on Medicare. Facility fees are shown at the national rate so places compare on the same footing; next to each hospital and surgery center "
               f"we also show Medicare's rate for that place after its local adjustment. <a href='/methodology/#medicare-local'>How Medicare adjusts for local costs</a>.</p><ol class='word'>"
               + ''.join(f"<li><span><b>{esc(a)}</b><span class='m'>{esc(b)} · {lk}</span></span><span class='n'>{money(v)}</span></li>" for a, b, v, lk in ml)
               + "</ol>" + local_range(S) + "</section>") if ml else ''

    chips = lambda items: ''.join(f"<a class='chip' href='{esc(u)}'>{esc(t)}</a>" for t, u in items)
    links_sec = (f"<section class='sec split'><div><p class='kicker'>Same procedure, other places</p><div class='chips'>{chips(others)}</div></div>"
                 f"<div><p class='kicker'>Related in {esc(area['name'])}</p><div class='chips'>{chips(related)}</div></div></section>")

    files = sorted({(e['ins'], e['date'], e['url']) for n in S['networks'] for e in (n['lo'], n['hi'])})
    src_sec = (f"<section class='sec srcs'><p class='kicker'>Sources</p><p>Insurer rates: the in-network rate files insurers must publish under the "
               f"Transparency in Coverage rule (45 CFR 147.212). Hospital prices: each hospital's standard charges file (45 CFR 180). "
               f"Medicare: CMS fee schedules. Cash prices posted by providers are checked by hand on the date shown. "
               f"This page was built {TODAY.isoformat()} from the same files as the <a href='{esc(finder)}'>{esc(area['name'])} Price Finder</a>. "
               f"<a href='/methodology/'>How we read the files</a>.</p>"
               + (f"<details><summary>Insurer files behind the figures on this page ({len(files)})</summary><ul>"
                  + ''.join(f"<li>{esc(i)} · {src_link({'url': u, 'date': d})}</li>" for i, d, u in files) + "</ul></details>" if files else '')
               + "</section>")

    stand = stands_out(S, area)
    stand_sec = (f"<section class='sec'><p class='kicker'>What stands out</p><h2>{esc(cm)} prices in {esc(area['name'])}, in brief</h2>"
                 f"<ul class='facts'>{''.join(f'<li>{x}</li>' for x in stand)}</ul>"
                 + (f"<p class='cap'>Providers on this page are in {esc(', '.join(S['cities'][:12]))}{' and more' if len(S['cities']) > 12 else ''}.</p>" if S.get('cities') else '')
                 + "</section>") if stand else ''
    qa = faq(S, area, med_area)
    faq_sec = (f"<section class='sec faq'><p class='kicker'>Questions people ask</p><h2>{esc(cm)} cost: the short answers</h2>"
               + ''.join(f"<h3>{esc(q)}</h3><p>{esc(a)}</p>" for q, a in qa) + "</section>")
    byline = (f"<p class='byline'>By <a href='/about/'>{esc(AUTHOR['name'])}</a>, {esc(AUTHOR['role'])} · Updated {updated()} · "
              f"<a href='/methodology/'>How we read the files</a></p>")
    aka = aliases(S)
    body = f'''<main>
<section class="board"><div class="wrap">
<p class="eyebrow"><a href="/costs/{au}/">{esc(area['name'])}</a> · {esc(S['type'] or 'Code')} {esc(S['code'])} · {esc(S['name'])}</p>
<h1 class="letters">{esc(cm)} cost<em> in {esc(area['name'])}</em></h1>
<p class="lead">{esc(desc)}</p>
{byline}
{f'<p class="aka">Also called: {esc(", ".join(aka))}.</p>' if aka else ''}
{('<div class="tiles">' + ''.join(tiles) + '</div>') if tiles else ''}
{allin_html(S)}
<p class="ctas"><a class="btn hi" href="{esc(finder)}">See every {esc(sg)} in the Price Finder</a>{f' <a class="btn ghost" href="/plan-calculator/?code={esc(S["code"])}">What would I owe?</a>' if area['slug'] == 'dfw' else ''}{f' <a class="btn ghost" href="{esc(csv_url)}" download>Download the data (CSV)</a>' if csv_url else ''}</p>
</div></section>
<div class="wrap">
{jump_html(S, cash_sec, hosp_sec, med_sec)}
{visuals.sections(S, sg, pf_path, mh, cc)}
{stand_sec}
{net_sec}
{('<section class="sec split">' + low_sec + high_sec + '</section>') if (low_sec or high_sec) else ''}
{('<section class="sec split">' + cash_sec + '<div></div></section>') if cash_sec else ''}
{hosp_sec}
{inc_sec}
{med_sec}
{faq_sec}
{links_sec}
{src_sec}
</div>{visuals.JS}
</main>'''
    jsonld = {'@context': 'https://schema.org', '@graph': [
        {'@type': 'MedicalWebPage', 'name': title, 'description': desc, 'url': SITE['origin'] + url, 'dateModified': TODAY.isoformat(),
         'inLanguage': 'en-US', 'about': {'@type': 'MedicalProcedure', 'name': S['name'],
                                         'code': {'@type': 'MedicalCode', 'code': S['code'], 'codingSystem': 'MS-DRG' if S['drg'] else (S['type'] or 'CPT')}},
         'spatialCoverage': {'@type': 'Place', 'name': area['name']}, 'isBasedOn': [u for _, _, u in files][:20],
         'author': {'@type': 'Person', 'name': AUTHOR['name'], 'jobTitle': AUTHOR['role'], 'url': SITE['origin'] + '/about/'},
         'lastReviewed': TODAY.isoformat(), 'alternateName': [cm] + aka},
        {'@type': 'FAQPage', 'mainEntity': [{'@type': 'Question', 'name': q, 'acceptedAnswer': {'@type': 'Answer', 'text': a}} for q, a in qa]},
        {'@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'name': 'Prices', 'item': SITE['origin'] + '/costs/'},
            {'@type': 'ListItem', 'position': 2, 'name': area['name'], 'item': SITE['origin'] + f'/costs/{au}/'},
            {'@type': 'ListItem', 'position': 3, 'name': cm, 'item': SITE['origin'] + url}]}]}
    if csv_url:
        jsonld['@graph'].append({'@type': 'Dataset', 'name': f'{cm} prices in {area["name"]}', 'description': desc, 'url': SITE['origin'] + url,
                                 'creator': {'@type': 'Person', 'name': AUTHOR['name']}, 'dateModified': TODAY.isoformat(),
                                 'license': SITE['origin'] + '/terms/', 'isAccessibleForFree': True,
                                 'distribution': [{'@type': 'DataDownload', 'encodingFormat': 'text/csv', 'contentUrl': SITE['origin'] + csv_url}]})
    return shell(title, desc, url, body, jsonld, noindex=not S.get('index', S['any']))


def range_cell(s):
    if not s or not s['lo']:
        return "<td class='num m' colspan='2'>none published yet</td>"
    if s['lines'] == 1:
        return f"<td class='num' data-l='Lowest'>{money(s['lo']['v'])}</td><td class='num' data-l='Highest'>—</td>"
    return f"<td class='num' data-l='Lowest'>{money(s['lo']['v'])}</td><td class='num' data-l='Highest'>{money(s['hi']['v'])}</td>"


def area_index(area, rows, au, pf_path, more=0):
    """rows: [(slug, summary)] in display order."""
    cats = {}
    for ps, s in rows:
        cats.setdefault(s['cat'], []).append((ps, s))
    tbl = ''.join(
        f"<h3 class='h3 cat'>{esc(c)}</h3><div class='tbl'><table class='rates stack'><thead><tr><th>Procedure</th><th>What's compared</th>"
        f"<th class='num'>Providers</th><th class='num'>Lowest</th><th class='num'>Highest</th></tr></thead><tbody>"
        + ''.join(f"<tr><td><a href='/costs/{au}/{ps}/'><b>{esc(s['name'])}</b></a><div class='m'>{esc(s['code'])}</div></td>"
                  f"<td class='m' data-l='Compared'>{esc(fee_words(s)[0]) if s['lo'] else ''}</td><td class='num' data-l='Providers'>{s['providers']:,}</td>{range_cell(s)}</tr>" for ps, s in items)
        + "</tbody></table></div>" for c, items in cats.items())
    n = sum(1 for _, s in rows if s['lo'])
    title = f"Healthcare Prices in {area['name']} ({TODAY.year}): {len(rows)} Procedures Compared | {SITE['name']}"
    cityc = {}
    for _, s_ in rows:
        for i, c_ in enumerate(s_.get('cities') or []):
            cityc[c_] = cityc.get(c_, 0) + (15 - i)
    cities = [c_ for c_, _ in sorted(cityc.items(), key=lambda x: -x[1])][:20]
    desc = (f"What insurers pay hospitals, surgery centers and doctors in {area['name']} for {len(rows)} common procedures, from the price "
            f"files they must publish. Lowest and highest published rate for each, linked to the source.")
    body = f'''<main><section class="board"><div class="wrap"><p class="eyebrow"><a href="/costs/">All places</a> · {esc(area_group(area))}</p>
<h1 class="letters">What care costs<em> in {esc(area['name'])}</em></h1><p class="lead">{esc(desc)} {n} of {len(rows)} have at least one published insurer rate here so far.</p>
<p class="byline">By <a href="/about/">{esc(AUTHOR['name'])}</a>, {esc(AUTHOR['role'])} · Updated {updated()}</p>
{f'<p class="aka">Includes providers in {esc(", ".join(cities))}{" and more" if len(cities) == 20 else ""}.</p>' if len(cities) > 1 else ''}
<p class="ctas">{f'<a class="btn hi" href="/costs/{au}/all/">See all {more + len(rows):,} procedures priced here</a> ' if more else ''}<a class="btn{'' if more else ' hi'}" href="{esc(pf_path)}">Search every billing code in the Price Finder</a></p></div></section>
<div class="wrap"><section class="sec">{tbl}<p class="cap">Lowest and highest are real published figures, not averages: one line per provider per network, that provider's standard rate. Open a procedure for the sources.</p></section></div></main>'''
    jsonld = {'@context': 'https://schema.org', '@type': 'CollectionPage', 'name': title, 'description': desc,
              'url': SITE['origin'] + f'/costs/{au}/', 'dateModified': TODAY.isoformat()}
    return shell(title, desc, f'/costs/{au}/', body, jsonld)


def national_procedure(code, name, ps, per_area):
    """per_area: [(area, au, summary)]."""
    have = [(a, au, s) for a, au, s in per_area if s and s['lo']]
    kinds = [s['kind'] for _, _, s in have]
    kind = max(set(kinds), key=kinds.count) if kinds else 'fac'
    same = [(a, au, s) for a, au, s in have if s['kind'] == kind]
    S0 = same[0][2] if same else (per_area[0][2] if per_area else None)
    pl = fee_words(S0)[0] if S0 else 'rates'
    lead = f"We haven't found a published rate for {name} yet."
    if same:
        lo = min(same, key=lambda x: x[2]['lo']['v'])
        hi = max(same, key=lambda x: x[2]['hi']['v'])
        lead = (f"Across the {len(same)} places we've read, insurers' negotiated {pl} for {lower_name(S0)} run from "
                f"{money(lo[2]['lo']['v'])} ({lo[2]['lo']['name']}, {lo[0]['name']}) to {money(hi[2]['hi']['v'])} "
                f"({hi[2]['hi']['name']}, {hi[0]['name']}).")
    ext = ''
    if same:
        ext = (f" Lowest: {src_link(lo[2]['lo'])} ({esc(netname(lo[2]['lo']['ins'], lo[2]['lo']['plan']))}). "
               f"Highest: {src_link(hi[2]['hi'])} ({esc(netname(hi[2]['hi']['ins'], hi[2]['hi']['plan']))}).")
    rows = ''.join(f"<tr><td><a href='/costs/{au}/{ps}/'><b>{esc(a['name'])}</b></a><div class='m'>{esc(area_group(a))}</div></td>"
                   f"<td class='num' data-l='Providers'>{(s['providers'] if s else 0):,}</td>{range_cell(s)}</tr>"
                   for a, au, s in sorted(per_area, key=lambda x: (not (x[2] and x[2]['lo']), x[2]['lo']['v'] if x[2] and x[2]['lo'] else 0)))
    cm = SEARCH_NAMES.get(code, (name,))[0]
    title = f"{cm} Cost by City and State ({TODAY.year}): {len(same) or len(per_area)} Places Compared | {SITE['name']}"
    body = f'''<main><section class="board"><div class="wrap"><p class="eyebrow"><a href="/costs/">All procedures</a> · {esc(S0['type'] if S0 else '')} {esc(code)}</p>
<h1 class="letters">{esc(cm)} cost<em> place by place</em></h1><p class="lead">{esc(lead)}</p></div></section>
<div class="wrap"><section class="sec"><div class="tbl"><table class="rates stack"><thead><tr><th>Place</th><th class="num">Providers</th><th class="num">Lowest</th><th class="num">Highest</th></tr></thead>
<tbody>{rows}</tbody></table></div><p class="cap">Each place's figures are its own {esc(pl)}, lowest to highest, from the insurer files read for that place. Rates a file marks as unusually low, or under 20% of the typical rate for this code, are left out. Open a place for every source.{ext}</p></section></div></main>'''
    jsonld = {'@context': 'https://schema.org', '@type': 'MedicalWebPage', 'name': title, 'description': lead,
              'url': SITE['origin'] + f'/costs/{ps}/', 'dateModified': TODAY.isoformat(),
              'about': {'@type': 'MedicalProcedure', 'name': name, 'code': {'@type': 'MedicalCode', 'code': code}}}
    return shell(title, lead, f'/costs/{ps}/', body, jsonld, noindex=not same)


def costs_index(areas, procs):
    """areas: [(area, au, n_with_price)], procs: [(code, name, cat, ps)]."""
    # One chip per state; a state with several places (Texas) opens to its places only when tapped (Betsy, Oct 19).
    # Plain <details>, so it works without script and the links stay in the page for search engines.
    by_st = {}
    for a, au, n in areas:
        by_st.setdefault(page_state(a), []).append((a, au))
    st_name = {}
    for a, au, n in areas:
        st_name.setdefault(page_state(a), STATE_NAME.get(page_state(a), a['name']))
    chips = []
    for st, items in sorted(by_st.items(), key=lambda x: (x[0] != 'TX', st_name[x[0]])):   # Texas first
        items.sort(key=lambda x: x[0]['name'])
        if len(items) == 1:
            chips.append(f"<a class='chip' href='/costs/{items[0][1]}/'>{esc(st_name[st])}</a>")
        else:
            chips.append(f"<details class='st'><summary class='chip'>{esc(st_name[st])} <span class='m'>· {len(items)} places</span></summary>"
                         "<div class='chips'>" + ''.join(f"<a class='chip' href='/costs/{au}/'>{esc(a['name'])}</a>" for a, au in items) + "</div></details>")
    places = "<div class='chips states'>" + ''.join(chips) + "</div>"
    cats = {}
    for code, name, cat, ps in procs:
        cats.setdefault(cat, []).append((code, name, ps))
    plist = ''.join(f"<h3 class='h3 cat'>{esc(c)}</h3><div class='chips'>" + ''.join(
        f"<a class='chip' href='/costs/{ps}/'>{esc(n)}</a>" for code, n, ps in items) + "</div>" for c, items in cats.items())
    title = f"Healthcare Prices by Place and Procedure ({TODAY.year}) | {SITE['name']}"
    desc = (f"{len(procs)} common procedures in {len(areas)} places, with every billing code in the Price Finder, from the price files hospitals and insurers are required to publish. "
            f"Every price links to its source.")
    body = f'''<main><section class="board"><div class="wrap"><p class="eyebrow">Prices</p><h1 class="letters">Look up<em> a price</em></h1>
<p class="lead">{esc(desc)}</p></div></section><div class="wrap"><section class="sec"><p class="kicker">Pick a place</p><h2>{len(areas)} places</h2>{places}</section>
<section class="sec"><p class="kicker">Or a procedure</p><h2>{len(procs)} common procedures</h2><p class="lede">Looking for something else? The <a href="/price-finder/">Price Finder</a> searches every billing code in the files.</p>{plist}</section></div></main>'''
    return shell(title, desc, '/costs/', body, {'@context': 'https://schema.org', '@type': 'CollectionPage', 'name': title,
                                                'url': SITE['origin'] + '/costs/', 'dateModified': TODAY.isoformat()})


CSS = r'''/* The Healthcare Files: shared stylesheet for /costs/ pages (written by price-files-bot site/procedure_pages.py). Same tokens as the homepage. */
:root{--board:#4A1C1C;--board-line:#552424;--board-ink:#F7EBD3;--board-sub:#E3CDB0;--paper:#FBF6EA;--ground:#E9DCBC;--ink:#2A1A12;--muted:#5E4B3A;--rule:#2A1A12;
--stamp:#1F4FBF;--hi:#FFE27A;--display:"Big Shoulders Display","Arial Narrow",Impact,sans-serif;--body:"Public Sans","Helvetica Neue",Arial,sans-serif;--mono:"Courier Prime","Courier New",monospace;color-scheme:light}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font:17px/1.55 var(--body)}a{color:var(--ink)}
:focus-visible{outline:3px solid var(--stamp);outline-offset:2px}.wrap{max-width:1120px;margin:0 auto;padding-inline:16px}@media(min-width:700px){.wrap{padding-inline:28px}}
.bar{position:sticky;top:0;z-index:20;background:var(--board);color:var(--board-ink);border-bottom:3px solid var(--stamp)}
.bar .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:64px;position:relative}
.logo{font:900 30px/1 var(--display);letter-spacing:.02em;text-transform:uppercase;color:var(--board-ink);text-decoration:none;white-space:nowrap}.logo span{color:var(--hi)}
.nav{display:flex;align-items:center;gap:4px;flex-wrap:wrap}.nav a{color:var(--board-ink);text-decoration:none;padding:10px 10px;font-weight:600;font-size:15px;white-space:nowrap}.nav a:hover{color:var(--hi)}
.start-pill{margin-left:auto;display:inline-flex;align-items:center;min-height:44px;padding:0 14px;background:var(--hi);color:var(--ink)!important;font:800 15px var(--body);text-decoration:none;white-space:nowrap}.start-pill:hover{filter:brightness(.93)}
@media(max-width:420px){.logo{font-size:24px}.start-pill{padding:0 10px;font-size:14px}.bar .wrap{gap:10px}}
@media(min-width:1601px){.start-pill{display:none}}
.menu-btn{display:none;background:none;border:2px solid var(--board-ink);color:var(--board-ink);min-height:44px;padding:0 14px;font:700 15px var(--body);cursor:pointer}
@media(max-width:1600px){.menu-btn{display:inline-block}.nav{display:none;position:absolute;left:0;right:0;top:100%;background:var(--board);flex-direction:column;align-items:stretch;padding:8px 16px 16px;border-bottom:3px solid var(--stamp)}.nav.open{display:flex}.nav a{padding:14px 4px;border-bottom:1px solid #6b3a3a}}
.board{background:var(--board);color:var(--board-ink);background-image:repeating-linear-gradient(var(--board) 0 26px,var(--board-line) 26px 28px)}
.board .wrap{padding-block:44px 52px;display:grid;gap:22px}.board a{color:var(--hi)}
.eyebrow{margin:0;font:700 14px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--hi)}
.letters{font:900 clamp(40px,7vw,88px)/.95 var(--display);text-transform:uppercase;letter-spacing:.03em;margin:0;text-wrap:balance}.letters em{font-style:normal;color:var(--hi)}
.lead{margin:0;max-width:44em;font-size:19px;color:var(--board-ink)}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.tile{background:var(--paper);color:var(--ink);padding:16px 16px 14px;border-top:6px solid var(--ink);min-width:0}
.tile .tl{margin:0;font:700 12.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.tile .tv{margin:4px 0 6px;font:700 30px/1.1 var(--mono);font-variant-numeric:tabular-nums}
.tile .ts{font-size:14px;line-height:1.45;color:var(--muted);overflow-wrap:anywhere}.tile a{color:var(--stamp)}
.ctas{margin:0;display:flex;flex-wrap:wrap;gap:10px}
.btn{display:inline-flex;align-items:center;min-height:48px;padding:12px 18px;font-weight:800;text-transform:uppercase;letter-spacing:.04em;text-decoration:none;font-size:15px}
.btn.hi{background:var(--hi);color:var(--ink)!important}.btn.ghost{border:2px solid var(--board-ink);color:var(--board-ink)!important}.btn:hover{filter:brightness(.93)}
.sec{padding-block:44px}.sec+.sec{border-top:2px solid var(--rule)}
.kicker{font:700 13px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--stamp);margin:0 0 8px}
.allin{margin:18px 0 0;padding:12px 16px;background:var(--paper);color:var(--ink);border-left:6px solid var(--hi);max-width:62em;font-size:16px}.allin a{color:var(--stamp)}
.jump{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:18px 0 0;padding:10px 0;border-bottom:1px solid var(--rule);font:600 15px var(--body)}.jump span{color:var(--muted);font-weight:500}
.jump a{color:var(--ink);border:1px solid var(--ink);padding:6px 10px;text-decoration:none;min-height:36px;display:inline-flex;align-items:center}.jump a:hover{background:var(--hi)}
td.tot{font-size:17px}
h2{font:900 clamp(32px,5vw,52px)/1 var(--display);text-transform:uppercase;letter-spacing:.02em;margin:0 0 12px;text-wrap:balance}
h3,.h3{font:800 20px/1.25 var(--body);margin:0 0 8px}.h3.cat{margin:26px 0 10px}
.lede{max-width:44em;margin:0 0 18px;color:var(--muted);font-size:17px}.cap{font-size:14px;color:var(--muted);margin:10px 0 0;max-width:52em}
.split{display:grid;gap:40px;align-items:start}@media(min-width:900px){.split{grid-template-columns:1fr 1fr}}.split>*{min-width:0}
.tbl{overflow-x:auto;background:var(--paper);border:2px solid var(--rule)}
table.rates{border-collapse:collapse;width:100%;min-width:560px;font-variant-numeric:tabular-nums}
.rates th,.rates td{padding:12px 14px;text-align:left;border-bottom:1px solid #CFC6B0;vertical-align:top}
.rates th{font:700 12.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;border-bottom:2px solid var(--rule)}
.rates .num{text-align:right;font-family:var(--mono);font-weight:700;white-space:nowrap}.rates .num .m{font-family:var(--body);font-weight:400;white-space:normal}
.m{font-size:13.5px;color:var(--muted);font-weight:400;line-height:1.4}td.m{font-size:14px}
.word{list-style:none;margin:0;padding:0}.word li{display:grid;grid-template-columns:1fr auto;gap:4px 20px;align-items:baseline;padding:12px 0;border-top:1px solid var(--rule)}
.word li:last-child{border-bottom:1px solid var(--rule)}.word li>span:first-child{min-width:0}.word .m{display:block;overflow-wrap:anywhere}
.word .n{font:700 19px var(--mono);white-space:nowrap;font-variant-numeric:tabular-nums}
.src{font:13px var(--mono);color:var(--stamp);white-space:nowrap}.flag{font:700 11.5px var(--mono);text-transform:uppercase;letter-spacing:.05em;background:var(--hi);color:var(--ink);padding:1px 5px;white-space:nowrap}
.doors{display:grid;border:2px solid var(--rule)}@media(min-width:800px){.doors{grid-template-columns:repeat(3,1fr)}}
.door{display:flex;flex-direction:column;gap:8px;padding:22px;background:var(--paper);border-bottom:2px solid var(--rule)}.door:last-child{border-bottom:0}
@media(min-width:800px){.door{border-bottom:0;border-right:2px solid var(--rule)}.door:last-child{border-right:0}}.door .cap{margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:8px}.st{display:inline-block}.st summary{cursor:pointer;list-style:none;font-weight:700}.st summary::-webkit-details-marker{display:none}.st summary::after{content:' \25B8';margin-left:6px}.st[open] summary::after{content:' \25BE'}.st[open]{display:block;flex-basis:100%}.st[open]>.chips{margin:10px 0 6px;padding:12px;border-left:3px solid var(--ink);background:var(--ground)}.chip{display:inline-flex;align-items:center;min-height:40px;padding:6px 12px;border:1px solid var(--ink);background:var(--paper);text-decoration:none;font-size:15px}
.chip:hover{background:var(--hi)}
.srcs p{max-width:52em;color:var(--muted);font-size:15px}.srcs details{margin-top:12px;font-size:15px}.srcs summary{cursor:pointer;font-weight:700;min-height:44px;display:flex;align-items:center}
.srcs ul{padding-left:18px;margin:6px 0 0}.srcs li{margin:6px 0}
@media(max-width:640px){table.stack{min-width:0}table.stack thead{display:none}table.stack tr{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;padding:12px 14px;border-bottom:1px solid #CFC6B0}
table.stack td{padding:0;border:0;text-align:left}table.stack td:first-child{grid-column:1/-1}table.stack .num{text-align:left;white-space:normal}
table.stack td[data-l]::before{content:attr(data-l);display:block;font:700 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}table.stack td[colspan]{grid-column:1/-1}}
footer{background:var(--board);color:#D8C7AE}
.byline{margin:0;font:14px/1.5 var(--mono);color:var(--board-sub)}.aka{margin:0;font-size:15px;color:var(--board-sub)}
.facts{margin:0;padding:0 0 0 20px;max-width:52em;display:grid;gap:10px;font-size:17px}.facts li::marker{color:var(--stamp)}
.save{margin:0 0 12px;padding:12px 14px;background:var(--paper);border-left:6px solid var(--stamp);font-size:16px}
.faq h3{margin:26px 0 6px;font-size:19px}.faq p{margin:0;max-width:50em;color:var(--muted)}footer .wrap{padding-block:30px 40px;display:flex;flex-wrap:wrap;justify-content:space-between;gap:14px;font-size:14px}
footer b{font:900 22px var(--display);text-transform:uppercase;color:var(--board-ink);letter-spacing:.03em}footer a{color:var(--board-ink)}
''' + visuals.CSS


# ---------------------------------------------------------------- the national section of the homepage
TILEMAP = [  # (state, col, row): a standard US tile-grid map
    ('AK', 0, 0), ('ME', 11, 0), ('VT', 10, 1), ('NH', 11, 1), ('WA', 1, 2), ('ID', 2, 2), ('MT', 3, 2), ('ND', 4, 2), ('MN', 5, 2), ('IL', 6, 2),
    ('WI', 7, 2), ('MI', 8, 2), ('NY', 9, 2), ('MA', 10, 2), ('RI', 11, 2), ('OR', 1, 3), ('NV', 2, 3), ('WY', 3, 3), ('SD', 4, 3), ('IA', 5, 3),
    ('IN', 6, 3), ('OH', 7, 3), ('PA', 8, 3), ('NJ', 9, 3), ('CT', 10, 3), ('CA', 1, 4), ('UT', 2, 4), ('CO', 3, 4), ('NE', 4, 4), ('MO', 5, 4),
    ('KY', 6, 4), ('WV', 7, 4), ('VA', 8, 4), ('MD', 9, 4), ('DE', 10, 4), ('AZ', 2, 5), ('NM', 3, 5), ('KS', 4, 5), ('AR', 5, 5), ('TN', 6, 5),
    ('NC', 7, 5), ('SC', 8, 5), ('DC', 9, 5), ('OK', 4, 6), ('LA', 5, 6), ('MS', 6, 6), ('AL', 7, 6), ('GA', 8, 6), ('HI', 0, 7), ('TX', 4, 7),
    ('FL', 9, 7)]


def hospital_counts():
    path = os.path.join(REPO, 'data', 'hospitals.csv')
    tot, found = {}, {}
    if not os.path.isfile(path):
        return tot, found
    for r in csv.DictReader(open(path, encoding='utf-8')):
        s = r['state']
        tot[s] = tot.get(s, 0) + 1
        if r.get('mrf_url'):
            found[s] = found.get(s, 0) + 1
    return tot, found


NCODES = [0]   # most billing codes on any area's Price Finder page (filled while reading pages)


def every_codes():
    n = NCODES[0]
    return f"{n // 1000 * 1000:,}+" if n >= 2000 else (f"{n:,}" if n else 'every')


def national_section(areas, summaries, procs, n_rates_note):
    """The homepage's front-page section. areas: [(area, au)]; summaries: {(slug, code): summary}."""
    tot, found = hospital_counts()
    live_states = {page_state(a) for a, _ in areas}
    by_st = {}
    for a, au in areas:
        by_st.setdefault(page_state(a), []).append((a, au))
    # one place: its page; several (Texas, California, states with regions): that state's list of places below the map
    state_link = {st: (f'/costs/{v[0][1]}/' if len(v) == 1 else f'#{st.lower()}-places') for st, v in by_st.items()}
    W, Hh = 12, 8
    cells = []
    for st, c, r in TILEMAP:
        t, f_ = tot.get(st, 0), found.get(st, 0)
        share = f_ / t if t else 0
        band = 0 if not t else (1 if share < .5 else 2 if share < .7 else 3)
        live = st in live_states
        label = f"{st}: price files found for {f_} of {t} hospitals" + ('; Price Finder live' if live else '')
        inner = (f"<rect x='{c * 50 + 2}' y='{r * 50 + 2}' width='46' height='46' class='b{band}{' live' if live else ''}'/>"
                 f"<text x='{c * 50 + 25}' y='{r * 50 + 24}' class='st'>{st}</text>"
                 f"<text x='{c * 50 + 25}' y='{r * 50 + 39}' class='pc'>{round(share * 100) if t else '–'}{'%' if t else ''}</text>")
        cells.append(f"<a href='{state_link[st]}' aria-label='{esc(label)}'><title>{esc(label)}</title>{inner}</a>" if st in state_link
                     else f"<g role='img' aria-label='{esc(label)}'><title>{esc(label)}</title>{inner}</g>")
    T, F = sum(tot.values()), sum(found.values())
    tx = by_st.get('TX', [])
    st_areas = [(a, au) for a, au in areas if page_state(a) != 'TX']
    multi = sorted((st for st, v in by_st.items() if st != 'TX' and len(v) > 1), key=lambda x: STATE_NAME.get(x, x))
    one = [(a, au) for a, au in st_areas if page_state(a) not in multi]

    # the biggest spread among featured procedures, from the data (changes with every build)
    best = None
    for (slug, code), s in summaries.items():
        if s['lo'] and s['lines'] >= 20 and s['kind'] == 'fac' and s['lo']['v'] >= 50:
            x = s['hi']['v'] / s['lo']['v']
            if x <= 20 and code in ('45378', '73721', '27447', '66984', '74177', '29881', '47562', '43239', '70551', '72148') and (best is None or x > best[0]):
                best = (x, slug, s)
    ex = ''
    if best:
        x, slug, s = best
        a = next(a for a, _ in areas if a['slug'] == slug)
        au = area_url_slug(slug)
        ps = next(p for c, _, _, p in procs if c == s['code'])
        ex = (f"<div class='receiptish'><p class='kicker'>From the files</p><h2>{esc(s['name'])} in {esc(a['name'])}: "
              f"{money(s['lo']['v'])} to {money(s['hi']['v'])}</h2><p class='lede'>Same procedure, same area, different negotiated facility fees. "
              f"The lowest is {esc(s['lo']['name'])} ({esc(s['lo']['ins'])}); the highest is {esc(s['hi']['name'])} ({esc(s['hi']['ins'])}), "
              f"{x:.1f} times as much. Both straight from the insurers' own files.</p>"
              f"<p><a class='btn' href='/costs/{au}/{ps}/'>See the receipts</a></p></div>")

    opts_proc = ''.join(f"<option value='{esc(ps)}'{' selected' if c == '45378' else ''}>{esc(n)}</option>" for c, n, cat, ps in sorted(procs, key=lambda p: p[1]))
    opts_area = (f"<optgroup label='Texas'>" + ''.join(f"<option value='{au}'{' selected' if a['slug'] == 'dfw' else ''}>{esc(a['name'])}</option>" for a, au in sorted(tx, key=lambda x: x[0]['name']))
                 + "</optgroup><optgroup label='States'>" + ''.join(f"<option value='{au}'>{esc(a['name'])}</option>" for a, au in sorted(st_areas, key=lambda x: x[0]['name']))
                 + "</optgroup>")
    chips = lambda items: ''.join(f"<a class='chip-l' href='/costs/{au}/'>{esc(a['name'])}</a>" for a, au in sorted(items, key=lambda x: x[0]['name']))
    pop = [('45378', 'a colonoscopy'), ('73721', 'a knee MRI'), ('27447', 'a knee replacement'), ('59400', 'having a baby'),
           ('66984', 'cataract surgery'), ('80053', 'a blood panel')]
    pslug = {c: p for c, _, _, p in procs}
    return f'''<!-- NATIONAL:START (written by price-files-bot site/procedure_pages.py; edits here are replaced on the next build) -->
<style>
.board .wrap{{grid-template-columns:minmax(0,1fr)}}.nat-search{{display:grid;gap:10px;max-width:760px;min-width:0}}.nat-search label{{font:700 14px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--hi)}}
.nat-search .row{{display:flex;flex-wrap:wrap;gap:10px}}.nat-search select{{flex:1 1 240px;min-width:0;width:100%;text-overflow:ellipsis;background:var(--paper);color:var(--ink);border:0;padding:14px 12px;font-size:18px;min-height:52px}}
.nat-search button{{background:var(--hi);color:var(--ink);border:0;padding:14px 20px;font-weight:800;min-height:52px;cursor:pointer;text-transform:uppercase;letter-spacing:.04em}}
.nat-pop{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:var(--board-sub);font-size:15px}}.nat-pop a{{color:var(--board-ink);border:1px solid #8a6a5a;padding:7px 11px;text-decoration:none;min-height:40px;display:inline-flex;align-items:center}}.nat-pop a:hover{{border-color:var(--hi);color:var(--hi)}}
.nat-map svg{{width:100%;max-width:680px;height:auto;display:block}}.nat-map rect{{stroke:var(--paper);stroke-width:2}}
.nat-map .b0{{fill:#D9CDB0}}.nat-map .b1{{fill:#B9A27E}}.nat-map .b2{{fill:#8A6A4E}}.nat-map .b3{{fill:#4A1C1C}}.nat-map rect.live{{stroke:var(--stamp);stroke-width:5}}
.nat-map text{{text-anchor:middle;pointer-events:none}}.nat-map .st{{font:700 14px var(--mono)}}.nat-map .pc{{font:11px var(--mono)}}
.nat-map .b0+text,.nat-map .b0+text+text,.nat-map .b1+text,.nat-map .b1+text+text{{fill:var(--ink)}}.nat-map .b2+text,.nat-map .b2+text+text,.nat-map .b3+text,.nat-map .b3+text+text{{fill:#FBF6EA}}
.nat-map a:hover rect{{filter:brightness(1.15)}}.nat-key{{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:14px;color:var(--muted);margin-top:10px}}.nat-key i{{display:inline-block;width:14px;height:14px;vertical-align:-2px;margin-right:6px}}
.chip-l{{display:inline-flex;align-items:center;min-height:40px;padding:6px 12px;border:1px solid var(--ink);background:var(--paper);text-decoration:none;font-size:15px;margin:0 8px 8px 0}}.chip-l:hover{{background:var(--hi)}}
.nat-new{{margin:4px 0 0;font:700 17px var(--body)}}.nat-new a{{color:var(--hi);text-underline-offset:4px}}
.nat-trust{{margin:0;font:14px/1.5 var(--mono);color:var(--board-sub);letter-spacing:.02em}}.nat-trust i{{font-style:normal;padding:0 6px}}
{home_bits.HOME_CSS2}.steps{{list-style:none;counter-reset:st;margin:18px 0 0;padding:0;display:grid;gap:0;border:2px solid var(--rule)}}@media(min-width:800px){{.steps{{grid-template-columns:repeat(3,1fr)}}}}
.steps li{{counter-increment:st;display:flex;flex-direction:column;gap:6px;padding:22px;background:var(--paper);border-bottom:2px solid var(--rule)}}.steps li:last-child{{border-bottom:0}}
@media(min-width:800px){{.steps li{{border-bottom:0;border-right:2px solid var(--rule)}}.steps li:last-child{{border-right:0}}}}
.steps li::before{{content:counter(st);font:900 44px/1 var(--display);color:var(--stamp)}}.steps b{{font-size:19px}}.steps span{{color:var(--muted)}}.steps a{{color:var(--stamp)}}.steps-more{{margin:18px 0 0}}
@media(max-width:1600px){{.menu-btn{{display:inline-block}}.nav{{display:none;position:absolute;left:0;right:0;top:100%;background:var(--board);flex-direction:column;align-items:stretch;padding:8px 16px 16px;border-bottom:3px solid var(--stamp)}}.nav.open{{display:flex}}.nav a{{padding:14px 4px;border-bottom:1px solid #6b3a3a}}.nav .ask{{margin-top:10px}}}}
</style>
<section data-view="home">
<div class="board"><div class="wrap">
<p class="hcf-tagline" style="font-size:18px;font-weight:600;line-height:1.4;color:var(--hi);margin:0;max-width:40em">The receipt for the U.S. healthcare system.</p>
<h1 class="letters">What is this going to <em>cost me?</em></h1>
{home_bits.LEDE}
<form class="nat-search" id="natForm" action="/costs/" method="get">
<label for="natQ">What do you need?</label>
<div class="row">{home_bits.qbox(opts_proc, every_codes())}
<select id="natArea" aria-label="Where">{opts_area}</select><button type="submit">Show me</button></div>
<p class="nat-pop">Popular: {''.join(f"<a href='/costs/{pslug[c]}/' data-slug='{pslug[c]}'>{esc(t)}</a>" for c, t in pop if c in pslug)} · <a href="/price-finder/">every billing code</a></p>
</form>
{home_bits.foot(every_codes())}
<p class="msg" style="display:none" id="msg"></p><div id="homeChips" hidden></div><form id="searchForm" hidden><input id="q"></form>
</div></div>
<div class="wrap">
<div class="sec"><p class="kicker">How it works</p><h2>Three steps to a real price</h2>
<ol class="steps">
<li><b>Pick a procedure and a place.</b><span>Start with a common one, like a colonoscopy or a knee MRI, or look up any billing code in the <a href="/price-finder/">Price Finder</a>.</span></li>
<li><b>See the real range.</b><span>The lowest and highest prices published in your area, from hospitals' and insurers' own files, plus cash prices where they're posted.</span></li>
<li><b>Check your plan.</b><span>Pick your insurer's network to see what it pays each place. On a HealthCare.gov plan in DFW? The <a href="/plan-calculator/">Plan Calculator</a> shows what you'd owe.</span></li>
</ol>
<p class="steps-more"><a class="btn" href="/start-here/">Read the 2-minute guide</a></p></div>
{('<div class="sec">' + ex + '</div>') if ex else ''}
<div class="sec"><p class="kicker">Who it's for</p><h2>Start where you are</h2><div class="doors">
<a class="door" href="/costs/"><h3>Patients</h3><span>Look up a procedure where you live, see what your insurer has agreed to pay, and find the lowest cash price.</span><span class="go">Look up a price →</span></a>
<a class="door" href="/employer-plans/"><h3>Employers and HR</h3><span>See which networks your plan uses and what those networks actually pay, provider by provider.</span><span class="go">Employer plans →</span></a>
<a class="door" href="/price-file-watch/"><h3>Reporters and lawmakers</h3><span>Who isn't publishing usable price files, with the date we checked and the rule they're under.</span><span class="go">Price File Watch →</span></a>
</div></div>
<div class="sec split">
<div><p class="kicker">Where the files are</p><h2>{F:,} of {T:,} hospitals</h2>
<p class="lede">That's how many hospital price files we've found so far, out of every hospital on Medicare's list, state by state. The rule has applied since 2021.
Outlined states have a Price Finder with insurers' negotiated rates. We're still looking for the rest, and the <a href="/price-file-watch/">Price File Watch</a> names who isn't publishing.</p>
<div class="nat-map"><svg viewBox="0 0 {W * 50} {Hh * 50}" role="group" aria-label="Map: share of hospitals whose price file we have found, by state">{''.join(cells)}</svg>
<div class="nat-key"><span><i style="background:#D9CDB0"></i>none yet</span><span><i style="background:#B9A27E"></i>under half</span><span><i style="background:#8A6A4E"></i>half to 70%</span><span><i style="background:#4A1C1C"></i>70% or more</span><span><i style="outline:3px solid var(--stamp);outline-offset:-3px;background:none"></i>Price Finder live</span></div>
<p class="cap">Source: CMS Hospital General Information list; files found by our bot, which keeps searching. "Not found yet" doesn't always mean a hospital isn't publishing.</p></div></div>
<div><p class="kicker">Pick your place</p><h2>{len(areas)} places, {every_codes()} billing codes</h2>
<p class="lede">The Price Finder searches every billing code in the files, {every_codes()} procedures, tests and services. {len(procs)} of the most common also get their own easy-read page in every place, with the lowest and highest published price and the source for each.</p>
<h3 id="tx-places">Texas</h3><div>{chips(tx)}</div>{''.join(f'<h3 id="{x.lower()}-places" style="margin-top:18px">{esc(STATE_NAME.get(x, x))}</h3><div>{chips(by_st[x])}</div>' for x in multi)}<h3 style="margin-top:18px">{'Other states' if multi else 'States'}</h3><div>{chips(one)}</div>
<p style="margin-top:14px"><a class="btn" href="/costs/">Browse every price page</a></p></div>
</div>
<div class="sec"><p class="kicker">House rules</p><h2>How we keep receipts</h2><div class="doors">
<div class="door"><h3>Every price has a source</h3><span>Each figure links to the hospital, insurer or provider file it came from.</span></div>
<div class="door"><h3>Every price has a date</h3><span>We show the date on each file and flag anything over six months old.</span></div>
<div class="door"><h3>Nothing is estimated</h3><span>No averages, no modeled prices. Lowest and highest are real published figures. <a href="/methodology/">How we read the files</a>.</span></div>
</div></div>
</div>
{home_bits.HOME_JS.replace('__EVERY__', every_codes())}
</section>
<!-- NATIONAL:END -->'''


def patch_home(site, section):
    """Put the national section on the homepage in place of the old DFW-only home view, once; later builds replace only the
    marked block. The receipts, chargemaster and house-rules views stay as they are."""
    p = os.path.join(site, 'index.html')
    s = open(p, encoding='utf-8').read()
    if '<!-- NATIONAL:START' in s:
        s = re.sub(r'<!-- NATIONAL:START.*?<!-- NATIONAL:END -->', lambda m: section, s, flags=re.S)
    else:
        m = re.search(r'<!-- HOME -->\s*<section data-view="home">.*?(?=<section data-view="receipt")', s, re.S)
        if not m:
            raise SystemExit('STOP: homepage layout changed (no <!-- HOME --> section before the receipt view); homepage not touched')
        s = s[:m.start()] + section + '\n' + s[m.end():]
    if '/costs/' not in re.search(r'<nav class="nav".*?</nav>', s, re.S).group(0):
        s = s.replace('<a href="/start-here/">Start here</a>', '<a href="/start-here/">Start here</a> <a href="/costs/">Prices</a>', 1)
    open(p, 'w', encoding='utf-8').write(s)


def about_page(site):
    """/about/: who is behind the site (written only if the site doesn't have its own About page)."""
    p = os.path.join(site, 'about', 'index.html')
    if os.path.isfile(p) and 'procedure_pages.py' not in open(p, encoding='utf-8').read():
        return
    body = f'''<main><section class="board"><div class="wrap"><p class="eyebrow">About</p><h1 class="letters">Who keeps<em> the receipts</em></h1>
<p class="lead">I'm Betsy Flickinger. I've spent years consulting across economics and healthcare, working with everyone who pays for care: executives, employers, employees and gig workers.</p></div></section><div class="wrap">
<section class="sec"><p class="kicker">Our mission</p><h2>Every price. From the source. In plain sight.</h2>
<p class="lede" style="font-size:1.35em;line-height:1.4;font-weight:600;max-width:34em">Make healthcare prices public in practice, not just on paper, so the people paying the bills can see what care costs before they buy it.</p></section>
<section class="sec"><p class="kicker">Why this exists</p><h2>The last black box in healthcare</h2>
<p class="lede">Healthcare prices are public by law but still hidden in practice. Since 2021, hospitals have had to post their prices. Since 2022, insurers have had to publish
the rates they negotiate with hospitals, doctors and clinics. But those files are enormous, inconsistent and often incomplete, so the people paying the bills
still can't see the price until it's too late.</p>
<p class="lede">The gaps are huge. An August 2026 study of four Dallas hospitals by Rice University's Baker Institute found that what an insurer pays for the same
hospital stay can differ by up to 203% from one hospital to the next
(<a href="https://www.bakerinstitute.org/research/price-transparency-yields-actionable-data-dallas-employers">Le, Ho and Ahmed, Aug. 12, 2026</a>).
Nobody can shop around a gap they can't see.</p>
<p class="lede">From the inside, I watched those negotiations stay hidden from the people paying the bills. When an industry answers a transparency rule with files
nobody can open, that tells you something. Hiding in plain sight is still hiding.</p>
<p class="lede">Until now, most people learned what care really costs by word of mouth, usually after the bill came. People deserve to know before they go.
The Healthcare Files reads the files and puts every price in plain view, with a link to the file it came from.</p>
<blockquote class="lede" style="margin:22px 0 0;padding-left:16px;border-left:3px solid currentColor;font-style:italic">There's a feed full of people narrating the fire.
I want to be the person holding the hose: looking for the receipt, not the hot take.</blockquote></section>
<section class="sec"><p class="kicker">Not your medical records</p><h2>What &ldquo;the files&rdquo; are</h2>
<p class="lede">The files are the price lists hospitals and insurers are required by law to publish: what a hospital charges, and what each insurer has agreed to pay
each hospital, doctor and clinic. Nothing here comes from anyone's medical records, and the site never asks for yours.</p></section>
<section class="sec"><p class="kicker">What we mean by transparency</p><h2>Disclosure isn't transparency</h2>
<p class="lede">A price is transparent only when a regular person can, before they get care:</p>
<ol class="lede" style="margin:10px 0 0;padding-left:1.4em;display:grid;gap:8px">
<li><b>Find it.</b> Searchable, not buried in a file their computer can't open.</li>
<li><b>Read it.</b> In plain English, for a named procedure, at a named place.</li>
<li><b>Compare it.</b> Against other providers, the cash price and Medicare.</li>
<li><b>Trust it.</b> Linked to its source and dated.</li>
<li><b>Know what it covers.</b> What's included, what's billed separately, what it leaves out.</li></ol>
<p class="lede" style="margin-top:16px">If any of these is missing, the price is disclosed, not transparent. A file that is technically compliant but impossible to use is a choice,
not an accident. We track those choices on the <a href="/price-file-watch/">Price File Watch</a>.</p></section>
<section class="sec"><p class="kicker">What we stand for</p><h2>Our values</h2><div class="doors" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
<div class="door"><h3>The people paying deserve to see the price</h3><span>Show the receipt, in plain English.</span></div>
<div class="door"><h3>Independence</h3><span>No one buys a better spot or a better story.</span></div>
<div class="door"><h3>Accountability</h3><span>Credit the people who do it right and name the ones who don't.</span></div>
<div class="door"><h3>Do something</h3><span>Complaining isn't a plan.</span></div></div></section>
<section class="sec"><p class="kicker">House rules</p><h2>Straight from the files</h2><div class="doors" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
<div class="door"><h3>Every price has a source</h3><span>Each figure links to the insurer, hospital or provider file it came from, with the file's date.</span></div>
<div class="door"><h3>Nothing is estimated</h3><span>No averages and no modeled prices. Ranges are the lowest and highest published figures.</span></div>
<div class="door"><h3>Old files are flagged</h3><span>Anything over six months old is marked. Files are re-read every month.</span></div>
<div class="door"><h3>What's in, what's out</h3><span>Each price says what it covers and what bills separately, like the doctor's fee or anesthesia.</span></div></div>
<p class="cap" style="margin-top:18px"><a href="/methodology/">How we read the files</a> · <a href="/corrections/">Corrections</a> · <a href="/price-file-watch/">Who isn't publishing</a></p></section>
<section class="sec"><p class="kicker">Independence</p><h2>No one pays to be listed</h2><p class="lede">I pay for this site myself. It's free to use and has no ads.
Providers can't pay to be listed, to rank higher, or to have a price changed or removed. Prices appear exactly as the insurer, hospital or provider published them.
If the site ever runs ads, they'll be clearly labeled and announced here first.</p></section>
<section class="sec"><p class="kicker">Where this goes</p><h2>Transparency as the expectation</h2><p class="lede">A healthcare market where providers post prices
because that's what honest businesses do, not because a federal rule makes them. We'd rather celebrate the providers doing it right. But we'll keep a light on
the ones who aren't.</p></section>
<section class="sec"><p class="kicker">Get in touch</p><h2>Spot something wrong?</h2><p class="lede">If a price looks wrong, the <a href="/corrections/">corrections page</a> says how to report it.
Hospitals and insurers: if we couldn't find your price file, or it's broken, the <a href="/price-file-watch/">Price File Watch</a> says what we tried. Send us the link.</p>
<p class="cap" style="margin-top:18px">Betsy Flickinger, Dallas, Texas</p></section>
</div></main><!-- written by procedure_pages.py -->'''
    jsonld = {'@context': 'https://schema.org', '@type': 'AboutPage', 'name': f"About {SITE['name']}", 'url': SITE['origin'] + '/about/',
              'mainEntity': {'@type': 'Person', 'name': AUTHOR['name'], 'jobTitle': AUTHOR['role'], 'description': AUTHOR['bio']}}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, 'w', encoding='utf-8').write(shell(f"About | {SITE['name']}", AUTHOR['bio'], '/about/', body, jsonld))


# ---------------------------------------------------------------- build
def build(site_dir, store, only_areas=None, only_codes=None, prev=None, rerender=False):
    site = os.path.join(site_dir, 'site')
    site_settings(site)
    areas = json.load(open(os.path.join(site, 'cities.json'), encoding='utf-8'))
    codes = [r['code'] for r in csv.DictReader(open(os.path.join(HERE, 'codes89.csv')))]
    if only_codes:
        codes = [c for c in codes if c in only_codes]
    out = os.path.join(site, 'costs')
    os.makedirs(out, exist_ok=True)
    import more_pages   # pages beyond the featured ones live in R2 (site/more/); this Function serves them (Oct 19)
    fdir = os.path.join(site_dir, 'functions', 'costs')
    if os.path.isdir(os.path.join(site_dir, 'functions')):
        os.makedirs(fdir, exist_ok=True)
        open(os.path.join(fdir, '[[path]].js'), 'w').write(more_pages.FUNCTION_JS)

    def more_n(au):
        if not getattr(store, 's3', None):
            return 0
        try:
            return len(json.loads(store.s3.get_object(Bucket=store.B, Key=f'{more_pages.PFX}{au}/manifest.json')['Body'].read())['pages'])
        except Exception:
            return 0
    open(os.path.join(out, 'costs.css'), 'w').write(CSS)
    # summaries of areas not rebuilt this run are kept from the last run, so the indexes always cover every area
    keep = prev.get('summaries', {}) if prev else {}
    summaries, names, cats, fps, todo = {}, {}, {}, {}, []
    pages, has = {}, {}                   # slug -> (page path, Medicare area label); slug -> featured codes on that page
    new_month = not prev or str(prev.get('built', ''))[:7] != TODAY.isoformat()[:7]   # once a month everything is rebuilt
    for a in areas:                       # pass 1: fingerprints and names only (each page's data is big; never hold them all)
        p = os.path.join(site, a['path'].strip('/'), 'index.html')
        if not os.path.isfile(p):
            print('no page for', a['slug'])
            continue
        D, med_area, fp = page_data(p)
        fps[a['slug']] = fp
        pages[a['slug']] = (p, med_area)
        has[a['slug']] = {c for c in codes if c in D['codes']}
        NCODES[0] = max(NCODES[0], len(D['codes']))
        for c in has[a['slug']]:
            names.setdefault(c, D['codes'][c]['name'])
            cats.setdefault(c, D['codes'][c].get('cat', ''))
        del D
        rebuild = (only_areas is None or a['slug'] in only_areas) and (new_month or not keep or prev.get('fp', {}).get(a['slug']) != fp)
        if rebuild:
            todo.append(a)
        else:
            # an area left out by --areas keeps its last fingerprint, so the next daily run still sees its page as changed
            fps[a['slug']] = (prev or {}).get('fp', {}).get(a['slug'], fp)
            for c in codes:
                k = f"{a['slug']}|{c}"
                if k in keep:
                    summaries[(a['slug'], c)] = keep[k]
    slugs = load_slugs(codes, names)
    procs = [(c, names[c], cats[c], slugs[c]) for c in codes if c in names]
    print(f"{len(todo)} of {len(pages)} areas to rebuild")

    with ThreadPoolExecutor(8) as ex:    # pass 2: one area at a time
        for a in todo:
            D = page_data(pages[a['slug']][0])[0]
            mine = [c for c in codes if c in has[a['slug']]]
            loc = store.local_medicare(a, len(D['fac']))
            pst = store.provider_states(a, D)   # Oct 19, 2026: out-of-state providers are left out of the figures
            if pst:
                n_away = sum(1 for v in pst.values() if v != area_state(a))
                print(f"  {a['slug']}: {n_away} provider(s) with a practice ZIP outside {area_state(a)} left out of the figures", flush=True)
            P_, npis_ = store.proof(a, len(D['fac']))   # Oct 31, 2026 (card #2): Texas only; None = pages exactly as before
            def one(c):
                try:
                    t_, h_ = store.get(a, 't', c), store.get(a, 'h', c)
                    pr = proof_mod.for_page(P_, D, c, t_, npis_, loc, kind_of) if P_ else None
                    return summarize(D, c, t_, h_, loc, area_state(a), pst, pr)
                except Exception as e:     # one bad file never stops the rest; that page keeps its last version
                    print(f"  {a['slug']} {c}: skipped ({type(e).__name__}: {e})", flush=True)
                    return None
            for c, S in zip(mine, ex.map(one, mine)):
                if S:
                    summaries[(a['slug'], c)] = S
                elif f"{a['slug']}|{c}" in keep:
                    summaries[(a['slug'], c)] = keep[f"{a['slug']}|{c}"]
            del D
            print('summarized', a['slug'], flush=True)

    live = [a for a in areas if a['slug'] in pages]
    sitemap = []
    n_written = 0
    for a in live:
        au, D_med = area_url_slug(a['slug']), pages[a['slug']][1]
        mine = [(slugs[c], summaries[(a['slug'], c)]) for c in codes if (a['slug'], c) in summaries]
        if a in todo or rerender:   # rerender: write every page again from saved figures (after a retry on a newer site)
            for c in codes:
                S = summaries.get((a['slug'], c))
                if not S:
                    continue
                ps = slugs[c]
                others = [(o['name'], f"/costs/{area_url_slug(o['slug'])}/{ps}/") for o in live if o is not a and (o['slug'], c) in summaries]
                related = [(names[x], f'/costs/{au}/{slugs[x]}/') for x in codes if x != c and cats.get(x) == S['cat'] and (a['slug'], x) in summaries][:12]
                d = os.path.join(out, au, ps)
                os.makedirs(d, exist_ok=True)
                open(os.path.join(d, 'index.html'), 'w', encoding='utf-8').write(procedure_page(a, S, others, related, D_med, a['path'], au, ps))
                rows = S.pop('csv', None)
                if rows is not None:
                    S['has_csv'] = True
                    with open(os.path.join(d, 'prices.csv'), 'w', newline='', encoding='utf-8') as fh:
                        w = csv.writer(fh)
                        w.writerow([f"# {S['name']} ({S['type']} {S['code']}) in {a['name']}. Source files linked per row. Built {TODAY.isoformat()} by {SITE['name']}."])
                        w.writerow(['type', 'provider', 'city', 'kind', 'insurer', 'network or label', 'price', 'file date', 'source'])
                        w.writerows(rows)
                n_written += 1
        os.makedirs(os.path.join(out, au), exist_ok=True)
        open(os.path.join(out, au, 'index.html'), 'w', encoding='utf-8').write(area_index(a, mine, au, a['path'], more_n(au)))
        sitemap.append(f'/costs/{au}/')
        sitemap += [f'/costs/{au}/{ps}/' for ps, s in mine if s.get('index', s['any'])]
    for c, n, cat, ps in procs:
        per = [(a, area_url_slug(a['slug']), summaries.get((a['slug'], c))) for a in live]
        d = os.path.join(out, ps)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, 'index.html'), 'w', encoding='utf-8').write(national_procedure(c, n, ps, per))
        sitemap.append(f'/costs/{ps}/')
    with_price = {a['slug']: sum(1 for c in codes if (summaries.get((a['slug'], c)) or {}).get('lo')) for a in live}
    open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(
        costs_index([(a, area_url_slug(a['slug']), with_price[a['slug']]) for a in live], procs))
    sitemap.insert(0, '/costs/')
    open(os.path.join(out, 'sitemap.xml'), 'w').write(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + ''.join(f'<url><loc>{esc(SITE["origin"] + u)}</loc><lastmod>{TODAY.isoformat()}</lastmod></url>\n' for u in sitemap) + '</urlset>\n')
    patch_home(site, national_section([(a, area_url_slug(a['slug'])) for a in live], summaries, procs, ''))
    notes_changed = patch_notes(site_dir, placeholder_notes(summaries, {a['slug']: a['name'] for a in live}, slugs))
    print(f'wrote {n_written} procedure pages, {len(live)} area indexes, {len(procs)} procedure indexes; sitemap {len(sitemap)} URLs')
    about_page(site)
    for v in summaries.values():
        v.pop('csv', None)
    slim = {f'{k[0]}|{k[1]}': v for k, v in summaries.items()}
    return {'fp': fps, 'summaries': slim, 'built': TODAY.isoformat(), 'pages': n_written, 'notes_changed': notes_changed}


# ---------------------------------------------------------------- deploy (same pull / deploy / save-back steps as site_files.py)
PF_OLD = """(r[4] & 2 ? '<span class="flag low">unusually low</span>' : '');"""
PF_NEW = """(r[4] & 2 ? '<span class="flag low">unusually low</span>' : '') + (r[4] & 8 ? '<span class="flag low">unusually high</span>' : '');"""


def placeholder_notes(summaries, areas_by_slug, slugs):
    """A private-notes card per insurer network whose files carry placeholder-looking rates (over 20x the median)."""
    by = {}
    for (slug, code), S in summaries.items():
        for k, n in (S.get('high_counts') or {}).items():
            b = by.setdefault(k, {'n': 0, 'codes': set(), 'areas': set(), 'ex': []})
            b['n'] += n
            b['codes'].add(code)
            b['areas'].add(areas_by_slug.get(slug, slug))
        for e in S.get('high_ex') or []:
            k = f"{e['ins']}|{e['plan']}"
            if k in by:
                by[k]['ex'].append(dict(e, code=code, proc=S.get('common') or S['name'], area=areas_by_slug.get(slug, slug)))
    if not by:
        return ''
    cards = []
    for k, b in sorted(by.items(), key=lambda x: -x[1]['n']):
        ins, plan = k.split('|', 1)
        ex = sorted(b['ex'], key=lambda e: -e['v'])[:3]
        exs = '; '.join(f"{esc(e['proc'])} ({esc(e['code'])}) at {esc(e['name'])}, {esc(e['area'])}: {money(e['v'])} "
                        f"(<a href=\"{esc(e['url'])}\" target=\"_blank\" rel=\"noopener\">file dated {esc(e['date'])}</a>)" for e in ex)
        cards.append(f"""<div class="item"><div>
  <h4>{esc(ins)}: {esc(plan)}</h4>
  <div class="who">{b['n']:,} filler-looking rates across {len(b['codes'])} of our featured procedures, in {len(b['areas'])} {'place' if len(b['areas']) == 1 else 'places'}</div>
  <p>Rates far above the typical published rate for the same procedure and place, shaped like filler (59,999.99, 99,999, round $100,000+) or over 100 times typical. Examples: {exs}.</p>
  <div class="ev">Found by the bot {TODAY.isoformat()}. Not yet checked by hand: open the linked file and confirm the line before publishing.</div>
</div><span class="stamp s-note">Review</span></div>""")
    return ("<!-- PLACEHOLDERS:START (written by price-files-bot site/procedure_pages.py every run; edits here are replaced) -->\n"
            "<h3>Placeholder-looking rates (rebuilt automatically)</h3>\n"
            "<p class=\"sub\">Insurer networks whose files carry filler-looking rates far above the typical rate for the same procedure and place, "
            "such as a $59,999.99 colonoscopy. Left out of the price pages' figures and labeled \"unusually high\" in the Price Finder. "
            "A possible public entry once checked: the rule requires real rates.</p>\n" + "\n".join(cards) + "\n<!-- PLACEHOLDERS:END -->")


def patch_notes(d, block):
    """Put the placeholder list into the Price File Watch private notes (functions/api/watch-notes.js), right after the intro."""
    p = os.path.join(d, 'functions', 'api', 'watch-notes.js')
    if not block or not os.path.isfile(p):
        return False
    js = open(p, encoding='utf-8').read()
    block = block.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')   # it lives inside a JS template string
    if '<!-- PLACEHOLDERS:START' in js:
        new = re.sub(r'<!-- PLACEHOLDERS:START.*?<!-- PLACEHOLDERS:END -->', lambda m: block, js, flags=re.S)
    else:
        m = re.search(r'(<h2>My notes \(private\)</h2>\s*<p class="sub">.*?</p>)', js, re.S)
        if not m:
            print('notes layout changed; placeholder list not added')
            return False
        new = js[:m.end()] + '\n\n' + block + '\n' + js[m.end():]
    if new != js:
        open(p, 'w', encoding='utf-8').write(new)
        return True
    return False


def label_high(text):
    """Teach a Price Finder page to show the 'unusually high' flag (bit 8). Idempotent; None if the page's code isn't as expected."""
    if PF_NEW in text:
        return text
    return text.replace(PF_OLD, PF_NEW, 1) if PF_OLD in text else None


METHOD_PROOF = """<h2 id="proof">Medicare and Medicaid records: evidence a provider does the procedure</h2>
<p>Insurer files list contract rates for providers who may never do a procedure. On our Texas procedure pages, the 10 lowest prices are in plain price order, and each line says whether public billing records show that provider does the procedure. We don't move anyone up or down because of these records.</p>
<ul>
<li><strong>Medicare:</strong> CMS's 2024 counts of services by doctor and procedure, and by hospital and procedure group (outpatient) or hospital-stay group (inpatient). A hospital's outpatient count is for the procedure's group (for example, "Level 1 Intraocular Procedures"), not the exact code. CMS moves codes between levels from year to year, so we accept a count anywhere in the same group family.</li>
<li><strong>Medicaid:</strong> HHS's provider spending file (2023 and 2024), which counts doctors' and other professionals' bills. We use the counts only; we don't use its payment amounts.</li>
<li><strong>"No Medicare or Medicaid record found"</strong> appears only when these files cover that kind of provider for that procedure and show nothing. It is not proof a provider doesn't do it: Medicare is mostly people 65 and older, CMS hides counts under 11 patients, and HHS hides counts under 12 claims. Call the provider to confirm.</li>
<li><strong>No label:</strong> surgery centers, group practices, clinics, imaging centers and labs (no public volume file covers them); hospitals for procedures Medicare's outpatient file doesn't count (colonoscopy, scans, lab tests); and procedures where Medicare is a poor guide: births, children's care, fertility, birth control, vasectomy and sports injuries.</li>
<li><strong>Counts never change the order.</strong> The list is sorted by price. A provider with 5,000 Medicare cases doesn't rank above one with 50.</li>
<li><strong>The 10 highest prices are unchanged.</strong> High real prices stay visible.</li>
<li><strong>Anesthesia clinicians:</strong> when an insurer lists a nurse anesthetist or anesthesiologist under a delivery, surgery, scan or lab code they don't perform, that line is left out of the page's figures and counted. It stays in the Price Finder.</li>
</ul>
<p>Sources: <a href="https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/medicare-physician-other-practitioners-by-provider-and-service" target="_blank" rel="noopener">CMS Medicare Physician &amp; Other Practitioners ↗</a>, <a href="https://data.cms.gov/provider-summary-by-type-of-service/medicare-outpatient-hospitals/medicare-outpatient-hospitals-by-provider-and-service" target="_blank" rel="noopener">CMS Medicare Outpatient Hospitals ↗</a>, <a href="https://data.cms.gov/provider-summary-by-type-of-service/medicare-inpatient-hospitals/medicare-inpatient-hospitals-by-provider-and-service" target="_blank" rel="noopener">CMS Medicare Inpatient Hospitals ↗</a>, <a href="https://opendata.hhs.gov/datasets/medicaid-provider-spending/" target="_blank" rel="noopener">HHS Medicaid Provider Spending ↗</a>. Texas pages only for now.</p>
"""


def patch_method_proof(d):
    """Puts the Medicare/Medicaid records section on the methodology page (card #2). Oct 31, 2026, option B: replaces the earlier
    "Who's ranked" version in place (that section ran up to the next <h2>), so the page never carries both."""
    p = os.path.join(d, 'site', 'methodology', 'index.html')
    if not os.path.isfile(p):
        return
    s = open(p, encoding='utf-8').read()
    if METHOD_PROOF in s:
        return
    j = s.find('<h2 id="proof">')
    if j >= 0:
        k = s.find('<h2', j + 5)
        k = k if k > 0 else s.find('</main>', j)
        if k > 0:
            open(p, 'w', encoding='utf-8').write(s[:j] + METHOD_PROOF + s[k:])
        return
    i = s.find('<h2 id="medicare-local">')
    i = i if i > 0 else s.rfind('</main>')
    if i > 0:
        open(p, 'w', encoding='utf-8').write(s[:i] + METHOD_PROOF + s[i:])


def deploy(force=False, only_areas=None):
    """only_areas: rebuild just these areas (others keep last run's figures, so the indexes still cover every area); not with --force."""
    if force and only_areas:
        sys.exit('--areas needs the last run\'s figures for the other areas; leave out --force')
    import boto3
    E = {k: "".join(os.environ.get(k, "").split()) for k in
         ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "CLOUDFLARE_API_TOKEN")}
    s3 = boto3.client("s3", endpoint_url=(os.environ.get("STORAGE_ENDPOINT") or f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"), aws_access_key_id=E["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=E["R2_SECRET_ACCESS_KEY"], region_name=(os.environ.get("STORAGE_REGION") or "auto"))
    B, KEY, SKEY, d = E["R2_BUCKET"], 'site/deploy/current.tgz', 'site/deploy/costs_state.json', '/tmp/costs'
    try:
        prev = None if force else json.loads(s3.get_object(Bucket=B, Key=SKEY)['Body'].read())
    except Exception:
        prev = None
    first = True
    for attempt in range(4):
        # If another build deploys while this one runs, start over from the newer site, reusing everything already worked out
        # (only areas whose page changed are recomputed), so nothing anyone else published is ever overwritten.
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        s3.download_file(B, KEY, f'{d}/site.tgz')
        etag = s3.head_object(Bucket=B, Key=KEY)['ETag']
        with tarfile.open(f'{d}/site.tgz') as t:
            t.extractall(d, filter='fully_trusted')
        if not os.path.isfile(f'{d}/site/price-finder/index.html') or not os.path.isdir(f'{d}/functions'):
            if first or attempt == 3:
                sys.exit(f'STOP: current.tgz is missing site/ or functions/ ({sorted(os.listdir(d))}); nothing deployed')
            print(f'site download looked incomplete ({sorted(os.listdir(d))}); trying again in a minute', flush=True)
            import time; time.sleep(60)
            continue
        for root, _, fs in os.walk(f'{d}/site'):
            if root.endswith('price-finder') and 'index.html' in fs:
                pth = os.path.join(root, 'index.html'); t0 = open(pth, encoding='utf-8').read(); t1 = label_high(t0)
                if t1 is None: print('price finder code not as expected, flag label not added:', pth)
                elif t1 != t0: open(pth, 'w', encoding='utf-8').write(t1)
        if first:
            try:   # the layout the monthly refresh starts from
                lay = s3.get_object(Bucket=B, Key='site/deploy/price-finder.html')['Body'].read().decode()
                new_lay = label_high(lay)
                if new_lay and new_lay != lay:
                    s3.put_object(Bucket=B, Key='site/deploy/price-finder.html', Body=new_lay.encode(), ContentType='text/html; charset=utf-8')
            except Exception as e:
                print('layout not updated:', e)
        home_before = open(f'{d}/site/index.html', encoding='utf-8').read()
        st = build(d, Store(d), only_areas, prev=prev, rerender=not first)
        changed = (not prev) or not first or st['pages'] > 0 or st.get('notes_changed') or open(f'{d}/site/index.html', encoding='utf-8').read() != home_before
        if not changed:
            print('no area page changed since the last run; nothing to deploy')
            return
        if s3.head_object(Bucket=B, Key=KEY)['ETag'] == etag:
            break
        print(f'the live site changed while building (another build deployed); starting over from the newer site (attempt {attempt + 2})')
        prev, first = dict(st, built=TODAY.isoformat()), False
        if attempt == 3:
            sys.exit('STOP: the live site kept changing; nothing deployed. The next daily run will try again.')
    patch_method_proof(d)
    env = dict(os.environ, **E, CLOUDFLARE_ACCOUNT_ID=E['R2_ACCOUNT_ID'])
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sitewide.py'), d], check=True)   # name, Units look, share image (Oct 18)
    r = subprocess.run(['npx', '--yes', 'wrangler@3.114.0', 'pages', 'deploy', 'site', '--project-name', 'upfront-dfw-preview', '--branch', 'main',
                        '--commit-dirty=true'], cwd=d, env=env, capture_output=True, text=True)
    print(r.stdout[-1500:], r.stderr[-1500:])
    if r.returncode != 0:
        sys.exit('deploy failed; the preview site is unchanged')
    os.remove(f'{d}/site.tgz')
    with tarfile.open('/tmp/costs-site.tgz', 'w:gz') as t:
        for name in os.listdir(d):
            if name != '.wrangler':          # wrangler's scratch folder isn't part of the site
                t.add(f'{d}/{name}', arcname=f'./{name}')
    s3.upload_file('/tmp/costs-site.tgz', B, KEY)
    s3.put_object(Bucket=B, Key=SKEY, Body=json.dumps(st).encode())
    print('deployed and saved to', KEY)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'deploy'])
    ap.add_argument('site_dir', nargs='?')
    ap.add_argument('--areas')
    ap.add_argument('--codes')
    ap.add_argument('--offline')
    ap.add_argument('--force', action='store_true')
    A = ap.parse_args()
    if A.cmd == 'deploy':
        deploy(A.force, set(A.areas.split(',')) if A.areas else None)
    else:
        build(A.site_dir, Store(A.site_dir, A.offline), set(A.areas.split(',')) if A.areas else None, set(A.codes.split(',')) if A.codes else None)
