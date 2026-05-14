# Scaling Enhancements

The current architecture is designed for nightly batch runs of tens to hundreds of files. Processing thousands of files requires changes to four distinct bottlenecks, each with a different fix.

---

## 1. Step Functions payload limit

The most immediate hard wall. Step Functions state data is capped at **256 KB**. `DetectFiles` returns all file descriptors as an inline list passed to the Map state. At ~300 bytes per file dict, the limit is hit around 800 files — the pipeline breaks before a single transfer starts.

**Fix:** AWS added a **Distributed Map** state specifically for this. Instead of an inline array, `DetectFiles` writes the file list to S3 as a JSON file, and the Distributed Map reads directly from S3 — no payload size limit, scales to millions of items. The state machine definition changes from:

```json
"ItemsPath": "$.files"
```

to:

```json
"ItemReader": {
  "Resource": "arn:aws:states:::s3:getObject",
  "Parameters": { "Bucket.$": "$.bucket", "Key.$": "$.key" }
}
```

---

## 2. RDS connection exhaustion

With thousands of concurrent Map iterations each running `ValidateFile` and `RegisterMetadata`, thousands of Lambda functions would simultaneously open PostgreSQL connections. A `db.t3.medium` has a maximum of roughly 170 connections. The pipeline would start throwing connection errors well before reaching that concurrency level.

**Fix:** Add **RDS Proxy** in front of the RDS instance. It pools and multiplexes Lambda connections — thousands of Lambdas share a small pool of real database connections. The change is a single Terraform resource plus pointing the `RDS_HOST` environment variable at the proxy endpoint instead of the RDS instance endpoint directly.

---

## 3. Lambda timeout for large file transfers

Lambda's hard ceiling is 15 minutes. At ~500 Mbps network throughput (typical for a 3 GB Lambda), files larger than ~55 GB will time out before the transfer completes. A Lambda timeout does not trigger the Python `except` block — the process is killed externally — so the DB record is left stuck in `transferring` status with no error message, and an incomplete multipart upload is left orphaned in S3 (which AWS charges for until cleaned up).

The right fix depends on the source platform.

### HealthOmics — eliminate the compute layer entirely

HealthOmics has a native API call, `StartReadSetExportJob`, that copies a ReadSet directly to S3 entirely within AWS — no Lambda, no Batch, no network bandwidth limits, no timeout. AWS moves the data internally between services.

```python
response = omics_client.start_read_set_export_job(
    sequenceStoreId=store_id,
    destination=f"s3://{bucket}/",
    sources=[{"readSetId": read_set_id}],
)
job_id = response["id"]
```

Step Functions polls for completion using a `.waitForTaskToken` pattern. This is objectively better than routing data through any compute layer — faster, cheaper, and architecturally simpler.

### DNAnexus — ECS Fargate over AWS Batch

DNAnexus has no equivalent native export, so compute is required. **ECS Fargate** is a better fit than AWS Batch for a pure streaming I/O task:

| | AWS Batch | ECS Fargate |
|---|---|---|
| No timeout | Yes | Yes |
| Infrastructure to define | Job definition, compute environment, job queue | Task definition only |
| Serverless (no EC2 to manage) | Optional | Yes |
| Startup time | 1–3 min | 30–60 sec |
| Best suited for | Compute-intensive batch jobs | Simple containerized tasks |

Batch is designed for compute-heavy workloads — array jobs, GPU, HPC grids. A streaming file transfer is purely I/O-bound; Fargate runs the same container image as Lambda (same Dockerfile, same Python code) but without the timeout ceiling.

### File size-based routing

The right production pattern routes by `file_size_bytes` before dispatching:

```python
LARGE_FILE_THRESHOLD = 40 * 1024 ** 3  # 40 GB

if event["file_size_bytes"] > LARGE_FILE_THRESHOLD:
    # HealthOmics: StartReadSetExportJob
    # DNAnexus:   submit ECS Fargate task, return task ARN
else:
    # transfer inline in Lambda (current path, no change)
```

Lambda handles the common case with zero operational overhead. Fargate handles the tail of large DNAnexus files. HealthOmics files of any size bypass compute entirely via native export.

### Incomplete multipart upload cleanup

A timed-out Lambda leaves an incomplete multipart upload in S3. AWS charges for the uploaded parts even if `CompleteMultipartUpload` is never called. Add a lifecycle rule to abort these automatically:

```hcl
rule {
  id     = "abort-incomplete-multipart"
  status = "Enabled"
  filter {}
  abort_incomplete_multipart_upload {
    days_after_initiation = 3
  }
}
```

This rule is not currently in `terraform/s3_rds.tf`.

---

## 4. Source platform API rate limits

DNAnexus and HealthOmics impose API rate limits on listing and downloading. Firing thousands of concurrent download requests would get the account throttled or temporarily blocked.

**Fix:** Replace the Step Functions Map with an **SQS queue**. `DetectFiles` enqueues all files rather than returning them inline. Lambda workers pull from the queue at a controlled concurrency (SQS event source mapping lets you set `max_concurrency` per function). When a download is throttled, the message returns to the queue with exponential backoff rather than failing the whole execution. This also decouples detection from transfer — a large backlog drains over hours rather than in one burst.

---

## Scaled architecture

```
EventBridge
    │
    ▼
DetectFiles Lambda
    │  writes file list → S3
    │  enqueues file IDs → SQS
    ▼
SQS Queue (one message per file)
    │  controlled fan-out (e.g. 50 concurrent)
    ▼
ValidateFile Lambda  (reads message, checks DB)
    │
    ├── small file               → TransferFile Lambda    (current path)
    ├── DNAnexus, large file     → ECS Fargate task       (no timeout)
    └── HealthOmics, any size   → StartReadSetExportJob   (no compute)
              │
              ▼
         RegisterMetadata Lambda
              │
              ▼
         (aggregate) NotifyCompletion
```

---

## What does not change

The core design holds at any scale. The PostgreSQL idempotency checks, the audit log, the connector abstraction, the streaming transfer logic, and the partial failure isolation are all unchanged. The fan-out mechanism and connection management are what need to be replaced — not the pipeline logic itself.
