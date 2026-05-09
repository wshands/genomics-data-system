# Operations Guide

---

## Manually triggering the pipeline

The pipeline runs automatically every night at 2am UTC via EventBridge. To trigger it on demand:

### AWS Console

1. Open **AWS Console → Step Functions → State Machines**
2. Find `genomics-data-system-{env}-ingest-pipeline`
3. Click **Start execution**
4. Paste an input JSON payload (see examples below) and click **Start execution**

### Input payloads

**Run against both platforms (same as the nightly schedule):**
```json
{
  "source": "manual",
  "platform": "all"
}
```

**DNAnexus only:**
```json
{
  "source": "manual",
  "platform": "dnanexus"
}
```

**HealthOmics only:**
```json
{
  "source": "manual",
  "platform": "healthomics"
}
```

**Override the project ID (useful for testing a specific project without changing the env var):**
```json
{
  "source": "manual",
  "platform": "dnanexus",
  "project_id": "project-XXXX"
}
```

**Ingest only a specific file type:**
```json
{
  "source": "manual",
  "platform": "all",
  "file_type": "BAM"
}
```

### AWS CLI

```bash
aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:genomics-data-system-prod-ingest-pipeline \
  --input '{"source": "manual", "platform": "all"}'
```

Get the state machine ARN from Terraform outputs:
```bash
cd terraform && terraform output state_machine_arn
```

---

## Monitoring

### CloudWatch alarms

Two alarms are provisioned in `terraform/monitoring.tf` and will notify via SNS when triggered.

| Alarm | Condition | What it means |
|-------|-----------|---------------|
| `genomics-data-system-{env}-lambda-errors` | Lambda error count > 5 in 5 minutes | The S3 event handler is repeatedly failing — likely a DB connectivity or schema issue |
| `genomics-data-system-{env}-sfn-failures` | Any Step Functions execution fails | A pipeline run failed before reaching `NotifyCompletion` — check the execution history |

To subscribe to alarms: **AWS Console → SNS → Topics → `genomics-data-system-{env}-notifications` → Create subscription** (email or Slack webhook via Lambda).

### Step Functions execution history

The most useful place to diagnose pipeline failures.

1. **AWS Console → Step Functions → State Machines → select the pipeline**
2. Click any execution to see the visual workflow — each step is green (success), red (failed), or grey (not reached)
3. Click a failed step to see the exact error message and input/output at that step

Common failure patterns:

| Symptom | Likely cause |
|---------|-------------|
| `DetectFiles` fails | DNAnexus token expired or HealthOmics store ID incorrect |
| `ValidateFile` fails | File extension not in the supported list (FASTQ, BAM, VCF, CRAM, BED) |
| `TransferToS3` fails after retries | Network timeout on large BAM file — check Lambda timeout (currently 15 min) |
| `RegisterMetadata` fails | RDS connectivity issue — check security group rules and VPC config |
| All steps skipped via `AlreadyIngestedException` | Files were already ingested on a previous run — this is normal, not an error |

### Lambda logs

Every Lambda writes structured logs to CloudWatch Logs at `/aws/lambda/genomics-data-system-{env}-{function-name}`.

**AWS Console → CloudWatch → Log groups** — or via CLI:
```bash
aws logs tail /aws/lambda/genomics-data-system-prod-detect-files --follow
aws logs tail /aws/lambda/genomics-data-system-prod-transfer-file --follow
```

Key log messages to look for:

| Message | Lambda | Meaning |
|---------|--------|---------|
| `Found N files in project-XXX` | `detect_files` | How many files are on the platform |
| `Detected N new file(s)` | `detect_files` | How many will be ingested this run |
| `Starting stream: ... → s3://` | `transfer_file` | Transfer has begun |
| `Transfer complete: X.XX GB` | `transfer_file` | File landed in S3 successfully |
| `Ingestion complete: file_id=N` | `register_metadata` | DB record updated |
| `Pipeline complete: N ingested, N failed` | `notify_completion` | End-of-run summary |

### Pipeline status via database

Connect with `make psql` (local) or any PostgreSQL client pointed at the RDS endpoint.

**Check recent pipeline runs:**
```sql
SELECT r.run_id, f.file_name, f.file_type, r.status,
       r.started_at, r.duration_secs, r.error_message
FROM pipeline_runs r
JOIN genomics_files f ON f.file_id = r.file_id
ORDER BY r.started_at DESC
LIMIT 20;
```

**Files currently in-flight (stuck transferring):**
```sql
SELECT file_id, file_name, source_platform, updated_at
FROM genomics_files
WHERE status = 'transferring'
ORDER BY updated_at;
```

**Ingestion summary by platform and file type:**
```sql
SELECT source_platform, file_type, status, COUNT(*) AS count,
       ROUND(SUM(file_size_bytes) / 1e12, 2) AS total_tb
FROM genomics_files
GROUP BY source_platform, file_type, status
ORDER BY source_platform, file_type;
```

**Recent audit log entries:**
```sql
SELECT log_id, file_id, action, actor, logged_at
FROM audit_log
ORDER BY logged_at DESC
LIMIT 20;
```

### SNS notifications

`notify_completion` publishes a message to the SNS topic after every pipeline run. The message includes a per-file summary:

```
Pipeline complete: 12 ingested, 0 failed

  [42] BAM  NA12878.bam          → s3://genomics-data-system-prod-data/BAM/...  (ingested)
  [43] FASTQ sample_R1.fastq.gz  → s3://genomics-data-system-prod-data/FASTQ/... (ingested)
  ...
```

Subscribe an email address or Slack webhook to the topic in the AWS Console under **SNS → Topics → `genomics-data-system-{env}-notifications`**.
