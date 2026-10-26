"""Writes stats.json to the public bucket."""

import json

CACHE_CONTROL = "public, max-age=3600"
STAGING_PREFIX = "_staging/"


def publish(bucket, payload, object_name="stats.json"):
    """
    Uploads under a staging name, then server-side copies over the live
    object. A single GCS upload is already all-or-nothing, so readers never
    see a partial file either way; staging just means a failed upload never
    touches the live object at all. Returns the published size in bytes.
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    staging = bucket.blob(f"{STAGING_PREFIX}{object_name}")
    staging.cache_control = CACHE_CONTROL
    staging.upload_from_string(body, content_type="application/json")

    # copy_blob carries the source's content type and cache-control across.
    bucket.copy_blob(staging, bucket, object_name)
    staging.delete()

    return len(body)
