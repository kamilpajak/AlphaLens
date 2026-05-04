# AlphaLens runpod CPU-pod base image.
#
# Pinned: Ubuntu 22.04 LTS + Python 3.13 (build-from-deadsnakes) + uv +
# rsync + git + jq + curl. No CUDA / GPU layers. Designed for runpod CPU
# Pod tier (16 vCPU / 16-32 GB RAM tier sufficient post-parquet refactor;
# audit_v4_memory.py extrapolates ~5.4 GB peak RSS at S&P 1500 universe).
#
# The image is intentionally minimal: code is git-cloned at pod start (not
# baked in) so the pod always tracks the latest commit, and dependencies
# are uv-synced from a frozen lockfile. Cold start: ~30s pull + ~30s uv
# sync = ~1 min total.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        ca-certificates \
        curl \
        git \
        jq \
        rsync \
        openssh-client \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.13 \
        python3.13-venv \
        python3.13-dev \
    && rm -rf /var/lib/apt/lists/*

# uv (Astral) — single static binary, no Python bootstrap.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /workspace

# Mount points (created at container start; runpod attaches storage here):
#   /network     — runpod network volume (persistent across pods)
#                  contains: companyfacts_parquet/, ivolatility_smd/,
#                  prices/, factors/, manifests/, results/
#   /workspace   — ephemeral pod disk (NVMe, fast random I/O)
#                  contains: alphalens/ (git clone), data/ (rsync from /network)
RUN mkdir -p /workspace/alphalens /workspace/data /network

COPY runpod/bootstrap.sh /usr/local/bin/bootstrap.sh
COPY runpod/sync_in.sh /usr/local/bin/sync_in.sh
COPY runpod/sync_out.sh /usr/local/bin/sync_out.sh
COPY runpod/run_experiment.sh /usr/local/bin/run_experiment.sh
RUN chmod +x /usr/local/bin/bootstrap.sh \
              /usr/local/bin/sync_in.sh \
              /usr/local/bin/sync_out.sh \
              /usr/local/bin/run_experiment.sh

# Default entrypoint just opens a shell -- the operator runs bootstrap +
# run_experiment manually (see runpod/README.md). Keeps pod debuggable.
CMD ["/bin/bash"]
