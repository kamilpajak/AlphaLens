#!/usr/bin/env bash
# postdeploy_check.sh — fail-loud drift check, run ON THE VPS after a Django deploy.
#
#   bash deploy/scripts/postdeploy_check.sh [--with-migrate]
#
# Test-strategy memo Phase 1a-ii (docs/research/integration_e2e_test_strategy_2026_06_01.md).
# Catches the deploy-env-drift class that a green CI / a successful `up -d` does
# NOT: (1) Prometheus rules that drifted from origin/main (the live file is a
# COPY converged hourly from the origin/main BLOB by
# alphalens-prometheus-rules-sync.service — not a bind-mount of the repo file,
# and NOT the local checkout — see
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
# Live Prometheus rules are a COPY bind-mounted to /etc/prometheus, NOT the repo
# file. The comparison reference is the origin/main BLOB — never this checkout:
# the VPS checkout is routinely behind origin/main (three commits on
# 2026-09-24), and comparing against it reported drift on a live file that was
# already correct, with a remedy that told the operator to copy the stale file
# over it (#1564). The copy is converged hourly, so it can lag a just-merged
# change by up to a cadence. Override via env if the host layout moves.
LIVE_RULES="${LIVE_RULES:-/home/jacoren/monitoring/prometheus/alphalens.rules}"
# Repo-relative path of the rules SoT. KEEP IN SYNC with
# sync_prometheus_rules.RULES_REPO_PATH — that script is what converges the
# live copy, and if the two name different files this check vouches for the
# wrong one. Pinned by
# apps/alphalens-research/tests/test_postdeploy_check_rules_source.py.
RULES_REPO_PATH="deploy/monitoring/prometheus/rules/alphalens.yaml"
RULES_SYNC_UNIT="alphalens-prometheus-rules-sync.service"
# The live copy is converged from the origin/main blob hourly at :27 UTC by
# RULES_SYNC_UNIT, so a difference YOUNGER than one cadence MAY be expected:
# 3600s cadence + 300s of slack for the run itself. This is a fallback bound,
# not the primary test — the primary test is whether the sync has completed a
# run since the rules commit (see SYNC_EXIT_EPOCH below). The minute in the
# operator text is pinned against the timer file by
# apps/alphalens-research/tests/test_postdeploy_check_rules_source.py.
RULES_SYNC_GRACE_SECONDS=3900
# Overridable so a test can substitute a stub; the VPS has the real binary.
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

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

# --- Shared origin/main reference --------------------------------------------
# BOTH checks below compare the host against origin/main, never against this
# checkout. The VPS checkout routinely sits behind origin/main (three commits on
# 2026-09-24), and the live rules file is converged from the origin/main BLOB,
# so the working tree is neither side of the truth. One fetch serves both
# checks; each check prints its OWN failure line so neither can go quiet.
# A fetch failure is a HARD FAIL, never a fall-back to local HEAD: a stale HEAD
# could match an old container or an old rules file and silently PASS real
# drift (false-pass is worse than false-fail for a gate).
ORIGIN_MAIN_ERR=""
if ! git -C "$REPO" fetch -q origin main 2>/dev/null; then
  ORIGIN_MAIN_ERR="git fetch origin main failed (network/remote?)"
elif ! git -C "$REPO" rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  ORIGIN_MAIN_ERR="origin/main missing after fetch"
fi

BLOB_TMP=""
cleanup() { if [ -n "$BLOB_TMP" ]; then rm -f "$BLOB_TMP"; fi; return 0; }
trap cleanup EXIT

# --- Last completed run of the rules sync ------------------------------------
# Check 1 must NOT decide "pending copy" from the age of the rules commit
# alone. A sync that already ran and left the difference in place is drift
# however young the commit is, and the worst shape of that — a live file that
# is empty or months old — reached the same age-only WARN as a one-minute-old
# pending copy, printed "there is nothing to do", and added no problem.
# `systemctl --user show` is a read and answers exactly the right question.
# A unit that has NEVER run reports an empty ExecMainExitTimestamp while still
# reporting Result=success, so the timestamp — not the result — is what says a
# run happened (verified on the VPS 2026-09-25). A run in flight still reports
# the PREVIOUS run's exit, which errs towards "not yet", never towards a pass.
# Anything unreadable (no systemd, no such unit, a timestamp `date -d` cannot
# parse) leaves these empty and check 1 falls back to the commit-age window.
SYNC_EXIT_EPOCH=""
SYNC_EXIT_HUMAN=""
SYNC_EXIT_STATUS=""
read_sync_unit_state() {
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      ExecMainStatus) SYNC_EXIT_STATUS="$value" ;;
      ExecMainExitTimestamp)
        if [ -n "$value" ]; then
          SYNC_EXIT_HUMAN="$value"
          SYNC_EXIT_EPOCH="$(date -d "$value" +%s 2>/dev/null)"
        fi
        ;;
    esac
  done
}
if command -v "$SYSTEMCTL" >/dev/null 2>&1; then
  read_sync_unit_state < <("$SYSTEMCTL" --user show "$RULES_SYNC_UNIT" \
    -p ExecMainStatus -p ExecMainExitTimestamp 2>/dev/null)
fi
case "$SYNC_EXIT_EPOCH" in ''|*[!0-9]*) SYNC_EXIT_EPOCH="" ;; esac
if [ -n "$SYNC_EXIT_EPOCH" ]; then
  SYNC_LAST_RUN="its last run exited ${SYNC_EXIT_STATUS:-?} at $SYNC_EXIT_HUMAN"
else
  SYNC_LAST_RUN="the sync unit's last run could not be read from here"
fi

echo "== Check 1/3: Prometheus rules (origin/main vs live + validity) =="
# Always show WHAT differs — including in the WARN arm, so a corrupt or empty
# live file cannot look like a young pending copy.
show_rules_diff() {
  diff -u --label origin/main --label live "$BLOB_TMP" "$LIVE_RULES" 2>&1 | head -40 || true
}
if [ ! -f "$LIVE_RULES" ]; then
  bad "live rules file missing: $LIVE_RULES — the sync creates it: systemctl --user start $RULES_SYNC_UNIT"
elif [ -n "$ORIGIN_MAIN_ERR" ]; then
  bad "cannot read origin/main ($ORIGIN_MAIN_ERR), so the live rules were not checked. This check never compares against your local checkout, because the checkout can be older than origin/main."
else
  BLOB_TMP="$(mktemp)" || BLOB_TMP=""
  if [ -z "$BLOB_TMP" ]; then
    # Without this arm the empty redirection target below fails and the next
    # branch blames a rules path that never moved.
    bad "live rules were not checked: could not create a temp file for the origin/main blob (full filesystem, or an unwritable TMPDIR?)"
  elif ! git -C "$REPO" show "origin/main:$RULES_REPO_PATH" >"$BLOB_TMP" 2>/dev/null; then
    bad "origin/main has no $RULES_REPO_PATH, so the live rules were not checked (did the rules file move? update RULES_REPO_PATH and sync_prometheus_rules.py together)"
  elif diff -q "$BLOB_TMP" "$LIVE_RULES" >/dev/null 2>&1; then
    ok "live rules match origin/main ($RULES_REPO_PATH)"
  else
    # --first-parent dates the change by when it REACHED main, not by when it
    # was written. On a true merge commit the two differ: a change authored on
    # a branch last week and merged seconds ago would otherwise read as a week
    # old and hard-FAIL a host that has simply not had a sync run yet. Squash
    # merges, which is what this repo does today, give the same answer either
    # way.
    RULES_COMMIT_TS="$(git -C "$REPO" log -1 --first-parent --format=%ct origin/main -- "$RULES_REPO_PATH" 2>/dev/null)"
    # Both clocks below can be unavailable, and both fail towards a FAIL. A
    # non-numeric `git log` answer leaves RULES_AGE_S empty; a `date -u +%s`
    # that produces nothing leaves the arithmetic negative. The first arm
    # catches either, reports that the age could not be read, and never lets an
    # unreadable clock become a pending-copy WARN.
    RULES_AGE_S=""
    case "$RULES_COMMIT_TS" in
      ''|*[!0-9]*) ;;
      *) RULES_AGE_S=$(( $(date -u +%s) - RULES_COMMIT_TS )) ;;
    esac
    SYNC_RAN_SINCE_THE_COMMIT=no
    if [ -n "$SYNC_EXIT_EPOCH" ] && [ -n "$RULES_AGE_S" ] \
      && [ "$SYNC_EXIT_EPOCH" -ge "$RULES_COMMIT_TS" ]; then
      SYNC_RAN_SINCE_THE_COMMIT=yes
    fi
    if [ -z "$RULES_AGE_S" ] || [ "$RULES_AGE_S" -lt 0 ]; then
      bad "live rules do not match origin/main, and the age of the last rules change on origin/main could not be read, so this cannot be confirmed as a pending sync. Do NOT copy your checkout over the live file — your checkout can be older than origin/main. Read the sync log: journalctl --user -u $RULES_SYNC_UNIT -n 50"
      show_rules_diff
    elif [ "$SYNC_RAN_SINCE_THE_COMMIT" = yes ]; then
      bad "live rules do not match origin/main, and the sync has already completed a run since that change ($SYNC_LAST_RUN). That makes this drift, not a pending copy, whatever the age of the commit. Do NOT copy your checkout over the live file — your checkout can be older than origin/main. Read the sync log: journalctl --user -u $RULES_SYNC_UNIT -n 50 ; then re-run the sync: systemctl --user start $RULES_SYNC_UNIT"
      show_rules_diff
    elif [ "$RULES_AGE_S" -lt "$RULES_SYNC_GRACE_SECONDS" ]; then
      warn "live rules are behind origin/main. The rules changed on main $((RULES_AGE_S / 60)) minute(s) ago, no sync run has completed since then ($SYNC_LAST_RUN), and the sync runs hourly at :27 UTC. No action yet. This is NOT a statement that the live file is otherwise correct — read the difference below. If it is still there after the next :27 UTC run, the sync is failing: journalctl --user -u $RULES_SYNC_UNIT -n 50. To copy it now: systemctl --user start $RULES_SYNC_UNIT"
      show_rules_diff
    else
      bad "live rules do not match origin/main, and the hourly sync has had time to run (the rules changed on main $((RULES_AGE_S / 60)) minute(s) ago; $SYNC_LAST_RUN). Do NOT copy your checkout over the live file — your checkout can be older than origin/main, so copying it would replace correct rules with older ones. Read the sync log: journalctl --user -u $RULES_SYNC_UNIT -n 50 ; then re-run the sync: systemctl --user start $RULES_SYNC_UNIT"
      show_rules_diff
    fi
  fi
fi
# promtool lives ONLY inside the prometheus container (not on the host).
if docker exec "$PROM_CONTAINER" promtool check rules /etc/prometheus/alphalens.rules >/dev/null 2>&1; then
  ok "promtool: live alphalens rules valid"
else
  bad "promtool check rules failed on the live alphalens rules (run: docker exec $PROM_CONTAINER promtool check rules /etc/prometheus/alphalens.rules)"
fi

echo "== Check 2/3: running Django image == current main django build =="
# Resolve the EXPECTED commit from origin/main, fetched once above check 1. A
# fetch failure or a missing origin/main is a HARD FAIL — NOT a fall-back to
# local HEAD: if HEAD is behind origin/main, the wrong EXPECTED_SHA could match
# an old container and silently PASS a real image drift (false-pass is worse
# than false-fail for a gate).
EXPECTED_SHA=""
SHORT=""
if [ -n "$ORIGIN_MAIN_ERR" ]; then
  bad "$ORIGIN_MAIN_ERR — cannot resolve the expected image commit; image-drift check skipped"
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
