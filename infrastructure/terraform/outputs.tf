# Everything you need after `terraform apply`: where the app is, where to push
# images, and what to paste into GitHub Actions secrets.

output "app_url" {
  description = "Public URL of the application (ALB DNS name)."
  value       = "http://${aws_lb.app.dns_name}"
}

output "alb_dns_name" {
  description = "ALB DNS name, for a CNAME record if you attach a domain."
  value       = aws_lb.app.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone id, for a Route 53 alias record."
  value       = aws_lb.app.zone_id
}

# --- Container registry ---

output "ecr_backend_repository_url" {
  description = "Push the backend image here."
  value       = aws_ecr_repository.backend.repository_url
}

output "ecr_frontend_repository_url" {
  description = "Push the frontend image here."
  value       = aws_ecr_repository.frontend.repository_url
}

output "ecr_registry" {
  description = "Registry host, for `docker login`."
  value       = local.ecr_registry
}

# --- Storage and data ---

output "s3_bucket" {
  description = "Dataset bucket. Raw uploads land under raw/, worker output under processed/."
  value       = aws_s3_bucket.data.bucket
}

output "db_endpoint" {
  description = "RDS endpoint. Only reachable from the instance security group."
  value       = aws_db_instance.main.address
}

output "ssm_parameter_prefix" {
  description = "Where the instance reads its configuration from at boot."
  value       = "/${local.name_prefix}"
}

# --- CI/CD wiring ---
#
# These three are what the deploy workflow needs. Set AWS_DEPLOY_ROLE_ARN as a
# repository secret (or variable — the ARN is not sensitive, but a secret keeps
# the account id out of public logs).

output "github_deploy_role_arn" {
  description = "Role the GitHub Actions deploy workflow assumes via OIDC. Empty if github_repo was not set."
  value       = var.github_repo == "" ? "" : aws_iam_role.deploy[0].arn
}

output "autoscaling_group_name" {
  description = "ASG the deploy workflow targets when it looks up the running instance."
  value       = aws_autoscaling_group.app.name
}

output "log_group_name" {
  description = "CloudWatch log group carrying boot and container logs."
  value       = aws_cloudwatch_log_group.app.name
}

# --- Operations ---

output "ssm_session_command" {
  description = "Open a shell on the instance. No SSH key, no open port 22."
  value       = "aws ssm start-session --region ${var.aws_region} --target $(aws ec2 describe-instances --region ${var.aws_region} --filters 'Name=tag:aws:autoscaling:groupName,Values=${aws_autoscaling_group.app.name}' 'Name=instance-state-name,Values=running' --query 'Reservations[0].Instances[0].InstanceId' --output text)"
}

output "db_password_command" {
  description = "Read the generated database password (it is never printed by apply)."
  value       = "aws ssm get-parameter --region ${var.aws_region} --name /${local.name_prefix}/DB_PASSWORD --with-decryption --query Parameter.Value --output text"
}
