"""
lambdas/notify_completion.py

Step Functions Task — Step 5: NotifyCompletion

Receives the Map state output (array of per-file results), tallies
success/failure counts, and publishes a summary to SNS for downstream
alerting (Slack, email, etc.).

Input (Map state output — array of RegisterMetadata results):
[
  {"db_file_id": 42, "file_name": "NA12878.bam", "status": "ingested", ...},
  {"db_file_id": 43, "file_name": "sample.vcf.gz", "status": "ingested", ...},
  ...
]

Output:
{
  "total":    3,
  "ingested": 3,
  "failed":   0,
  "message":  "Pipeline complete: 3 ingested, 0 failed"
}
"""

import json
import logging
import os
import sys

sys.path.insert(0, "/var/task")

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")


def handler(event, context) -> dict:
    # event is the raw Map state output — a list of per-file result dicts
    results = event if isinstance(event, list) else []

    ingested = sum(1 for r in results if r.get("status") == "ingested")
    failed   = len(results) - ingested

    message = f"Pipeline complete: {ingested} ingested, {failed} failed"
    logger.info(message)

    detail_lines = [
        f"  [{r.get('db_file_id')}] {r.get('file_type')} {r.get('file_name')} "
        f"→ {r.get('s3_uri')}  ({r.get('status')})"
        for r in results
    ]
    full_message = "\n".join([message, ""] + detail_lines)

    if SNS_TOPIC_ARN:
        sns = boto3.client("sns")
        sns.publish(
            TopicArn = SNS_TOPIC_ARN,
            Subject  = f"Genomics Pipeline: {ingested}/{len(results)} files ingested",
            Message  = full_message,
        )
        logger.info(f"SNS notification sent to {SNS_TOPIC_ARN}")
    else:
        logger.warning("SNS_TOPIC_ARN not set — skipping notification")

    return {
        "total":    len(results),
        "ingested": ingested,
        "failed":   failed,
        "message":  message,
    }
