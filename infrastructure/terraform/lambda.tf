# S3 -> Lambda -> backend re-drive.
#
# Why this exists: the backend already enqueues pre-processing on upload, so the
# S3 event is not the primary trigger. It is the safety net for objects that
# arrive in the bucket without going through the upload endpoint — a bulk sync
# with the AWS CLI, a restore from a backup, another service writing raw data.
# Those appear in the dashboard as unprocessed items with nothing driving them.
#
# And why it calls the API instead of speaking to Celery directly: the Redis
# broker runs on the EC2 instance and is not reachable from Lambda. The Lambda
# posts the affected keys to a shared-secret-guarded endpoint, and the backend
# enqueues them.

data "archive_file" "s3_trigger" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/s3_trigger"
  output_path = "${path.module}/.build/s3_trigger.zip"
}

resource "aws_iam_role" "lambda" {
  name = "${local.name_prefix}-s3-trigger"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-s3-trigger" }
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.name_prefix}-s3-trigger"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        # The shared secret and the API base URL. Scoped to this one parameter.
        Sid      = "ReadInternalToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.internal_api_token.arn
      },
      {
        Sid      = "DecryptInternalToken"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com"
          }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name_prefix}-s3-trigger"
  retention_in_days = 7

  tags = { Name = "${local.name_prefix}-s3-trigger-logs" }
}

resource "aws_lambda_function" "s3_trigger" {
  function_name = "${local.name_prefix}-s3-trigger"
  role          = aws_iam_role.lambda.arn

  filename         = data.archive_file.s3_trigger.output_path
  source_code_hash = data.archive_file.s3_trigger.output_base64sha256

  runtime = "python3.12"
  handler = "handler.handler"

  timeout     = 30
  memory_size = 128

  environment {
    # API_BASE_URL: the ALB is the only public entry point; the Lambda reaches
    # the API over the internet like any other client.
    # KEY_PREFIX: ignore everything outside raw/ — processed/ derivatives are
    # written by the worker and must not re-enter the pipeline.
    variables = {
      API_BASE_URL                 = "http://${aws_lb.app.dns_name}"
      INTERNAL_TOKEN_SSM_PARAMETER = aws_ssm_parameter.internal_api_token.name
      KEY_PREFIX                   = "raw/"
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]

  tags = { Name = "${local.name_prefix}-s3-trigger" }
}

resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.s3_trigger.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.data.arn
}

resource "aws_s3_bucket_notification" "data" {
  bucket = aws_s3_bucket.data.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.s3_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
  }

  depends_on = [aws_lambda_permission.allow_s3]
}
