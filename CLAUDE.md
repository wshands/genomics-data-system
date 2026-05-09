# Genomics Data System — CLAUDE.md

Context for AI assistants working in this repo.

---

## What this system does

Retrieves genomics files (FASTQ, BAM, VCF, CRAM) from **DNAnexus** and **AWS HealthOmics**, streams them to a tiered **S3** bucket, and records metadata in a **PostgreSQL** database. Built as an interview portfolio for a DevOps Engineer role at Natera — reflects real production work done at Biogen (migrating ~0.5 PB DNAnexus → AWS).

---

## Architecture

```
EventBridge (nightly 2am UTC)
        │
        ▼
Step Functions state machine
        │
        ├── DetectFiles      → lambdas/detect_files.py
        │     Polls DNAnexus/HealthOmics, diffs against DB,
        │     returns only files not yet ingested.
        │
        └── Map (≤10 parallel)
              ├── ValidateFile    → lambdas/validate_file.py
              ├── TransferToS3   → lambdas/transfer_file.py
              ├── RegisterMetadata → lambdas/register_metadata.py
              └── NotifyCompletion → lambdas/notify_completion.py (after Map)

S3 ObjectCreated events → lambdas/s3_event_handler.py (confirms ingest status)
```

---

## Key design decisions

**Step Functions data contract** — each Lambda's output dict is the next Lambda's input. Fields accumulate across steps:
- After `DetectFiles`: `{platform, file_id, file_name, file_type, file_size_bytes, source_path, sample_id, metadata}`
- After `ValidateFile`: adds `db_file_id, run_id, s3_bucket, s3_key`
- After `TransferToS3`: adds `checksum_md5, checksum_sha256, bytes_transferred`
- After `RegisterMetadata`: returns summary `{db_file_id, file_name, s3_uri, status: "ingested"}`
- `NotifyCompletion` receives the full Map results array

**Idempotency** — `validate_file.py` checks `source_path` against the DB before inserting. If already ingested, raises `AlreadyIngestedException` which the Step Functions `ValidateFile` Catch block routes to `FileSucceeded` (not `FileFailed`). If `pending`/`transferring`, it re-uses the existing `file_id` for retry safety.

**Partial failure handling** — `FileFailed` in the Map iterator is `Type = "Succeed"` (not `Fail`). This means individual file failures don't abort the whole Map — `NotifyCompletion` always runs and tallies success/failure counts. Failed items lack a `status: "ingested"` field, so `notify_completion.py` counts them correctly.

**Streaming transfers** — files are never fully loaded into memory. DNAnexus and HealthOmics connectors use chunked reads + boto3 multipart upload (100 MB parts, 4 threads). The `transfer_file` Lambda gets 3 GB RAM and a 15-minute timeout for 100 GB+ BAM files.

**S3 ETag caveat** — for multipart uploads, the S3 ETag is NOT a plain MD5. `transfer_file.py` only checks that the object exists post-upload (not ETag == MD5). The CLI path in `pipeline/ingest.py` does compare ETag to MD5 and will raise a false ValueError on large files — known limitation of that code path, which is not used by the Lambda pipeline.

**Audit log** — every file operation writes to `audit_log` table for HIPAA compliance. The `_write_audit_log()` function in `db/metadata.py` is called inside the same DB transaction as the primary write.

---

## Directory structure

```
connectors/         Platform connectors (DNAnexus, HealthOmics)
db/                 PostgreSQL schema + metadata CRUD
pipeline/           Ingestion orchestrator (CLI path) + file validator
lambdas/            One handler per Step Functions task
terraform/          All AWS infrastructure as code
scripts/            Local dev utilities
tests/              Pytest unit tests (mocked AWS + DB)
.github/workflows/  CI/CD (lint → test → build ECR → Terraform deploy)
```

---

## Running locally

Requires Docker (for PostgreSQL).

```bash
pip install -r requirements-dev.txt
make demo        # up + init schema + run end-to-end demo
make test        # run pytest
make psql        # open psql shell
```

`make demo` runs the full pipeline with mocked AWS (`moto`) and a `MockConnector` — no real credentials needed. See `scripts/demo_ingest.py`.

---

## Environment variables

| Variable | Used by | Notes |
|---|---|---|
| `RDS_HOST` | all Lambdas, CLI | Set by Terraform from RDS endpoint |
| `RDS_DB` | all Lambdas | `genomics_metadata` |
| `RDS_USER` | all Lambdas | `genomics_user` |
| `RDS_PASSWORD` | all Lambdas | Sensitive — from `var.db_password` |
| `S3_BUCKET_NAME` | all Lambdas | Set by Terraform |
| `DNANEXUS_TOKEN` | `detect_files`, `transfer_file` | DNAnexus API auth |
| `DNANEXUS_PROJECT_ID` | `detect_files` | Which DNAnexus project to poll |
| `HEALTHOMICS_STORE_ID` | `detect_files` | Which HealthOmics store to poll |
| `SNS_TOPIC_ARN` | `notify_completion` | Set by Terraform |
| `AWS_REGION` | connectors | Default `us-east-1` |

---

## Terraform variables (terraform.tfvars)

```hcl
db_password          = "..."
dnanexus_project_id  = "project-XXXX"
dnanexus_token       = "your-dnanexus-api-token"
healthomics_store_id = "store-XXXX"
```

Terraform creates everything else from scratch: VPC, public/private subnets, Internet Gateway, NAT Gateway, route tables, security groups, and RDS subnet group (`terraform/networking.tf`). No pre-existing infrastructure required.

---

## Database schema

Four tables in `db/schema.py`:

| Table | Purpose |
|---|---|
| `samples` | One row per biological sample |
| `genomics_files` | One row per tracked file — source, S3 location, checksums, tier, status |
| `pipeline_runs` | One row per pipeline execution — timing, status, errors |
| `audit_log` | Immutable record of every file operation (HIPAA) |

S3 key pattern: `{file_type}/{sample_id}/{platform}/{file_name}` e.g. `BAM/SAMPLE-001/DNAnexus/NA12878.bam`

---

## Known limitations

- **RDS password** is passed as a Lambda env var. Production should use AWS Secrets Manager.
- **CLI path** (`pipeline/ingest.py`) compares S3 ETag to computed MD5, which breaks for multipart uploads (files > 100 MB). The Lambda pipeline path avoids this comparison.
- **HealthOmics `get_metadata()`** raises `NotImplementedError` — metadata comes from `list_files()` only.
