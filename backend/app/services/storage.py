"""S3-compatible object storage helper.

Wraps a boto3 client configured for either AWS S3 (production) or a local
MinIO container (dev). All raw uploads and processed derivatives are stored
here; PostgreSQL only holds the object keys.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache
def get_s3_client():
    """Return a cached boto3 S3 client.

    When ``s3_endpoint_url`` is set (MinIO/local), path-style addressing is
    used. In production against real AWS S3 the endpoint and credentials are
    ``None``, so boto3 builds the AWS URL and resolves credentials through its
    default chain — which on EC2 means the instance role. They must be ``None``
    and not ``""``; see the validator in app/config.py.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@lru_cache
def get_s3_presign_client():
    """Client used only to sign URLs handed to the browser.

    SigV4 signs the Host header, so the presigning client must target the same
    host the browser will call (``s3_public_endpoint_url``). Falls back to the
    internal client's endpoint when no public endpoint is configured (prod S3).
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint_url or settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_bucket(bucket: str | None = None) -> None:
    """Create the target bucket if it does not already exist (idempotent)."""
    bucket = bucket or settings.s3_bucket
    client = get_s3_client()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        logger.info("Creating S3 bucket %s", bucket)
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as exc:  # pragma: no cover - depends on backend
            # BucketAlreadyOwnedByYou etc. are safe to ignore.
            logger.warning("create_bucket returned %s", exc)


def upload_bytes(
    key: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    bucket: str | None = None,
) -> str:
    """Upload raw bytes and return the object key."""
    bucket = bucket or settings.s3_bucket
    get_s3_client().put_object(
        Bucket=bucket, Key=key, Body=data, ContentType=content_type
    )
    return key


def download_bytes(key: str, bucket: str | None = None) -> bytes:
    """Download an object's bytes into memory."""
    bucket = bucket or settings.s3_bucket
    buffer = io.BytesIO()
    get_s3_client().download_fileobj(bucket, key, buffer)
    return buffer.getvalue()


def generate_presigned_url(
    key: str, expires_in: int = 3600, bucket: str | None = None
) -> str:
    """Return a temporary URL the browser can use to fetch an object."""
    bucket = bucket or settings.s3_bucket
    return get_s3_presign_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def delete_object(key: str, bucket: str | None = None) -> None:
    bucket = bucket or settings.s3_bucket
    get_s3_client().delete_object(Bucket=bucket, Key=key)


# ---------------------------------------------------------------------------
# Async wrappers
#
# boto3 is synchronous and blocking. Calling it directly from an `async def`
# endpoint stalls the whole event loop for the duration of the transfer — a
# 25 MB upload would block every other in-flight request. Celery workers call
# the sync functions above directly (they are not on an event loop); FastAPI
# routes should use these wrappers instead.
# ---------------------------------------------------------------------------


async def ensure_bucket_async(bucket: str | None = None) -> None:
    await run_in_threadpool(ensure_bucket, bucket)


async def upload_bytes_async(
    key: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    bucket: str | None = None,
) -> str:
    return await run_in_threadpool(upload_bytes, key, data, content_type, bucket)


async def download_bytes_async(key: str, bucket: str | None = None) -> bytes:
    return await run_in_threadpool(download_bytes, key, bucket)


async def delete_object_async(key: str, bucket: str | None = None) -> None:
    await run_in_threadpool(delete_object, key, bucket)
