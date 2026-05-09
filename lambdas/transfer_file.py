"""
lambdas/transfer_file.py

Step Functions Task — Step 3: TransferToS3

Streams a file from the source platform directly to S3 using multipart
upload — no full file in memory. Handles 100 GB+ BAM files.

Input (output of ValidateFile):
{
  "platform":        "DNAnexus",
  "file_id":         "file-XXX",
  "file_name":       "NA12878.bam",
  "file_type":       "BAM",
  "file_size_bytes": 85000000000,
  "source_path":     "...",
  "sample_id":       "SAMPLE-001",
  "metadata":        {...},
  "db_file_id":      42,
  "run_id":          7,
  "s3_bucket":       "genomics-data-prod",
  "s3_key":          "BAM/SAMPLE-001/DNAnexus/NA12878.bam"
}

Output (passed to RegisterMetadata):
  Same dict + checksum_md5, checksum_sha256, bytes_transferred
"""

import logging
import sys

sys.path.insert(0, "/var/task")

from connectors.dnanexus import DNAnexusConnector, RemoteFile
from connectors.healthomics import HealthOmicsConnector
from db.metadata import update_file_status
from pipeline.validator import verify_s3_checksum

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONNECTOR_MAP = {
    "DNAnexus": DNAnexusConnector,
    "HealthOmics": HealthOmicsConnector,
}


def _build_remote_file(event: dict) -> RemoteFile:
    return RemoteFile(
        platform=event["platform"],
        file_id=event["file_id"],
        file_name=event["file_name"],
        file_type=event["file_type"],
        file_size_bytes=event["file_size_bytes"],
        source_path=event["source_path"],
        sample_id=event["sample_id"],
        metadata=event.get("metadata", {}),
    )


def handler(event: dict, context) -> dict:
    db_file_id = event["db_file_id"]
    s3_bucket = event["s3_bucket"]
    s3_key = event["s3_key"]
    platform = event["platform"]

    logger.info(
        f"TransferToS3: file_id={db_file_id} "
        f"{event['file_name']} → s3://{s3_bucket}/{s3_key}"
    )

    connector_cls = CONNECTOR_MAP.get(platform)
    if not connector_cls:
        raise ValueError(f"Unknown platform: {platform}")

    update_file_status(db_file_id, "transferring")

    connector = connector_cls()
    remote_file = _build_remote_file(event)

    transfer_result = connector.stream_to_s3(remote_file, s3_bucket, s3_key)

    # Verify the object landed correctly
    s3_etag = verify_s3_checksum(s3_bucket, s3_key)
    if s3_etag is None:
        raise RuntimeError(
            f"Object not found in S3 after transfer: s3://{s3_bucket}/{s3_key}"
        )

    logger.info(
        f"Transfer complete: {event['file_name']} "
        f"({transfer_result['bytes_transferred'] / 1e9:.2f} GB) "
        f"md5={transfer_result['checksum_md5']}"
    )

    return {
        **event,
        "checksum_md5": transfer_result["checksum_md5"],
        "checksum_sha256": transfer_result["checksum_sha256"],
        "bytes_transferred": transfer_result["bytes_transferred"],
    }
