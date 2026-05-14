# Genomics Data System — Interview Description

## Background and Motivation

The Genomics Data System is a production-grade AWS data pipeline that automates the ingestion of large genomics files — FASTQ, BAM, VCF, and CRAM — from external research platforms into a centralized, cost-optimized cloud data lake. The system was designed as an interview portfolio piece for a DevOps Engineer role at Natera, but it directly reflects work done in production at Biogen, where I led the migration of approximately 0.5 petabytes of genomics sequencing data from DNAnexus to AWS. That migration involved exactly the challenges this system solves: dealing with files that are 50–100 GB each, tracking provenance and checksums for regulatory compliance, handling partial failures gracefully in large batch runs, and building infrastructure that can be version-controlled, reviewed, and torn down reliably.

---

## What the System Does

The pipeline ingests genomics files from two source platforms — **DNAnexus** (a cloud bioinformatics platform used widely in pharma research) and **AWS HealthOmics** (Amazon's managed genomics store). Each night at 2 AM UTC, an EventBridge schedule triggers an AWS Step Functions state machine that runs the full ingestion workflow. The pipeline detects which files on those platforms have not yet been ingested, validates their file types, streams them to S3 using chunked multipart upload, records checksums and metadata in a PostgreSQL database, and publishes a completion summary via SNS. The result is a durable, auditable, cost-tiered data lake of genomics files with a queryable metadata layer sitting on top of it.

---

## Pipeline Architecture

The core pipeline is a Step Functions state machine with five Lambda-backed steps. I chose Step Functions rather than a simple cron Lambda or Airflow job for three reasons: it gives you a visual execution graph in the console that makes debugging straightforward, it provides built-in retry logic and error routing, and its `Map` state lets you process multiple files concurrently without managing that parallelism yourself.

The first step, **DetectFiles**, polls both platforms, compares the results against the PostgreSQL database, and returns only files that have not yet been successfully ingested. It returns a list of file descriptors that the Step Functions `Map` state then fans out — up to 10 files processed in parallel. Each parallel branch runs **ValidateFile** → **TransferToS3** → **RegisterMetadata** in sequence, with **NotifyCompletion** executing after the entire Map completes. A sixth Lambda, the **S3 event handler**, reacts to S3 `ObjectCreated` events to confirm ingest status and track storage-tier transitions as S3 lifecycle rules migrate objects from Standard to Standard-IA to Glacier over time.

One architectural decision I'm particularly deliberate about is the **Step Functions data contract**. Each Lambda's output dict is the next Lambda's input. Fields accumulate across steps: `DetectFiles` returns the platform metadata; `ValidateFile` adds the database file ID and pipeline run ID; `TransferToS3` adds the MD5 and SHA-256 checksums; `RegisterMetadata` returns a final summary. This accumulation pattern means each Lambda is self-contained — it has everything it needs from prior steps without any shared state or external lookups, which makes individual steps easy to test and rerun in isolation.

---

## Key Engineering Decisions

**Streaming transfers with zero memory buffering.** Both platform connectors read from the source platform in 64 MB chunks, computing MD5 and SHA256 checksums incrementally as each chunk passes through. Those chunks are piped into boto3 multipart upload via a generator adapter, which reassembles them into 100 MB S3 parts uploaded 4 at a time across a thread pool. Files are never fully loaded into Lambda memory — peak usage is roughly 730 MB regardless of file size. The `transfer_file` Lambda is provisioned with 3 GB of RAM and a 15-minute timeout specifically for 100 GB+ BAM files.

**Idempotency at every step.** Step Functions retries failed tasks automatically, so every Lambda must be safe to run more than once for the same file. `DetectFiles` filters out files already in `ingested` status at the DB query level. `ValidateFile` checks `source_path` before inserting — if a `pending` or `transferring` record already exists (from a prior attempt), it reuses that record rather than creating a duplicate. If a file is already `ingested` and `force` is not set, it raises a typed exception (`AlreadyIngestedException`) that the Step Functions Catch block routes to the `FileSucceeded` branch rather than `FileFailed`, keeping the success tallies accurate.

**Partial failure isolation.** In a batch run of 50 files, a single network timeout on one large BAM should not abort the other 49. The `FileFailed` branch in the Map iterator is typed as `Type = "Succeed"` in the Step Functions definition. This means individual file failures are swallowed at the iterator level and reported as failed items in the final summary — but they never propagate upward to kill the Map or skip `NotifyCompletion`. The notification Lambda counts items with `status: "ingested"` vs. those missing that field to produce the success/failure tally.

**Immutable audit logging for HIPAA compliance.** Every file operation — ingestion, status change, storage tier change, access, deletion — writes a row to an `audit_log` table inside the same PostgreSQL transaction as the primary write. The audit log's `file_id` column is deliberately not a foreign key: if a file record is hard-deleted, the audit history must survive. This design ensures the audit trail can never be accidentally skipped by a Lambda that forgets to call a secondary logging function, because the log write happens atomically with the primary record update inside `db/metadata.py`.

**S3 key naming convention.** Files land at `{file_type}/{sample_id}/{platform}/{file_name}` — for example, `BAM/SAMPLE-001/DNAnexus/NA12878.bam`. This structure makes it straightforward to use S3 inventory, Athena, or prefix-scoped lifecycle rules if the tiering strategy ever needs to vary by file type or platform.

---

## Infrastructure as Code

The entire AWS environment is defined in Terraform and creates itself from scratch with no pre-existing infrastructure required. The Terraform configuration provisions a VPC with public and private subnets across two availability zones, an Internet Gateway, a NAT Gateway (so private-subnet Lambdas can reach S3 and the external APIs), security groups with least-privilege rules between Lambda and RDS, an RDS PostgreSQL 17 instance with Multi-AZ and KMS encryption, an ECR repository, the Step Functions state machine and all six Lambda functions as container images, EventBridge schedule rules, SNS topic, CloudWatch log groups with 30-day retention, and CloudWatch alarms for Lambda error rate and Step Functions execution failures.

Lambda functions are deployed as container images (not ZIP packages) for two reasons: the connectors require compiled dependencies like `psycopg2` that don't cross-compile cleanly from macOS to Lambda's `linux/amd64` runtime, and container images make it straightforward to use a single `Dockerfile` for both local development and production deployment. All six Lambda functions share the same image, with each function's entrypoint set via `image_config.command` in Terraform.

One infrastructure challenge I worked through was that Terraform's `aws_lambda_function` resource does not detect changes to a `:latest` ECR tag — it only re-creates the function if the `image_uri` value changes in state. To handle this, the deploy script explicitly calls `aws lambda update-function-code` for all six functions after every image push, forcing Lambda to pull the new image regardless of whether the URI string changed.

---

## CI/CD Pipeline

The GitHub Actions workflow runs on every push. It has three sequential jobs: **Lint & Test** (ruff, black, pytest with moto mocks), **Build & Push** (builds the `linux/amd64` container image and pushes to ECR), and **Terraform Deploy** (applies infrastructure changes). The deploy job only runs on pushes to `main`, so pull requests get lint and test coverage without touching AWS. The CI pipeline authenticates to AWS using GitHub's OIDC provider, exchanging a short-lived JWT for STS credentials — no long-lived access keys are stored as GitHub secrets.

---

## Local Development

The system runs entirely locally without real AWS credentials. `make demo` starts a Docker PostgreSQL instance, initializes the schema, and runs a full end-to-end pipeline using a `MockConnector` and moto-mocked S3. This lets any developer verify the full pipeline logic — detect, validate, transfer, register, notify — without touching AWS. The test suite follows the same pattern: pytest with moto for S3 and mocked database connections, so tests are fast, deterministic, and require no external dependencies.

---

## Reflecting on the Design

The system is intentionally scoped to what a single DevOps engineer would own: infrastructure, deployment automation, data movement, and observability. It deliberately does not include secondary analysis, variant calling, or anything upstream of "get the file from A to B safely." The pieces I would add in a real production environment — Secrets Manager for the database password instead of an environment variable, a bastion or SSM Session Manager for direct RDS access, cost-allocation tags per project, S3 Object Lambda for on-the-fly format conversion — are called out in the codebase as known limitations rather than papered over. I think that kind of transparency about scope is more useful in an interview context than a system that looks complete but has hidden shortcuts.
