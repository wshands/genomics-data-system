# terraform/monitoring.tf
# CloudWatch alarms, EventBridge schedule, and outputs

# ---------------------------------------------------------------------------
# CloudWatch — Lambda error alarm
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.name_prefix}-lambda-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Lambda error rate too high"

  dimensions = {
    FunctionName = aws_lambda_function.s3_event_handler.function_name
  }
}

# ---------------------------------------------------------------------------
# CloudWatch — Step Functions failure alarm
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "sfn_failed_executions" {
  alarm_name          = "${local.name_prefix}-sfn-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Step Functions pipeline execution failed"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.genomics_ingest_pipeline.arn
  }
}

# ---------------------------------------------------------------------------
# EventBridge — nightly schedule → Step Functions
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "nightly_ingest" {
  name                = "${local.name_prefix}-nightly-ingest"
  description         = "Trigger genomics ingestion pipeline nightly at 2am UTC"
  schedule_expression = "cron(0 2 * * ? *)"
}

resource "aws_cloudwatch_event_target" "nightly_ingest_sfn" {
  rule     = aws_cloudwatch_event_rule.nightly_ingest.name
  arn      = aws_sfn_state_machine.genomics_ingest_pipeline.arn
  role_arn = aws_iam_role.eventbridge_sfn.arn

  input = jsonencode({
    source   = "scheduled"
    platform = "all"
  })
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "s3_bucket_name"    { value = aws_s3_bucket.genomics_data.bucket }
output "rds_endpoint"      { value = aws_db_instance.genomics_metadata.address }
output "state_machine_arn" { value = aws_sfn_state_machine.genomics_ingest_pipeline.arn }
output "sns_topic_arn"     { value = aws_sns_topic.pipeline_notifications.arn }
