# Infrastructure

Terraform for the AWS deployment, the EC2 boot script, and the S3-event Lambda.

```
                        ┌─────────────┐
   internet ──:80──────>│     ALB     │  public subnets (2 AZs)
                        └──────┬──────┘
                               │ :80
                        ┌──────┴──────┐
                        │ EC2 (ASG 1) │  nginx · FastAPI · Celery · Redis
                        └──┬───────┬──┘
                    :5432  │       │  HTTPS
                    ┌──────┴───┐ ┌─┴──────────┐
                    │   RDS    │ │     S3     │──event──> Lambda ──> /internal/s3-events
                    │ postgres │ │  datasets  │
                    └──────────┘ └────────────┘
```

## Why one instance

The whole stack runs on a single EC2 instance under Docker Compose. That is the
deliberate choice for this project — Redis and the Celery worker are colocated
with the API, which means a second node would need a shared broker and a real
answer for which node owns which queue. The ASG with `min = max = 1` is not
there for scaling; it is there so the instance comes back on its own when it
dies at 3am.

Raising `max_size` above 1 is a design change, not a config tweak. The natural
next step is ECS Fargate with ElastiCache, where the broker stops being a
per-node concern.

## Files

| | |
|---|---|
| `terraform/network.tf` | VPC, 2 public + 2 private subnets, IGW, S3 gateway endpoint |
| `terraform/security.tf` | Three chained security groups: ALB → instance → RDS |
| `terraform/compute.tf` | Launch template, ASG, ALB, target group, alarms |
| `terraform/database.tf` | RDS PostgreSQL 16, generated password → SSM |
| `terraform/storage.tf` | Dataset S3 bucket, two ECR repositories |
| `terraform/iam.tf` | Instance role, GitHub OIDC deploy role, log group |
| `terraform/secrets.tf` | `SECRET_KEY` and `INTERNAL_API_TOKEN` → SSM |
| `terraform/lambda.tf` | S3 → Lambda → backend re-drive |
| `ec2/user-data.sh.tftpl` | Boot: install Docker, read SSM, migrate, start |
| `ec2/docker-compose.prod.yml.tftpl` | The production compose file |
| `lambda/s3_trigger/handler.py` | Posts new `raw/` keys to the internal endpoint |

## First deploy

### 1. Provision

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform apply
```

Takes roughly 10–15 minutes, most of it RDS.

The instance boots, finds no images in ECR yet, and fails its health check.
That is expected — push images next.

> `terraform init` writes `.terraform.lock.hcl`, which **is** committed so CI and
> every operator resolve the same provider versions. It records hashes per
> platform, so if you run Terraform on Windows or macOS, add the CI platform once
> before committing it or `terraform init` on the runner will fail on a missing
> checksum:
>
> ```bash
> terraform providers lock -platform=linux_amd64 -platform=windows_amd64 -platform=darwin_arm64
> ```
>
> Also run `terraform fmt -recursive` once before the first push — CI checks
> formatting.

### 2. Push images

Either let CI do it (below), or once by hand:

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$(terraform output -raw ecr_registry)"

docker build -t "$(terraform output -raw ecr_backend_repository_url):latest" ../../backend
docker push "$(terraform output -raw ecr_backend_repository_url):latest"

docker build -t "$(terraform output -raw ecr_frontend_repository_url):latest" ../../frontend
docker push "$(terraform output -raw ecr_frontend_repository_url):latest"
```

Then replace the instance so it boots against real images:

```bash
aws autoscaling start-instance-refresh \
  --auto-scaling-group-name "$(terraform output -raw autoscaling_group_name)"
```

### 3. Check

```bash
curl "$(terraform output -raw app_url)/api/v1/health"
```

## Continuous deployment

Set `github_repo` in `terraform.tfvars` and apply. That creates an IAM role
GitHub Actions assumes through OIDC — no AWS access keys stored in the
repository.

Then in **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | `terraform output -raw github_deploy_role_arn` |
| Variable | `AWS_REGION` | must match `aws_region` |
| Variable | `PROJECT` | must match `project` |
| Variable | `ENVIRONMENT` | must match `environment` |

`.github/workflows/deploy.yml` then runs on every push to `main`: it runs CI,
builds both images tagged with the commit SHA, pushes to ECR, and rolls the
instance over SSM — pull, `alembic upgrade head` in a one-shot container, then
`docker compose up -d`. A failing migration fails the deploy with the old stack
still serving.

To require a human before production changes, create a **production**
environment in repository settings and add yourself as a required reviewer; the
deploy job already targets it.

**Rolling back** is a manual workflow run with `image_tag` set to a previously
deployed SHA. That path skips the build entirely and redeploys the existing
image, so you get the artifact that was actually running before, not a fresh
build of old source.

> If the account already has a GitHub OIDC provider (AWS allows exactly one),
> set `create_github_oidc_provider = false` or `apply` fails with
> `EntityAlreadyExists`.

## Secrets

Nothing sensitive is in Terraform state as plaintext output, in user-data, or in
the repository. The database password, JWT signing key, and internal API token
are generated by Terraform and written to SSM Parameter Store as SecureStrings.
At boot, the instance reads its own parameter prefix using its instance role and
assembles `/opt/mlops-qa/.env` (mode 600).

The DSN is assembled on the instance rather than in Terraform for a specific
reason: the generated password contains characters that must be percent-encoded
inside a URL — a literal `#` would truncate the DSN at the fragment — and
Terraform has no `urlencode`. The boot script does it with Python's
`urllib.parse.quote`, which is correct by construction.

```bash
# Read a value back
aws ssm get-parameter --name /mlops-qa-prod/DB_PASSWORD --with-decryption \
  --query Parameter.Value --output text
```

**Breakdown API keys** (optional) are *not* created by Terraform, so they never
enter its state. Add them by hand under the same prefix and they are picked up
at the next boot (or instance refresh):

```bash
aws ssm put-parameter --type SecureString --name /mlops-qa-prod/ANTHROPIC_API_KEY --value '...'
aws ssm put-parameter --type SecureString --name /mlops-qa-prod/ELEVENLABS_API_KEY --value '...'
# Optional: a voice from your ElevenLabs library
aws ssm put-parameter --type String --name /mlops-qa-prod/ELEVENLABS_VOICE_ID --value '...'
```

Without them, breakdowns still render, with a template script and captions.

> Terraform state contains these values regardless — that is inherent to
> `random_password`. Use the S3 backend with encryption for any real
> deployment; see the commented block in `versions.tf`.

## Operating it

**Shell on the instance.** There is no SSH: port 22 is not open and no key pair
is created. Access is SSM Session Manager, which logs to CloudTrail and needs no
inbound port.

```bash
terraform output -raw ssm_session_command   # prints the full command
```

Once connected:

```bash
cd /opt/mlops-qa
docker compose ps
docker compose logs -f backend
docker compose logs -f worker
```

**Logs.** The CloudWatch agent ships `/var/log/user-data.log` and the container
logs to the group in the `log_group_name` output, so they survive the instance.
Boot problems are in the `<instance-id>/boot` stream.

**Database.** RDS is only reachable from the instance security group — there is
no public endpoint. To reach it from a laptop, port-forward through SSM:

```bash
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<rds-endpoint>"],"portNumber":["5432"],"localPortNumber":["5432"]}'
```

## Costs

Rough monthly, us-east-1, on-demand, at rest:

| | |
|---|---|
| EC2 `t3.large` (needed for YOLO tracking) | ~$60 |
| RDS `db.t4g.micro` + 20 GB | ~$15 |
| ALB | ~$16 + traffic |
| NAT gateway | **$0** — there isn't one |
| S3, ECR, Lambda, logs | a few dollars at demo volume |
| Claude + ElevenLabs | per breakdown; usage-billed by each provider |

The ALB is the least justifiable line item for a single instance. It is here for
the health checks and because it is where TLS terminates once a certificate
exists. An Elastic IP on the instance would be cheaper and materially worse.

Private subnets exist but have no NAT gateway — that is a $32/month line item
for outbound traffic nothing currently needs. RDS lives there with no internet
route at all, which is the point. The instance sits in a public subnet with a
public IP, protected by its security group rather than by address.

## Tearing down

```bash
# Deletion protection blocks destroy; turn it off and apply that first.
terraform apply -var db_deletion_protection=false
terraform destroy
```

S3 buckets with objects will not delete. Empty it first:

```bash
aws s3 rm "s3://$(terraform output -raw s3_bucket)" --recursive
```

Versioning is on, so `rm --recursive` leaves noncurrent versions behind. If
`destroy` still refuses, delete the versions too (the console's "Empty bucket"
button does this) and re-run.
