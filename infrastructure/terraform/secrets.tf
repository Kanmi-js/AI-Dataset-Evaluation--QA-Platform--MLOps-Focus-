# Application secrets, generated once and stored as SecureStrings.
#
# `random_password` with `special = false` because these values are pasted into
# a dotenv file at boot: a "#" would start a comment and an "=" would split the
# assignment. URL-safe characters only.

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

resource "random_password" "internal_api_token" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "secret_key" {
  name        = "/${local.name_prefix}/SECRET_KEY"
  description = "JWT signing key"
  type        = "SecureString"
  value       = random_password.secret_key.result

  tags = { Name = "${local.name_prefix}-SECRET_KEY" }
}

resource "aws_ssm_parameter" "internal_api_token" {
  name        = "/${local.name_prefix}/INTERNAL_API_TOKEN"
  description = "Shared secret the S3-event Lambda presents on /internal/* routes"
  type        = "SecureString"
  value       = random_password.internal_api_token.result

  tags = { Name = "${local.name_prefix}-INTERNAL_API_TOKEN" }
}
