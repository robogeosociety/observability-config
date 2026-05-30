#!/usr/bin/env python3
"""Minimal Cloudflare R2 uploader (S3 API via boto3), used by backup.sh.

Credentials from the environment (sourced from influxdb/.env):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Usage:
  r2_upload.py put <local_path> <key>   # upload a file
  r2_upload.py check                     # verify write access (put+delete a probe)
  r2_upload.py list [prefix]             # list keys

Invoke with `uv run --with boto3 python r2_upload.py ...`.
"""

import os
import sys

import boto3
from botocore.config import Config


def _client():
    acct = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", region_name="auto"),
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bucket = os.environ["R2_BUCKET"]
    s3 = _client()
    cmd = sys.argv[1]

    if cmd == "put":
        local_path, key = sys.argv[2], sys.argv[3]
        with open(local_path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=key, Body=f.read(),
                          ContentType="application/gzip")
        print(f"uploaded {local_path} -> r2://{bucket}/{key}")
        return 0

    if cmd == "check":
        s3.put_object(Bucket=bucket, Key=".write-probe", Body=b"ok")
        s3.delete_object(Bucket=bucket, Key=".write-probe")
        print(f"write OK -> r2://{bucket}")
        return 0

    if cmd == "list":
        prefix = sys.argv[2] if len(sys.argv) > 2 else ""
        n = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                print(f"{obj['Size']:>12}  {obj['LastModified']:%Y-%m-%d %H:%M}  {obj['Key']}")
                n += 1
        print(f"({n} objects)")
        return 0

    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
