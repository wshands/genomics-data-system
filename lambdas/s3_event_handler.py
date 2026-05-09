"""
lambdas/s3_event_handler.py

AWS Lambda function triggered by S3 events (ObjectCreated, LifecycleTransition).
Updates the metadata DB when files are created or transition storage tiers.

Deploy via: Terraform + GitHub Actions
"""

import json
import logging
import os
import sys

# Lambda environment — add project root to path
sys.path.insert(0, "/var/task")

from db.metadata import update_file_status, update_storage_tier
from db.schema import get_connection

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TIER_MAP = {
    "STANDARD": "hot",
    "STANDARD_IA": "warm",
    "GLACIER": "cold",
    "DEEP_ARCHIVE": "cold",
}


def handler(event, context):
    """
    Entry point for Lambda.
    Handles two event types:
    1. s3:ObjectCreated — verify file exists, update status
    2. s3:LifecycleTransition — update storage tier in metadata DB
    """
    logger.info(f"Event received: {json.dumps(event)}")

    for record in event.get("Records", []):
        event_name = record.get("eventName", "")

        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        if event_name.startswith("ObjectCreated"):
            _handle_object_created(bucket, key, record)
        elif "LifecycleTransition" in event_name:
            _handle_lifecycle_transition(bucket, key, record)
        else:
            logger.warning(f"Unhandled event type: {event_name}")

    return {"statusCode": 200, "body": "OK"}


def _handle_object_created(bucket: str, key: str, record: dict):
    """
    When a new object lands in S3, find its file_id by s3_key
    and confirm status as 'ingested' if still in 'transferring'.
    """
    logger.info(f"ObjectCreated: s3://{bucket}/{key}")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, status FROM genomics_files WHERE s3_bucket = %s AND s3_key = %s",
                (bucket, key)
            )
            row = cur.fetchone()
            if row:
                file_id, status = row
                if status == "transferring":
                    cur.execute(
                        "UPDATE genomics_files SET status = 'ingested', updated_at = NOW() WHERE file_id = %s",
                        (file_id,)
                    )
                    conn.commit()
                    logger.info(f"Confirmed ingestion for file_id={file_id}")
            else:
                logger.warning(f"No DB record found for s3://{bucket}/{key}")
    finally:
        conn.close()


def _handle_lifecycle_transition(bucket: str, key: str, record: dict):
    """
    When S3 lifecycle transitions an object to a new storage class,
    update the storage_tier in the metadata DB.
    """
    storage_class = record.get("s3", {}).get("object", {}).get("storageClass", "STANDARD")
    tier = TIER_MAP.get(storage_class, "hot")
    logger.info(f"Lifecycle transition: s3://{bucket}/{key} → {storage_class} ({tier})")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id FROM genomics_files WHERE s3_bucket = %s AND s3_key = %s",
                (bucket, key)
            )
            row = cur.fetchone()
            if row:
                file_id = row[0]
                update_storage_tier(file_id, tier)
                logger.info(f"Updated file_id={file_id} to tier={tier}")
    finally:
        conn.close()
