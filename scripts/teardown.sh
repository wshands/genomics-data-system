#!/usr/bin/env bash
# scripts/teardown.sh — Tear down the Genomics Data System from AWS
#
# Handles three things Terraform cannot do on its own:
#   1. RDS has deletion_protection=true — must be disabled first
#   2. Versioned S3 bucket — all object versions must be deleted before Terraform
#      can remove the bucket
#   3. ECR images — must be deleted before the repository can be removed
#
# Usage:
#   ./scripts/teardown.sh          # prompts for confirmation
#   ./scripts/teardown.sh --force  # skips confirmation prompt

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"
AWS_PROFILE="${AWS_PROFILE:-AdministratorAccess-230407893272}"
AWS_REGION="${AWS_REGION:-us-east-1}"

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

  # Get the RDS instance identifier from Terraform state
  local db_id
  db_id=$(tf output -raw rds_endpoint 2>/dev/null | cut -d. -f1 || true)

  if [[ -z "$db_id" ]]; then
    warn "Could not determine RDS identifier from Terraform output — skipping"
    return
  fi

  aws_cmd rds modify-db-instance \
    --db-instance-identifier "$db_id" \
    --no-deletion-protection \
    --apply-immediately &>/dev/null || warn "Could not disable deletion protection (may already be disabled)"

  info "  Waiting for RDS modification to complete..."
  aws_cmd rds wait db-instance-available \
    --db-instance-identifier "$db_id" 2>/dev/null || true

  success "RDS deletion protection disabled"
}

# ── Step 2: Empty the S3 bucket ───────────────────────────────────────────────
empty_s3_bucket() {
  info "Step 2/4 — Emptying S3 bucket (all versions and delete markers)..."

  local bucket
  bucket=$(tf output -raw s3_bucket_name 2>/dev/null || true)

  if [[ -z "$bucket" ]]; then
    warn "Could not determine S3 bucket name from Terraform output — skipping"
    return
  fi

  # Check bucket exists
  if ! aws_cmd s3api head-bucket --bucket "$bucket" &>/dev/null; then
    warn "Bucket $bucket does not exist — skipping"
    return
  fi

  info "  Deleting all object versions in s3://$bucket..."
  # Delete all versions in batches
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

  local repo
  repo=$(tf output -raw ecr_repository_url 2>/dev/null | cut -d/ -f2 || true)

  if [[ -z "$repo" ]]; then
    warn "Could not determine ECR repository name — skipping"
    return
  fi

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

# ── Step 4: Terraform destroy ─────────────────────────────────────────────────
terraform_destroy() {
  info "Step 4/4 — Running terraform destroy..."
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
  tf -chdir="$TF_DIR" init -input=false -upgrade &>/dev/null

  disable_rds_deletion_protection
  empty_s3_bucket
  empty_ecr_repository
  terraform_destroy

  echo ""
  success "Teardown complete."
  echo ""
}

main "$@"
