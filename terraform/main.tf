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

variable "aws_region"         { default = "us-east-1" }
variable "environment"        { default = "prod" }
variable "db_password"        { sensitive = true }
variable "ecr_repository_url" { description = "ECR repo URL for Lambda images" }

locals {
  name_prefix = "genomics-data-system-${var.environment}"
  common_tags = {
    Environment = var.environment
    System      = "genomics-data-system"
  }
}
