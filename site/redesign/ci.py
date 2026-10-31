"""V2 Redesign workflow steps (see .github/workflows/v2-redesign.yml). Never deploys to production."""
import io, json, os, sys, tarfile
from pathlib import Path
import boto3, requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
OUT = Path('/tmp/v2out'); OUT.mkdir(parents=True, exist_ok=True)
E = {k: "".join(os.environ.get(k, "").split()) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "CLOUDFLARE_API_TOKEN")}
s3 = boto3.client("s3", endpoint_url=(os.environ.get("STORAGE_ENDPOINT") or f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"),
                  aws_access_key_id=E["R2_ACCESS_KEY_ID"], aws_secret_access_key=E["R2_SECRET_ACCESS_KEY"], region_name=(os.environ.get("STORAGE_REGION") or "auto"))
B = E["R2_BUCKET"]
get = lambda k: s3.get_object(Bucket=B, Key=k)['Body'].read()


def pages_settings():
    """Names only: which env vars / bindings exist for production vs preview deployments."""
    h = {"Authorization": f"Bearer {E['CLOUDFLARE_API_TOKEN']}"}
    r = requests.get(f"https://api.cloudflare.com/client/v4/accounts/{E['R2_ACCOUNT_ID']}/pages/projects/upfront-dfw-preview", headers=h, timeout=60)
    j = r.json().get('result') or {}
    out = {}
    for env, c in (j.get('deployment_configs') or {}).items():
        out[env] = {k: (sorted(v.keys()) if isinstance(v, dict) else v) for k, v in c.items()
                    if k in ('env_vars', 'r2_buckets', 'kv_namespaces', 'd1_databases', 'compatibility_date', 'compatibility_flags', 'services')}
        # r2 bucket NAMES are not secret, but print only binding names to be safe
    out['production_branch'] = j.get('production_branch')
    out['domains'] = j.get('domains')
    print('## Pages settings (names only)\n```\n' + json.dumps(out, indent=1) + '\n```')
    return out


def devkit():
    raw = get('site/deploy/current.tgz')
    print(f'current.tgz {len(raw)/1e6:.1f} MB')
    t = tarfile.open(fileobj=io.BytesIO(raw))
    keep = ('./functions/', './site/index.html', './site/v2/', './site/folders/', './site/price-finder/', './site/plan-calculator/',
            './site/units.css', './site/looks/', './site/cities.json', './site/start-here/', './site/hospitals/index.html',
            './site/costs/dallas-fort-worth/colonoscopy/', './site/costs/dallas-fort-worth/mri-knee/', './site/costs/index.html',
            './site/story/', './site/assets/', './site/hcf-t.js', './site/area-menu.js', './site/_headers', './site/_redirects',
            './site/404.html', './site/methodology/', './site/insurers/index.html', './site/employers/index.html')
    names = t.getnames()
    # every top-level site entry (to see the route families)
    tops = sorted({n.split('/')[2] for n in names if n.startswith('./site/') and n.count('/') >= 2})
    (OUT / 'site_top.txt').write_text('\n'.join(tops))
    hp = sorted({'/'.join(n.split('/')[:4]) for n in names if n.startswith('./site/hospitals/')})[:40]
    (OUT / 'hospitals_sample.txt').write_text('\n'.join(hp))
    sel = [m for m in t.getmembers() if m.name.startswith(keep) and (m.isfile() or m.isdir())]
    # first two hospital profile pages + Parkland
    prof = [m for m in t.getmembers() if m.name.startswith('./site/hospitals/') and m.name.endswith('index.html')
            and ('parkland' in m.name.lower() or 'dallas-county' in m.name.lower())]
    sel += prof[:6]
    big = [m for m in sel if m.isfile() and m.size > 40e6]
    sel = [m for m in sel if m not in big]
    with tarfile.open(OUT / 'devkit.tgz', 'w:gz') as o:
        for m in sel:
            f = t.extractfile(m) if m.isfile() else None
            o.addfile(m, f)
    print(f'packed {len(sel)} entries; skipped big: {[m.name for m in big]}')
    cur = get('site/every/CURRENT').decode().strip()
    print('site/every/CURRENT =', cur)
    d = OUT / 'pf'; d.mkdir(exist_ok=True)
    for c in os.environ.get('CODES', '45378').split(','):
        for kind in ('t', 'h'):
            try:
                (d / f'{kind}_{c}.txt').write_bytes(get(f'site/every/{cur}/{kind}/{c}.txt'))
            except Exception as ex:
                print('missing', kind, c, repr(ex)[:100])
    for k in ('geo.json', 'npis.json', 'ml.json'):
        try:
            (d / k).write_bytes(get(f'site/every/{cur}/{k}'))
        except Exception as ex:
            print('missing', k, repr(ex)[:80])
    try:
        (OUT / 'costs_state.json').write_bytes(get('site/deploy/costs_state.json'))
    except Exception as ex:
        print('costs_state missing', ex)
    try:
        pages_settings()
    except Exception as ex:
        print('pages settings failed:', repr(ex)[:200])


if __name__ == '__main__':
    mode = os.environ.get('MODE', 'devkit')
    if mode == 'devkit':
        devkit()
    elif mode == 'preview':
        import build_preview
        build_preview.main(OUT)
    else:
        sys.exit('unknown mode')
