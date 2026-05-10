"""
lambdas/detect_files.py

Step Functions Task — Step 1: DetectFiles

Lists all files on the target platform(s), diffs against the metadata DB,
and returns only files not yet ingested (status not in pending/transferring/ingested).

Expected Step Functions / EventBridge input:
{
  "source": "scheduled" | "manual",
  "platform": "dnanexus" | "healthomics" | "all",
  "project_id": "project-XXX",   # optional — falls back to env var
  "file_type": "FASTQ"           # optional filter
}

Output consumed by Step Functions Map state (InputPath = "$.files"):
{
  "file_count": 3,
  "files": [
    {
      "platform": "DNAnexus",
      "file_id": "file-XXX",
      "file_name": "NA12878.bam",
      "file_type": "BAM",
      "file_size_bytes": 85000000000,
      "source_path": "project-XXX:/aligned/NA12878.bam",
      "sample_id": "SAMPLE-001",
      "metadata": {...}
    },
    ...
  ]
}
"""

import json
import logging
import os
import sys

sys.path.insert(0, "/var/task")

from db.schema import get_connection, init_schema

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment-variable fallbacks for project IDs
DNANEXUS_PROJECT_ID = os.environ.get("DNANEXUS_PROJECT_ID", "")
HEALTHOMICS_STORE_ID = os.environ.get("HEALTHOMICS_STORE_ID", "")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_already_ingested_paths(platform: str) -> set[str]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_path
                FROM genomics_files
                WHERE source_platform = %s
                  AND status = 'ingested'
            """,
                (platform,),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Detection logic per platform
# ---------------------------------------------------------------------------


def _detect_dnanexus(project_id: str, file_type: str | None, force: bool = False) -> list[dict]:
    from connectors.dnanexus import DNAnexusConnector

    connector = DNAnexusConnector()
    all_files = connector.list_files(project_id, file_type=file_type)
    known_paths = set() if force else _get_already_ingested_paths("DNAnexus")

    new_files = [f for f in all_files if f.source_path not in known_paths]
    logger.info(
        f"DNAnexus {project_id}: {len(all_files)} total, "
        f"{len(known_paths)} known, {len(new_files)} new (force={force})"
    )
    return [_remote_file_to_dict(f, force=force) for f in new_files]


def _detect_healthomics(store_id: str, file_type: str | None, force: bool = False) -> list[dict]:
    from connectors.healthomics import HealthOmicsConnector

    connector = HealthOmicsConnector()
    all_files = connector.list_files(store_id, file_type=file_type)
    known_paths = set() if force else _get_already_ingested_paths("HealthOmics")

    new_files = [f for f in all_files if f.source_path not in known_paths]
    logger.info(
        f"HealthOmics {store_id}: {len(all_files)} total, "
        f"{len(known_paths)} known, {len(new_files)} new (force={force})"
    )
    return [_remote_file_to_dict(f, force=force) for f in new_files]


def _remote_file_to_dict(remote_file, force: bool = False) -> dict:
    """Serialize a RemoteFile dataclass to a plain dict for Step Functions."""
    return {
        "platform": remote_file.platform,
        "file_id": remote_file.file_id,
        "file_name": remote_file.file_name,
        "file_type": remote_file.file_type,
        "file_size_bytes": remote_file.file_size_bytes,
        "source_path": remote_file.source_path,
        "sample_id": remote_file.sample_id,
        "metadata": remote_file.metadata or {},
        "force": force,
    }


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context) -> dict:
    init_schema()  # CREATE TABLE IF NOT EXISTS — idempotent, initializes RDS on first run
    logger.info(f"DetectFiles invoked: {json.dumps(event)}")

    platform = event.get("platform", "all").lower()
    file_type = event.get("file_type")  # optional — None means all types
    force = bool(event.get("force", False))

    if force:
        logger.info("Force mode enabled — skipping already-ingested check")

    new_files: list[dict] = []

    if platform in ("dnanexus", "all"):
        project_id = event.get("project_id") or DNANEXUS_PROJECT_ID
        if not project_id:
            logger.warning("DNANEXUS_PROJECT_ID not set — skipping DNAnexus detection")
        else:
            new_files.extend(_detect_dnanexus(project_id, file_type, force=force))

    if platform in ("healthomics", "all"):
        store_id = event.get("project_id") or HEALTHOMICS_STORE_ID
        if not store_id:
            logger.warning(
                "HEALTHOMICS_STORE_ID not set — skipping HealthOmics detection"
            )
        else:
            new_files.extend(_detect_healthomics(store_id, file_type, force=force))

    if platform not in ("dnanexus", "healthomics", "all"):
        raise ValueError(
            f"Unknown platform '{platform}'. Use: dnanexus | healthomics | all"
        )

    logger.info(f"Detected {len(new_files)} new file(s) across platform(s): {platform}")

    return {
        "file_count": len(new_files),
        "files": new_files,
    }
