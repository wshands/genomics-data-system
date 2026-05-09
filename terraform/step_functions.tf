# terraform/step_functions.tf
# Step Functions state machine — full ingestion pipeline
# Flow: DetectFiles → (Map) ValidateFile → TransferToS3 → RegisterMetadata → NotifyCompletion

resource "aws_sfn_state_machine" "genomics_ingest_pipeline" {
  name     = "${local.name_prefix}-ingest-pipeline"
  role_arn = aws_iam_role.step_functions_exec.arn

  definition = jsonencode({
    Comment = "Genomics ingestion: detect → validate → transfer → register → notify"
    StartAt = "DetectFiles"

    States = {

      DetectFiles = {
        Type     = "Task"
        Resource = aws_lambda_function.detect_files.arn
        Comment  = "List new files on DNAnexus or HealthOmics"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "States.TaskFailed"]
          IntervalSeconds = 10
          MaxAttempts     = 3
          BackoffRate     = 2.0
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "PipelineFailed"
          ResultPath  = "$.error"
        }]
        Next = "FilesDetected"
      }

      FilesDetected = {
        Type = "Choice"
        Choices = [{
          Variable           = "$.file_count"
          NumericGreaterThan = 0
          Next               = "ProcessFiles"
        }]
        Default = "NoFilesFound"
      }

      NoFilesFound = {
        Type = "Succeed"
      }

      # Map state — process up to 10 files in parallel
      ProcessFiles = {
        Type           = "Map"
        InputPath      = "$.files"
        MaxConcurrency = 10
        Iterator = {
          StartAt = "ValidateFile"
          States = {

            ValidateFile = {
              Type     = "Task"
              Resource = aws_lambda_function.validate_file.arn
              Retry = [{
                ErrorEquals     = ["States.TaskFailed"]
                IntervalSeconds = 5
                MaxAttempts     = 2
                BackoffRate     = 1.5
              }]
              Catch = [
                {
                  # Already ingested on a previous run — not an error, skip cleanly.
                  ErrorEquals = ["AlreadyIngestedException"]
                  Next        = "FileSucceeded"
                  ResultPath  = null
                },
                {
                  ErrorEquals = ["States.ALL"]
                  Next        = "FileFailed"
                  ResultPath  = "$.error"
                }
              ]
              Next = "TransferToS3"
            }

            TransferToS3 = {
              Type     = "Task"
              Resource = aws_lambda_function.transfer_file.arn
              Comment  = "Multipart stream to S3 — no full file in memory"
              Retry = [{
                ErrorEquals     = ["States.TaskFailed"]
                IntervalSeconds = 30
                MaxAttempts     = 3
                BackoffRate     = 2.0
              }]
              Catch = [{
                ErrorEquals = ["States.ALL"]
                Next        = "FileFailed"
                ResultPath  = "$.error"
              }]
              Next = "RegisterMetadata"
            }

            RegisterMetadata = {
              Type     = "Task"
              Resource = aws_lambda_function.register_metadata.arn
              Retry = [{
                ErrorEquals     = ["States.TaskFailed"]
                IntervalSeconds = 5
                MaxAttempts     = 3
                BackoffRate     = 1.5
              }]
              Catch = [{
                ErrorEquals = ["States.ALL"]
                Next        = "FileFailed"
                ResultPath  = "$.error"
              }]
              Next = "FileSucceeded"
            }

            FileSucceeded = { Type = "Succeed" }
            # Succeed (not Fail) so the Map collects all per-file results and
            # always reaches NotifyCompletion. Failed items have no "status"
            # field so notify_completion counts them correctly as failures.
            FileFailed = { Type = "Succeed" }
          }
        }
        Next = "NotifyCompletion"
      }

      NotifyCompletion = {
        Type     = "Task"
        Resource = aws_lambda_function.notify_completion.arn
        Retry = [{
          ErrorEquals     = ["States.TaskFailed"]
          IntervalSeconds = 5
          MaxAttempts     = 2
          BackoffRate     = 1.5
        }]
        Next = "PipelineSucceeded"
      }

      PipelineSucceeded = { Type = "Succeed" }
      PipelineFailed    = { Type = "Fail" }
    }
  })

  tags = local.common_tags
}
