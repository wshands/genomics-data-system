# terraform/s3_rds.tf
# S3 bucket (tiered storage) and RDS PostgreSQL (metadata DB)

# ---------------------------------------------------------------------------
# S3 — genomics data with lifecycle tiering
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "genomics_data" {
  bucket = "${local.name_prefix}-data"
  tags   = local.common_tags
}

resource "aws_s3_bucket_versioning" "genomics_data" {
  bucket = aws_s3_bucket.genomics_data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "genomics_data" {
  bucket = aws_s3_bucket.genomics_data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "genomics_data" {
  bucket = aws_s3_bucket.genomics_data.id

  rule {
    id     = "tier-warm"
    status = "Enabled"
    filter {}
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }

  rule {
    id     = "tier-cold"
    status = "Enabled"
    filter {}
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL — metadata database
# ---------------------------------------------------------------------------
resource "aws_db_instance" "genomics_metadata" {
  identifier        = "${local.name_prefix}-metadata"
  engine            = "postgres"
  engine_version    = "17"
  instance_class    = "db.t3.medium"
  allocated_storage = 100
  storage_encrypted = true
  db_name           = "genomics_metadata"
  username          = "genomics_user"
  password          = var.db_password
  multi_az          = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  backup_retention_period = 7
  deletion_protection     = false
  skip_final_snapshot     = true

  tags = local.common_tags

  lifecycle {
    ignore_changes = [engine_version]
  }
}
