"""S3 ObjectCreated -> backend re-drive.

Invoked by an S3 bucket notification filtered to the ``raw/`` prefix. Posts the
affected object keys to the backend's internal endpoint, which resets each
matching DataItem to pending and enqueues pre-processing.

Only objects that arrived without going through the upload endpoint need this —
the upload route enqueues its own work. See infrastructure/terraform/lambda.tf
for why the Lambda calls the API rather than the Celery broker.

Environment:
  API_BASE_URL                  e.g. http://mlops-qa-prod-alb-1234567890.us-east-1.elb.amazonaws.com
  INTERNAL_TOKEN_SSM_PARAMETER  SSM parameter holding the shared secret
  KEY_PREFIX                    only handle keys under this prefix (default "raw/")
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# How many keys to send in one request. S3 can deliver up to ~10k records in a
# single event; the backend does a DB lookup and a Redis round-trip per key, so
# chunking keeps any one request inside the Lambda's 30s budget.
CHUNK_SIZE = 50
REQUEST_TIMEOUT_SECONDS = 20

_ssm = boto3.client("ssm")
_cached_token: str | None = None


def _get_internal_token() -> str:
    """Fetch the shared secret, caching it across warm invocations."""
    global _cached_token
    if _cached_token is None:
        parameter_name = os.environ["INTERNAL_TOKEN_SSM_PARAMETER"]
        response = _ssm.get_parameter(Name=parameter_name, WithDecryption=True)
        _cached_token = response["Parameter"]["Value"]
    return _cached_token


def _extract_keys(event: dict) -> list[tuple[str, str]]:
    """Pull (bucket, key) pairs out of an S3 notification, filtered by prefix."""
    prefix = os.environ.get("KEY_PREFIX", "raw/")
    pairs: list[tuple[str, str]] = []

    for record in event.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            continue

        s3 = record.get("s3", {})
        bucket = s3.get("bucket", {}).get("name")
        # S3 URL-encodes keys in the notification payload: spaces arrive as "+"
        # and other characters as %XX.
        key = urllib.parse.unquote_plus(s3.get("object", {}).get("key", ""))

        if not bucket or not key:
            continue
        if prefix and not key.startswith(prefix):
            continue

        pairs.append((bucket, key))

    return pairs


def _post_chunk(base_url: str, token: str, pairs: list[tuple[str, str]]) -> None:
    """POST one chunk to the backend.

    Raises on failure so the Lambda returns an error and S3 retries.
    """
    payload = json.dumps(
        [{"bucket": bucket, "key": key} for bucket, key in pairs]
    ).encode("utf-8")

    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/v1/internal/s3-events",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": token,
        },
    )

    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8")
        logger.info("Backend accepted %d key(s): %s", len(pairs), body)


def handler(event: dict, context: object) -> dict:
    pairs = _extract_keys(event)
    if not pairs:
        logger.info("No raw/ objects in this event; nothing to do.")
        return {"queued": 0, "skipped": len(event.get("Records", []))}

    token = _get_internal_token()
    base_url = os.environ["API_BASE_URL"]

    queued = 0
    for start in range(0, len(pairs), CHUNK_SIZE):
        chunk = pairs[start : start + CHUNK_SIZE]
        try:
            _post_chunk(base_url, token, chunk)
        except urllib.error.HTTPError as exc:
            # 404s are expected for objects with no matching DataItem (an
            # out-of-band upload the app does not know about). The endpoint
            # answers 200 with an "unknown_key" action for those, so a real
            # HTTPError here is something else — surface it.
            logger.error(
                "Backend rejected chunk (%d key(s)): %s %s",
                len(chunk),
                exc.code,
                exc.read().decode("utf-8", errors="replace"),
            )
            raise
        except urllib.error.URLError as exc:
            logger.error("Could not reach the backend: %s", exc.reason)
            raise

        queued += len(chunk)

    logger.info("Re-drove %d object(s).", queued)
    return {"queued": queued, "skipped": len(event.get("Records", [])) - queued}
