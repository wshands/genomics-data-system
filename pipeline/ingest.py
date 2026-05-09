"""
pipeline/ingest.py

Main ingestion pipeline — ties together connector, validator,
S3 transfer, and metadata registration.

Can be run standalone or invoked by AWS Step Functions.
"""

import os
import logging
import argparse
from typing import Optional

from connectors.dnanexus import DNAnexusConnector, RemoteFile
from connectors.healthomics import HealthOmicsConnector
from db.metadata import (
    GenomicsFileRecord,
    register_file,
    update_file_status,
    start_pipeline_run,
    complete_pipeline_run,
)
from pipeline.validator import verify_s3_checksum

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_BUCKET_NAME", "genomics-data-prod")

CONNECTOR_MAP = {
    "dnanexus": DNAnexusConnector,
    "healthomics": HealthOmicsConnector,
}


def build_s3_key(remote_file: RemoteFile) -> str:
    """
    Construct a deterministic S3 key for a genomics file.
    Pattern: {file_type}/{sample_id}/{platform}/{file_name}
    Example: BAM/SAMPLE-001/DNAnexus/NA12878.bam
    """
    return f"{remote_file.file_type}/{remote_file.sample_id}/{remote_file.platform}/{remote_file.file_name}"


def ingest_file(remote_file: RemoteFile, connector) -> dict:
    """
    Full ingestion workflow for a single file:
    1. Register pending record in metadata DB
    2. Stream file from platform to S3
    3. Verify checksum
    4. Update metadata DB to 'ingested'
    5. Log pipeline run result

    Returns summary dict.
    """
    s3_key = build_s3_key(remote_file)

    # Step 1: Register file as pending
    record = GenomicsFileRecord(
        sample_id=remote_file.sample_id,
        file_name=remote_file.file_name,
        file_type=remote_file.file_type,
        source_platform=remote_file.platform,
        source_path=remote_file.source_path,
        s3_bucket=S3_BUCKET,
        s3_key=s3_key,
        file_size_bytes=remote_file.file_size_bytes,
        metadata=remote_file.metadata,
    )
    file_id = register_file(record)
    run_id = start_pipeline_run(file_id, run_type="ingest")

    try:
        # Step 2: Stream to S3
        update_file_status(file_id, "transferring")
        transfer_result = connector.stream_to_s3(remote_file, S3_BUCKET, s3_key)

        # Step 3: Verify checksum
        logger.info(f"Verifying checksum for file_id={file_id}")
        s3_md5 = verify_s3_checksum(S3_BUCKET, s3_key)
        if s3_md5 and s3_md5 != transfer_result["checksum_md5"]:
            raise ValueError(
                f"Checksum mismatch for {remote_file.file_name}: "
                f"expected {transfer_result['checksum_md5']}, got {s3_md5}"
            )

        # Step 4: Update metadata to ingested
        from db.schema import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE genomics_files
                    SET status = 'ingested',
                        checksum_md5 = %s,
                        checksum_sha256 = %s,
                        updated_at = NOW()
                    WHERE file_id = %s
                """,
                    (
                        transfer_result["checksum_md5"],
                        transfer_result["checksum_sha256"],
                        file_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        complete_pipeline_run(run_id, success=True)
        logger.info(f"✅ Ingested file_id={file_id}: {remote_file.file_name}")

        return {
            "file_id": file_id,
            "status": "ingested",
            "s3_uri": f"s3://{S3_BUCKET}/{s3_key}",
            **transfer_result,
        }

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Ingestion failed for {remote_file.file_name}: {error_msg}")
        update_file_status(file_id, "failed", error_message=error_msg)
        complete_pipeline_run(run_id, success=False, error_message=error_msg)
        raise


def ingest_project(platform: str, project_id: str, file_type: Optional[str] = None):
    """
    Ingest all files from a platform project.
    Iterates through all files and calls ingest_file for each.
    """
    connector_cls = CONNECTOR_MAP.get(platform.lower())
    if not connector_cls:
        raise ValueError(
            f"Unknown platform: {platform}. Supported: {list(CONNECTOR_MAP.keys())}"
        )

    connector = connector_cls()
    files = connector.list_files(project_id, file_type=file_type)
    logger.info(f"Ingesting {len(files)} files from {platform} project {project_id}")

    results = {"success": 0, "failed": 0, "file_ids": []}
    for remote_file in files:
        try:
            result = ingest_file(remote_file, connector)
            results["success"] += 1
            results["file_ids"].append(result["file_id"])
        except Exception as e:
            results["failed"] += 1
            logger.error(f"Skipping {remote_file.file_name}: {e}")

    logger.info(
        f"Project ingest complete: {results['success']} succeeded, "
        f"{results['failed']} failed"
    )
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Genomics file ingestion pipeline")
    parser.add_argument(
        "--source",
        required=True,
        choices=["dnanexus", "healthomics"],
        help="Source platform",
    )
    parser.add_argument(
        "--project-id", required=True, help="Project or sequence store ID"
    )
    parser.add_argument(
        "--file-type",
        choices=["FASTQ", "BAM", "VCF", "CRAM"],
        help="Filter by file type",
    )
    args = parser.parse_args()

    ingest_project(
        platform=args.source,
        project_id=args.project_id,
        file_type=args.file_type,
    )
