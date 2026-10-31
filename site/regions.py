"""Oct 31, 2026 (Betsy chose regional pages): a state whose Price Finder page is too big for one file is published as regional
pages instead. California's statewide page came out at 32.5 MB; the website host takes files up to 25 MB.

Each region is a list of whole counties. A provider belongs to the region of the county its practice ZIP lies mostly in (the
Census ZIP-to-county file behind data/medicare_areas_zip.csv, the same one the Medicare doctor-fee areas use). A hospital file
belongs to the region of its hospital's ZIP (CMS Hospital General Information, as recorded in the hospital plan). The state's
own address (/california/price-finder/) becomes a short page that links to its regional pages.

REGIONS: state page slug -> {region slug: (page name, Medicare locality shown by default, locality name, map box, counties)}.
The default locality is the one with the most providers in the region; every provider is still compared with Medicare for their
own area (site/med_areas.py)."""
import csv, os

HERE = os.path.dirname(os.path.abspath(__file__))

REGIONS = {
    'california': {
        'los-angeles': ('Los Angeles, Orange & Ventura', '18', 'Los Angeles', '-119.8,33.0,-117.1,35.0',
                        ['Los Angeles', 'Orange', 'Ventura']),
        'san-diego-inland-empire': ('San Diego & Inland Empire', '62', 'Riverside-San Bernardino', '-117.9,32.3,-114.0,35.9',
                                    ['San Diego', 'Imperial', 'Riverside', 'San Bernardino']),
        'bay-area': ('San Francisco Bay Area', '05', 'San Francisco', '-123.8,36.6,-121.0,39.0',
                     ['Alameda', 'Contra Costa', 'Marin', 'Napa', 'San Francisco', 'San Mateo', 'Santa Clara', 'Solano', 'Sonoma',
                      'Santa Cruz']),
        'sacramento-northern-california': ('Sacramento & Northern California', '63', 'Sacramento', '-124.6,38.0,-119.5,42.3',
                                           ['Sacramento', 'Placer', 'Yolo', 'El Dorado', 'Sutter', 'Yuba', 'Nevada', 'Butte', 'Shasta',
                                            'Tehama', 'Glenn', 'Colusa', 'Lake', 'Mendocino', 'Humboldt', 'Del Norte', 'Siskiyou',
                                            'Trinity', 'Lassen', 'Modoc', 'Plumas', 'Sierra', 'Amador', 'Alpine']),
        'central-california': ('Central California', '56', 'Fresno', '-122.2,34.2,-115.4,38.8',
                               ['Fresno', 'Kern', 'Kings', 'Tulare', 'Madera', 'Merced', 'Stanislaus', 'San Joaquin', 'Mariposa',
                                'Tuolumne', 'Calaveras', 'Mono', 'Inyo', 'Monterey', 'San Benito', 'San Luis Obispo', 'Santa Barbara']),
    },
    'new-york': {   # Oct 31, 2026 (Betsy): regions beside the statewide page (ALONGSIDE)
        'new-york-city': ('New York City', '01', 'Manhattan', '-74.4,40.3,-73.5,41.1',
            ['Bronx', 'Kings', 'New York', 'Queens', 'Richmond']),
        'long-island': ('Long Island', '02', 'NYC Suburbs/Long Island', '-74.0,40.4,-71.7,41.5',
            ['Nassau', 'Suffolk']),
        'hudson-valley': ('Hudson Valley', '03', 'Poughkeepsie/N NYC Suburbs', '-75.3,40.7,-73.3,42.4',
            ['Westchester', 'Rockland', 'Putnam', 'Orange', 'Dutchess', 'Ulster', 'Sullivan']),
        'western-new-york': ('Western New York & Finger Lakes', '99', 'Rest of New York', '-79.9,41.8,-76.4,43.5',
            ['Erie', 'Niagara', 'Chautauqua', 'Cattaraugus', 'Allegany', 'Wyoming', 'Genesee', 'Orleans', 'Monroe', 'Livingston', 'Ontario', 'Wayne', 'Seneca', 'Yates', 'Steuben', 'Schuyler', 'Chemung']),
        'upstate-new-york': ('Capital Region, Central & Northern New York', '99', 'Rest of New York', '-76.9,41.7,-73.1,45.2',
            ['Albany', 'Broome', 'Cayuga', 'Chenango', 'Clinton', 'Columbia', 'Cortland', 'Delaware', 'Essex', 'Franklin', 'Fulton', 'Greene', 'Hamilton', 'Herkimer', 'Jefferson', 'Lewis', 'Madison', 'Montgomery', 'Oneida', 'Onondaga', 'Oswego', 'Otsego', 'Rensselaer', 'Saratoga', 'Schenectady', 'Schoharie', 'St. Lawrence', 'Tioga', 'Tompkins', 'Warren', 'Washington']),
    },
    'florida': {   # Oct 31, 2026 (Betsy): regions beside the statewide page (ALONGSIDE)
        'south-florida': ('South Florida', '03', 'Fort Lauderdale', '-82.1,24.3,-79.8,28.0',
            ['Miami-Dade', 'Broward', 'Palm Beach', 'Monroe', 'Martin', 'St. Lucie', 'Indian River', 'Okeechobee']),
        'tampa-bay': ('Tampa Bay', '99', 'Rest of Florida', '-83.0,26.8,-81.0,29.2',
            ['Hillsborough', 'Pinellas', 'Pasco', 'Hernando', 'Citrus', 'Polk', 'Manatee', 'Sarasota']),
        'central-florida': ('Orlando & Central Florida', '99', 'Rest of Florida', '-82.7,27.6,-80.3,29.8',
            ['Orange', 'Osceola', 'Seminole', 'Lake', 'Sumter', 'Marion', 'Volusia', 'Flagler', 'Brevard']),
        'southwest-florida': ('Southwest Florida', '03', 'Fort Lauderdale', '-82.5,25.6,-80.7,27.8',
            ['Lee', 'Collier', 'Charlotte', 'Hendry', 'Glades', 'DeSoto', 'Highlands', 'Hardee']),
        'north-florida': ('North Florida & the Panhandle', '99', 'Rest of Florida', '-87.7,28.8,-81.1,31.2',
            ['Alachua', 'Baker', 'Bay', 'Bradford', 'Calhoun', 'Clay', 'Columbia', 'Dixie', 'Duval', 'Escambia', 'Franklin', 'Gadsden', 'Gilchrist', 'Gulf', 'Hamilton', 'Holmes', 'Jackson', 'Jefferson', 'Lafayette', 'Leon', 'Levy', 'Liberty', 'Madison', 'Nassau', 'Okaloosa', 'Putnam', 'Santa Rosa', 'St. Johns', 'Suwannee', 'Taylor', 'Union', 'Wakulla', 'Walton', 'Washington']),
    },
    'illinois': {   # Oct 31, 2026 (Betsy): regions beside the statewide page (ALONGSIDE)
        'chicago-area': ('Chicago Area', '16', 'Chicago', '-89.1,40.8,-87.4,42.7',
            ['Cook', 'DuPage', 'Kane', 'Lake', 'McHenry', 'Will', 'Kendall', 'Grundy', 'DeKalb', 'Kankakee']),
        'central-southern-illinois': ('Central & Southern Illinois', '99', 'Rest of Illinois', '-91.6,36.8,-87.4,42.7',
            ['Adams', 'Alexander', 'Bond', 'Boone', 'Brown', 'Bureau', 'Calhoun', 'Carroll', 'Cass', 'Champaign', 'Christian', 'Clark', 'Clay', 'Clinton', 'Coles', 'Crawford', 'Cumberland', 'De Witt', 'Douglas', 'Edgar', 'Edwards', 'Effingham', 'Fayette', 'Ford', 'Franklin', 'Fulton', 'Gallatin', 'Greene', 'Hamilton', 'Hancock', 'Hardin', 'Henderson', 'Henry', 'Iroquois', 'Jackson', 'Jasper', 'Jefferson', 'Jersey', 'Jo Daviess', 'Johnson', 'Knox', 'LaSalle', 'Lawrence', 'Lee', 'Livingston', 'Logan', 'Macon', 'Macoupin', 'Madison', 'Marion', 'Marshall', 'Mason', 'Massac', 'McDonough', 'McLean', 'Menard', 'Mercer', 'Monroe', 'Montgomery', 'Morgan', 'Moultrie', 'Ogle', 'Peoria', 'Perry', 'Piatt', 'Pike', 'Pope', 'Pulaski', 'Putnam', 'Randolph', 'Richland', 'Rock Island', 'Saline', 'Sangamon', 'Schuyler', 'Scott', 'Shelby', 'St. Clair', 'Stark', 'Stephenson', 'Tazewell', 'Union', 'Vermilion', 'Wabash', 'Warren', 'Washington', 'Wayne', 'White', 'Whiteside', 'Williamson', 'Winnebago', 'Woodford']),
    },
    'pennsylvania': {   # Oct 31, 2026 (Betsy): regions beside the statewide page (ALONGSIDE)
        'philadelphia-area': ('Philadelphia Area', '01', 'Metropolitan Philadelphia', '-76.3,39.6,-74.6,40.8',
            ['Philadelphia', 'Bucks', 'Chester', 'Delaware', 'Montgomery']),
        'western-pennsylvania': ('Pittsburgh & Western Pennsylvania', '99', 'Rest of Pennsylvania', '-80.7,39.5,-77.8,42.4',
            ['Allegheny', 'Armstrong', 'Beaver', 'Butler', 'Fayette', 'Greene', 'Washington', 'Westmoreland', 'Lawrence', 'Mercer', 'Erie', 'Crawford', 'Venango', 'Warren', 'Forest', 'Clarion', 'Indiana', 'Jefferson', 'Cambria', 'Somerset', 'Elk', 'McKean', 'Cameron', 'Clearfield', 'Blair', 'Bedford']),
        'central-eastern-pennsylvania': ('Central & Eastern Pennsylvania', '99', 'Rest of Pennsylvania', '-78.4,39.5,-74.5,42.2',
            ['Adams', 'Berks', 'Bradford', 'Carbon', 'Centre', 'Clinton', 'Columbia', 'Cumberland', 'Dauphin', 'Franklin', 'Fulton', 'Huntingdon', 'Juniata', 'Lackawanna', 'Lancaster', 'Lebanon', 'Lehigh', 'Luzerne', 'Lycoming', 'Mifflin', 'Monroe', 'Montour', 'Northampton', 'Northumberland', 'Perry', 'Pike', 'Potter', 'Schuylkill', 'Snyder', 'Sullivan', 'Susquehanna', 'Tioga', 'Union', 'Wayne', 'Wyoming', 'York']),
    },
}
LIVE = True   # Oct 31, 2026: test split checked row by row (region-test.yml: 0 differences) and the LA page built at 12.1 MiB
HOSP_V = 2   # Oct 31: hospital placement; v2 adds ZIP-prefix and CMS Provider of Services ZIPs (raising it re-places hospitals)
SPLIT_V = 2   # Oct 31: v2 keeps every state code and the 'coming' list on regional pages; raising it splits again
STATE_CODE = {'california': 'CA', 'new-york': 'NY', 'florida': 'FL', 'illinois': 'IL', 'pennsylvania': 'PA'}
# Oct 31, 2026 (Betsy): New York, Florida, Illinois and Pennsylvania fit on one page, so their statewide page stays live (its
# address and /costs/ pages never change) and the regions are added beside it. California's statewide page was too big, so its
# address became a page linking to the regions.
ALONGSIDE = {'new-york', 'florida', 'illinois', 'pennsylvania'}
# States whose regional pages are switched on (hosp_area.py splits them on its next hourly run). A state not listed here is
# built and published exactly as before. Pennsylvania: its first state build is split as soon as it is done (no rebuild).
ON = {'california'}
PARENT = {r: s for s, R in REGIONS.items() for r in R}
NEIGHBORS = {   # regions that share a border, linked from each regional page for people who live near one
    'los-angeles': ['san-diego-inland-empire', 'central-california'],
    'san-diego-inland-empire': ['los-angeles', 'central-california'],
    'bay-area': ['sacramento-northern-california', 'central-california'],
    'sacramento-northern-california': ['bay-area', 'central-california'],
    'central-california': ['los-angeles', 'bay-area', 'sacramento-northern-california', 'san-diego-inland-empire'],
    'new-york-city': ['long-island', 'hudson-valley'], 'long-island': ['new-york-city'],
    'hudson-valley': ['new-york-city', 'upstate-new-york'], 'western-new-york': ['upstate-new-york'],
    'upstate-new-york': ['hudson-valley', 'western-new-york'],
    'south-florida': ['southwest-florida', 'central-florida'], 'southwest-florida': ['south-florida', 'tampa-bay'],
    'tampa-bay': ['central-florida', 'southwest-florida', 'north-florida'], 'central-florida': ['tampa-bay', 'south-florida', 'north-florida'],
    'north-florida': ['central-florida', 'tampa-bay'],
    'chicago-area': ['central-southern-illinois'], 'central-southern-illinois': ['chicago-area'],
    'philadelphia-area': ['central-eastern-pennsylvania'], 'western-pennsylvania': ['central-eastern-pennsylvania'],
    'central-eastern-pennsylvania': ['philadelphia-area', 'western-pennsylvania'],
}   # region slug -> state page slug


def zip_county(state_code):
    """{zip5: county} for one state (Census 2020 ZCTA -> county with the most land)"""
    for p in (os.path.join(HERE, '..', 'data', 'medicare_areas_zip.csv'), os.path.join(HERE, 'medicare_areas_zip.csv')):
        if os.path.exists(p):
            return {r['zip5']: r['county'] for r in csv.DictReader(open(p)) if r['state'] == state_code}
    raise FileNotFoundError('medicare_areas_zip.csv')


def county_region(state):
    m = {c: r for r, v in REGIONS[state].items() for c in v[4]}
    return m


def check(state):
    """every county of the state in exactly one region"""
    counties = set(zip_county(STATE_CODE[state]).values())
    listed = [c for v in REGIONS[state].values() for c in v[4]]
    dup = {c for c in listed if listed.count(c) > 1}
    missing, extra = counties - set(listed), set(listed) - counties
    if dup or missing or extra: raise ValueError(f'{state} regions: twice {sorted(dup)}, missing {sorted(missing)}, unknown {sorted(extra)}')
    return True


def region_of_zip(state, z, _cache={}):
    """the region of a ZIP's county; a ZIP that isn't a Census ZIP area (a PO box or a hospital's own ZIP, e.g. 90509) by its
    first three digits, when every Census ZIP area starting with them is in the same region (else None)"""
    if state not in _cache:
        zc, cr = zip_county(STATE_CODE[state]), county_region(state)
        z3 = {}
        for k, c in zc.items(): z3.setdefault(k[:3], set()).add(cr.get(c))
        _cache[state] = (zc, cr, {k: next(iter(v)) for k, v in z3.items() if len(v) == 1})
    zc, cr, z3 = _cache[state]
    z = str(z or '')[:5]
    return cr.get(zc.get(z)) or (z3.get(z[:3]) if len(z) == 5 else None)


def hospital_zips(state_code, _cache={}):
    """{CMS number: ZIP} for hospitals missing from Medicare's Hospital General Information list (long-term care and rehab
    hospitals), from CMS's Provider of Services file (data/hospital_zip_extra.csv, written by site/hosp_zip_lookup.py)"""
    if state_code not in _cache:
        _cache[state_code] = {}
        for p in (os.path.join(HERE, '..', 'data', 'hospital_zip_extra.csv'), os.path.join(HERE, 'hospital_zip_extra.csv')):
            if os.path.exists(p):
                _cache[state_code] = {r['ccn']: r['zip'] for r in csv.DictReader(open(p)) if r['state'] == state_code and r['zip']}; break
    return _cache[state_code]


def by_county_text(counties):
    return 'in a ZIP code that lies mostly in ' + ', '.join(counties[:-1]) + ' or ' + counties[-1] + ' County'


if __name__ == '__main__':
    for s in REGIONS: print(s, check(s))
