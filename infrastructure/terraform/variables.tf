variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Short project slug; prefixes every resource name."
  type        = string
  default     = "mlops-qa"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "project must be lowercase alphanumeric/hyphens, 2-21 chars (it feeds S3 and ECR names)."
  }
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "prod"
}

# --- Network ---

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "allowed_http_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the ALB on port 80. Defaults to the whole internet
    because this is a public dashboard; narrow it to your office/VPN range if
    the deployment is internal. The ALB is the only public entry point — the
    instance and database are not reachable directly.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# --- Compute ---

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. The whole stack (FastAPI, Celery worker, Redis, nginx)
    runs on one instance via docker compose. YOLO tracking (torch on CPU) is
    the memory and CPU driver: each worker process holds ~1-1.5 GB while
    tracking and the worker runs two. t3.large (8 GB) is the practical floor;
    a compute-optimised c6i.xlarge roughly halves tracking time.
  EOT
  type        = string
  default     = "t3.large"
}

variable "root_volume_size" {
  description = "Root EBS volume size in GB. The backend image (CPU torch included) is ~3 GB; with old images kept for rollback, budget ~12 GB."
  type        = number
  default     = 30
}

# --- Database ---

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GB."
  type        = number
  default     = 20
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "mlops_qa"
}

variable "db_username" {
  description = "RDS master username. The password is generated and stored in SSM."
  type        = string
  default     = "mlops"
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. 0 disables backups."
  type        = number
  default     = 7
}

variable "db_deletion_protection" {
  description = <<-EOT
    Block `terraform destroy` from dropping the database. Leave true for any
    real deployment; set false when tearing down a throwaway environment.
  EOT
  type        = bool
  default     = true
}

# --- Application images ---

variable "image_tag" {
  description = <<-EOT
    Container image tag the instance pulls at boot. CD overwrites the running
    stack with the commit SHA via SSM, so this only sets the initial value —
    it is not the source of truth for what is deployed.
  EOT
  type        = string
  default     = "latest"
}

# --- CI/CD ---

variable "github_repo" {
  description = <<-EOT
    GitHub repository allowed to assume the deploy role via OIDC, as
    "owner/name". Leave empty to skip creating the OIDC provider and role
    (e.g. if you deploy manually, or the provider already exists in the account).
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.github_repo == "" || can(regex("^[^/]+/[^/]+$", var.github_repo))
    error_message = "github_repo must be in \"owner/name\" form, or empty."
  }
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Create the GitHub OIDC provider. AWS allows only one per account, so set
    this to false if another stack already created it — the deploy role will
    reference the existing one.
  EOT
  type        = bool
  default     = true
}
