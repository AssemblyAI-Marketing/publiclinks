"""Cloudflare R2 helpers.

Vercel Functions cap request bodies at 4.5 MB, so the browser uploads straight
to R2 with a presigned PUT URL instead of going through the app.
The bucket needs a CORS rule allowing PUT from the app's origin.
"""

import os
import uuid

import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "").rstrip("/")


def enabled() -> bool:
    return all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL])


def _client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def list_objects() -> list[dict]:
    objects = []
    for page in _client().get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET_NAME):
        objects.extend(page.get("Contents", []))
    return objects


def presign_upload(filename: str, content_type: str) -> dict:
    client = _client()
    object_key = f"{uuid.uuid4().hex[:8]}-{filename}"
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": object_key, "ContentType": content_type},
        ExpiresIn=3600,
    )
    return {"upload_url": upload_url, "public_url": f"{R2_PUBLIC_URL}/{object_key}"}
