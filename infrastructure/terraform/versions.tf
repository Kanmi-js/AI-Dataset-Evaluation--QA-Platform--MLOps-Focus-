terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.82"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
  }

  # Remote state is a partial configuration: the bucket differs per AWS account,
  # so it is supplied at init time rather than committed here.
  #
  #   terraform init \
  #     -backend-config="bucket=my-tfstate-bucket" \
  #     -backend-config="key=mlops-qa/terraform.tfstate" \
  #     -backend-config="region=us-east-1" \
  #     -backend-config="use_lockfile=true"
  #
  # Uncomment to enable. Local state works fine for a single operator; do this
  # before a second person or CI ever runs apply.
  #
  # backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-${var.environment}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
