"""Decide whether a hospital file changed, without downloading it when we can.

Order (same idea as 06g_check_changed.py; adapted from lkowalcz/hospital-price-history, CC0):
  1. Ask the server for the file's headers (HEAD, or a 1-byte range GET if HEAD is refused).
     Same ETag as last time -> unchanged.  Same Last-Modified AND same size -> unchanged.
  2. No usable headers -> fetch two small samples (first and last 64 KB) and fingerprint them with
     the size. Same fingerprint -> unchanged.
  3. Anything else -> download the whole file and compare SHA-256 (the only proof).
Weekly fallback: if a file has been called "unchanged" only by header/sample for 7+ days, download
it in full anyway, because some servers send stale headers.
"""
import hashlib
import re
import subprocess

import requests

from discover import UA

SAMPLE = 64 * 1024
FULL_EVERY_DAYS = 7


def _req(method, url, **kw):
    return requests.request(method, url, headers={"User-Agent": UA, **kw.pop("headers", {})},
                            timeout=60, allow_redirects=True, verify=False, stream=True, **kw)


def headers(url):
    """Returns dict(ok, http, etag, last_modified, size, ranges, final_url, error)."""
    out = dict(ok=False, http="", etag="", last_modified="", size="", ranges=False, final_url=url, error="")
    try:
        r = _req("HEAD", url)
        r.close()
    except requests.RequestException:
        r = None
    try:
        if r is None or r.status_code >= 400:  # some servers answer HEAD with 403/404/405 but serve GET fine
            r = _req("GET", url, headers={"Range": "bytes=0-0"})
            r.close()
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return out
    out["http"] = str(r.status_code)
    out["final_url"] = r.url
    if r.status_code >= 400:
        out["error"] = f"HTTP {r.status_code}"
        return out
    h = r.headers
    out["ok"] = True
    out["etag"] = h.get("ETag", "").replace("W/", "").strip('"')
    out["last_modified"] = h.get("Last-Modified", "")
    cr = h.get("Content-Range", "")
    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
        out["size"], out["ranges"] = cr.rsplit("/", 1)[1], True
    elif r.status_code == 200 and "gzip" not in h.get("Content-Encoding", "").lower():
        out["size"] = h.get("Content-Length", "")  # HEAD, or a server that ignored the range
    out["ranges"] = out["ranges"] or h.get("Accept-Ranges", "").lower() == "bytes"
    # A web page instead of a file (login wall, "page not found" page served with 200)
    if "text/html" in h.get("Content-Type", "").lower():
        out["html"] = True
    return out


def sample_fingerprint(url, size):
    """SHA-256 of size + first 64 KB + last 64 KB. Empty string if the server won't do ranges."""
    if not size or not size.isdigit():
        return ""
    n = int(size)
    h = hashlib.sha256(size.encode())
    spans = [f"bytes=0-{min(SAMPLE, n) - 1}"]
    if n > 2 * SAMPLE:
        spans.append(f"bytes={n - SAMPLE}-{n - 1}")
    for span in spans:
        try:
            r = _req("GET", url, headers={"Range": span, "Accept-Encoding": "identity"})
            if r.status_code != 206:
                r.close()
                return ""
            h.update(r.raw.read(SAMPLE + 1, decode_content=False))
            r.close()
        except requests.RequestException:
            return ""
    return h.hexdigest()


def unchanged_by_headers(prev, now):
    """True/why if headers alone prove the file is the same as last time."""
    if not prev or not prev.get("sha256"):
        return False, ""
    if now["etag"] and now["etag"] == prev.get("etag"):
        return True, "same ETag"
    if now["last_modified"] and now["size"] and now["last_modified"] == prev.get("last_modified") \
            and now["size"] == prev.get("size"):
        return True, "same Last-Modified and size"
    return False, ""


def download(url, out, max_time=5400, insecure=False):
    """curl with retries. Returns (ok, error_text, cert_unverified)."""
    cmd = ["curl", "-sSL", "--fail", "--retry", "3", "--retry-delay", "20", "--retry-all-errors",
           "--connect-timeout", "60", "-m", str(max_time), "-A", UA,
           "-H", "Accept: text/csv,application/json,application/zip,text/plain,*/*;q=0.8",
           "-H", "Accept-Language: en-US,en;q=0.9", "-o", out, url]
    if insecure:
        cmd.insert(1, "-k")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return True, "", insecure
    err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"curl exit {r.returncode}"
    if "(60)" in r.stderr and not insecure:  # bad certificate on a public file: fetch anyway, flag it
        ok, e2, _ = download(url, out, max_time, insecure=True)
        if ok:
            return True, "", True
    return False, err[:300], False


def sha256_file(path):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for ch in iter(lambda: f.read(1 << 24), b""):
            h.update(ch)
            n += len(ch)
    return h.hexdigest(), n


def looks_like_web_page(path):
    with open(path, "rb") as f:
        head = f.read(1024).lstrip(b"\xef\xbb\xbf \r\n\t").lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or bool(re.match(rb"<(head|body)\b", head))
