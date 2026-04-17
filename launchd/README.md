# Watchdog launchd setup

Two launchd jobs run Layer 1 on macOS:

- **detect** (`com.alphalens.watchdog.detect.plist`) — every 15 min
  - Polls SEC EDGAR, classifies, dispatches alerts.
  - High-severity held-position signals are enqueued (not executed inline).
- **worker** (`com.alphalens.watchdog.worker.plist`) — every 5 min
  - Drains the auto-trigger queue one job at a time.
  - Each job runs `TradingAgents.propagate` (~15 min, costs API $).
  - Daily budget cap (default 5 analyses/day, configurable in code).

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
launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.detect.plist
launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.worker.plist

# Optional: trigger once to smoke test
launchctl start com.alphalens.watchdog.detect
tail -f ~/.alphalens/watchdog/detect.log
```

## Stop / remove

```bash
launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.detect.plist
launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.worker.plist
rm ~/Library/LaunchAgents/com.alphalens.watchdog.*.plist
```

## Inspect state

```bash
# Queue status
sqlite3 ~/.alphalens/watchdog/auto_trigger_queue.db \
  "SELECT id, ticker, status, decision, enqueued_at FROM auto_trigger_queue ORDER BY id DESC LIMIT 20;"

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

See `alphalens/watchdog/queue.py` and `worker.py`.
