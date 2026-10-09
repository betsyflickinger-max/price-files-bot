"""Cloud storage (Cloudflare R2) and Internet Archive helpers. Keys come from GitHub repo secrets."""
import csv
import io
import os

import boto3
from botocore.exceptions import ClientError

R2_PREFIX = "bot/"  # everything the bot writes besides originals lives under bot/


def env(name):
    """A secret, minus any spaces or line breaks that came along when it was pasted in."""
    return "".join(os.environ.get(name, "").split())


def r2():
    s3 = boto3.client("s3", endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
                      aws_access_key_id=env("R2_ACCESS_KEY_ID"),
                      aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"), region_name="auto")
    return s3, env("R2_BUCKET")


def exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def read_csv(s3, bucket, key):
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except ClientError:
        return []
    return list(csv.DictReader(io.StringIO(body)))


def write_csv(s3, bucket, key, rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue().encode("utf-8"), ContentType="text/csv")


def ia_enabled():
    return bool(env("IA_ACCESS_KEY") and env("IA_SECRET_KEY"))


def ia_item(state, month):
    return f"upfront-hospital-price-files-{state.lower()}-{month}"


def ia_upload(path, state, month, name):
    """Send one original to the state's archive.org item for the month. Returns '' or an error message."""
    if not ia_enabled():
        return "archive.org keys not set"
    from internetarchive import upload
    md = dict(title=f"Hospital price transparency files, {state}, {month}", mediatype="data",
              collection="opensource_media",
              description=("Machine-readable hospital standard-charge files (45 CFR 180) as posted by each hospital, "
                           "zstd-compressed. Each file name starts with the first 12 characters of the SHA-256 of the "
                           "original file. Collected for public price-transparency research."),
              subject="healthcare; price transparency; hospital prices; 45 CFR 180")
    try:
        rs = upload(ia_item(state, month), files={name: path}, metadata=md,
                    access_key=env("IA_ACCESS_KEY"), secret_key=env("IA_SECRET_KEY"),
                    retries=5, retries_sleep=30)
        bad = [r for r in rs if getattr(r, "status_code", 200) >= 300]
        return f"archive.org HTTP {bad[0].status_code}" if bad else ""
    except Exception as e:  # never let archive.org stop the run
        return f"archive.org: {type(e).__name__}: {str(e)[:200]}"
