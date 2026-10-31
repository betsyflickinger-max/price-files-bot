"""Screenshots of the live site (Oct 31, 2026). The live site is password-protected, so this opens the deployed bundle
(site/deploy/current.tgz) in a browser on the GitHub machine, with the control center's data read from R2 the same way the
site's /control/api/data/<id> does. Pictures go to /tmp/shots (the workflow keeps them as the run's "shots" download).

  python site/live_shots.py            (workflow: .github/workflows/live-shots.yml)

Today's set: the states-first area dropdowns (site/area_menu.py) on the homepage, the Price Finder and the control center.
"""
import http.server, io, os, re, socketserver, sys, tarfile, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import procedure_pages as pp

OUT, ROOT = '/tmp/shots', '/tmp/live_site'
store = pp.Store('/tmp/live')
os.makedirs(OUT, exist_ok=True)
tarfile.open(fileobj=io.BytesIO(store.s3.get_object(Bucket=store.B, Key='site/deploy/current.tgz')['Body'].read())).extractall(ROOT, filter='fully_trusted')
SITE = os.path.join(ROOT, 'site')


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=SITE, **k)
    def log_message(self, *a): pass
    def do_GET(self):
        m = re.match(r'^/control/api/data/([a-z0-9_]{1,64})$', self.path.split('?')[0])
        if m:
            try:
                b = store.s3.get_object(Bucket=store.B, Key=f'control/data/{m.group(1)}.json')['Body'].read()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            except Exception:
                self.send_error(404)
            return
        if self.path.startswith('/control/api/'):
            self.send_error(404); return
        return super().do_GET()


socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = socketserver.ThreadingTCPServer(('127.0.0.1', 8811), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:8811'

from playwright.sync_api import sync_playwright


def open_menu(p, sel, hover='Texas', name='shot'):
    b = p.locator(f'{sel} + .am-btn').first
    b.scroll_into_view_if_needed(); p.wait_for_timeout(300)
    p.screenshot(path=f'{OUT}/{name}-closed.png')
    b.click(); p.wait_for_timeout(250)
    row = p.locator('.am-pop .am-it', has_text=hover).first
    row.scroll_into_view_if_needed(); row.hover(); p.wait_for_timeout(400)
    p.screenshot(path=f'{OUT}/{name}-open.png')
    p.keyboard.press('Escape')


with sync_playwright() as pw:
    br = pw.chromium.launch()
    log = []
    def page(w, h, mobile=False):
        p = br.new_page(viewport={'width': w, 'height': h}, is_mobile=mobile, has_touch=mobile)
        p.on('pageerror', lambda e: log.append(f'{p.url}: {e}'))
        return p
    p = page(1280, 860)
    p.goto(BASE + '/'); p.wait_for_timeout(1500)
    open_menu(p, '#natArea', 'Texas', '1-home')
    open_menu(p, '#natArea', 'California', '2-home-california')
    p = page(1280, 860)
    p.goto(BASE + '/price-finder/'); p.wait_for_timeout(6000)
    open_menu(p, 'select#area', 'Texas', '3-price-finder')
    m = page(390, 800, True)
    m.goto(BASE + '/'); m.wait_for_timeout(1500)
    m.locator('#natArea + .am-btn').first.scroll_into_view_if_needed(); m.locator('#natArea + .am-btn').first.tap(); m.wait_for_timeout(250)
    m.locator('.am-pop .am-it', has_text='Texas').first.tap(); m.wait_for_timeout(300)
    m.screenshot(path=f'{OUT}/4-phone-home-texas.png')
    c = page(1360, 900)
    c.goto(BASE + '/control/'); c.wait_for_timeout(6000)
    open_menu(c, '#market', 'Texas', '5-control-prices')
    for tab, sel, n in (('network', '#nmarket', '6-control-network'), ('cash', '#c-mkt', '7-control-cash')):
        try:
            c.locator(f'[data-tab="{tab}"]').first.click(); c.wait_for_timeout(5000)
            open_menu(c, sel, 'Texas', n)
        except Exception as e:
            log.append(f'{n}: {type(e).__name__}: {str(e)[:200]}')
    br.close()
print('\n'.join(['# Live screenshots', ''] + [f'- {f}' for f in sorted(os.listdir(OUT))] + ['', 'page errors:'] + (log or ['none'])))
