# Watchdog launchd setup

Three launchd jobs run the live pipeline on macOS:

- **detect** (`com.alphalens.watchdog.detect.plist`) — every 15 min
  - Polls SEC EDGAR, classifies, dispatches alerts.
  - High-severity held-position signals are enqueued to `~/.alphalens/candidates.db` (no consumer drains the queue today; see ADR 0008).
- **literature-review monthly** (`com.alphalens.literature-review.monthly.plist`) — 1st of month, 09:00 local
  - Perplexity high-context scan across 4 baskets (retail order flow,
    LLM 10-K intangibles, cross-asset overlays, factor decay 2025+).
  - 5-filter triage; output to `docs/research/literature_review/YYYY-MM.md` + Telegram digest.
- **literature-review weekly** (`com.alphalens.literature-review.weekly.plist`) — Sundays, 18:00 local
  - Top-3 paper RSS scan, recency=week.
  - Output to `docs/research/literature_review/weekly/YYYY-Www.md` + terse Telegram digest.

**Archived strategies** (no longer scheduled) — see `archived/README.md`:

- Layer 3 worker (`com.alphalens.watchdog.worker.plist`) — archived 2026-04-30 per [ADR 0008](../docs/adr/0008-sunset-tradingagents-integration.md). The TradingAgents-based per-stock LLM analyzer is gone; the candidate queue accumulates as a historical log only.
- Layer 2b themed scan (`com.alphalens.watchdog.themed.plist`) — CLOSED 2026-04-22 (momentum overfit OOS, realistic execution cost ~100% ann eats signal).
- Layer 2c Lean Russell screener (`com.alphalens.watchdog.lean.plist`) — ARCHIVED 2026-04-19 (5y Sharpe 0.25 net, FF3 α t-stat 0.14).
- Layer 2d Form 4 insider scan (`com.alphalens.insider.screen.plist`) — CLOSED 2026-04-24 (Carhart t=2.14 in-sample → 0.68 OOS, classic overfit).

## Install

```bash
# Copy the plists to LaunchAgents (user-level, survives reboots)
cp launchd/com.alphalens.watchdog.detect.plist ~/Library/LaunchAgents/
cp launchd/com.alphalens.literature-review.*.plist ~/Library/LaunchAgents/

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

# Make sure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and PERPLEXITY_API_KEY
# are set in .env (the CLI uses python-dotenv to load them).

# Load the detect job
launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.detect.plist

# Load the literature-review jobs
for cadence in monthly weekly; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.literature-review.${cadence}.plist
done

# Optional: trigger once to smoke test
launchctl start com.alphalens.watchdog.detect
tail -f ~/.alphalens/watchdog/detect.log
```

To revive an archived strategy (themed/lean/insider): copy its plist back from `archived/` and `launchctl load`. Verify the underlying validation gap was addressed first — see `docs/research/paradigm_failures_postmortem.md`.

## Stop / remove

```bash
launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.detect.plist
for cadence in monthly weekly; do
  launchctl unload ~/Library/LaunchAgents/com.alphalens.literature-review.${cadence}.plist
done
rm ~/Library/LaunchAgents/com.alphalens.watchdog.*.plist
rm ~/Library/LaunchAgents/com.alphalens.literature-review.*.plist
```

## Inspect state

```bash
# Queue status (historical log — watchdog SEC writes here; no consumer drains)
sqlite3 ~/.alphalens/candidates.db \
  "SELECT id, ticker, source, priority, status, decision, enqueued_at FROM candidates ORDER BY id DESC LIMIT 20;"

# Dedup (filings already seen)
sqlite3 ~/.alphalens/watchdog/seen_events.db \
  "SELECT COUNT(*) FROM seen_events;"

# Tail logs
tail -f ~/.alphalens/watchdog/detect.log
```
