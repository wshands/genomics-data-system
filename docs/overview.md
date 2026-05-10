# Genomics Data System — Overview

## Purpose

The Genomics Data System automates the ingestion of large genomics files — FASTQ, BAM, VCF, and CRAM — from two source platforms (DNAnexus and AWS HealthOmics) into a centralized AWS data lake. It handles files up to 100 GB+, records metadata in a PostgreSQL database, and provides a tiered storage strategy that balances access speed against cost. The system was designed to reflect real production work migrating approximately 0.5 PB of genomics data from DNAnexus to AWS at Biogen.

---

## Architecture

A nightly EventBridge rule (2 AM UTC) triggers an AWS Step Functions state machine that runs the ingestion pipeline. The pipeline is built as a series of Lambda functions, each responsible for one stage of the workflow:

```
EventBridge (nightly)
    │
    └── Step Functions state machine
          │
          ├── DetectFiles       Poll DNAnexus / HealthOmics; diff against DB
          │
          └── Map (≤10 parallel iterations per file)
                ├── ValidateFile       Validate file type; create DB record
                ├── TransferToS3       Stream file to S3 via multipart upload
                ├── RegisterMetadata   Write checksums and status to PostgreSQL
                └── NotifyCompletion   Publish summary to SNS (Slack / email)
```

A separate Lambda handles S3 `ObjectCreated` events to confirm ingest status and track storage-tier transitions as S3 lifecycle rules move objects from Standard → Standard-IA (30 days) → Glacier (90 days).

---

## Key Design Decisions

**Streaming transfers.** Files are never fully loaded into memory. Both platform connectors use chunked reads combined with boto3 multipart upload (100 MB parts, 4 threads), keeping Lambda memory usage flat regardless of file size.

**Idempotent processing.** The `ValidateFile` step checks `source_path` against the database before inserting. Files already ingested are routed to a `FileSucceeded` branch via a Step Functions Catch block rather than being reprocessed or causing failures.

**Partial failure isolation.** Individual file failures within the Map state do not abort the entire pipeline. The `FileFailed` branch is typed as `Succeed` in Step Functions, so `NotifyCompletion` always runs and reports a complete success/failure tally.

**Audit trail.** Every file operation writes to an `audit_log` table within the same database transaction as the primary write, providing an immutable HIPAA-compliant record of all data movements.

**No long-lived credentials.** The CI/CD pipeline authenticates to AWS via OIDC, exchanging a GitHub-issued JWT for short-lived STS credentials. No AWS access keys are stored as secrets.

---

## Infrastructure

All infrastructure is defined in Terraform and created from scratch — no pre-existing AWS resources required. Key components:

| Resource | Purpose |
|---|---|
| VPC (10.0.0.0/16) | Isolated network with public and private subnets across 2 AZs |
| NAT Gateway | Allows private-subnet Lambdas to reach S3, SNS, DNAnexus, and HealthOmics |
| S3 bucket | Tiered genomics data store with versioning, KMS encryption, and lifecycle rules |
| RDS PostgreSQL 17 | Metadata database — samples, files, pipeline runs, audit log |
| ECR repository | Container image registry for Lambda functions |
| Step Functions | Orchestrates the pipeline with parallel Map execution and error routing |
| EventBridge | Nightly cron schedule; also supports manual execution |
| SNS topic | Publishes pipeline completion summaries for alerting |
| CloudWatch alarms | Monitors Lambda error rates and Step Functions execution failures |

---

## Technology Stack

**Languages & frameworks:** Python 3.11, Terraform 1.6+, bash

**AWS services:** Lambda, Step Functions, EventBridge, S3, RDS (PostgreSQL), ECR, SNS, CloudWatch, HealthOmics, IAM, VPC

**External platforms:** DNAnexus (dxpy SDK), AWS HealthOmics

**Local development:** Docker Compose (PostgreSQL), moto (AWS mocks), pytest, ruff, black

**CI/CD:** GitHub Actions — lint → test → build ECR image → Terraform deploy (main branch only)
