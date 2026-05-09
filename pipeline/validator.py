"""
pipeline/validator.py

File validation utilities — checksum verification at source and S3.
"""

import hashlib
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024  # 8MB


def compute_file_checksum(file_path: str) -> dict:
    """Compute MD5 and SHA256 for a local file."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            md5.update(chunk)
            sha256.update(chunk)
    return {"md5": md5.hexdigest(), "sha256": sha256.hexdigest()}


def verify_s3_checksum(bucket: str, key: str) -> str:
    """
    Retrieve the ETag (MD5) of an S3 object.
    For multipart uploads the ETag is not a simple MD5,
    but we use it for basic verification.
    Returns the ETag string or None if object not found.
    """
    s3 = boto3.client("s3")
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
        etag = response.get("ETag", "").strip('"')
        logger.info(f"S3 ETag for s3://{bucket}/{key}: {etag}")
        return etag
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            logger.warning(f"Object not found: s3://{bucket}/{key}")
            return None
        raise


def validate_genomics_file_type(file_name: str) -> bool:
    """
    Basic validation that the file has a recognized genomics extension.
    """
    VALID_EXTENSIONS = (
        ".fastq",
        ".fastq.gz",
        ".fq",
        ".fq.gz",
        ".bam",
        ".bam.bai",
        ".vcf",
        ".vcf.gz",
        ".cram",
        ".cram.crai",
        ".bed",
    )
    return file_name.lower().endswith(VALID_EXTENSIONS)
