"""
tests/test_metadata.py + tests/test_validator.py + tests/test_ingest.py

Pytest unit tests for the Genomics Data System.
Uses mocking to avoid real AWS/DNAnexus calls.
"""

from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Tests: db/metadata.py
# ---------------------------------------------------------------------------

class TestRegisterFile:
    """Tests for metadata registration logic."""

    @patch("db.metadata.get_connection")
    @patch("db.metadata._write_audit_log")
    def test_register_file_returns_file_id(self, mock_audit, mock_conn):
        from db.metadata import register_file, GenomicsFileRecord

        # Mock DB cursor
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (42,)

        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_connection

        record = GenomicsFileRecord(
            sample_id="SAMPLE-001",
            file_name="NA12878.bam",
            file_type="BAM",
            source_platform="DNAnexus",
            source_path="project-XXX:/results/NA12878.bam",
            s3_bucket="genomics-data-prod",
            s3_key="BAM/SAMPLE-001/DNAnexus/NA12878.bam",
            file_size_bytes=10_000_000_000,
        )

        file_id = register_file(record)
        assert file_id == 42
        mock_audit.assert_called_once()

    @patch("db.metadata.get_connection")
    def test_update_file_status(self, mock_conn):
        from db.metadata import update_file_status

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_connection

        # Should not raise
        with patch("db.metadata._write_audit_log"):
            update_file_status(42, "ingested")

        mock_cursor.execute.assert_called_once()
        mock_connection.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: pipeline/validator.py
# ---------------------------------------------------------------------------

class TestValidator:

    def test_validate_genomics_file_type_valid(self):
        from pipeline.validator import validate_genomics_file_type
        assert validate_genomics_file_type("sample.fastq.gz") is True
        assert validate_genomics_file_type("NA12878.bam") is True
        assert validate_genomics_file_type("variants.vcf.gz") is True
        assert validate_genomics_file_type("reads.cram") is True

    def test_validate_genomics_file_type_invalid(self):
        from pipeline.validator import validate_genomics_file_type
        assert validate_genomics_file_type("report.pdf") is False
        assert validate_genomics_file_type("data.csv") is False
        assert validate_genomics_file_type("image.png") is False

    @patch("pipeline.validator.boto3")
    def test_verify_s3_checksum_found(self, mock_boto3):
        from pipeline.validator import verify_s3_checksum

        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        mock_s3.head_object.return_value = {"ETag": '"abc123def456"'}

        result = verify_s3_checksum("my-bucket", "BAM/SAMPLE-001/file.bam")
        assert result == "abc123def456"

    @patch("pipeline.validator.boto3")
    def test_verify_s3_checksum_not_found(self, mock_boto3):
        from pipeline.validator import verify_s3_checksum
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        result = verify_s3_checksum("my-bucket", "BAM/SAMPLE-001/missing.bam")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: pipeline/ingest.py
# ---------------------------------------------------------------------------

class TestBuildS3Key:

    def test_build_s3_key_structure(self):
        from pipeline.ingest import build_s3_key
        from connectors.dnanexus import RemoteFile

        remote = RemoteFile(
            platform="DNAnexus",
            file_id="file-XXXX",
            file_name="NA12878.bam",
            file_type="BAM",
            file_size_bytes=1000,
            source_path="project-X:/NA12878.bam",
            sample_id="SAMPLE-001",
            metadata={},
        )
        key = build_s3_key(remote)
        assert key == "BAM/SAMPLE-001/DNAnexus/NA12878.bam"

    def test_build_s3_key_fastq(self):
        from pipeline.ingest import build_s3_key
        from connectors.dnanexus import RemoteFile

        remote = RemoteFile(
            platform="HealthOmics",
            file_id="readset-001",
            file_name="sample_R1.fastq.gz",
            file_type="FASTQ",
            file_size_bytes=500,
            source_path="omics://store-X/readset-001",
            sample_id="SAMP-XYZ",
            metadata={},
        )
        key = build_s3_key(remote)
        assert key == "FASTQ/SAMP-XYZ/HealthOmics/sample_R1.fastq.gz"


class TestDNAnexusConnector:

    def test_infer_file_type_bam(self):
        from connectors.dnanexus import DNAnexusConnector
        import dxpy

        with patch.object(dxpy, "set_security_context"):
            with patch.dict("os.environ", {"DNANEXUS_TOKEN": "fake-token"}):
                conn = DNAnexusConnector.__new__(DNAnexusConnector)
                conn.SUPPORTED_EXTENSIONS = DNAnexusConnector.SUPPORTED_EXTENSIONS

        assert conn._infer_file_type("NA12878.bam") == "BAM"
        assert conn._infer_file_type("reads.fastq.gz") == "FASTQ"
        assert conn._infer_file_type("variants.vcf") == "VCF"
        assert conn._infer_file_type("report.pdf") == "OTHER"
