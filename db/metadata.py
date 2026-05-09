"""
db/metadata.py

Metadata registration and retrieval for the Genomics Data System.
All DB writes go through here so audit logging is never skipped.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from db.schema import get_connection

logger = logging.getLogger(__name__)


@dataclass
class GenomicsFileRecord:
    sample_id: str
    file_name: str
    file_type: str
    source_platform: str
    source_path: str
    s3_bucket: str
    s3_key: str
    file_size_bytes: int
    checksum_md5: Optional[str] = None
    checksum_sha256: Optional[str] = None
    storage_tier: str = "hot"
    status: str = "pending"
    metadata: Optional[dict] = field(default_factory=dict)


def _write_audit_log(conn, file_id: int, action: str, actor: str = "system",
                     before_state: dict = None, after_state: dict = None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO audit_log (file_id, action, actor, before_state, after_state)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            file_id,
            action,
            actor,
            json.dumps(before_state) if before_state else None,
            json.dumps(after_state) if after_state else None,
        ))


def register_sample(sample_id: str, patient_id: str = None,
                    project_id: str = None, assay_type: str = None,
                    organism: str = "human") -> str:
    """
    Insert a sample record if it doesn't already exist (upsert by sample_id).
    Returns the sample_id.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO samples (sample_id, patient_id, project_id, assay_type, organism)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sample_id) DO NOTHING
            """, (sample_id, patient_id, project_id, assay_type, organism))
        conn.commit()
        logger.info(f"Registered sample: {sample_id}")
        return sample_id
    finally:
        conn.close()


def register_file(record: GenomicsFileRecord) -> int:
    """
    Insert a new genomics file record and write an INGEST audit entry.
    Returns the generated file_id.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO genomics_files (
                    sample_id, file_name, file_type, source_platform,
                    source_path, s3_bucket, s3_key, file_size_bytes,
                    checksum_md5, checksum_sha256, storage_tier, status, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING file_id
            """, (
                record.sample_id,
                record.file_name,
                record.file_type,
                record.source_platform,
                record.source_path,
                record.s3_bucket,
                record.s3_key,
                record.file_size_bytes,
                record.checksum_md5,
                record.checksum_sha256,
                record.storage_tier,
                record.status,
                json.dumps(record.metadata or {}),
            ))
            file_id = cur.fetchone()[0]

        _write_audit_log(conn, file_id, action="INGEST",
                         after_state={"status": record.status, "s3_key": record.s3_key})
        conn.commit()
        logger.info(f"Registered file_id={file_id}: {record.file_name}")
        return file_id
    finally:
        conn.close()


def update_file_status(file_id: int, status: str, error_message: str = None):
    """Update status (and optional error message) on a genomics file record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE genomics_files
                SET status = %s, error_message = %s, updated_at = NOW()
                WHERE file_id = %s
            """, (status, error_message, file_id))

        _write_audit_log(conn, file_id, action="STATUS_CHANGE",
                         after_state={"status": status, "error_message": error_message})
        conn.commit()
        logger.debug(f"file_id={file_id} status → {status}")
    finally:
        conn.close()


def start_pipeline_run(file_id: int, run_type: str = "ingest") -> int:
    """
    Create a pipeline_runs row and return the run_id.
    Called at the start of each pipeline execution.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pipeline_runs (file_id, run_type, status)
                VALUES (%s, %s, 'running')
                RETURNING run_id
            """, (file_id, run_type))
            run_id = cur.fetchone()[0]
        conn.commit()
        logger.info(f"Started pipeline run_id={run_id} for file_id={file_id}")
        return run_id
    finally:
        conn.close()


def complete_pipeline_run(run_id: int, success: bool, error_message: str = None):
    """Mark a pipeline run as success or failed."""
    status = "success" if success else "failed"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pipeline_runs
                SET status = %s, completed_at = NOW(), error_message = %s
                WHERE run_id = %s
            """, (status, error_message, run_id))
        conn.commit()
        logger.info(f"Completed run_id={run_id}: {status}")
    finally:
        conn.close()


def get_files_by_sample(sample_id: str) -> list[dict]:
    """Return all file records for a given sample."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT file_id, file_name, file_type, source_platform,
                       s3_bucket, s3_key, storage_tier, status, checksum_md5,
                       file_size_bytes, created_at
                FROM genomics_files
                WHERE sample_id = %s
                ORDER BY created_at DESC
            """, (sample_id,))
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def update_storage_tier(file_id: int, tier: str):
    """Update storage_tier when S3 lifecycle transitions the object."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE genomics_files
                SET storage_tier = %s, updated_at = NOW()
                WHERE file_id = %s
            """, (tier, file_id))
        _write_audit_log(conn, file_id, action="TIER_CHANGE", after_state={"storage_tier": tier})
        conn.commit()
        logger.debug(f"file_id={file_id} storage_tier → {tier}")
    finally:
        conn.close()


def get_files_by_status(status: str) -> list[dict]:
    """Return all file records with a given status — used by archival jobs."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT file_id, file_name, file_type, s3_bucket, s3_key,
                       storage_tier, last_accessed, file_size_bytes, status
                FROM genomics_files
                WHERE status = %s
                ORDER BY created_at
            """, (status,))
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
