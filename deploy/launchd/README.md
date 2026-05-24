# launchd jobs

macOS `launchd` plists for AlphaLens scheduled jobs. Naming follows the
project taxonomy: each unit is `com.alphalens.{domain}-{verb}` where the
verb is one of `detect / scan / track / backfill / build`.

| Plist | Cadence | What it does |
|---|---|---|
| `com.alphalens.edgar-detect.plist` | every 15 min | **Layer 1 EDGAR detector** — polls SEC EDGAR, classifies new filings, dispatches Telegram alerts. High-severity held-position signals enqueued to `~/.alphalens/candidates.db` (historical log only — no consumer drains per ADR 0008). |
| `com.alphalens.literature-scan-monthly.plist` | 1st of month, 09:00 | **Monthly literature scan** — Perplexity high-context, 5-filter triage across 4 baskets (retail order flow, LLM 10-K intangibles, cross-asset overlays, factor decay 2025+). Output: `docs/research/literature_review/YYYY-MM.md` + Telegram digest. |
| `com.alphalens.literature-scan-weekly.plist` | Sundays, 18:00 | **Weekly literature scan** — top-3 paper RSS, recency=week. Output: `docs/research/literature_review/weekly/YYYY-Www.md` + Telegram digest. |
| `com.alphalens.paper-trade-track.plist` | Sundays, 17:00 | **Weekly paper-trade track** — refresh iVolatility SMD for PIT universe, then score + append ledger (v9D strategy). Output: `~/.alphalens/paper-trade/track.{log,err}`. |

Wrapper scripts in `bin/` are thin shells that `exec` the `alphalens` CLI;
plists call the wrapper rather than `alphalens` directly so the venv path
stays inside the wrapper.

**Archived strategies** (no longer scheduled) — see `archived/README.md`:

- Layer 3 worker (`com.alphalens.watchdog.worker.plist`) — archived 2026-04-30 per [ADR 0008](../docs/adr/0008-sunset-tradingagents-integration.md). TradingAgents per-stock LLM analyzer gone; queue is historical log.
- Layer 2b themed scan, Layer 2c Lean Russell, Layer 2d Form 4 insider — all CLOSED; see `docs/research/paradigm_failures_postmortem.md`.

## Install

```bash
# Copy plists to LaunchAgents (user-level, survives reboots)
cp deploy/launchd/com.alphalens.*.plist ~/Library/LaunchAgents/

# Create per-service state dirs (for logs + SQLite)
mkdir -p ~/.alphalens/edgar-detect ~/.alphalens/literature-scan ~/.alphalens/paper-trade

# Create a portfolio file (held/watchlist tickers to monitor)
cat > ~/.alphalens/edgar-detect/portfolio.yaml <<'EOF'
held:
  - AAPL
  - MSFT
watchlist:
  - GOOGL
  - NVDA
EOF

# Make sure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and PERPLEXITY_API_KEY
# are set in .env (the CLI uses python-dotenv to load them).

# Load all four jobs
for unit in edgar-detect literature-scan-weekly literature-scan-monthly paper-trade-track; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.${unit}.plist
done

# Optional: trigger once to smoke test
launchctl start com.alphalens.edgar-detect
tail -f ~/.alphalens/edgar-detect/detect.log
```

To revive an archived strategy: copy its plist back from `archived/` and `launchctl load`. Verify the underlying validation gap was addressed first — see `docs/research/paradigm_failures_postmortem.md`.

## Stop / remove

```bash
for unit in edgar-detect literature-scan-weekly literature-scan-monthly paper-trade-track; do
  launchctl unload ~/Library/LaunchAgents/com.alphalens.${unit}.plist
done
rm ~/Library/LaunchAgents/com.alphalens.*.plist
```

## Migrating from the old `watchdog`-prefixed units

If the host previously had `com.alphalens.watchdog.detect`, `com.alphalens.literature-review.*`, or `com.alphalens.paper-trade.{refresh,score}` loaded, unload them all before loading the renamed units:

```bash
launchctl unload ~/Library/LaunchAgents/com.alphalens.*.plist
rm ~/Library/LaunchAgents/com.alphalens.*.plist
cp deploy/launchd/com.alphalens.*.plist ~/Library/LaunchAgents/
for unit in edgar-detect literature-scan-weekly literature-scan-monthly paper-trade-track; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.${unit}.plist
done

# Move any state from the legacy ~/.alphalens/watchdog/ dir
mv ~/.alphalens/watchdog ~/.alphalens/edgar-detect
```

## Inspect state

```bash
# Queue (historical log — edgar-detect writes here; no consumer drains)
sqlite3 ~/.alphalens/candidates.db \
  "SELECT id, ticker, source, priority, status, decision, enqueued_at FROM candidates ORDER BY id DESC LIMIT 20;"

# Dedup (filings already seen)
sqlite3 ~/.alphalens/edgar-detect/seen_events.db \
  "SELECT COUNT(*) FROM seen_events;"

# Tail logs
tail -f ~/.alphalens/edgar-detect/detect.log
tail -f ~/.alphalens/literature-scan/weekly.log
tail -f ~/.alphalens/paper-trade/track.log
```
