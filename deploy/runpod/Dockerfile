# AlphaLens runpod CPU-pod base image.
#
# FROM runpod/base — runpod's purpose-built image with sshd preconfigured to
# read the PUBLIC_KEY env var into authorized_keys at startup, plus runpod
# runtime hooks that handle network volume mounts and proxy SSH. Adding our
# own deps (Python 3.13 from deadsnakes, uv, rsync, jq) on top.
#
# We did NOT use plain ubuntu:22.04 because we'd need to reimplement sshd
# bootstrap + PUBLIC_KEY injection, which runpod/base already solves.
#
# Cold start budget: ~30s pull (image is pre-cached on runpod machines) +
# ~30s `uv sync --frozen` at first bootstrap.sh = ~1 min total.

FROM runpod/base:1.0.2-ubuntu2204

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        gnupg \
        jq \
        rsync \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.13 \
        python3.13-venv \
        python3.13-dev \
    && rm -rf /var/lib/apt/lists/*

# uv (Astral) — single static binary, no Python bootstrap. UV_INSTALL_DIR
# avoids relying on the installer's default ~/.local/bin path which is
# not a stable contract.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh

# Mount points (created at container start; runpod attaches storage here):
#   /network     — runpod network volume (persistent across pods)
#                  contains: companyfacts_parquet/, ivolatility_smd/,
#                  prices/, factors/, manifests/, results/
#   /workspace   — ephemeral pod disk (NVMe, fast random I/O)
#                  contains: alphalens/ (git clone), data/ (rsync from /network)
RUN mkdir -p /workspace/alphalens /workspace/data /network

COPY deploy/runpod/bootstrap.sh /usr/local/bin/bootstrap.sh
COPY deploy/runpod/sync_in.sh /usr/local/bin/sync_in.sh
COPY deploy/runpod/sync_out.sh /usr/local/bin/sync_out.sh
COPY deploy/runpod/run_experiment.sh /usr/local/bin/run_experiment.sh
RUN chmod +x /usr/local/bin/bootstrap.sh \
              /usr/local/bin/sync_in.sh \
              /usr/local/bin/sync_out.sh \
              /usr/local/bin/run_experiment.sh

WORKDIR /workspace

# CMD/ENTRYPOINT inherited from runpod/base: starts sshd, sets up
# PUBLIC_KEY into /root/.ssh/authorized_keys, and tails /dev/null so the
# container stays alive for SSH connections. Operator workflow:
#   ssh root@<pod>   ->   bootstrap.sh && sync_in.sh && run_experiment.sh '<cmd>'
