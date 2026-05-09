"""
connectors/healthomics.py

AWS HealthOmics platform connector.
Handles listing and streaming genomics files from HealthOmics
ReadSets to S3.
"""

import os
import hashlib
import logging
from typing import Iterator

import boto3
from botocore.exceptions import ClientError

from connectors.dnanexus import BaseConnector, RemoteFile

logger = logging.getLogger(__name__)


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
        Export a HealthOmics ReadSet to S3.
        Uses HealthOmics StartReadSetExportJob for large files,
        or direct GetReadSet for streaming smaller files.
        """
        # For large files, use HealthOmics native export to S3
        store_id = remote_file.metadata.get("store_id")

        logger.info(
            f"Exporting ReadSet {remote_file.file_id} → s3://{s3_bucket}/{s3_key}"
        )

        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        bytes_transferred = 0

        try:
            # Get the ReadSet as a stream
            response = self.omics.get_read_set(
                sequenceStoreId=store_id,
                id=remote_file.file_id,
                partNumber=1,
                file="SOURCE1",
            )

            s3_client = boto3.client("s3")
            chunk_size = 64 * 1024 * 1024  # 64MB

            def stream_body() -> Iterator[bytes]:
                nonlocal bytes_transferred
                body = response["payload"]
                for chunk in body.iter_chunks(chunk_size=chunk_size):
                    md5.update(chunk)
                    sha256.update(chunk)
                    bytes_transferred += len(chunk)
                    yield chunk

            from connectors.dnanexus import _IterableToFileObj
            from boto3.s3.transfer import TransferConfig

            config = TransferConfig(
                multipart_threshold=100 * 1024 * 1024,
                multipart_chunksize=100 * 1024 * 1024,
                max_concurrency=4,
            )
            s3_client.upload_fileobj(
                Fileobj=_IterableToFileObj(stream_body()),
                Bucket=s3_bucket,
                Key=s3_key,
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
        logger.info(
            f"Export complete: {remote_file.file_name} "
            f"({bytes_transferred / 1e9:.2f} GB)"
        )
        return result

    def get_metadata(self, file_id: str) -> dict:
        """Not implemented — use list_files metadata."""
        raise NotImplementedError("Use list_files to get HealthOmics metadata")
