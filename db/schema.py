"""
db/schema.py

PostgreSQL schema definition and initialization for the Genomics Data System.
Tables: genomics_files, samples, pipeline_runs, audit_log
"""

import os
import argparse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

DDL = """
-- Samples table: one row per biological sample
CREATE TABLE IF NOT EXISTS samples (
    sample_id       VARCHAR(64) PRIMARY KEY,
    patient_id      VARCHAR(64),
    project_id      VARCHAR(64),
    assay_type      VARCHAR(64),   -- WGS, WES, RNA-seq, scATAC-seq, etc.
    organism        VARCHAR(64) DEFAULT 'human',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Genomics files: one row per file tracked in the system
CREATE TABLE IF NOT EXISTS genomics_files (
    file_id         BIGSERIAL PRIMARY KEY,
    sample_id       VARCHAR(64) REFERENCES samples(sample_id),
    file_name       VARCHAR(512) NOT NULL,
    file_type       VARCHAR(16) NOT NULL CHECK (file_type IN ('FASTQ','BAM','VCF','CRAM','BED','OTHER')),
    source_platform VARCHAR(32) NOT NULL CHECK (source_platform IN ('DNAnexus','HealthOmics','Illumina','PacBio','Local')),
    source_path     TEXT,                  -- original path on source platform
    s3_bucket       VARCHAR(255),
    s3_key          TEXT,                  -- full S3 object key
    storage_tier    VARCHAR(16) DEFAULT 'hot' CHECK (storage_tier IN ('hot','warm','cold')),
    file_size_bytes BIGINT,
    checksum_md5    VARCHAR(64),
    checksum_sha256 VARCHAR(128),
    status          VARCHAR(32) DEFAULT 'pending'
                    CHECK (status IN ('pending','transferring','ingested','archived','failed','deleted')),
    error_message   TEXT,
    last_accessed   TIMESTAMPTZ,
    metadata        JSONB,                 -- flexible platform-specific metadata
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_files_sample_id   ON genomics_files(sample_id);
CREATE INDEX IF NOT EXISTS idx_files_file_type   ON genomics_files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_status      ON genomics_files(status);
CREATE INDEX IF NOT EXISTS idx_files_platform    ON genomics_files(source_platform);
CREATE INDEX IF NOT EXISTS idx_files_storage_tier ON genomics_files(storage_tier);
CREATE INDEX IF NOT EXISTS idx_files_metadata    ON genomics_files USING gin(metadata);

-- Pipeline runs: one row per execution of the ingestion pipeline
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    file_id         BIGINT REFERENCES genomics_files(file_id),
    run_type        VARCHAR(32) DEFAULT 'ingest'
                    CHECK (run_type IN ('ingest','archive','validate','delete')),
    status          VARCHAR(16) DEFAULT 'running'
                    CHECK (status IN ('running','success','failed','retrying')),
    attempt_number  INT DEFAULT 1,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    duration_secs   FLOAT GENERATED ALWAYS AS (
                        EXTRACT(EPOCH FROM (completed_at - started_at))
                    ) STORED,
    error_message   TEXT,
    run_metadata    JSONB
);

CREATE INDEX IF NOT EXISTS idx_runs_file_id  ON pipeline_runs(file_id);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON pipeline_runs(status);

-- Audit log: immutable record of every file operation (HIPAA compliance)
CREATE TABLE IF NOT EXISTS audit_log (
    log_id          BIGSERIAL PRIMARY KEY,
    file_id         BIGINT,
    action          VARCHAR(32) NOT NULL,  -- INGEST, ARCHIVE, ACCESS, DELETE, etc.
    actor           VARCHAR(128),          -- system service or user
    before_state    JSONB,
    after_state     JSONB,
    ip_address      VARCHAR(64),
    logged_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_file_id  ON audit_log(file_id);
CREATE INDEX IF NOT EXISTS idx_audit_action   ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_logged_at ON audit_log(logged_at);
"""


def get_connection():
    return psycopg2.connect(
        host=os.environ.get("RDS_HOST", "localhost"),
        dbname=os.environ.get("RDS_DB", "genomics_metadata"),
        user=os.environ.get("RDS_USER", "postgres"),
        password=os.environ.get("RDS_PASSWORD", ""),
        port=int(os.environ.get("RDS_PORT", 5432)),
    )


def init_schema():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        print("✅ Schema initialized successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genomics DB schema manager")
    parser.add_argument("--init", action="store_true", help="Initialize schema")
    args = parser.parse_args()
    if args.init:
        init_schema()
