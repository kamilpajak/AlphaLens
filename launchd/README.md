# Watchdog launchd setup

Four launchd jobs run the pipeline on macOS:

- **detect** (`com.alphalens.watchdog.detect.plist`) — every 15 min
  - Polls SEC EDGAR, classifies, dispatches alerts.
  - High-severity held-position signals are enqueued (not executed inline).
- **worker** (`com.alphalens.watchdog.worker.plist`) — every 5 min
  - Drains the auto-trigger queue one job at a time.
  - Each job runs `TradingAgents.propagate` (~15 min, costs API $).
  - Daily budget cap (default 5 analyses/day, configurable in code).
- **themed** (`com.alphalens.watchdog.themed.plist`) — daily 22:00 CET
  - Layer 2b themed scan over the curated YAML universe.
  - Wrapper runs `--scorer early-stage --analyze` → top-5 auto-queued to Layer 3 + Telegram report.
  - Early-stage scorer is in paper-trade observation; momentum (validated Sharpe 1.53, FF3 α_t 2.60) is ad-hoc only.
  - If queue submit raises, Telegram still fires with an `[ALERT]` note so you aren't blinded.
- **insider** (`com.alphalens.insider.screen.plist`) — daily 22:00 CET
  - Layer 2d Form 4 cluster-buy scan over IWM current constituents.
  - Wrapper runs `--dry-run --top-n 20 --report ~/.alphalens/insider/daily_{date}.md`.
  - Feeds GATE 1 firing-rate check: aggregate ≥5 trading days of reports
    to decide whether to invest Phase 2.5 PIT reconstruction (Perplexity R8 Q5).
  - Requires seeded `alphalens/alt_data/data/{ticker_cik_map,iwm_current}.yaml`
    and `SEC_EDGAR_USER_AGENT` env var.

**Archived strategies** (nie deployowane) — zobacz `archived/README.md`:
- Layer 2c Lean-based broad Russell screener — failed 5-year validation (Sharpe 0.25).

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
for job in detect worker themed; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done

# Optional: trigger once to smoke test
launchctl start com.alphalens.watchdog.detect
tail -f ~/.alphalens/watchdog/detect.log
```

Dla Layer 2c (archived) — plist w `archived/` można skopiować gdy zdecydujesz się wskrzesić strategię. Wymaga Docker Desktop + `POLYGON_API_KEY`.

## Stop / remove

```bash
for job in detect worker themed; do
  launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done
rm ~/Library/LaunchAgents/com.alphalens.watchdog.*.plist
```

## Inspect state

```bash
# Queue status (unified candidate queue — watchdog SEC, momentum, prescreener all land here)
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
