#!/usr/bin/env bash
# Bootstrap an AlphaLens runpod CPU pod from a clean state.
#
# Idempotent: re-running on a pod that already has the repo + venv just
# refreshes git + re-syncs deps. Skips work that is already up-to-date.
#
# Required environment:
#   ALPHALENS_REPO_URL          - SSH or HTTPS URL of the AlphaLens git remote
#   ALPHALENS_BRANCH            - branch / SHA to check out (default: main)
#   ALPHALENS_DEPLOY_KEY_PATH   - optional: path to SSH private key for SSH cloning
#                                 (set to /workspace/secrets/deploy_key in pod template)
#   ALPHALENS_DEPLOY_KEY        - optional: inline SSH private key contents
#                                 (alternative to _PATH; runpod env-var friendly)
#
# Usage on the pod:
#   bootstrap.sh
#   sync_in.sh
#   run_experiment.sh "scripts/experiment_event_drift_v4.py --mode breadth-audit ..."

set -euo pipefail

REPO_DIR="/workspace/alphalens"
BRANCH="${ALPHALENS_BRANCH:-main}"

if [[ -z "${ALPHALENS_REPO_URL:-}" ]]; then
    echo "ERROR: ALPHALENS_REPO_URL is not set" >&2
    exit 1
fi

# SSH deploy-key handling (optional): copy into ~/.ssh/ with strict perms.
# Path-mounted secret takes precedence; inline env var (ALPHALENS_DEPLOY_KEY)
# is the runpod-API-friendly fallback because runpod templates set via API
# don't support file-mount secrets the way the web UI does.
if [[ -n "${ALPHALENS_DEPLOY_KEY_PATH:-}" && ! -f "${ALPHALENS_DEPLOY_KEY_PATH}" && -n "${ALPHALENS_DEPLOY_KEY:-}" ]]; then
    mkdir -p "$(dirname "${ALPHALENS_DEPLOY_KEY_PATH}")"
    # %b interprets backslash escapes; required because runpod env vars store
    # literal "\n" characters rather than real newlines, and openssl rejects
    # one-line keys ("error in libcrypto").
    printf '%b\n' "${ALPHALENS_DEPLOY_KEY}" > "${ALPHALENS_DEPLOY_KEY_PATH}"
    chmod 600 "${ALPHALENS_DEPLOY_KEY_PATH}"
fi
if [[ -n "${ALPHALENS_DEPLOY_KEY_PATH:-}" && -f "${ALPHALENS_DEPLOY_KEY_PATH}" ]]; then
    mkdir -p ~/.ssh
    cp "${ALPHALENS_DEPLOY_KEY_PATH}" ~/.ssh/id_alphalens
    chmod 600 ~/.ssh/id_alphalens
    cat > ~/.ssh/config <<EOF
Host github.com
  IdentityFile ~/.ssh/id_alphalens
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
    chmod 600 ~/.ssh/config
fi

echo ">>> Clone or fast-forward repo at ${REPO_DIR}"
if [[ -d "${REPO_DIR}/.git" ]]; then
    git -C "${REPO_DIR}" fetch --quiet --all
    git -C "${REPO_DIR}" checkout --quiet "${BRANCH}"
    # Surface ff-only failures rather than silently running stale code.
    # Force-push or branch reset on origin will land here; operator must
    # then re-clone or hard-reset deliberately.
    git -C "${REPO_DIR}" pull --quiet --ff-only origin "${BRANCH}" \
        || echo "WARN: fast-forward failed; pod is running local checkout that diverges from origin/${BRANCH}." >&2
else
    git clone --quiet --branch "${BRANCH}" "${ALPHALENS_REPO_URL}" "${REPO_DIR}"
fi

cd "${REPO_DIR}"

echo ">>> Resolve Python venv via uv (frozen lockfile)"
uv venv --python 3.13 --quiet
uv sync --frozen --quiet

echo ">>> Bootstrap done"
echo "    git SHA: $(git rev-parse --short HEAD)"
echo "    Python:  $(.venv/bin/python --version)"
echo "    Next:    sync_in.sh && run_experiment.sh '<command>'"
