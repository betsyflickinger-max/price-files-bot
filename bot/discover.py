"""Find each hospital's current price-file link from its own cms-hpt.txt.

Every hospital must post https://<its domain>/cms-hpt.txt listing its location names and file links
(mrf-url). We read that index each time we check a hospital, so a renamed or moved file is followed
automatically. Link cleanup is the same as the Oct 9 retry run (stray "mrf-url:" prefixes,
non-breaking spaces, missing https://, unescaped characters in the path).
"""
import re
import urllib.parse

import requests
import urllib3
from rapidfuzz import fuzz

urllib3.disable_warnings()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
STOP = {"the", "of", "and", "inc", "llc", "lp", "at", "a", "dba", "d/b/a"}
# Hosts that serve files for many unrelated hospitals; their cms-hpt.txt (if any) is not the hospital's own.
SHARED_HOSTS = ("blob.core.windows.net", "amazonaws.com", "cloudfront.net", "googleapis.com",
                "hospitalpricedisclosure.com", "hospitalpriceindex.com", "panaceainc.com",
                "azureedge.net", "sharepoint.com", "box.com", "dropbox.com")


def clean(u):
    u = (u or "").replace(" ", " ").replace("Â", "").strip()
    u = re.sub(r"^\s*mrf-url\s*:\s*", "", u, flags=re.I).strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    p = urllib.parse.urlsplit(u)
    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/%-._~!$&'()*+,;=:@")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))


def host(u):
    try:
        h = urllib.parse.urlsplit(u if "://" in u else "https://" + u).netloc.lower().split(":")[0]
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def index_domain(row):
    """The hospital's known index domain, else the host of its known file link if that's its own site."""
    if row.get("index_domain"):
        return row["index_domain"].lower()
    h = host(row.get("mrf_url", ""))
    return "" if not h or h.endswith(SHARED_HOSTS) else h


def _toks(s):
    s = (s or "").lower().replace("&", " and ").replace("'", "").replace("saint ", "st ").replace("st. ", "st ")
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", s).split() if t not in STOP]


def score(a, b):
    a, b = " ".join(_toks(a)), " ".join(_toks(b))
    if not a or not b:
        return 0
    return (fuzz.token_set_ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 2


def parse(txt):
    ents, cur = [], {}
    for line in txt.replace("\r", "").splitlines():
        line = line.strip()
        if not line:
            if cur:
                ents.append(cur); cur = {}
            continue
        m = re.match(r"([a-z\-_ ]+?)\s*:\s*(.*)", line, re.I)
        if m:
            k = m.group(1).lower().strip().replace("_", "-").replace(" ", "-")
            if k == "location-name" and "location-name" in cur:
                ents.append(cur); cur = {}
            cur[k] = m.group(2).strip()
    if cur:
        ents.append(cur)
    return [dict(e, **{"mrf-url": clean(e["mrf-url"])}) for e in ents if e.get("mrf-url")]


_CACHE = {}


def fetch_index(domain):
    """Returns (index_url, entries) or (index_url_tried, None) if no usable cms-hpt.txt."""
    if domain in _CACHE:
        return _CACHE[domain]
    res = (f"https://{domain}/cms-hpt.txt", None)
    alt = domain[4:] if domain.startswith("www.") else "www." + domain
    for url in (f"{sch}://{h}/cms-hpt.txt" for h in (domain, alt) for sch in ("https", "http")):
        r = None
        for attempt in range(2):  # one retry: slow or flaky hospital sites often answer the second time
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True, verify=False)
                break
            except requests.RequestException:
                r = None
        if r is None:
            continue
        body = r.text.lstrip()
        if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower() and not body.startswith("<"):
            ents = parse(r.text)
            if ents:
                res = (url, ents)
                break
    _CACHE[domain] = res
    return res


def current_link(row):
    """Pick this hospital's file from its index. Returns dict(index_url, index_found, mrf_url, how)."""
    known = clean(row.get("mrf_url", "")) if row.get("mrf_url") else ""
    dom = index_domain(row)
    if not dom:
        return dict(index_url="", index_found=False, mrf_url=known, how="known link (no index domain)" if known else "")
    idx, ents = fetch_index(dom)
    if not ents:
        return dict(index_url=idx, index_found=False, mrf_url=known, how="known link (index missing)" if known else "")
    if known and any(e["mrf-url"] == known for e in ents):
        return dict(index_url=idx, index_found=True, mrf_url=known, how="index lists known link")
    scored = sorted(((score(row["name"], e.get("location-name", "")), e) for e in ents),
                    key=lambda x: x[0], reverse=True)
    top_sc, top = scored[0]
    if top_sc >= 80 or len(ents) == 1:
        return dict(index_url=idx, index_found=True, mrf_url=top["mrf-url"],
                    how=f"index match '{top.get('location-name', '')[:60]}' ({round(top_sc)})")
    return dict(index_url=idx, index_found=True, mrf_url=known,
                how="known link (no confident index match)" if known else "")
