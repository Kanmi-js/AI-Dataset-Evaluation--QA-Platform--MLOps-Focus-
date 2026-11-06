# Security groups. Three layers, each opening only to the one in front of it:
#
#   internet -> ALB (:80) -> instance (:80) -> RDS (:5432)
#
# No SSH anywhere. Administrative access is via SSM Session Manager, which needs
# no inbound rule at all (the agent dials out) and gives an auditable session log
# instead of a shared key pair.

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "Public ingress to the dashboard"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.allowed_http_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from ${each.value}"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_instance" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the application instance"
  referenced_security_group_id = aws_security_group.instance.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "instance" {
  name        = "${local.name_prefix}-instance"
  description = "Application instance: nginx fronting FastAPI + Celery"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-instance" }
}

resource "aws_vpc_security_group_ingress_rule" "instance_from_alb" {
  security_group_id            = aws_security_group.instance.id
  description                  = "HTTP from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

# Outbound: ECR + S3 + SSM + Secrets Manager over 443, RDS on 5432, DNS, and
# whatever the OS package mirror needs on 80/443.
resource "aws_vpc_security_group_egress_rule" "instance_https" {
  security_group_id = aws_security_group.instance.id
  description       = "HTTPS to AWS APIs and package mirrors"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "instance_http" {
  security_group_id = aws_security_group.instance.id
  description       = "HTTP for OS package mirrors"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "instance_dns_udp" {
  security_group_id = aws_security_group.instance.id
  description       = "DNS resolution"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "udp"
}

resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-rds"
  description = "PostgreSQL, reachable only from the application instance"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-rds" }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_instance" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL from the application instance"
  referenced_security_group_id = aws_security_group.instance.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# --- SSM endpoints ---
#
# Session Manager and the deploy path (ssm send-command) both need the instance
# to reach the SSM, EC2 Messages and ssmmessages endpoints. Public subnets have
# an IGW route, so these are optional — but they keep that traffic on the AWS
# backbone. Free to run; commented out to reduce moving parts on a first apply.
# Uncomment if you later move the instance into a private subnet.
#
# resource "aws_security_group" "ssm_endpoints" {
#   name        = "${local.name_prefix}-ssm-endpoints"
#   description = "Interface endpoints for SSM"
#   vpc_id      = aws_vpc.main.id
# }
#
# resource "aws_vpc_security_group_ingress_rule" "ssm_from_instance" {
#   security_group_id            = aws_security_group.ssm_endpoints.id
#   referenced_security_group_id = aws_security_group.instance.id
#   from_port                    = 443
#   to_port                      = 443
#   ip_protocol                  = "tcp"
# }
#
# resource "aws_vpc_endpoint" "ssm" {
#   for_each = toset(["ssm", "ssmmessages", "ec2messages"])
#
#   vpc_id              = aws_vpc.main.id
#   service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
#   vpc_endpoint_type   = "Interface"
#   subnet_ids          = aws_subnet.public[*].id
#   security_group_ids  = [aws_security_group.ssm_endpoints.id]
#   private_dns_enabled = true
# }
