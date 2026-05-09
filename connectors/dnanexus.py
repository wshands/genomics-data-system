"""
connectors/base.py + connectors/dnanexus.py

Base connector interface and DNAnexus platform connector.
All platform connectors implement the same interface so the
orchestration engine can treat them interchangeably.
"""

import os
import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

import dxpy  # DNAnexus Python SDK

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------


@dataclass
class RemoteFile:
    """Represents a file on a remote platform before transfer."""

    platform: str
    file_id: str
    file_name: str
    file_type: str  # FASTQ, BAM, VCF, etc.
    file_size_bytes: int
    source_path: str
    sample_id: str
    metadata: dict


# ---------------------------------------------------------------------------
# Base connector interface
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """
    Abstract base class for all platform connectors.
    New platforms (e.g., BaseSpace, Terra) only need to implement these methods.
    """

    @abstractmethod
    def list_files(self, project_id: str, file_type: str = None) -> list[RemoteFile]:
        """List files available on the platform, optionally filtered by type."""
        ...

    @abstractmethod
    def stream_to_s3(
        self, remote_file: RemoteFile, s3_bucket: str, s3_key: str
    ) -> dict:
        """
        Stream a file directly from the platform to S3 without
        materializing the full file in memory.
        Returns: {"checksum_md5": str, "checksum_sha256": str, "bytes_transferred": int}
        """
        ...

    @abstractmethod
    def get_metadata(self, file_id: str) -> dict:
        """Fetch platform-specific metadata for a file."""
        ...


# ---------------------------------------------------------------------------
# DNAnexus connector
# ---------------------------------------------------------------------------


class DNAnexusConnector(BaseConnector):
    """
    Connector for DNAnexus platform.
    Handles auth, file listing, and streaming transfers to S3.
    Based on direct experience migrating ~0.5 PB from DNAnexus to AWS at Biogen.
    """

    SUPPORTED_EXTENSIONS = {
        ".fastq": "FASTQ",
        ".fastq.gz": "FASTQ",
        ".bam": "BAM",
        ".bam.bai": "BAM",
        ".vcf": "VCF",
        ".vcf.gz": "VCF",
        ".cram": "CRAM",
    }

    def __init__(self, token: str = None):
        token = token or os.environ.get("DNANEXUS_TOKEN")
        if not token:
            raise ValueError("DNANEXUS_TOKEN not set")
        dxpy.set_security_context({"auth_token_type": "Bearer", "auth_token": token})
        logger.info("DNAnexus connector initialized")

    def _infer_file_type(self, file_name: str) -> str:
        file_name_lower = file_name.lower()
        for ext, ftype in self.SUPPORTED_EXTENSIONS.items():
            if file_name_lower.endswith(ext):
                return ftype
        return "OTHER"

    def list_files(self, project_id: str, file_type: str = None) -> list[RemoteFile]:
        """List all genomics files in a DNAnexus project."""
        logger.info(f"Listing files in DNAnexus project {project_id}")
        results = []

        try:
            for item in dxpy.find_data_objects(
                classname="file",
                project=project_id,
                describe=True,
            ):
                desc = item["describe"]
                fname = desc["name"]
                inferred_type = self._infer_file_type(fname)

                if file_type and inferred_type != file_type:
                    continue

                # Extract sample_id from tags or properties if available
                props = desc.get("properties", {})
                sample_id = props.get("sample_id", props.get("sample", "unknown"))

                results.append(
                    RemoteFile(
                        platform="DNAnexus",
                        file_id=item["id"],
                        file_name=fname,
                        file_type=inferred_type,
                        file_size_bytes=desc.get("size", 0),
                        source_path=f"{project_id}:{desc.get('folder','/')}/{fname}",
                        sample_id=sample_id,
                        metadata={
                            "dx_project": project_id,
                            "dx_folder": desc.get("folder", "/"),
                            "dx_tags": desc.get("tags", []),
                            "dx_properties": props,
                            "dx_created": desc.get("created"),
                            "dx_modified": desc.get("modified"),
                        },
                    )
                )

        except dxpy.exceptions.DXAPIError as e:
            logger.error(f"DNAnexus API error listing {project_id}: {e}")
            raise

        logger.info(f"Found {len(results)} files in {project_id}")
        return results

    def stream_to_s3(
        self, remote_file: RemoteFile, s3_bucket: str, s3_key: str
    ) -> dict:
        """
        Stream file from DNAnexus directly to S3 using chunked reads.
        Uses DNAnexus download URL + boto3 multipart upload.
        No full file in memory — handles 100GB+ BAM files.
        """
        import boto3
        from boto3.s3.transfer import TransferConfig

        s3_client = boto3.client("s3")
        dx_file = dxpy.DXFile(remote_file.file_id)

        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        bytes_transferred = 0

        logger.info(
            f"Starting stream: {remote_file.file_name} → s3://{s3_bucket}/{s3_key}"
        )

        # Multipart upload config — 100MB parts, 4 parallel threads
        config = TransferConfig(
            multipart_threshold=100 * 1024 * 1024,
            multipart_chunksize=100 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        )

        # Stream in 64MB chunks, compute checksums in flight
        def chunked_stream() -> Iterator[bytes]:
            nonlocal bytes_transferred
            chunk_size = 64 * 1024 * 1024  # 64MB
            with dx_file.open() as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    md5.update(chunk)
                    sha256.update(chunk)
                    bytes_transferred += len(chunk)
                    yield chunk

        # Upload using streaming body
        s3_client.upload_fileobj(
            Fileobj=_IterableToFileObj(chunked_stream()),
            Bucket=s3_bucket,
            Key=s3_key,
            Config=config,
            ExtraArgs={"StorageClass": "STANDARD"},
        )

        result = {
            "checksum_md5": md5.hexdigest(),
            "checksum_sha256": sha256.hexdigest(),
            "bytes_transferred": bytes_transferred,
        }
        logger.info(
            f"Transfer complete: {remote_file.file_name} "
            f"({bytes_transferred / 1e9:.2f} GB) md5={result['checksum_md5']}"
        )
        return result

    def get_metadata(self, file_id: str) -> dict:
        """Fetch full metadata for a single DNAnexus file."""
        try:
            desc = dxpy.DXFile(file_id).describe(
                fields={
                    "name": True,
                    "size": True,
                    "properties": True,
                    "tags": True,
                    "folder": True,
                    "created": True,
                    "modified": True,
                }
            )
            return desc
        except dxpy.exceptions.DXAPIError as e:
            logger.error(f"Failed to get metadata for {file_id}: {e}")
            raise


class _IterableToFileObj:
    """Adapter to make a generator look like a file object for boto3 upload_fileobj."""

    def __init__(self, iterable):
        self._iter = iterable
        self._buffer = b""

    def read(self, size=-1):
        try:
            while size < 0 or len(self._buffer) < size:
                self._buffer += next(self._iter)
        except StopIteration:
            pass
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data
