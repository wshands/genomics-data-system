#!/usr/bin/env bash
# scripts/deploy.sh — Deploy the Genomics Data System to AWS
#
# Usage:
#   ./scripts/deploy.sh               # full deploy (ECR bootstrap + image push + infra)
#   ./scripts/deploy.sh --push-only   # rebuild and push image, then re-apply Terraform
#   ./scripts/deploy.sh --plan        # terraform plan only, no apply

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"
AWS_PROFILE="${AWS_PROFILE:-AdministratorAccess-230407893272}"
AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "latest")"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BOLD}▶ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $*${RESET}"; }
die()     { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }

# ── Prerequisite checks ───────────────────────────────────────────────────────
check_prereqs() {
  info "Checking prerequisites..."
  local missing=()
  for cmd in aws terraform docker git; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
  done
  [[ ${#missing[@]} -eq 0 ]] || die "Missing required tools: ${missing[*]}"
  success "All prerequisites found"
}

check_aws_auth() {
  info "Checking AWS SSO session (profile: $AWS_PROFILE)..."
  if ! aws sts get-caller-identity --profile "$AWS_PROFILE" &>/dev/null; then
    warn "AWS session expired. Run: aws sso login --profile $AWS_PROFILE"
    die "Not authenticated"
  fi
  ACCOUNT_ID=$(aws sts get-caller-identity --profile "$AWS_PROFILE" \
    --query Account --output text)
  success "Authenticated — account $ACCOUNT_ID"
}

check_tfvars() {
  local tfvars="$TF_DIR/terraform.tfvars"
  [[ -f "$tfvars" ]] || die "Missing $tfvars — copy and fill in CLAUDE.md tfvars section"
  for key in db_password dnanexus_project_id dnanexus_token healthomics_store_id; do
    grep -q "^${key}" "$tfvars" || die "$tfvars is missing required key: $key"
  done
  success "terraform.tfvars looks complete"
}

# ── Terraform helpers ─────────────────────────────────────────────────────────
tf() {
  AWS_PROFILE="$AWS_PROFILE" terraform -chdir="$TF_DIR" "$@"
}

tf_init() {
  info "Terraform init..."
  tf init -input=false -upgrade
  success "Terraform initialised"
}

# ── ECR bootstrap ─────────────────────────────────────────────────────────────
bootstrap_ecr() {
  info "Stage 1/3 — Bootstrapping ECR repository..."
  # Create only the ECR repo so we have somewhere to push the image.
  # All other resources are deferred to Stage 3.
  tf apply -target=aws_ecr_repository.main -auto-approve -input=false
  success "ECR repository ready"
}

# ── Docker image ──────────────────────────────────────────────────────────────
build_and_push() {
  info "Stage 2/3 — Building and pushing Docker image (tag: $IMAGE_TAG)..."

  ECR_URL=$(tf output -raw ecr_repository_url)

  info "  Logging into ECR..."
  aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    | docker login --username AWS --password-stdin "$ECR_URL"

  info "  Building image..."
  docker build -t "$ECR_URL:$IMAGE_TAG" -t "$ECR_URL:latest" "$REPO_ROOT"

  info "  Pushing image..."
  docker push "$ECR_URL:$IMAGE_TAG"
  docker push "$ECR_URL:latest"

  success "Image pushed — $ECR_URL:$IMAGE_TAG"
}

# ── Full infra apply ──────────────────────────────────────────────────────────
apply_infra() {
  info "Stage 3/3 — Applying full infrastructure..."
  tf apply -auto-approve -input=false
  echo ""
  success "Deployment complete."
  echo ""
  echo -e "${BOLD}Key outputs:${RESET}"
  tf output
}

plan_only() {
  info "Running terraform plan (no changes applied)..."
  tf plan -input=false
}

# ── Entry point ───────────────────────────────────────────────────────────────
main() {
  local mode="full"
  [[ "${1:-}" == "--push-only" ]] && mode="push-only"
  [[ "${1:-}" == "--plan"      ]] && mode="plan"

  echo ""
  echo -e "${BOLD}════════════════════════════════════════${RESET}"
  echo -e "${BOLD}  Genomics Data System — Deploy         ${RESET}"
  echo -e "${BOLD}════════════════════════════════════════${RESET}"
  echo ""

  check_prereqs
  check_aws_auth
  check_tfvars
  tf_init

  case "$mode" in
    full)
      bootstrap_ecr
      build_and_push
      apply_infra
      ;;
    push-only)
      build_and_push
      apply_infra
      ;;
    plan)
      plan_only
      ;;
  esac
}

main "$@"
