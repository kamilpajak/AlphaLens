# Watchdog launchd setup

Two launchd jobs run the live pipeline on macOS:

- **detect** (`com.alphalens.watchdog.detect.plist`) — every 15 min
  - Polls SEC EDGAR, classifies, dispatches alerts.
  - High-severity held-position signals are enqueued (not executed inline).
- **worker** (`com.alphalens.watchdog.worker.plist`) — every 5 min
  - Drains the auto-trigger queue one job at a time.
  - Each job runs `TradingAgents.propagate` (~15 min, costs API $).
  - Daily budget cap (default 5 analyses/day, configurable in code).

**Archived strategies** (no longer scheduled) — see `archived/README.md`:

- Layer 2b themed scan (`com.alphalens.watchdog.themed.plist`) — CLOSED 2026-04-22 (momentum overfit OOS, realistic execution cost ~100% ann eats signal).
- Layer 2c Lean Russell screener (`com.alphalens.watchdog.lean.plist`) — ARCHIVED 2026-04-19 (5y Sharpe 0.25 net, FF3 α t-stat 0.14).
- Layer 2d Form 4 insider scan (`com.alphalens.insider.screen.plist`) — CLOSED 2026-04-24 (Carhart t=2.14 in-sample → 0.68 OOS, classic overfit).

## Install

```bash
# Copy the plists to LaunchAgents (user-level, survives reboots)
cp launchd/com.alphalens.watchdog.*.plist ~/Library/LaunchAgents/

# Create state dir (for logs + SQLite)
mkdir -p ~/.alphalens/watchdog

# Create a portfolio file (held/watchlist tickers to monitor)
cat > ~/.alphalens/watchdog/portfolio.yaml <<'EOF'
held:
  - AAPL
  - MSFT
watchlist:
  - GOOGL
  - NVDA
EOF

# Make sure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env
# (the CLI uses python-dotenv to load them)

# Load the jobs
for job in detect worker; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done

# Optional: trigger once to smoke test
launchctl start com.alphalens.watchdog.detect
tail -f ~/.alphalens/watchdog/detect.log
```

To revive an archived strategy (themed/lean/insider): copy its plist back from `archived/` and `launchctl load`. Verify the underlying validation gap was addressed first — see `docs/research/5_paradigm_failures_postmortem.md`.

## Stop / remove

```bash
for job in detect worker; do
  launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done
rm ~/Library/LaunchAgents/com.alphalens.watchdog.*.plist
```

## Inspect state

```bash
# Queue status (unified candidate queue — watchdog SEC writes here; archived
# screeners used to write here too)
sqlite3 ~/.alphalens/candidates.db \
  "SELECT id, ticker, source, priority, status, decision, enqueued_at FROM candidates ORDER BY id DESC LIMIT 20;"

# Dedup (filings already seen)
sqlite3 ~/.alphalens/watchdog/seen_events.db \
  "SELECT COUNT(*) FROM seen_events;"

# Tail logs
tail -f ~/.alphalens/watchdog/detect.log
tail -f ~/.alphalens/watchdog/worker.log
```

## Why two jobs

Detection must complete in <1 minute so launchd stays predictable. TradingAgents deep
analysis takes ~15 min and costs API $. Keeping them separate means:

- Detection never blocks the 15-min loop.
- Queue is durable — reboots / crashes leave jobs in place for retry.
- Budget guard in the worker caps real cost regardless of how many alerts fire.

See `alphalens/queue.py`, `alphalens/worker.py`, and `alphalens/runner.py`.
