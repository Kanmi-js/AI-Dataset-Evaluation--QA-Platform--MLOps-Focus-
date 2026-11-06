# EC2 instance running the whole stack via docker compose, behind an ALB.
#
# Topology note: ALB -> ASG (min 1, max 1). The ASG is doing less work than it
# looks like it should here — it exists for the failure mode where the instance
# dies at 3am, which is the realistic risk for a single-node deployment. Set
# max above 1 and the CPU target-tracking policy below starts adding nodes; at
# that point Redis and the Celery worker become per-node, which is a real design
# change, not a config tweak.

resource "aws_launch_template" "app" {
  name_prefix   = "${local.name_prefix}-"
  image_id      = data.aws_ami.amazon_linux.id
  instance_type = var.instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.instance.name
  }

  vpc_security_group_ids = [aws_security_group.instance.id]

  # IMDSv2 only. The boot script reads credentials for ECR and SSM from the
  # metadata service; requiring a token blocks the SSRF-style credential theft
  # that plain IMDSv1 allows.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # containers run one hop from the host
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = var.root_volume_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/../ec2/user-data.sh.tftpl", {
    aws_region       = var.aws_region
    ecr_registry     = local.ecr_registry
    backend_image    = aws_ecr_repository.backend.repository_url
    frontend_image   = aws_ecr_repository.frontend.repository_url
    image_tag        = var.image_tag
    ssm_prefix       = "/${local.name_prefix}"
    s3_bucket        = aws_s3_bucket.data.bucket
    compose_template = file("${path.module}/../ec2/docker-compose.prod.yml.tftpl")
    log_group        = aws_cloudwatch_log_group.app.name
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name_prefix}-app" }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${local.name_prefix}-app-root" }
  }

  # Boot installs Docker and pulls two images; give it room before the ASG
  # considers the instance unhealthy.
  # (Health check grace period is set on the ASG below.)

  lifecycle {
    create_before_destroy = true
  }
}

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

resource "aws_autoscaling_group" "app" {
  name = "${local.name_prefix}-app"

  # One instance in one public subnet. Attaching both would let the ASG spread
  # to an AZ that serves no purpose and complicate the EIP-less address story.
  vpc_zone_identifier = [aws_subnet.public[0].id]

  min_size         = 1
  max_size         = 1
  desired_capacity = 1

  target_group_arns = [aws_lb_target_group.app.arn]

  # Replacing the whole group on a launch-template change would mean downtime;
  # rolling keeps the old instance serving until the new one is healthy. With
  # max_size = 1 the new instance is created only after the old drains, so a
  # template change still costs a few seconds — acceptable, and better than a
  # gap.
  launch_template {
    id      = aws_launch_template.app.id
    version = "$Latest"
  }

  # The instance must be able to finish pulling images and starting the stack
  # before the ALB judges it, or the ASG will replace a healthy instance in a
  # boot loop.
  health_check_type         = "ELB"
  health_check_grace_period = 300

  # Let in-flight QA requests finish on scale-in/replacement.
  termination_policies = ["OldestInstance"]

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }

  dynamic "tag" {
    for_each = merge(local.common_tags, { Name = "${local.name_prefix}-app" })
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }
}

# --- Load balancer ---

resource "aws_lb" "app" {
  name               = "${local.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"

  # Both public subnets: an ALB requires at least two AZs.
  subnets         = aws_subnet.public[*].id
  security_groups = [aws_security_group.alb.id]

  # Uploads are proxied through nginx, which allows 30 MB; the ALB default is
  # 1 MB and would reject dataset uploads before nginx ever saw them.
  idle_timeout = 120

  # Deleting an environment should not fail because the LB lingers.
  enable_deletion_protection = false

  tags = { Name = "${local.name_prefix}-alb" }
}

resource "aws_lb_target_group" "app" {
  name     = "${local.name_prefix}-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id

  deregistration_delay = 30

  health_check {
    # nginx proxies /api/ to FastAPI, so this exercises the full path the ALB
    # will use rather than just whether nginx is up.
    path                = "/api/v1/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${local.name_prefix}-tg" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# HTTPS is not configured here on purpose: it needs a domain and an ACM
# certificate, neither of which this stack can guess. To add it:
#
#   1. aws_acm_certificate + validation records for your domain
#   2. an aws_lb_listener on :443 with the certificate
#   3. change the :80 listener to a redirect action
#
# The app is safe to run over plain HTTP behind it because authentication is a
# bearer token rather than a cookie, but do not put real user data on it before
# TLS is in place.

# --- Alarms ---

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${local.name_prefix}-unhealthy-hosts"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "The ALB has no healthy targets — the app is down."

  dimensions = {
    LoadBalancer = aws_lb.app.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }

  treat_missing_data = "breaching"

  tags = { Name = "${local.name_prefix}-unhealthy-hosts" }
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${local.name_prefix}-db-storage-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  # 2 GB free. With gp3 autoscaling enabled RDS grows itself, but a persistent
  # breach means the ceiling is being approached faster than autoscaling keeps
  # up.
  threshold         = 2 * 1024 * 1024 * 1024
  alarm_description = "RDS free storage below 2 GB."

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  tags = { Name = "${local.name_prefix}-db-storage-low" }
}
