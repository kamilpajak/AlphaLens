#!/usr/bin/env bash
# postdeploy_check.sh — fail-loud drift check, run ON THE VPS after a Django deploy.
#
#   bash deploy/scripts/postdeploy_check.sh [--with-migrate]
#
# Test-strategy memo Phase 1a-ii (docs/research/integration_e2e_test_strategy_2026_06_01.md).
# Catches the deploy-env-drift class that a green CI / a successful `up -d` does
# NOT: (1) Prometheus rules that drifted from the repo (live rules are a HAND-
# SYNCED copy, NOT a bind-mount of the repo file — see
# reference_prometheus_live_rules_not_repo_mounted_2026_05_31), and (2) the VPS
# running an OLD Django image because nobody pulled after a new main build (the
# silent-stale-image class that, combined with migrate-on-start, broke prod —
# feedback_django_latest_tag_migrate_on_start_drift / the #292 incident).
#
# Read-only: diff / docker inspect / docker exec promtool check / buildx inspect /
# (optional) migrate --check. Does NOT remediate — it reports + exits non-zero so
# an operator runbook step can gate on it.
#
# NOT `set -e`: every check runs so the operator sees ALL drift in one pass; the
# script collects failures and exits 1 at the end.
set -uo pipefail

REPO="${REPO:-$HOME/AlphaLens}"
COMPOSE_DIR="$REPO/deploy/docker/django-prod"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yaml"
IMAGE="ghcr.io/kamilpajak/alphalens-django"
PROM_CONTAINER="${PROM_CONTAINER:-prometheus}"
# Live Prometheus rules are a hand-synced copy bind-mounted to /etc/prometheus,
# NOT the repo file. Override via env if the host layout moves.
LIVE_RULES="${LIVE_RULES:-/home/jacoren/monitoring/prometheus/alphalens.rules}"
REPO_RULES="$REPO/deploy/monitoring/prometheus/rules/alphalens.yaml"

# The exact on.push.paths of .github/workflows/django-image.yml. The image is
# only (re)built when a main commit touches one of these, so the "expected"
# image commit is the latest origin/main commit touching THEM — NOT HEAD (a
# docs/research commit builds no image, and comparing against sha-<HEAD> would
# false-fail every time). KEEP IN SYNC with the workflow — pinned by
# apps/alphalens-research/tests/test_postdeploy_check_paths_parity.py.
DJANGO_TRIGGER_PATHS=(
  "apps/alphalens-django"
  "pyproject.toml"
  "uv.lock"
  "deploy/docker/django-prod/Dockerfile"
  ".github/workflows/django-image.yml"
)

problems=()
ok()   { printf 'OK   %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; problems+=("$*"); }

# Resolve the running django container name from compose (robust to a project
# rename), falling back to the conventional name.
CONTAINER="$(docker compose -f "$COMPOSE_FILE" ps -q django 2>/dev/null | head -1)"
[ -n "$CONTAINER" ] || CONTAINER="alphalens-prod-django-1"

echo "== Check 1/3: Prometheus rules (repo vs live + validity) =="
if [ ! -f "$LIVE_RULES" ]; then
  bad "live rules file missing: $LIVE_RULES"
elif diff -q "$REPO_RULES" "$LIVE_RULES" >/dev/null 2>&1; then
  ok "live rules match repo ($REPO_RULES)"
else
  bad "live rules drift from repo — copy '$REPO_RULES' -> '$LIVE_RULES' then 'docker exec $PROM_CONTAINER kill -HUP 1'"
  diff -u "$REPO_RULES" "$LIVE_RULES" 2>&1 | head -40 || true
fi
# promtool lives ONLY inside the prometheus container (not on the host).
if docker exec "$PROM_CONTAINER" promtool check rules /etc/prometheus/alphalens.rules >/dev/null 2>&1; then
  ok "promtool: live alphalens rules valid"
else
  bad "promtool check rules failed on the live alphalens rules (run: docker exec $PROM_CONTAINER promtool check rules /etc/prometheus/alphalens.rules)"
fi

echo "== Check 2/3: running Django image == current main django build =="
# Resolve the EXPECTED commit from origin/main. A fetch failure or a missing
# origin/main is a HARD FAIL — NOT a fall-back to local HEAD: if HEAD is behind
# origin/main, the wrong EXPECTED_SHA could match an old container and silently
# PASS a real image drift (false-pass is worse than false-fail for a gate).
EXPECTED_SHA=""
SHORT=""
if ! git -C "$REPO" fetch -q origin main 2>/dev/null; then
  bad "git fetch origin main failed — cannot resolve the expected image commit (network/remote?); image-drift check skipped"
elif ! git -C "$REPO" rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  bad "origin/main missing after fetch — cannot resolve the expected image commit; image-drift check skipped"
else
  EXPECTED_SHA="$(git -C "$REPO" log -1 --format=%H origin/main -- "${DJANGO_TRIGGER_PATHS[@]}" 2>/dev/null)"
  SHORT="$(git -C "$REPO" rev-parse --short "$EXPECTED_SHA" 2>/dev/null)"
  if [ -z "$EXPECTED_SHA" ] || [ -z "$SHORT" ]; then
    bad "could not resolve a django build commit on origin/main from the trigger paths"
    EXPECTED_SHA=""
  fi
fi

if [ -n "$EXPECTED_SHA" ]; then
  RUN_REV="$(docker inspect "$CONTAINER" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)"
  if [ "$RUN_REV" = "$EXPECTED_SHA" ]; then
    ok "running image revision matches latest main django build (sha-$SHORT)"
  else
    bad "running revision '${RUN_REV:-<none>}' != expected '$EXPECTED_SHA' (sha-$SHORT) — VPS has not pulled the current main image: 'docker compose pull && docker compose up -d'"
  fi
  # Registry cross-check (best-effort). RepoDigest is the pullable digest; .Image
  # is the LOCAL config-blob id and must NOT be compared to the registry. A
  # registry blip degrades to WARN — the revision-label check above is the
  # registry-free authority that already FAILs on real drift.
  RUN_IMGID="$(docker inspect "$CONTAINER" --format '{{.Image}}' 2>/dev/null)"
  RUN_DIGEST="$(docker image inspect "$RUN_IMGID" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null | sed 's/.*@//')"
  EXP_DIGEST="$(docker buildx imagetools inspect "$IMAGE:sha-$SHORT" 2>/dev/null | awk '/^Digest:/{print $2; exit}')"
  if [ -z "$EXP_DIGEST" ]; then
    warn "could not resolve GHCR digest for $IMAGE:sha-$SHORT (registry unreachable or image not built) — relied on the revision-label check above"
  elif [ -z "$RUN_DIGEST" ]; then
    warn "running image has no RepoDigest (built locally, never pulled?) — relied on the revision-label check above"
  elif [ "$RUN_DIGEST" = "$EXP_DIGEST" ]; then
    ok "running image digest matches GHCR sha-$SHORT"
  else
    bad "running digest $RUN_DIGEST != GHCR $EXP_DIGEST for sha-$SHORT"
  fi
fi

if [ "${1:-}" = "--with-migrate" ]; then
  echo "== Check 3/3: Django migrations applied (--with-migrate) =="
  if docker compose -f "$COMPOSE_FILE" exec -T django python manage.py migrate --check >/dev/null 2>&1; then
    ok "no unapplied Django migrations"
  else
    bad "unapplied Django migrations (manage.py migrate --check exited non-zero)"
  fi
fi

echo
if [ "${#problems[@]}" -ne 0 ]; then
  echo "POSTDEPLOY CHECK: FAIL (${#problems[@]} problem(s))"
  printf '  - %s\n' "${problems[@]}"
  exit 1
fi
echo "POSTDEPLOY CHECK: all good"
