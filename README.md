# Genomics Data System

A production-grade genomics data pipeline for retrieving, archiving, and managing metadata for genomics files (FASTQ, BAM, VCF) across DNAnexus and AWS HealthOmics.

## Architecture

```
[Data Sources]              [Ingestion]              [Storage & DB]
DNAnexus        ──►                                
                      Step Functions          ──►   S3 (tiered: hot/warm/cold)
AWS HealthOmics ──►   Orchestration           ──►   RDS PostgreSQL (metadata)
                       │                            CloudWatch (monitoring)
                       ├── Platform Connectors
                       ├── File Validator (checksum)
                       ├── Streaming Transfer
                       └── Metadata Registration
```

## Components

| Component | Description |
|---|---|
| `connectors/` | Platform-specific connectors for DNAnexus and AWS HealthOmics |
| `db/` | PostgreSQL schema, models, and metadata registration logic |
| `pipeline/` | Step Functions orchestration and workflow definitions |
| `lambdas/` | AWS Lambda functions for S3 event processing |
| `terraform/` | IaC for all AWS resources |
| `tests/` | Pytest unit and integration tests |

## Genomics File Types Supported

- **FASTQ** — Raw sequencer output (Illumina, PacBio)
- **BAM** — Aligned reads (can be 100GB+, handled via streaming)
- **VCF** — Variant call format

## Running Locally

Docker is required for the local PostgreSQL instance.

```bash
# First time setup
pip install -r requirements-dev.txt

# Start Postgres, init schema, and run the demo
make demo
```

The demo runs the full ingestion pipeline end-to-end using mocked AWS and fake platform files — no real DNAnexus or HealthOmics credentials needed.

### All available commands

| Command | Description |
|---|---|
| `make up` | Start local PostgreSQL container |
| `make init` | Initialize DB schema |
| `make demo` | Run end-to-end pipeline demo (mocked AWS) |
| `make test` | Run pytest with coverage |
| `make lint` | Run ruff + black check |
| `make psql` | Open psql shell to inspect data |
| `make logs` | Tail PostgreSQL container logs |
| `make down` | Stop and remove PostgreSQL container |

`make demo` automatically runs `make up` and `make init` first — you don't need to call them separately. The container can be left running between sessions; subsequent `make demo` runs will reuse it. Only run `make down` when you want a clean slate (e.g. to reset the database to empty).

## Environment Variables

```bash
# AWS
AWS_REGION=us-east-1
S3_BUCKET_NAME=genomics-data-prod
RDS_HOST=your-rds-endpoint.rds.amazonaws.com
RDS_DB=genomics_metadata
RDS_USER=genomics_user
RDS_PASSWORD=...

# DNAnexus
DNANEXUS_TOKEN=your-token

# Storage tier thresholds (days)
WARM_TIER_DAYS=30
COLD_TIER_DAYS=90
```

## Storage Tiers

| Tier | S3 Class | Transition |
|---|---|---|
| Hot | S3 Standard | On ingest |
| Warm | S3 Standard-IA | 30 days no access |
| Cold | S3 Glacier | 90 days no access |

## Author

Walt Shands — [github.com/wshands](https://github.com/wshands)
