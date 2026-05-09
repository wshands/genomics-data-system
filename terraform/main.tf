# terraform/main.tf
# Provider, backend, variables, and locals

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket = "genomics-tfstate"
    key    = "genomics-data-system/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region"            { default = "us-east-1" }
variable "environment"           { default = "prod" }
variable "db_password"           { sensitive = true }
variable "dnanexus_project_id"   { description = "DNAnexus project ID to poll for new files" }
variable "dnanexus_token" {
  description = "DNAnexus API token"
  sensitive   = true
}
variable "healthomics_store_id"  { description = "AWS HealthOmics sequence store ID to poll for new files" }

locals {
  name_prefix = "genomics-data-system-${var.environment}"
  common_tags = {
    Environment = var.environment
    System      = "genomics-data-system"
  }
}
