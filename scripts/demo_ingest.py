"""
scripts/demo_ingest.py

Local end-to-end demo of the genomics ingestion pipeline.

Uses moto to mock S3 so no real AWS credentials are needed.
Connects to the local PostgreSQL started by `make up`.

Run: python scripts/demo_ingest.py
  or: make demo
"""

import os
import sys
import logging

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Moto must be activated before boto3 is imported anywhere
from moto import mock_aws
import boto3

from connectors.dnanexus import BaseConnector, RemoteFile
from pipeline.ingest import ingest_file
from db.metadata import get_files_by_sample, get_files_by_status, register_sample
from db.schema import get_connection, init_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("demo")

BUCKET = os.environ.get("S3_BUCKET_NAME", "genomics-data-local")
REGION = os.environ.get("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Mock connector — returns fake files without touching DNAnexus or HealthOmics
# ---------------------------------------------------------------------------


class MockConnector(BaseConnector):
    """Simulates a platform connector for local testing."""

    DEMO_FILES = [
        RemoteFile(
            platform="DNAnexus",
            file_id="file-DEMO001",
            file_name="NA12878_R1.fastq.gz",
            file_type="FASTQ",
            file_size_bytes=2_500_000_000,
            source_path="project-DEMO:/reads/NA12878_R1.fastq.gz",
            sample_id="SAMPLE-NA12878",
            metadata={
                "dx_project": "project-DEMO",
                "dx_folder": "/reads",
                "dx_tags": ["WGS"],
            },
        ),
        RemoteFile(
            platform="DNAnexus",
            file_id="file-DEMO002",
            file_name="NA12878.bam",
            file_type="BAM",
            file_size_bytes=85_000_000_000,
            source_path="project-DEMO:/aligned/NA12878.bam",
            sample_id="SAMPLE-NA12878",
            metadata={
                "dx_project": "project-DEMO",
                "dx_folder": "/aligned",
                "assay": "WGS",
            },
        ),
        RemoteFile(
            platform="HealthOmics",
            file_id="readset-DEMO003",
            file_name="BRCA_variants.vcf.gz",
            file_type="VCF",
            file_size_bytes=125_000_000,
            source_path="omics://store-DEMO/readSet/DEMO003",
            sample_id="SAMPLE-BRCA01",
            metadata={"store_id": "store-DEMO", "subject_id": "SUBJ-001"},
        ),
    ]

    def list_files(self, project_id: str, file_type: str = None) -> list[RemoteFile]:
        if file_type:
            return [f for f in self.DEMO_FILES if f.file_type == file_type]
        return self.DEMO_FILES

    def stream_to_s3(
        self, remote_file: RemoteFile, s3_bucket: str, s3_key: str
    ) -> dict:
        """Write a small placeholder object to mocked S3."""
        s3 = boto3.client("s3", region_name=REGION)
        body = f"MOCK GENOMICS DATA: {remote_file.file_name} ({remote_file.file_size_bytes} bytes)\n".encode()
        s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=body)
        import hashlib

        md5 = hashlib.md5(body).hexdigest()
        sha256 = hashlib.sha256(body).hexdigest()
        logger.info(f"Mock upload: s3://{s3_bucket}/{s3_key}")
        return {
            "checksum_md5": md5,
            "checksum_sha256": sha256,
            "bytes_transferred": len(body),
        }

    def get_metadata(self, file_id: str) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------


def _hr(char="─", width=70):
    print(char * width)


def _print_results(files: list[dict]):
    for f in files:
        size_gb = (f.get("file_size_bytes") or 0) / 1e9
        print(
            f"  [{f['file_id']:>3}] {f['file_type']:<6}  {f['file_name']:<35}"
            f"  {size_gb:>7.2f} GB  status={f['status']}"
        )


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


@mock_aws
def run_demo():
    # Set dummy AWS credentials for moto
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    os.environ["S3_BUCKET_NAME"] = BUCKET

    print()
    _hr("═")
    print("  Genomics Data System — Local Demo")
    _hr("═")

    # 1. Ensure schema is initialized
    print("\n[1/4] Initializing database schema...")
    init_schema()

    # 2. Create mocked S3 bucket
    print(f"\n[2/4] Creating mock S3 bucket: {BUCKET}")
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    # 3. Register samples (required before files due to FK constraint)
    register_sample(
        "SAMPLE-NA12878",
        patient_id="PT-001",
        project_id="project-DEMO",
        assay_type="WGS",
    )
    register_sample(
        "SAMPLE-BRCA01",
        patient_id="PT-002",
        project_id="project-DEMO",
        assay_type="WES",
    )

    print("\n[3/4] Ingesting demo files...\n")
    connector = MockConnector()
    results = []

    for remote_file in connector.list_files(project_id="project-DEMO"):
        print(
            f"  → {remote_file.file_name} ({remote_file.file_type}, "
            f"{remote_file.file_size_bytes / 1e9:.1f} GB)"
        )
        try:
            result = ingest_file(remote_file, connector)
            results.append(result)
            print(f"     ✓ file_id={result['file_id']}  s3={result['s3_uri']}\n")
        except Exception as e:
            print(f"     ✗ FAILED: {e}\n")

    # 4. Query metadata DB
    print("\n[4/4] Metadata database — current state:\n")

    _hr()
    print("  Sample: SAMPLE-NA12878")
    _hr()
    _print_results(get_files_by_sample("SAMPLE-NA12878"))

    print()
    _hr()
    print("  Sample: SAMPLE-BRCA01")
    _hr()
    _print_results(get_files_by_sample("SAMPLE-BRCA01"))

    print()
    _hr()
    print("  All ingested files")
    _hr()
    _print_results(get_files_by_status("ingested"))

    # 5. Show a raw audit log entry
    print()
    _hr()
    print("  Audit log (last 3 entries)")
    _hr()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT log_id, file_id, action, actor, logged_at
                FROM audit_log ORDER BY logged_at DESC LIMIT 3
            """)
            for row in cur.fetchall():
                print(
                    f"  [{row[0]:>3}] file_id={row[1]}  action={row[2]:<15} "
                    f"actor={row[3]:<10}  at={row[4]}"
                )
    finally:
        conn.close()

    print()
    _hr("═")
    print(f"  Done — {len(results)} file(s) ingested successfully.")
    _hr("═")
    print()


if __name__ == "__main__":
    run_demo()
