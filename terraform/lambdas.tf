# terraform/lambdas.tf
# All Lambda functions — one per pipeline step + S3 event handler

locals {
  lambda_env_base = {
    RDS_HOST             = aws_db_instance.genomics_metadata.address
    RDS_DB               = "genomics_metadata"
    RDS_USER             = "genomics_user"
    RDS_PASSWORD         = var.db_password
    S3_BUCKET_NAME       = aws_s3_bucket.genomics_data.bucket
    ENVIRONMENT          = var.environment
    DNANEXUS_PROJECT_ID  = var.dnanexus_project_id
    DNANEXUS_TOKEN       = var.dnanexus_token
    HEALTHOMICS_STORE_ID = var.healthomics_store_id
  }
}

# S3 event handler — reacts to ObjectCreated and lifecycle transitions
resource "aws_lambda_function" "s3_event_handler" {
  function_name = "${local.name_prefix}-s3-event-handler"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.main.repository_url}:latest"
  timeout       = 300
  memory_size   = 512
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

resource "aws_lambda_permission" "allow_s3_invoke" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.s3_event_handler.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.genomics_data.arn
}

resource "aws_s3_bucket_notification" "genomics_data_lambda" {
  bucket     = aws_s3_bucket.genomics_data.id
  depends_on = [aws_lambda_permission.allow_s3_invoke]
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
  image_uri     = "${aws_ecr_repository.main.repository_url}:latest"
  timeout       = 300
  memory_size   = 512
  image_config { command = ["lambdas.detect_files.handler"] }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 2 — Validate file type and create DB record
resource "aws_lambda_function" "validate_file" {
  function_name = "${local.name_prefix}-validate-file"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.main.repository_url}:latest"
  timeout       = 300
  memory_size   = 1024
  image_config { command = ["lambdas.validate_file.handler"] }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 3 — Stream file to S3 (large timeout + memory for 100GB+ BAM files)
resource "aws_lambda_function" "transfer_file" {
  function_name = "${local.name_prefix}-transfer-file"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.main.repository_url}:latest"
  timeout       = 900
  memory_size   = 3008
  image_config { command = ["lambdas.transfer_file.handler"] }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 4 — Register metadata and checksums in PostgreSQL
resource "aws_lambda_function" "register_metadata" {
  function_name = "${local.name_prefix}-register-metadata"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.main.repository_url}:latest"
  timeout       = 60
  memory_size   = 256
  image_config { command = ["lambdas.register_metadata.handler"] }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment { variables = local.lambda_env_base }
  tags = local.common_tags
}

# Step 5 — Publish SNS notification with ingestion summary
resource "aws_lambda_function" "notify_completion" {
  function_name = "${local.name_prefix}-notify-completion"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.main.repository_url}:latest"
  timeout       = 30
  memory_size   = 128
  image_config { command = ["lambdas.notify_completion.handler"] }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
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

# CloudWatch log groups — explicitly managed so terraform destroy removes them
resource "aws_cloudwatch_log_group" "lambda_logs" {
  for_each = toset([
    "s3-event-handler",
    "detect-files",
    "validate-file",
    "transfer-file",
    "register-metadata",
    "notify-completion",
  ])

  name              = "/aws/lambda/${local.name_prefix}-${each.key}"
  retention_in_days = 30
  tags              = local.common_tags
}
