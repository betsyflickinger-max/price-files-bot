"""Copy everything in Cloudflare R2 to Backblaze B2 (Oct 28, 2026; Betsy said yes).

Why: R2 was switched off over a card problem (Oct 26) and B2 is about $82/month for the same ~11.8 TB vs ~$280+.
Plan: copy all of R2 to B2, then switch the bot over (repo variables STORAGE_ENDPOINT / STORAGE_REGION and the
storage keys). See claude/storage-options-oct27.md in the project.

What this does: rents ONE DigitalOcean machine (tag "upfront-copy", so the queue's cleanup never touches it) that runs
rclone COPY (never sync, never deletes anything on either side), twice: the second pass catches files the bot wrote
during the first. Then it compares both sides, posts the result on a GitHub issue and returns itself.
Progress is posted on the issue every 2 hours. Started automatically by the storage alarm the first time R2 answers
again, or by hand (workflow "Copy storage to Backblaze").
"""
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import store  # noqa: E402

REPO = os.environ["GITHUB_REPOSITORY"]
GH = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
API = f"https://api.github.com/repos/{REPO}"
DO = "https://api.digitalocean.com/v2"
TITLE = "Copying storage to Backblaze"
SIZES = ["s-8vcpu-16gb", "s-8vcpu-16gb-amd", "s-8vcpu-16gb-intel", "s-4vcpu-8gb"]
REGIONS = ["nyc3", "nyc1", "sfo3"]
e = store.env


def existing():
    r = requests.get(f"{API}/issues", headers=GH, params={"state": "all", "per_page": 100}, timeout=60)
    return next((i for i in r.json() if i.get("title", "").startswith(TITLE) and "pull_request" not in i), None)


def user_data(issue):
    v = {k: e(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "B2_BUCKET",
                           "B2_ACCESS_KEY_ID", "B2_SECRET_ACCESS_KEY", "DIGITALOCEAN_TOKEN", "BOT_GITHUB_TOKEN")}
    return f"""#!/bin/bash
exec > /var/log/copy-setup.log 2>&1
export HOME=/root
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_R2_ENDPOINT='https://{v['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com'
export RCLONE_CONFIG_R2_ACCESS_KEY_ID='{v['R2_ACCESS_KEY_ID']}' RCLONE_CONFIG_R2_SECRET_ACCESS_KEY='{v['R2_SECRET_ACCESS_KEY']}'
export RCLONE_CONFIG_B2_TYPE=b2 RCLONE_CONFIG_B2_ACCOUNT='{v['B2_ACCESS_KEY_ID']}' RCLONE_CONFIG_B2_KEY='{v['B2_SECRET_ACCESS_KEY']}'
SRC='r2:{v['R2_BUCKET']}'; DST='b2:{v['B2_BUCKET']}'
GHT='{v['BOT_GITHUB_TOKEN']}'; DOT='{v['DIGITALOCEAN_TOKEN']}'
say() {{ python3 -c 'import json,sys; print(json.dumps({{"body": sys.stdin.read()}}))' | \\
  curl -s -o /dev/null -X POST -H "Authorization: Bearer $GHT" -H "Accept: application/vnd.github+json" \\
  https://api.github.com/repos/{REPO}/issues/{issue}/comments -d @-; }}
apt-get update -y && apt-get install -y curl unzip python3
curl -s https://rclone.org/install.sh | bash
OPTS="--transfers 48 --checkers 64 --retries 10 --low-level-retries 20 --b2-chunk-size 96M --stats 10m --stats-one-line --stats-log-level NOTICE --log-level NOTICE"
# Oct 30: progress also saved to Backblaze (bot/migration/logs/) every 2 hours, so it can be read even if the GitHub
# comment can't be posted (the first copy, Oct 29, posted nothing and stopped when Backblaze's free 10 GB cap was hit)
( while sleep 7200; do echo "Still copying. Latest: $(grep -a 'Transferred' /var/log/copy.log | tail -1)" | say;
  rclone copy /var/log "$DST/bot/migration/logs" --include 'copy*.log' --include 'check.log' -q || true; done ) &
TICK=$!
rclone copy /var/log "$DST/bot/migration/logs" --include 'copy-setup.log' -q || true
echo "Machine is up; copy pass 1 starting (copy only: nothing is deleted on either side)." | say
rclone copy "$SRC" "$DST" $OPTS --log-file /var/log/copy.log; P1=$?
echo "Pass 1 finished (exit $P1): $(grep -a 'Transferred' /var/log/copy.log | tail -2 | tr '\\n' ' '). Pass 2 catches anything written meanwhile." | say
rclone copy "$SRC" "$DST" $OPTS --log-file /var/log/copy2.log; P2=$?
kill $TICK
rclone check "$SRC" "$DST" --one-way --size-only --checkers 64 --log-file /var/log/check.log --log-level NOTICE; C=$?
MISS=$(grep -ac 'not in' /var/log/check.log); DIFF=$(grep -ac 'sizes differ' /var/log/check.log)
{{ echo "Copy finished. Pass 2: exit $P2. Check (every R2 file is in Backblaze with the same size): exit $C, $MISS missing, $DIFF size differences."
   echo; echo '```'; grep -a 'Transferred\\|Errors\\|Checks' /var/log/copy2.log | tail -4; tail -5 /var/log/check.log; echo '```'
   echo; echo "Next: switch the bot to Backblaze (repo variables STORAGE_ENDPOINT / STORAGE_REGION and the storage keys)."; }} | say
rclone copy /var/log "$DST/bot/migration/logs" --include 'copy*.log' --include 'check.log' || true
ID=$(curl -s http://169.254.169.254/metadata/v1/id)
curl -s -X DELETE -H "Authorization: Bearer $DOT" https://api.digitalocean.com/v2/droplets/$ID
"""


def main():
    force = os.environ.get("FORCE") == "true"
    missing = [k for k in ("B2_BUCKET", "B2_ACCESS_KEY_ID", "B2_SECRET_ACCESS_KEY", "DIGITALOCEAN_TOKEN") if not e(k)]
    if missing:
        sys.exit(f"missing secrets: {missing}")
    old = existing()
    if old and not force:
        print(f"Already started (issue #{old['number']}); run with force to start again.")
        return
    s3, b = store.r2()
    try:
        s3.list_objects_v2(Bucket=b, MaxKeys=1)
    except Exception as ex:
        sys.exit(f"R2 isn't answering yet, so there's nothing to copy: {ex}")
    issue = requests.post(f"{API}/issues", headers=GH, timeout=60, json={"title": f"{TITLE} (R2 → B2)", "assignees": [REPO.split("/")[0]], "body": (
        "Copying everything in Cloudflare R2 to Backblaze B2 on one rented machine. **Copy only: nothing is deleted "
        "on either side.** Two passes (the second catches files written during the first), then a check that every "
        "R2 file is in Backblaze. Expect about 1–2 days for ~11.8 TB. Progress is posted here every 2 hours; the "
        "machine returns itself when done.")}).json()["number"]
    h = {"Authorization": f"Bearer {e('DIGITALOCEAN_TOKEN')}", "Content-Type": "application/json"}
    r = None
    for size in SIZES:
        for region in REGIONS:
            r = requests.post(f"{DO}/droplets", headers=h, timeout=60, json=dict(
                name=f"upfront-copy-r2-b2-{issue}", region=region, size=size, image="ubuntu-24-04-x64",
                tags=["upfront", "upfront-copy"], user_data=user_data(issue), monitoring=True))
            if r.status_code != 422:
                break
        if r.status_code != 422:
            break
    if r.status_code >= 300:
        requests.post(f"{API}/issues/{issue}/comments", headers=GH, timeout=60,
                      json={"body": f"Couldn't rent the copy machine: DigitalOcean {r.status_code} {r.text[:200]}"})
        sys.exit(f"DigitalOcean {r.status_code}: {r.text[:300]}")
    d = r.json()["droplet"]
    requests.post(f"{API}/issues/{issue}/comments", headers=GH, timeout=60, json={
        "body": f"Rented machine `{d['name']}` ({d['size_slug']}, {d['region']['slug']}). Setting up rclone now."})
    print(f"copy started: issue #{issue}, machine {d['name']}")


if __name__ == "__main__":
    main()
