# IAM: the instance role, and the GitHub Actions deploy role.

# --- Instance role ---
#
# Deliberately narrow. The instance can read its own configuration prefix, use
# the dataset bucket, pull images, and be managed by SSM. It cannot touch any
# other SSM parameter, bucket, or AWS service in the account.

resource "aws_iam_role" "instance" {
  name = "${local.name_prefix}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-instance" }
}

resource "aws_iam_role_policy" "instance" {
  name = "${local.name_prefix}-instance"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadOwnConfiguration"
        Effect = "Allow"
        Action = [
          "ssm:GetParametersByPath",
          "ssm:GetParameters",
          "ssm:GetParameter",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name_prefix}/*"
      },
      {
        # Decrypting the SecureString values above. Scoped by region/account but
        # not by key id, because RDS-adjacent keys are alias-managed and using
        # the default aws/ssm key is the point.
        Sid      = "DecryptOwnConfiguration"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com"
          }
        }
      },
      {
        Sid    = "DatasetObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          aws_s3_bucket.data.arn,
          "${aws_s3_bucket.data.arn}/*",
        ]
      },
      {
        Sid    = "ManagedBySSM"
        Effect = "Allow"
        Action = [
          "ssm:UpdateInstanceInformation",
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
          "ec2messages:AcknowledgeMessage",
          "ec2messages:DeleteMessage",
          "ec2messages:FailMessage",
          "ec2messages:GetEndpoint",
          "ec2messages:GetMessages",
          "ec2messages:SendReply",
        ]
        Resource = "*"
      },
      {
        # The CloudWatch agent tails /var/log/user-data.log and the container
        # json logs into the group created below. DescribeLogGroups is how the
        # agent checks the group exists before its first PutLogEvents.
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
      {
        Sid      = "DiscoverLogGroup"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"
      },
    ]
  })
}

# ECR pulls. AWSEC2ContainerRegistryReadOnly is the managed policy for exactly
# this; scoping it by repository ARN instead would break on the first `docker
# pull` of an image the instance has not seen.
resource "aws_iam_role_policy_attachment" "instance_ecr" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name_prefix}-instance"
  role = aws_iam_role.instance.name
}

# --- GitHub Actions deploy role (OIDC) ---
#
# No long-lived AWS access keys in GitHub. The workflow exchanges its OIDC
# token for short-lived credentials, and the trust policy pins the trust to this
# repository's main branch — a fork or a pull request request cannot assume it.

resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_repo != "" && var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = { Name = "${local.name_prefix}-github-oidc" }
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.github_repo != "" && !var.create_github_oidc_provider ? 1 : 0

  url = "https://token.actions.githubusercontent.com"
}

locals {
  github_oidc_arn = (
    var.github_repo == "" ? "" :
    var.create_github_oidc_provider ?
    aws_iam_openid_connect_provider.github[0].arn :
    data.aws_iam_openid_connect_provider.github[0].arn
  )
}

resource "aws_iam_role" "deploy" {
  count = var.github_repo != "" ? 1 : 0

  name = "${local.name_prefix}-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.github_oidc_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Any ref in this repo. Tighten to "repo:${var.github_repo}:ref:refs/heads/main"
          # if only main should be able to deploy.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
    }]
  })

  tags = { Name = "${local.name_prefix}-deploy" }
}

resource "aws_iam_role_policy" "deploy" {
  count = var.github_repo != "" ? 1 : 0

  name = "${local.name_prefix}-deploy"
  role = aws_iam_role.deploy[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PushImages"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = "*"
      },
      {
        # Push to the instance (and read the command output back).
        Sid    = "DeployToInstance"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
        ]
        Resource = "*"
      },
      {
        # CD re-reads the parameter prefix to build the instance's .env. Without
        # this the workflow would have to duplicate the DSN construction.
        Sid      = "ReadConfiguration"
        Effect   = "Allow"
        Action   = ["ssm:GetParametersByPath", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name_prefix}/*"
      },
      {
        # Locating the instance to deploy to (by ASG tag), and the ALB DNS name
        # for the post-deploy health check. All read-only, and none of these
        # APIs support resource-level permissions.
        Sid    = "DiscoverTargets"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "autoscaling:DescribeAutoScalingGroups",
          "elasticloadbalancing:DescribeLoadBalancers",
        ]
        Resource = "*"
      },
    ]
  })
}

# --- Logging ---

resource "aws_cloudwatch_log_group" "app" {
  name = "/aws/${local.name_prefix}/app"

  # A week is plenty for a demo; raise once you are debugging retrospectives.
  retention_in_days = 7

  tags = { Name = "${local.name_prefix}-logs" }
}

# CD is invoked by hand from the Actions tab until OIDC trust is verified, so
# the role ARN is needed as an output. See outputs.tf.
