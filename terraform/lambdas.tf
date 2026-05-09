# terraform/lambdas.tf
# All Lambda functions — one per pipeline step + S3 event handler

locals {
  lambda_env_base = {
    RDS_HOST       = aws_db_instance.genomics_metadata.address
    RDS_DB         = "genomics_metadata"
    RDS_USER       = "genomics_user"
    S3_BUCKET_NAME = aws_s3_bucket.genomics_data.bucket
    ENVIRONMENT    = var.environment
  }
}

# S3 event handler — reacts to ObjectCreated and lifecycle transitions
resource "aws_lambda_function" "s3_event_handler" {
  function_name = "${local.name_prefix}-s3-event-handler"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  timeout       = 300
  memory_size   = 512
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

resource "aws_s3_bucket_notification" "genomics_data_lambda" {
  bucket = aws_s3_bucket.genomics_data.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.s3_event_handler.arn
    events              = ["s3:ObjectCreated:*"]
  }
}

# Step 1 — Detect new files on source platform
resource "aws_lambda_function" "detect_files" {
  function_name = "${local.name_prefix}-detect-files"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  timeout       = 300
  memory_size   = 512
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 2 — Validate file type and compute checksum
resource "aws_lambda_function" "validate_file" {
  function_name = "${local.name_prefix}-validate-file"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  timeout       = 300
  memory_size   = 1024
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 3 — Stream file to S3 (large timeout + memory for 100GB+ BAM files)
resource "aws_lambda_function" "transfer_file" {
  function_name = "${local.name_prefix}-transfer-file"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  timeout       = 900
  memory_size   = 3008
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 4 — Register metadata in PostgreSQL
resource "aws_lambda_function" "register_metadata" {
  function_name = "${local.name_prefix}-register-metadata"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  timeout       = 60
  memory_size   = 256
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 5 — Notify downstream systems via SNS
resource "aws_lambda_function" "notify_completion" {
  function_name = "${local.name_prefix}-notify-completion"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  timeout       = 30
  memory_size   = 128
  environment {
    variables = merge(local.lambda_env_base, {
      SNS_TOPIC_ARN = aws_sns_topic.pipeline_notifications.arn
    })
  }
  tags = local.common_tags
}

# SNS topic for pipeline notifications → Slack / email
resource "aws_sns_topic" "pipeline_notifications" {
  name = "${local.name_prefix}-notifications"
  tags = local.common_tags
}
