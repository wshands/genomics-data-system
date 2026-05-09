"""
lambdas/register_metadata.py

Step Functions Task — Step 4: RegisterMetadata

Updates the genomics_files record with checksums and final status,
and closes out the pipeline_run row.

Input (output of TransferToS3):
{
  ...(all previous fields)...,
  "db_file_id":       42,
  "run_id":           7,
  "s3_bucket":        "genomics-data-prod",
  "s3_key":           "BAM/SAMPLE-001/DNAnexus/NA12878.bam",
  "checksum_md5":     "abc123",
  "checksum_sha256":  "def456",
  "bytes_transferred": 85000000000
}

Output (one element in the Map state result array):
{
  "db_file_id": 42,
  "file_name":  "NA12878.bam",
  "file_type":  "BAM",
  "sample_id":  "SAMPLE-001",
  "s3_uri":     "s3://genomics-data-prod/BAM/SAMPLE-001/DNAnexus/NA12878.bam",
  "checksum_md5": "abc123",
  "status":     "ingested"
}
"""

import logging
import sys

sys.path.insert(0, "/var/task")

from db.metadata import complete_pipeline_run
from db.schema import get_connection

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    db_file_id = event["db_file_id"]
    run_id = event["run_id"]
    checksum_md5 = event["checksum_md5"]
    checksum_sha256 = event["checksum_sha256"]
    s3_bucket = event["s3_bucket"]
    s3_key = event["s3_key"]

    logger.info(f"RegisterMetadata: file_id={db_file_id} md5={checksum_md5}")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE genomics_files
                SET status          = 'ingested',
                    checksum_md5    = %s,
                    checksum_sha256 = %s,
                    last_accessed   = NOW(),
                    updated_at      = NOW()
                WHERE file_id = %s
            """,
                (checksum_md5, checksum_sha256, db_file_id),
            )
        conn.commit()
    finally:
        conn.close()

    complete_pipeline_run(run_id, success=True)
    logger.info(f"Ingestion complete: file_id={db_file_id}")

    return {
        "db_file_id": db_file_id,
        "file_name": event["file_name"],
        "file_type": event["file_type"],
        "sample_id": event["sample_id"],
        "s3_uri": f"s3://{s3_bucket}/{s3_key}",
        "checksum_md5": checksum_md5,
        "status": "ingested",
    }
