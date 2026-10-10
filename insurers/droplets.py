"""Rent and return DigitalOcean machines. Every machine is tagged so the bot can always find and delete it."""
import os
import time

import requests

API = "https://api.digitalocean.com/v2"
TAG = "upfront-bot"
REGION = "nyc3"            # near AWS us-east-1, where most insurer files are hosted
IMAGE = "ubuntu-24-04-x64"
DEFAULT_SIZE = "s-4vcpu-8gb"  # 4 vCPU / 8 GB / 160 GB disk; new accounts can't rent 8 vCPU


def _h():
    tok = "".join(os.environ.get("DIGITALOCEAN_TOKEN", "").split())
    if not tok:
        raise RuntimeError("DIGITALOCEAN_TOKEN is not set")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def create(name, user_data, size=DEFAULT_SIZE):
    body = dict(name=name, region=REGION, size=size, image=IMAGE, tags=["upfront", TAG], user_data=user_data,
                monitoring=False, ipv6=False)
    r = requests.post(f"{API}/droplets", json=body, headers=_h(), timeout=60)
    if r.status_code == 422 and size != DEFAULT_SIZE:  # size not available to this account: use the default
        body["size"] = DEFAULT_SIZE
        r = requests.post(f"{API}/droplets", json=body, headers=_h(), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"DigitalOcean {r.status_code}: {r.text[:300]}")
    return r.json()["droplet"]


def mine():
    """Every machine the bot rented that still exists: list of (id, name, created_at)."""
    r = requests.get(f"{API}/droplets", params={"tag_name": TAG, "per_page": 200}, headers=_h(), timeout=60)
    r.raise_for_status()
    return [(d["id"], d["name"], d["created_at"]) for d in r.json()["droplets"]]


def delete(droplet_id):
    for _ in range(3):
        r = requests.delete(f"{API}/droplets/{droplet_id}", headers=_h(), timeout=60)
        if r.status_code in (204, 404):
            return True
        time.sleep(10)
    return False


def room():
    """Free machine slots on the account (DigitalOcean caps how many can exist at once)."""
    acct = requests.get(f"{API}/account", headers=_h(), timeout=60).json()["account"]
    r = requests.get(f"{API}/droplets", params={"per_page": 200}, headers=_h(), timeout=60)
    r.raise_for_status()
    return int(acct.get("droplet_limit") or 3) - len(r.json()["droplets"])
