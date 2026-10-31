"""Proof a provider does a procedure, for the price pages (card #2, Betsy OK'd Oct 30-31, 2026).

Reads the proof table built by utilization/build_evidence.py: one file per state (storage
data/utilization/<year>/by_state/evidence_<st>.parquet, Oct 31, 2026), or, with no state given, the first Texas table
(data/utilization/<year>/evidence_tx.parquet), which is what the live Texas pages have used since Oct 31.
Rules (Betsy, Oct 30):
  * Medicare and Medicaid counts only confirm ("yes, they do this"). They never rank anyone by size and are never a price.
  * A provider is held out of the "lowest/highest" rankings ("also listed, may not offer this procedure") only when a public
    volume file covers that kind of provider for that procedure AND it has no record. Everyone else is ranked as before:
      - surgery centers, group practices, imaging centers, labs, clinics: no public volume file covers them
      - hospitals, for procedures Medicare's hospital outpatient file doesn't count (only "comprehensive" groups, so not
        colonoscopy, scans, lab tests)
      - procedures where Medicare is a poor guide (births and maternity, children's care, fertility, birth control,
        vasectomy, sports injuries such as ACL repair): Medicare is mostly people 65+
  * Which states use it is decided in procedure_pages.py (PROOF_STATES). Betsy switches each step on.

  * Anesthesia clinicians (nurse anesthetists, anesthesiologists) listed by an insurer under a delivery, surgery, scan or lab
    code they don't perform (Oct 31: CRNAs at $435 were the "lowest" vaginal delivery in DFW) are left out of that page's
    figures and counted, like the existing kind-of-place test. Their rates stay in the files and the Price Finder.

status(...) returns (state, record):
  ('proved', rec)       rec = {'src', 'n', 'unit', 'year', 'url', 'group'}: shown as a proof line with link and year
  ('not_covered', why)  ranked as before; why says which rule applied
  ('no_record', why)    held out of the lowest-10 ranking, still listed; why is the plain note shown with it
  ('wrong_specialty', why) left out of the page's figures (anesthesia clinician on a code they don't perform; on a delivery
                           code, a clinician whose listed specialty doesn't deliver babies)
"""
import io
import re

POOR_GUIDE_CATS = {'Maternity'}
POOR_GUIDE_NAME = re.compile(r'birth|deliver|c-section|cesarean|pregnan|prenatal|newborn|infant|pediatric|child|fertil|ivf|'
                             r'insemination|contracept|iud|implant(?=.*(arm|birth))|vasectomy|tubal|cruciate|acl\b|meniscus repair'
                             r'|well[- ]child|vaccin|immuniz', re.I)
LABEL = {'medicare_physician': 'Medicare', 'medicare_outpatient': 'Medicare', 'medicare_inpatient': 'Medicare',
         'medicaid_professional': 'Medicaid'}
ANESTHESIA_TAX = ('207L', '367500000X', '367H00000X')     # anesthesiology, nurse anesthetist, anesthesiologist assistant
ANESTHESIA_WORDS = re.compile(r'\bCRNA\b|\bC\.R\.N\.A\b|anesthes|\bsedation\b', re.I)


def performed_code(code):
    """CPT surgery (incl. deliveries), radiology and lab ranges: services an anesthesia clinician doesn't perform. Visits, therapy,
    psych and other medicine codes (90281-99607) are left alone: pain-medicine anesthesiologists bill office visits."""
    return bool(re.fullmatch(r'\d{5}', code or '')) and 10004 <= int(code) <= 89398


# Oct 31, 2026 (Betsy OK'd): obstetric delivery codes (59400-59622: vaginal, cesarean and VBAC deliveries) are performed by
# OB/GYNs, family and general practice physicians and midwives. DFW's "lowest vaginal delivery" was a $435-$440 amount that one
# insurer lists for every clinician in a group, nurses and anesthetists included. A clinician whose federal NPI listing names
# another specialty is left out of that page's figures, like the anesthesia rule. Unknown specialty: kept.
DELIVERY_CODE = re.compile(r'^59(?:4\d\d|5[01]\d|6[0-2]\d)$')
DELIVERY_TAX = ('207V', '207Q', '208D', '176B', '367A')   # OB/GYN (incl. maternal-fetal), family med, general practice, midwives


def is_non_delivery(t):
    tax = (t[0] if isinstance(t, tuple) else t) or ''
    return bool(tax) and not str(tax).startswith(DELIVERY_TAX)


def is_anesthesia(t, name):
    tax, cred = (t if isinstance(t, tuple) else (t, ''))
    return str(tax or '').startswith(ANESTHESIA_TAX) or bool(ANESTHESIA_WORDS.search(f"{cred or ''} {name or ''}"))
NO_RECORD = ("No Medicare or Medicaid record for this procedure ({year}). That doesn't mean they don't do it: "
             "Medicare is mostly people 65+, and rows under 11 patients are hidden.")


class Proof:
    def __init__(self, s3=None, bucket=None, year='2024', path=None, tax=None, state=None):
        self.tax = tax or {}     # npi -> primary taxonomy (NPPES), for the anesthesia check
        self.state = (state or 'TX').upper()
        self.state_name = STATE_NAME.get(self.state, self.state)
        import pyarrow.parquet as pq
        if path:
            tb = pq.read_table(path)
        else:
            key = evidence_key(year, state)
            body = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
            tb = pq.read_table(io.BytesIO(body))
        d = tb.select(['npi', 'ccn', 'evidence_source', 'code', 'group_name', 'volume', 'unit', 'data_year', 'file_url']).to_pydict()
        self.year = year
        self.code = {}       # (npi, hcpcs) -> best record (Medicare first, then Medicaid)
        self.apc = {}        # (ccn, apc) -> record
        self.drg = {}        # (ccn, drg) -> record
        self.apcs, self.phy_codes = set(), set()
        self.apc_fam = {}    # apc -> family name ("Level 3 Musculoskeletal Procedures" -> "Musculoskeletal Procedures")
        self.fam = {}        # (ccn, family) -> best record in that family (CMS moves codes between levels from year to year)
        self.ccn_groups = {} # ccn -> number of Medicare outpatient groups with volume (diagnostic)
        self.ccn_drgs = set()  # ccns with any Medicare inpatient record
        for i in range(tb.num_rows):
            src = d['evidence_source'][i]
            rec = {'src': LABEL[src], 'n': int(d['volume'][i] or 0), 'unit': d['unit'][i], 'year': d['data_year'][i],
                   'url': d['file_url'][i], 'group': d['group_name'][i] or '',
                   'level_group': src in ('medicare_outpatient', 'medicare_inpatient')}
            if rec['n'] <= 0:
                continue
            c = (d['code'][i] or '').strip()
            if src == 'medicare_outpatient':
                self.apcs.add(c)
                fam = re.sub(r'^Level \d+\s+', '', rec['group']).strip().lower()
                self.apc_fam[c] = fam
                if d['ccn'][i]:
                    self.apc[(d['ccn'][i], c)] = rec
                    self.ccn_groups[d['ccn'][i]] = self.ccn_groups.get(d['ccn'][i], 0) + 1
                    k = (d['ccn'][i], fam)
                    if k not in self.fam or rec['n'] > self.fam[k]['n']:
                        self.fam[k] = rec
            elif src == 'medicare_inpatient':
                if d['ccn'][i]:
                    self.drg[(d['ccn'][i], c.zfill(3))] = rec
                    self.ccn_drgs.add(d['ccn'][i])
            elif d['npi'][i]:
                if src == 'medicare_physician':
                    self.phy_codes.add(c)
                k = (str(d['npi'][i]), c)
                if k not in self.code or (self.code[k]['src'] == 'Medicaid' and rec['src'] == 'Medicare'):
                    self.code[k] = rec
        self.c2a = {}
        try:
            body = (pq.read_table(re.sub(r'(by_state/)?evidence_[a-z]{2}', 'code_to_apc', path)) if path else
                    pq.read_table(io.BytesIO(s3.get_object(Bucket=bucket, Key=f'data/utilization/{year}/code_to_apc.parquet')['Body'].read())))
            x = body.to_pydict()
            self.c2a = dict(zip(x['code'], x['apc']))
        except Exception:
            pass

    def status(self, kind, npi, ccn, code, name, cat, drg, name_of=''):
        """kind: the page's provider kind (H hospital, A surgery center, G group/other place, P individual clinician)."""
        if kind in ('P', 'G') and performed_code(code) and is_anesthesia(self.tax.get(str(npi or '')), name_of):
            return 'wrong_specialty', 'anesthesia clinician listed under a code they don\'t perform'
        if DELIVERY_CODE.match(code or ''):
            t_ = self.tax.get(str(npi or ''))
            # a clinician with another specialty; a practice only when its listing names one other physician specialty
            # (Illinois: a dermatology group was the "lowest vaginal delivery"); multi-specialty groups and clinics are kept
            if (kind == 'P' and is_non_delivery(t_)) or (kind == 'G' and is_non_delivery(t_) and str((t_ or ('',))[0] if isinstance(t_, tuple) else t_).startswith(('207', '208'))):
                return 'wrong_specialty', 'listed specialty doesn\'t deliver babies'

        if cat in POOR_GUIDE_CATS or POOR_GUIDE_NAME.search(name or ''):
            return 'not_covered', 'Medicare is a poor guide for this procedure (mostly people 65+)'
        npi = str(npi or '')
        rec = self.code.get((npi, code))
        if kind == 'H':
            if drg:
                g = re.sub(r'\D', '', code).zfill(3)
                rec = self.drg.get((ccn, g)) if ccn else None
                if rec:
                    return 'proved', rec
                return ('no_record', NO_RECORD.format(year=self.year)) if ccn in self.ccn_drgs else ('not_covered', 'no Medicare hospital ID matched')
            apc = self.c2a.get(code)
            if not apc or apc not in self.apcs:
                return ('proved', rec) if rec else ('not_covered', "Medicare publishes no hospital volume for this kind of procedure")
            hrec = (self.apc.get((ccn, apc)) or self.fam.get((ccn, self.apc_fam.get(apc)))) if ccn else None
            if hrec or rec:
                return 'proved', hrec or rec
            # held out only when the hospital ID matches real Medicare records (a wrong ID must never look like "doesn't do it")
            return ('no_record', NO_RECORD.format(year=self.year)) if ccn in self.ccn_groups else ('not_covered', 'no Medicare hospital ID matched')
        if kind == 'P':
            if rec:
                return 'proved', rec
            if code in self.phy_codes:
                return 'no_record', NO_RECORD.format(year=self.year)
            return 'not_covered', f'no Medicare volume for this code in {self.state_name}'
        if rec:                       # groups and other places: a Medicaid or Medicare record is proof, no record changes nothing
            return 'proved', rec
        return 'not_covered', 'no public volume file covers this kind of provider'


def line(rec):
    """The proof line shown with a provider: 'Medicare: 240 services, 2024' (group-level says so)."""
    g = f" in the procedure group “{rec['group']}”" if rec['group'] and rec['unit'] != 'claim lines' and rec['src'] == 'Medicare' \
        and rec.get('level_group') else ''
    return f"{rec['src']}: {rec['n']:,} {rec['unit']}{g}, {rec['year']}"


STATE_NAME = {'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut',
              'DE': 'Delaware', 'DC': 'the District of Columbia', 'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
              'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine',
              'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri',
              'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
              'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon',
              'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
              'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming'}


def evidence_key(year, state=None):
    """Storage key of the proof table: the state's own file, or (no state) the first Texas table."""
    return f'data/utilization/{year}/by_state/evidence_{state.lower()}.parquet' if state else f'data/utilization/{year}/evidence_tx.parquet'


def load_taxonomy_state(s3, bucket, state, year='2024'):
    """npi -> (primary taxonomy, credential) for one state, from the per-state file build_evidence.py writes (NPPES practice state)."""
    return load_taxonomy(s3, bucket, key=f'data/utilization/{year}/by_state/npi_tax_{state.lower()}.parquet')


def load_taxonomy(s3, bucket, key='data/scope/tx_npis_all.parquet'):
    """npi -> primary taxonomy code from the Texas NPPES extract the bot already keeps."""
    import pyarrow.parquet as pq
    tb = pq.read_table(io.BytesIO(s3.get_object(Bucket=bucket, Key=key)['Body'].read()), columns=['npi', 'taxonomy_1', 'credential']).to_pydict()
    return {str(n): (t, c) for n, t, c in zip(tb['npi'], tb['taxonomy_1'], tb['credential']) if t or c}


def for_page(P, D, code, t, npis, loc, kind_of):
    """{fac index: (state, info)} for one code on one page. info for 'proved' = {'text', 'url'}; otherwise the note."""
    C = D['codes'][code]
    drg = 'DRG' in (C.get('type') or '')
    f = (t or {}).get('f') or C.get('f') or {}
    out = {}
    for fi in f:
        i = int(fi)
        ccn = ((loc or {}).get(i) or [None, None, None])[2]
        st, info = P.status(kind_of(D['fac'][i]), npis[i], ccn, code, C['name'], C.get('cat', ''), drg, D['fac'][i][0])
        out[i] = (st, {'text': line(info), 'url': info['url']} if st == 'proved' else info)
    return out
