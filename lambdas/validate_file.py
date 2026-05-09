"""
lambdas/validate_file.py

Step Functions Task — Step 2: ValidateFile

Validates the file type and creates (or recovers) the metadata DB record.
Idempotent: if a 'pending' or 'transferring' record already exists for
this source_path (Step Functions retry), it re-uses that record rather
than inserting a duplicate.

Input (one item from detect_files $.files array):
{
  "platform":        "DNAnexus",
  "file_id":         "file-XXX",
  "file_name":       "NA12878.bam",
  "file_type":       "BAM",
  "file_size_bytes": 85000000000,
  "source_path":     "project-XXX:/aligned/NA12878.bam",
  "sample_id":       "SAMPLE-001",
  "metadata":        {...}
}

Output (passed to TransferToS3):
  Same dict + db_file_id, run_id, s3_bucket, s3_key
"""

import logging
import os
import sys

sys.path.insert(0, "/var/task")

from db.schema import get_connection
from db.metadata import (
    GenomicsFileRecord,
    register_file,
    register_sample,
    start_pipeline_run,
)
from pipeline.validator import validate_genomics_file_type


class AlreadyIngestedException(Exception):
    """Raised when a file has already been successfully ingested.
    Caught by the Step Functions ValidateFile Catch block → routed to FileSucceeded."""


logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ["S3_BUCKET_NAME"]


def _build_s3_key(event: dict) -> str:
    return "{file_type}/{sample_id}/{platform}/{file_name}".format(**event)


def _find_existing_record(source_path: str) -> dict | None:
    """
    Return (file_id, status) if this source_path is already in the DB
    with a non-terminal status, else None.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT file_id, status FROM genomics_files
                WHERE source_path = %s
                  AND status NOT IN ('failed', 'deleted')
                LIMIT 1
            """,
                (source_path,),
            )
            row = cur.fetchone()
            return {"file_id": row[0], "status": row[1]} if row else None
    finally:
        conn.close()


def handler(event: dict, context) -> dict:
    logger.info(f"ValidateFile: {event.get('file_name')} ({event.get('file_type')})")

    file_name = event["file_name"]
    source_path = event["source_path"]
    platform = event["platform"]

    # 1. Validate file type
    if not validate_genomics_file_type(file_name):
        raise ValueError(f"Unrecognised genomics file type: {file_name}")

    # 2. Find or create DB record (idempotent on retry)
    existing = _find_existing_record(source_path)

    if existing and existing["status"] == "ingested":
        logger.info(
            f"Already ingested: {source_path} (file_id={existing['file_id']}) — skipping"
        )
        # Step Functions Catch block routes AlreadyIngestedException → FileSucceeded.
        raise AlreadyIngestedException(
            f"file_id={existing['file_id']} already ingested"
        )

    if existing:
        db_file_id = existing["file_id"]
        logger.info(f"Re-using existing record file_id={db_file_id} (retry)")
    else:
        # Ensure sample row exists before inserting the file (FK constraint).
        # ON CONFLICT DO NOTHING makes this safe whether or not the sample
        # was registered by an external system beforehand.
        register_sample(
            sample_id=event["sample_id"],
            project_id=event.get("metadata", {}).get("dx_project")
            or event.get("metadata", {}).get("store_id"),
        )

        record = GenomicsFileRecord(
            sample_id=event["sample_id"],
            file_name=file_name,
            file_type=event["file_type"],
            source_platform=platform,
            source_path=source_path,
            s3_bucket=S3_BUCKET,
            s3_key=_build_s3_key(event),
            file_size_bytes=event["file_size_bytes"],
            metadata=event.get("metadata", {}),
        )
        db_file_id = register_file(record)
        logger.info(f"Registered new record file_id={db_file_id}")

    run_id = start_pipeline_run(db_file_id, run_type="ingest")

    return {
        **event,
        "db_file_id": db_file_id,
        "run_id": run_id,
        "s3_bucket": S3_BUCKET,
        "s3_key": _build_s3_key(event),
    }
