#!/usr/bin/env bash
# scripts/teardown.sh — Tear down the Genomics Data System from AWS
#
# Handles two things Terraform cannot do on its own:
#   1. Versioned S3 bucket — all object versions must be deleted before Terraform
#      can remove the bucket
#   2. ECR images — must be deleted before the repository can be removed
#
# Usage:
#   ./scripts/teardown.sh          # prompts for confirmation
#   ./scripts/teardown.sh --force  # skips confirmation prompt

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"
AWS_PROFILE="${AWS_PROFILE:-AdministratorAccess-230407893272}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
NAME_PREFIX="genomics-data-system-${ENVIRONMENT}"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BOLD}▶ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $*${RESET}"; }
die()     { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }

# ── Helpers ───────────────────────────────────────────────────────────────────
tf() {
  AWS_PROFILE="$AWS_PROFILE" terraform -chdir="$TF_DIR" "$@"
}

aws_cmd() {
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}

check_aws_auth() {
  info "Checking AWS SSO session (profile: $AWS_PROFILE)..."
  if ! aws_cmd sts get-caller-identity &>/dev/null; then
    warn "AWS session expired. Run: aws sso login --profile $AWS_PROFILE"
    die "Not authenticated"
  fi
  success "Authenticated"
}

# ── Step 1: Disable RDS deletion protection ───────────────────────────────────
disable_rds_deletion_protection() {
  info "Step 1/4 — Disabling RDS deletion protection..."

  local db_id="${NAME_PREFIX}-metadata"

  info "  Checking RDS instance $db_id in $AWS_REGION..."
  local current_protection
  if ! current_protection=$(aws_cmd rds describe-db-instances \
      --db-instance-identifier "$db_id" \
      --query 'DBInstances[0].DeletionProtection' \
      --output text 2>&1); then
    # Show the actual error so we know what went wrong
    warn "describe-db-instances failed: $current_protection"
    warn "Skipping — if the instance exists, delete it manually in the console"
    return
  fi

  if [[ -z "$current_protection" || "$current_protection" == "None" ]]; then
    warn "RDS instance $db_id not found in $AWS_REGION — skipping"
    return
  fi

  info "  deletion_protection=$current_protection"

  if [[ "$current_protection" =~ [Tt]rue ]]; then
    info "  Disabling deletion protection..."
    aws_cmd rds modify-db-instance \
      --db-instance-identifier "$db_id" \
      --no-deletion-protection \
      --apply-immediately > /dev/null

    info "  Waiting for deletion protection to be disabled..."
    local attempts=0
    until aws_cmd rds describe-db-instances \
        --db-instance-identifier "$db_id" \
        --query 'DBInstances[0].DeletionProtection' \
        --output text 2>/dev/null | grep -iq "false"; do
      sleep 10
      attempts=$((attempts + 1))
      [[ $attempts -gt 36 ]] && die "Timed out — check RDS status in the console"
    done
  fi

  success "RDS deletion protection disabled"
}

# ── Step 2: Empty the S3 bucket ───────────────────────────────────────────────
empty_s3_bucket() {
  info "Step 2/4 — Emptying S3 bucket (all versions and delete markers)..."

  local bucket="${NAME_PREFIX}-data"

  if ! aws_cmd s3api head-bucket --bucket "$bucket" &>/dev/null; then
    warn "Bucket $bucket does not exist — skipping"
    return
  fi

  info "  Deleting all object versions in s3://$bucket..."
  aws_cmd s3api list-object-versions --bucket "$bucket" \
    --query '{Objects: Versions[].{Key: Key, VersionId: VersionId}}' \
    --output json 2>/dev/null \
    | jq -c 'select(.Objects != null) | .Objects | _nwise(1000) | {Objects: .}' \
    | while read -r batch; do
        aws_cmd s3api delete-objects --bucket "$bucket" --delete "$batch" &>/dev/null
      done

  info "  Deleting all delete markers in s3://$bucket..."
  aws_cmd s3api list-object-versions --bucket "$bucket" \
    --query '{Objects: DeleteMarkers[].{Key: Key, VersionId: VersionId}}' \
    --output json 2>/dev/null \
    | jq -c 'select(.Objects != null) | .Objects | _nwise(1000) | {Objects: .}' \
    | while read -r batch; do
        aws_cmd s3api delete-objects --bucket "$bucket" --delete "$batch" &>/dev/null
      done

  success "S3 bucket emptied"
}

# ── Step 3: Delete ECR images ─────────────────────────────────────────────────
empty_ecr_repository() {
  info "Step 3/4 — Deleting ECR images..."

  local repo="${NAME_PREFIX}"

  local image_ids
  image_ids=$(aws_cmd ecr list-images --repository-name "$repo" \
    --query 'imageIds' --output json 2>/dev/null || echo "[]")

  if [[ "$image_ids" == "[]" ]]; then
    success "ECR repository already empty"
    return
  fi

  aws_cmd ecr batch-delete-image \
    --repository-name "$repo" \
    --image-ids "$image_ids" &>/dev/null

  success "ECR images deleted"
}

# ── Step 4: Delete CloudWatch log groups ─────────────────────────────────────
# Lambda log groups are auto-created by AWS and not managed by Terraform.
delete_log_groups() {
  info "Step 4/5 — Deleting CloudWatch log groups..."

  local prefix="/aws/lambda/${NAME_PREFIX}"
  local groups
  groups=$(aws_cmd logs describe-log-groups \
    --log-group-name-prefix "$prefix" \
    --query 'logGroups[].logGroupName' \
    --output text 2>/dev/null || true)

  if [[ -z "$groups" ]]; then
    success "No log groups found"
    return
  fi

  for group in $groups; do
    aws_cmd logs delete-log-group --log-group-name "$group" &>/dev/null \
      && info "  Deleted $group" \
      || warn "  Could not delete $group"
  done

  success "Log groups deleted"
}

# ── Step 5: Terraform destroy ─────────────────────────────────────────────────
terraform_destroy() {
  info "Step 5/5 — Running terraform destroy..."
  tf destroy -auto-approve -input=false
  success "Infrastructure destroyed"
}

# ── Entry point ───────────────────────────────────────────────────────────────
main() {
  echo ""
  echo -e "${RED}${BOLD}════════════════════════════════════════${RESET}"
  echo -e "${RED}${BOLD}  Genomics Data System — TEARDOWN       ${RESET}"
  echo -e "${RED}${BOLD}════════════════════════════════════════${RESET}"
  echo ""
  warn "This will permanently destroy ALL infrastructure and data."
  echo ""

  if [[ "${1:-}" != "--force" ]]; then
    read -r -p "Type 'destroy' to confirm: " confirm
    [[ "$confirm" == "destroy" ]] || die "Aborted"
    echo ""
  fi

  check_aws_auth
  tf init -input=false -upgrade &>/dev/null

  disable_rds_deletion_protection
  empty_s3_bucket
  empty_ecr_repository
  delete_log_groups
  terraform_destroy

  echo ""
  success "Teardown complete."
  echo ""
}

main "$@"
