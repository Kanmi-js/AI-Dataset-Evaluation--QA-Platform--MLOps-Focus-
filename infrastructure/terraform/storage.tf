# S3 (raw + processed dataset objects) and ECR (application images).

resource "aws_s3_bucket" "data" {
  bucket = "${local.name_prefix}-data-${data.aws_caller_identity.current.account_id}"

  # Bucket names are globally unique, so the account id is appended rather than
  # hoping the project name is free.

  tags = { Name = "${local.name_prefix}-data" }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    # Datasets are the durable asset here; a bad re-processing run or an
    # accidental delete should be recoverable.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # Abort multipart uploads left behind by an interrupted large upload.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  # Objects are served exclusively through presigned URLs, which work fine while
  # the bucket is private. Nothing here should ever be world-readable.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_cors_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # The dashboard fetches presigned URLs directly from the browser (image canvas
  # and text preview), which is a cross-origin request.
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Length", "Content-Type"]
    max_age_seconds = 3000
  }
}

# Expire noncurrent versions after 30 days; without this, versioning grows
# without bound as workers re-write processed derivatives on every reprocess.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# --- ECR repositories ---

resource "aws_ecr_repository" "backend" {
  name = "${local.name_prefix}/backend"

  # Mutable so CD can push the same tag repeatedly during a hotfix; the deploy
  # path always addresses images by git SHA.
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${local.name_prefix}-backend" }
}

resource "aws_ecr_repository" "frontend" {
  name = "${local.name_prefix}/frontend"

  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${local.name_prefix}-frontend" }
}

# Untagged layers accumulate from every rebuild; this is the main ECR cost lever.
resource "aws_ecr_lifecycle_policy" "prune_untagged" {
  for_each = {
    backend  = aws_ecr_repository.backend.name
    frontend = aws_ecr_repository.frontend.name
  }

  repository = each.value

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })
}
