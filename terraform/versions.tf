# Pin everything. An unpinned provider means "terraform apply" can behave
# differently tomorrow than it did today, which is the opposite of the point.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource created by this project carries these tags.
  # Teardown and cost attribution both depend on them.
  default_tags {
    tags = {
      project     = "rds-migration-lab"
      managed_by  = "terraform"
      environment = "lab"
    }
  }
}
