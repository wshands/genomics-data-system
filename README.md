# Genomics Data System

A production-grade genomics data pipeline for retrieving, archiving, and managing metadata for genomics files (FASTQ, BAM, VCF) across DNAnexus and AWS HealthOmics.

## Architecture

```
[Data Sources]              [Ingestion]              [Storage & DB]
DNAnexus        ──►                                
AWS HealthOmics ──►   Step Functions          ──►   S3 (tiered: hot/warm/cold)
Illumina/PacBio ──►   Orchestration           ──►   RDS PostgreSQL (metadata)
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

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up local PostgreSQL
createdb genomics_metadata
python db/schema.py --init

# Run tests
pytest tests/

# Simulate a file ingestion
python pipeline/ingest.py --source dnanexus --file-id file-XXXX
```

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
