# AlphaLens runpod operator runbook

Carefully-prepared runpod.io deployment for AlphaLens experiments. Replaces
the local-Mac workflow that OOM-crashed on the v4 breadth-audit (2026-05-03)
because three independent companyfacts caches projected to ~90 GB peak RSS
at S&P 1500 universe.

After the JSON->Parquet refactor (commit 2026-05-04), peak RSS extrapolates
to ~5.4 GB at full universe (17x reduction); the deployment target is now a
**16-32 GB CPU pod** (~$0.05-0.10/h spot).

---

## Architecture overview

```
                    +--------------------------------------+
                    |   Local Mac (code editing only)      |
                    |   - git push + pod deploy            |
                    +-------------------+------------------+
                                        | git
                                        v
+----------------------+      +----------+----------+      +-------------------+
|  runpod network     |      |   runpod CPU pod    |      |   Telegram bot    |
|  volume /network    |<-----+   16-32 GB / 16 vCPU+----->+   notify on done  |
|  - companyfacts_pq  | rsync|   - /workspace      |      +-------------------+
|  - ivolatility_smd  |<-----+     ephemeral NVMe  |
|  - prices, factors  |      |   - bootstrap.sh    |
|  - results/         |      |   - run_experiment  |
+----------------------+      +---------------------+
   $0.07/GB/mo                ~$0.05-0.10/h spot
   persistent                 ephemeral
```

Two-tier storage is intentional. Network volumes are NVMe-backed but have
higher latency than local pod disk; our access pattern is 2784 small
parquet files (random reads), so we `rsync` the working set to the pod's
ephemeral disk at session start and let subsequent reads hit the local
NVMe.

---

## One-time setup (operator)

1. **Create a runpod account** and add credit. Spot CPU pods run cheaply
   so $20 covers many hours.

2. **Create a network volume** (~20 GB) in the same region as your usual
   pod template. The volume holds the working set (~10 GB today) plus
   results + manifests written by `sync_out.sh`. Pricing is roughly
   $0.07/GB/month -> $1.40/month for 20 GB.

3. **Push the deploy SSH key** that the pod will use to clone this repo.
   Generate locally:
   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/runpod_alphalens_deploy -N ""
   ```
   Add the public key to the AlphaLens GitHub repo (Settings > Deploy keys,
   read-only is fine). The private key gets uploaded to runpod as a secret
   (step 5).

4. **Seed the network volume with initial data.** Spin up any cheap CPU
   pod with the network volume attached at `/network`. From the pod:
   ```sh
   mkdir -p /network/companyfacts_parquet /network/ivolatility_smd \
            /network/prices /network/factors /network/results
   ```
   Then rsync from your local Mac (one-time, ~10 GB):
   ```sh
   rsync -av --progress ~/.alphalens/companyfacts_parquet/  \
       runpod-user@<pod-ssh>:/network/companyfacts_parquet/
   rsync -av --progress ~/.alphalens/ivolatility_smd/       \
       runpod-user@<pod-ssh>:/network/ivolatility_smd/
   rsync -av --progress ~/.alphalens/prices/                \
       runpod-user@<pod-ssh>:/network/prices/
   rsync -av --progress ~/.alphalens/factors/               \
       runpod-user@<pod-ssh>:/network/factors/
   ```
   Stop the seed pod when done.

5. **Configure pod template secrets and env.** In the runpod template UI
   (Settings > Templates > new template), add these environment variables:

   | name                           | source                 | purpose                              |
   |--------------------------------|------------------------|--------------------------------------|
   | `ALPHALENS_REPO_URL`           | const                  | `git@github.com:<user>/AlphaLens.git`|
   | `ALPHALENS_BRANCH`             | const                  | usually `main` (override per run)    |
   | `ALPHALENS_DEPLOY_KEY_PATH`    | const                  | `/workspace/secrets/deploy_key`      |
   | `POLYGON_API_KEY`              | secret                 | OHLCV / options                       |
   | `IVOLATILITY_USER` / `_PASS`   | secret                 | iVolatility downloads                |
   | `PERPLEXITY_API_KEY`           | secret                 | literature scan (optional in pod)    |
   | `TELEGRAM_BOT_TOKEN`           | secret                 | run-completion notify                |
   | `TELEGRAM_CHAT_ID`             | const                  | your chat id                         |

   Mount the deploy key as a file at `/workspace/secrets/deploy_key` (runpod
   templates accept multi-line secrets with file mounting).

6. **Build the Docker image** (locally) and push to a registry:
   ```sh
   docker build -t <your-registry>/alphalens-runpod:latest -f runpod/Dockerfile .
   docker push <your-registry>/alphalens-runpod:latest
   ```
   Set the runpod template image to that tag.

---

## Per-session workflow

Standard cycle for a single experiment run:

```sh
# 1. Spin up a pod from your template (runpod UI or CLI)
#    - Template: alphalens-runpod
#    - Pod type: CPU pod
#    - Tier:     16 GB / 16 vCPU      (sufficient post-parquet refactor)
#    - Volume:   /network mounted, rw

# 2. SSH into the pod (runpod UI shows the ssh command)
ssh root@<pod>

# 3. Bootstrap (clone repo + uv sync)
bootstrap.sh

# 4. Sync working set to ephemeral pod disk
sync_in.sh

# 5. Run an experiment (any AlphaLens script + args, in quotes)
run_experiment.sh "scripts/experiment_event_drift_v4.py \
                   --mode breadth-audit \
                   --start 2024-04-30 --end 2026-04-30 \
                   --output /workspace/alphalens/runs/\$RUN_ID/artifacts/breadth.json"

# 6. Persist results back to the network volume
sync_out.sh

# 7. Stop the pod (manual; auto-stop deliberately omitted so you can
#    inspect logs first)
runpodctl stop pod $RUNPOD_POD_ID
```

`run_experiment.sh` produces:

* `runs/<run_id>/run.log`        — full stdout/stderr
* `runs/<run_id>/manifest.json`  — git SHA, deps, env, pod specs
* `runs/<run_id>/artifacts/...`  — whatever the experiment wrote

`sync_out.sh` copies all three into `/network/results/<run_id>/`.

---

## First smoke run (gating)

Before trusting full holdout runs, validate the toolchain end-to-end:

```sh
# On the pod, after bootstrap.sh + sync_in.sh
.venv/bin/python -m unittest discover tests 2>&1 | tail -5    # 1820+ tests green
.venv/bin/python runpod/verify_data.py                        # all datasets present
.venv/bin/python scripts/audit_v4_memory.py --ns 100          # peak RSS < 1 GB

# If those three pass, fire a small breadth audit:
run_experiment.sh "scripts/experiment_event_drift_v4.py \
                   --mode breadth-audit \
                   --start 2024-04-30 --end 2025-04-30 \
                   --max-tickers 200 \
                   --output /workspace/alphalens/runs/\$RUN_ID/artifacts/breadth_smoke.json"
```

Expected: < 5 minutes wall, peak RSS < 2 GB, exit 0, telegram notify "OK".

---

## Quality safeguards in place

* **Pinned Python 3.13** + `uv sync --frozen` -> deterministic environment
* **Run manifest** (git SHA, deps lockfile sha256, env, pod specs) persisted
  with every result -> reproducibility
* **Pre-flight `verify_data.py`** -> fails fast on missing datasets
* **On-demand pods, not spot, for holdout runs** -> no preemption mid-run
  (spot is fine for smoke / audits)
* **Telegram notify on completion / failure** -> walk away during long runs
* **Companyfacts JSON tree kept on local Mac** until parquet is proven
  stable for several weeks (rollback path)
* **No auto-stop** -> operator inspects logs before releasing pod

---

## Costs at a glance (May 2026 reference, may drift)

| line item                            | typical                |
|--------------------------------------|------------------------|
| Network volume 20 GB                 | ~$1.40 / month         |
| CPU pod 16 GB spot, smoke runs       | ~$0.05 / hour          |
| CPU pod 32 GB on-demand, holdout     | ~$0.20 / hour          |
| Cumulative monthly (20 hrs work)     | ~$5-10 / month         |

vs. the alternative of forcing a 96 GB pod pre-refactor: ~$0.30+/hr, $20+/mo.

---

## Troubleshooting

* **bootstrap fails on `git clone` -> permission denied (publickey)**
  The deploy key is missing or has wrong perms. Check
  `/workspace/secrets/deploy_key` exists and is mounted by the template.

* **sync_in fails "network volume not mounted"**
  Pod was launched without the network volume attached. Stop, attach via
  template, restart.

* **`verify_data.py` reports `X files < min N`**
  The network volume is partially seeded. Re-run the rsync from step 4
  of the one-time setup.

* **OOM mid-experiment**
  Should not happen at 16 GB pod post-parquet refactor. If it does:
  rerun `audit_v4_memory.py` to confirm the memory profile, then escalate
  to 32 GB pod. Check whether a new code path skipped the parquet reader.

* **Telegram notify silent**
  Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are populated in the
  pod env (`echo $TELEGRAM_CHAT_ID`). The script logs "WARN" but does not
  fail -- experiment results are unaffected.
