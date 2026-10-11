"""Monthly website refresh, run by .github/workflows/site-build.yml every 6 hours.

One build per month (build id = YYYY-MM). State lives in R2 site/builds/<id>/state.json. Stages:
  wait     start once it is the 8th or later (insurers post early in the month; the insurer bot needs a few days),
           no DFW insurer file is still being read, and there are free machines (always leaves RESERVE free)
  build1   rented machines: every_code.py over data/full/rates (2 machines, opposite ends), and hosp_every.py over
           the DFW hospital files that changed since last month (unchanged files reuse last month's rows)
  build2   one machine: h2_build.py -> per-code hospital files + hosp_index.json
  publish  on the GitHub runner: merge into the page, run check_build.py against the live page, point
           site/every/CURRENT at the new build and deploy with wrangler. If a check fails: nothing goes live, an issue opens.
Dead machines are relaunched (up to 3 times per role). Machines delete themselves when done.
Env: R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET DIGITALOCEAN_TOKEN [CLOUDFLARE_API_TOKEN] [FORCE=1] [BUILD=YYYY-MM]"""
import csv, io, json, os, subprocess, sys, tarfile, time, urllib.request
import boto3

E = os.environ
B = E.get('R2_BUCKET', 'upfrontdfwdata')
s3 = boto3.client('s3', endpoint_url=f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=E['R2_ACCESS_KEY_ID'],
                  aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name='auto')
HERE = os.path.dirname(os.path.abspath(__file__))
BID = E.get('BUILD') or time.strftime('%Y-%m')
SP = f'site/builds/{BID}/'
TOUT = f'site/every/{BID}/'
HPFX = 'data/hosp_every/'
LIMIT, RESERVE = 10, 1
SIZE_INS, SIZE_HOSP, SIZE_H2 = 's-8vcpu-16gb-amd', 's-4vcpu-8gb-intel', 's-8vcpu-16gb-amd'
REGIONS = ['nyc3', 'sfo3', 'nyc1']


def log(*a): print(time.strftime('%H:%M:%S'), *a, flush=True)
def exists(k):
    try: s3.head_object(Bucket=B, Key=k); return True
    except Exception: return False
def getj(k, default=None):
    try: return json.loads(s3.get_object(Bucket=B, Key=k)['Body'].read())
    except Exception: return default
def putj(k, v): s3.put_object(Bucket=B, Key=k, Body=json.dumps(v, indent=1).encode())
def ls(p): return [o['Key'] for pg in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix=p) for o in pg.get('Contents', [])]


# ---------- DigitalOcean ----------
def do(method, path, body=None):
    req = urllib.request.Request('https://api.digitalocean.com/v2' + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={'Authorization': 'Bearer ' + E['DIGITALOCEAN_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r: return json.loads(r.read() or b'{}')
def droplets(): return do('GET', '/droplets?per_page=200')['droplets']
def free_slots(): return LIMIT - RESERVE - len(droplets())

ENV_KEYS = ['R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY']
def userdata(script, env):
    envs = '\n'.join(f"export {k}='{E[k]}'" for k in ENV_KEYS) + f"\nexport R2_BUCKET={B} DO_TOKEN='{E['DIGITALOCEAN_TOKEN']}'\n" + \
        '\n'.join(f"export {k}='{v}'" for k, v in env.items())
    return f"""#!/bin/bash
exec > /root/boot.log 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update -y && apt-get install -y python3-venv curl
python3 -m venv /root/v && /root/v/bin/pip install -q duckdb==1.1.3 boto3 ijson zstandard
cat > /root/env.sh <<'ENV'
{envs}
ENV
cat > /root/run.sh <<'RUN'
#!/bin/bash
. /root/env.sh
mkdir -p /root/h /d && cd /root/h
/root/v/bin/python -c "
import boto3,os
s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],region_name='auto')
s3.download_file(os.environ['R2_BUCKET'],'{SP}bundle.tgz','/root/b.tgz')"
tar xzf /root/b.tgz -C /root/h
{script}
/root/v/bin/python -c "
import boto3,os
s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],region_name='auto')
s3.upload_file('/root/run.log',os.environ['R2_BUCKET'],'{SP}logs/'+os.environ['ROLE']+'.log')"
ID=$(curl -s http://169.254.169.254/metadata/v1/id)
curl -s -X DELETE -H "Authorization: Bearer $DO_TOKEN" https://api.digitalocean.com/v2/droplets/$ID
RUN
chmod +x /root/run.sh
setsid nohup /root/run.sh < /dev/null > /root/run.out 2>&1 &
"""
def launch(role, size, script, env):
    name = f'upfront-site-{BID}-{role}'
    ud = userdata(script, dict(env, ROLE=role))
    for region in REGIONS:
        try:
            d = do('POST', '/droplets', {'name': name, 'region': region, 'size': size, 'image': 'ubuntu-24-04-x64', 'user_data': ud, 'tags': ['site-build']})
            log('launched', name, region, d['droplet']['id']); return True
        except urllib.error.HTTPError as e:
            log('launch failed', name, region, e.read()[:150])
    return False


RUN_INS = "for t in $(seq 1 10); do /root/v/bin/python /root/h/every_code.py >> /root/run.log 2>&1 && break; sleep 5; done"
RUN_HOSP = "for t in 1 2 3; do /root/v/bin/python /root/h/hosp_every.py >> /root/run.log 2>&1 && break; sleep 5; done"
RUN_H2 = """/root/v/bin/python - >> /root/run.log 2>&1 <<'PY'
import boto3, os, json
from concurrent.futures import ThreadPoolExecutor
E = os.environ
s3 = boto3.client('s3', endpoint_url='https://' + E['R2_ACCOUNT_ID'] + '.r2.cloudflarestorage.com', aws_access_key_id=E['R2_ACCESS_KEY_ID'], aws_secret_access_key=E['R2_SECRET_ACCESS_KEY'], region_name='auto')
jobs = json.loads(s3.get_object(Bucket=E['R2_BUCKET'], Key=E['JOBS_KEY'])['Body'].read())
os.makedirs('/root/rows', exist_ok=True)
with ThreadPoolExecutor(16) as P: list(P.map(lambda j: s3.download_file(E['R2_BUCKET'], E['HPFX'] + 'rows/' + j['id'] + '.parquet', '/root/rows/' + j['id'] + '.parquet'), jobs))
s3.download_file(E['R2_BUCKET'], E['PAGE_KEY'], '/root/page.html')
PY
/root/v/bin/python /root/h/h2_build.py '/root/rows/*.parquet' /root/h/payer_name_mapping.csv /root/page.html /d >> /root/run.log 2>&1
/root/v/bin/python -c "
import boto3,os
s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],region_name='auto')
s3.upload_file('/d/hosp_index.json',os.environ['R2_BUCKET'],os.environ['SP']+'hosp_index.json')" >> /root/run.log 2>&1"""


# ---------- hospital jobs: newest file per DFW hospital, reuse rows when unchanged ----------
def hospital_jobs():
    base = getj('site/builds/hosp_jobs_base.json', [])
    latest = {}
    for k in sorted(x for x in ls('bot/status/') if x.endswith('/TX.csv')):
        for r in csv.DictReader(io.StringIO(s3.get_object(Bucket=B, Key=k)['Body'].read().decode())):
            if r.get('r2_key') and r.get('sha256'): latest[r['mrf_url']] = r
    jobs = []
    for j in base:
        r = latest.get(j['url'])
        jobs.append(dict(j, id=r['sha256'][:16], key=r['r2_key'], size=int(r.get('size') or 0)) if r else j)
    cached = {k.rsplit('/', 1)[1][:-8] for k in ls(HPFX + 'rows/')}
    todo = [j for j in jobs if j['id'] not in cached]
    return jobs, todo


def bundle():
    p = '/tmp/site_bundle.tgz'
    with tarfile.open(p, 'w:gz') as t:
        for f in os.listdir(HERE):
            if f.endswith(('.py', '.csv', '.tsv')): t.add(os.path.join(HERE, f), arcname=f)
    s3.upload_file(p, B, SP + 'bundle.tgz')


def insurer_bot_busy():
    m = s3.get_object(Bucket=B, Key='bot/insurers/manifest.csv')['Body'].read().decode()
    return sum(1 for r in csv.DictReader(io.StringIO(m)) if r.get('status') == 'running')


def alive(role): return any(d['name'] == f'upfront-site-{BID}-{role}' for d in droplets())


def ensure(st, role, size, script, env, done):
    """keep one machine per role running until its work is done; relaunch dead ones up to 3 times"""
    if done or alive(role): return
    n = st['launches'].get(role, 0)
    if n >= 4: st['problems'].append(f'{role} failed 4 times'); return
    if free_slots() < 1: log('no free machine for', role); return
    if launch(role, size, script, env): st['launches'][role] = n + 1


def issue(title, body):
    try:
        subprocess.run(['gh', 'issue', 'create', '--title', title, '--body', body], check=False)
    except Exception as e: log('issue failed', e)


def main():
    st = getj(SP + 'state.json', {'stage': 'wait', 'launches': {}, 'problems': [], 'log': []})
    log('build', BID, 'stage', st['stage'])
    if st['stage'] == 'wait':
        if int(time.strftime('%d')) < 8 and not E.get('FORCE'): log('waiting for the 8th'); return
        busy = insurer_bot_busy()
        if busy and not E.get('FORCE'): log(busy, 'insurer files still being read; waiting'); return
        if free_slots() < 2: log('not enough free machines; waiting'); return
        jobs, todo = hospital_jobs()
        putj(SP + 'hosp_jobs.json', jobs); putj(SP + 'hosp_todo.json', todo)
        s3.copy_object(Bucket=B, Key=SP + 'page_before.html', CopySource={'Bucket': B, 'Key': 'site/deploy/price-finder.html'})
        bundle()
        st.update(stage='build1', started=time.strftime('%Y-%m-%d %H:%M'), hosp_changed=len(todo), hosp_total=len(jobs))
        log(len(todo), 'of', len(jobs), 'hospital files changed')
    if st['stage'] == 'build1':
        common = dict(TOUT=TOUT, OUTP=SP, WK=f'work/every/{BID}/', ACACHE='work/every/Acache/', NOXPART='1', PIECE_GB='0.4',
                      MAXPIECES='3', THREADS='4', MEM='9GB', WORKERS='3', WTHREADS='1', WMEM='2500MB', WORK='/d')
        ins_done = exists(TOUT + 'embed.json')
        ensure(st, 'ins1', SIZE_INS, RUN_INS, common, ins_done)
        ensure(st, 'ins2', SIZE_INS, RUN_INS, dict(common, AORDER='desc', BORDER='desc'), ins_done)
        todo = getj(SP + 'hosp_todo.json', [])
        hdone = {k.rsplit('/', 1)[1][:-5] for k in ls(HPFX + 'done/')}
        left = [j for j in todo if j['id'] not in hdone]
        if todo:
            for sh in range(2 if len(todo) > 10 else 1):
                ensure(st, f'hosp{sh}', SIZE_HOSP, RUN_HOSP, dict(JOBS_KEY=SP + 'hosp_todo.json', SHARD=str(sh), SHARDS='2' if len(todo) > 10 else '1', HPFX=HPFX), not left)
        log('insurer part', 'done' if ins_done else 'running', '| hospital files left', len(left))
        if ins_done and not left: st['stage'] = 'build2'
    if st['stage'] == 'build2':
        done = exists(SP + 'hosp_index.json')
        ensure(st, 'h2', SIZE_H2, RUN_H2, dict(JOBS_KEY=SP + 'hosp_jobs.json', HPFX=HPFX, PAGE_KEY=SP + 'page_before.html', SP=SP,
                                                UPLOAD='1', HKEY=TOUT + 'h/', NP='8', MEM='11GB'), done)
        if done: st['stage'] = 'publish'
    if st['stage'] == 'publish':
        publish(st)
    putj(SP + 'state.json', st)


def publish(st):
    os.makedirs('/tmp/pub', exist_ok=True)
    for k, f in [(SP + 'page_before.html', 'old.html'), (TOUT + 'embed.json', 'embed.json'), (SP + 'hosp_index.json', 'hosp.json'),
                 ('reference/site/med_all.json', 'med.json'), ('reference/site/msdrg_table5.txt', 't5.txt')]:
        s3.download_file(B, k, '/tmp/pub/' + f)
    import shutil; shutil.copy('/tmp/pub/old.html', '/tmp/pub/new.html')
    py = sys.executable
    subprocess.run([py, f'{HERE}/merge_page.py', '/tmp/pub/new.html', '/tmp/pub/embed.json', '/tmp/pub/hosp.json'], check=True)
    subprocess.run([py, f'{HERE}/apply_names_med.py', '/tmp/pub/new.html', '/tmp/pub/med.json', f'{HERE}/plain_names_cpt.tsv', '/tmp/pub/t5.txt'], check=True)
    chk = subprocess.run([py, f'{HERE}/check_build.py', '/tmp/pub/old.html', '/tmp/pub/new.html'], capture_output=True, text=True)
    print(chk.stdout)
    s3.upload_file('/tmp/pub/new.html', B, SP + 'page_new.html')
    if chk.returncode != 0:
        st['stage'] = 'held'
        issue(f'Website refresh {BID} held: checks failed', f'Nothing went live.\n\n```\n{chk.stdout}\n```\nNew page saved to R2 `{SP}page_new.html`.')
        return
    if not E.get('CLOUDFLARE_API_TOKEN'):
        st['stage'] = 'ready'
        issue(f'Website refresh {BID} ready, not deployed', 'Checks passed but the CLOUDFLARE_API_TOKEN secret is not set, so it could not deploy.\n\n```\n' + chk.stdout + '\n```')
        return
    # deploy: site files come from R2 (the site is not in this public repo)
    s3.download_file(B, 'site/deploy/current.tgz', '/tmp/pub/site.tgz')
    shutil.rmtree('/tmp/pub/deploy', ignore_errors=True)
    with tarfile.open('/tmp/pub/site.tgz') as t: t.extractall('/tmp/pub/deploy')
    shutil.copy('/tmp/pub/new.html', '/tmp/pub/deploy/site/price-finder/index.html')
    try: prev = s3.get_object(Bucket=B, Key='site/every/CURRENT')['Body'].read().decode().strip()
    except Exception: prev = '2026-10-08'
    s3.put_object(Bucket=B, Key='site/every/CURRENT', Body=BID.encode())
    env = dict(os.environ, CLOUDFLARE_ACCOUNT_ID=E['R2_ACCOUNT_ID'])
    r = subprocess.run(['npx', '--yes', 'wrangler@3.114.0', 'pages', 'deploy', 'site', '--project-name', 'upfront-dfw-preview', '--branch', 'main', '--commit-dirty=true'],
                       cwd='/tmp/pub/deploy', env=env, capture_output=True, text=True)
    print(r.stdout[-2000:], r.stderr[-2000:])
    if r.returncode != 0:
        s3.put_object(Bucket=B, Key='site/every/CURRENT', Body=prev.encode())
        st['stage'] = 'held'; issue(f'Website refresh {BID}: deploy failed', '```\n' + r.stderr[-3000:] + '\n```'); return
    s3.upload_file('/tmp/pub/new.html', B, 'site/deploy/price-finder.html')
    with tarfile.open('/tmp/pub/site.tgz', 'w:gz') as t: t.add('/tmp/pub/deploy', arcname='.')
    s3.upload_file('/tmp/pub/site.tgz', B, 'site/deploy/current.tgz')
    st['stage'] = 'live'; st['live_at'] = time.strftime('%Y-%m-%d %H:%M')
    issue(f'Website refresh {BID} is live', '```\n' + chk.stdout + '\n```')


if __name__ == '__main__':
    main()
