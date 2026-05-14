"""
connectors/healthomics.py

AWS HealthOmics platform connector.
Handles listing and streaming genomics files from HealthOmics
ReadSets to S3.
"""

import hashlib
import logging
from typing import Iterator

import boto3
from botocore.exceptions import ClientError

from connectors.dnanexus import BaseConnector, RemoteFile

logger = logging.getLogger(__name__)


def _derive_r2_key(s3_key: str) -> str:
    """Insert _R2 before the file extension to produce the paired read S3 key."""
    if s3_key.endswith(".gz"):
        # Handle compound extensions: .fastq.gz, .fq.gz
        inner = s3_key[:-3]
        dot = inner.rfind(".")
        return (inner[:dot] + "_R2" + inner[dot:] + ".gz") if dot != -1 else s3_key + "_R2.gz"
    dot = s3_key.rfind(".")
    return (s3_key[:dot] + "_R2" + s3_key[dot:]) if dot != -1 else s3_key + "_R2"


class HealthOmicsConnector(BaseConnector):
    """
    Connector for AWS HealthOmics.
    Supports ReadSet retrieval and streaming to S3.
    """

    FILE_TYPE_MAP = {
        "FASTQ": "FASTQ",
        "BAM": "BAM",
        "CRAM": "CRAM",
        "VCF": "VCF",
    }

    def __init__(self, region: str = None):
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.omics = boto3.client("omics", region_name=self.region)
        self.s3 = boto3.client("s3", region_name=self.region)
        logger.info(f"HealthOmics connector initialized in {self.region}")

    def list_files(self, project_id: str, file_type: str = None) -> list[RemoteFile]:
        """
        List ReadSets in a HealthOmics sequence store.
        project_id = HealthOmics sequence store ID.
        """
        logger.info(f"Listing ReadSets in HealthOmics store {project_id}")
        results = []

        try:
            paginator = self.omics.get_paginator("list_read_sets")
            for page in paginator.paginate(sequenceStoreId=project_id):
                for rs in page.get("readSets", []):
                    if rs.get("status") != "ACTIVE":
                        logger.info(
                            f"Skipping ReadSet {rs['id']} — status={rs.get('status')}"
                        )
                        continue

                    inferred_type = self.FILE_TYPE_MAP.get(
                        rs.get("fileType", ""), "OTHER"
                    )

                    if file_type and inferred_type != file_type:
                        continue

                    sample_id = rs.get("sampleId", rs.get("name", "unknown"))

                    results.append(
                        RemoteFile(
                            platform="HealthOmics",
                            file_id=rs["id"],
                            file_name=rs.get("name", rs["id"]),
                            file_type=inferred_type,
                            file_size_bytes=rs.get("sequenceInformation", {}).get(
                                "totalBaseCount", 0
                            ),
                            source_path=f"omics://{project_id}/readSet/{rs['id']}",
                            sample_id=sample_id,
                            metadata={
                                "store_id": project_id,
                                "subject_id": rs.get("subjectId"),
                                "reference_arn": rs.get("referenceArn"),
                                "created_time": str(rs.get("creationTime")),
                                "status": rs.get("status"),
                                "tags": rs.get("tags", {}),
                            },
                        )
                    )

        except ClientError as e:
            logger.error(f"HealthOmics API error for store {project_id}: {e}")
            raise

        logger.info(f"Found {len(results)} ReadSets in store {project_id}")
        return results

    def stream_to_s3(
        self, remote_file: RemoteFile, s3_bucket: str, s3_key: str
    ) -> dict:
        """
        Stream a HealthOmics ReadSet to S3 via GetReadSet.
        Fetches all parts for SOURCE1, and SOURCE2 if the ReadSet is
        paired-end (FASTQ R1/R2). SOURCE2 lands at a derived _R2 key.
        """
        store_id = remote_file.metadata.get("store_id")
        chunk_size = 64 * 1024 * 1024  # 64 MB

        # Discover part counts and whether a paired reverse read exists.
        rs_meta = self.omics.get_read_set_metadata(
            sequenceStoreId=store_id,
            id=remote_file.file_id,
        )
        files_meta = rs_meta.get("files", {})
        source1_parts = files_meta.get("source1", {}).get("totalParts", 1)
        source2_parts = files_meta.get("source2", {}).get("totalParts", 0)
        is_paired = source2_parts > 0

        logger.info(
            f"ReadSet {remote_file.file_id}: source1={source1_parts} part(s)"
            + (f", source2={source2_parts} part(s)" if is_paired else " (single-end)")
        )

        s3_client = boto3.client("s3")
        from connectors.dnanexus import _IterableToFileObj
        from boto3.s3.transfer import TransferConfig

        config = TransferConfig(
            multipart_threshold=100 * 1024 * 1024,
            multipart_chunksize=100 * 1024 * 1024,
            max_concurrency=4,
        )

        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        bytes_transferred = 0

        def _stream_source(file_key: str, total_parts: int) -> Iterator[bytes]:
            nonlocal bytes_transferred
            for part_num in range(1, total_parts + 1):
                resp = self.omics.get_read_set(
                    sequenceStoreId=store_id,
                    id=remote_file.file_id,
                    partNumber=part_num,
                    file=file_key,
                )
                for chunk in resp["payload"].iter_chunks(chunk_size=chunk_size):
                    md5.update(chunk)
                    sha256.update(chunk)
                    bytes_transferred += len(chunk)
                    yield chunk

        s3_key_r2 = None
        try:
            logger.info(
                f"Exporting ReadSet {remote_file.file_id} → s3://{s3_bucket}/{s3_key}"
            )
            s3_client.upload_fileobj(
                Fileobj=_IterableToFileObj(_stream_source("SOURCE1", source1_parts)),
                Bucket=s3_bucket,
                Key=s3_key,
                Config=config,
            )

            if is_paired:
                s3_key_r2 = _derive_r2_key(s3_key)
                logger.info(
                    f"Uploading SOURCE2 → s3://{s3_bucket}/{s3_key_r2}"
                )
                s3_client.upload_fileobj(
                    Fileobj=_IterableToFileObj(
                        _stream_source("SOURCE2", source2_parts)
                    ),
                    Bucket=s3_bucket,
                    Key=s3_key_r2,
                    Config=config,
                )

        except ClientError as e:
            logger.error(f"Failed to stream ReadSet {remote_file.file_id}: {e}")
            raise

        result = {
            "checksum_md5": md5.hexdigest(),
            "checksum_sha256": sha256.hexdigest(),
            "bytes_transferred": bytes_transferred,
        }
        if s3_key_r2:
            # R2 key included so register_metadata can record the paired object
            result["s3_key_r2"] = s3_key_r2

        logger.info(
            f"Export complete: {remote_file.file_name} "
            f"({bytes_transferred / 1e9:.2f} GB)"
        )
        return result

    def get_metadata(self, file_id: str) -> dict:
        """Not implemented — use list_files metadata."""
        raise NotImplementedError("Use list_files to get HealthOmics metadata")
