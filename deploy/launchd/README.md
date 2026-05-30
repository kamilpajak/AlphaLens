# launchd jobs (ARCHIVED — migrated to VPS systemd-user)

> **All three units in this directory are no longer the source of truth.** They
> moved to VPS systemd-user timers on 2026-05-30 so they survive Mac sleep,
> reboot, and travel. See [`deploy/systemd/README.md`](../systemd/README.md) for
> the live runbook and the cutover recipe.
>
> The plist + wrapper files stay in-tree as historical artifacts. CI does not
> validate them. Removing them later (when the Mac side is decommissioned for
> good) is a one-liner.

## What lived here

macOS `launchd` plists for AlphaLens scheduled jobs. Naming follows the
project taxonomy: each unit is `com.alphalens.{domain}-{verb}` where the
verb is one of `detect / scan / backfill / build`.

| Plist | Cadence | What it did |
|---|---|---|
| `com.alphalens.edgar-detect.plist` | every 15 min | **Layer 1 EDGAR detector** — polls SEC EDGAR, classifies new filings, dispatches Telegram alerts. High-severity held-position signals enqueued to `~/.alphalens/candidates.db` (historical log only — no consumer drains per ADR 0008). |
| `com.alphalens.literature-scan-monthly.plist` | 1st of month, 09:00 | **Monthly literature scan** — Perplexity high-context, 5-filter triage across 4 baskets. Output: `docs/research/literature_review/YYYY-MM.md` + Telegram digest. |
| `com.alphalens.literature-scan-weekly.plist` | Sundays, 18:00 | **Weekly literature scan** — top-3 paper RSS, recency=week. Output: `docs/research/literature_review/weekly/YYYY-Www.md` + Telegram digest. |

VPS replacement units:

- `deploy/systemd/alphalens-edgar-detect.{service,timer}` — `OnUnitActiveSec=15min`, host venv.
- `deploy/systemd/alphalens-literature-scan-weekly.{service,timer}` — `OnCalendar=Sun *-*-* 18:00:00 Europe/Warsaw`, calls the `alphalens-literature-scan-publish` wrapper that auto-commits scan output back to `main`.
- `deploy/systemd/alphalens-literature-scan-monthly.{service,timer}` — `OnCalendar=*-*-01 09:00:00 Europe/Warsaw`, same wrapper.

## Decommission the Mac launchd jobs

After 7 days of clean VPS runs (`systemctl --user list-timers | grep alphalens`
shows no skipped fires, Telegram alerts continue arriving):

```bash
for unit in edgar-detect literature-scan-weekly literature-scan-monthly; do
    launchctl unload ~/Library/LaunchAgents/com.alphalens.${unit}.plist
    rm ~/Library/LaunchAgents/com.alphalens.${unit}.plist
done
launchctl list | grep alphalens   # expect: empty
```

`~/.alphalens/edgar-detect/` on the Mac stays in place as historical state;
it's read-only after decommission and can be deleted whenever convenient.

## Inspect legacy Mac state (still valid pre-decommission)

```bash
sqlite3 ~/.alphalens/candidates.db \
    "SELECT id, ticker, source, priority, status, decision, enqueued_at FROM candidates ORDER BY id DESC LIMIT 20;"

sqlite3 ~/.alphalens/edgar-detect/seen_events.db \
    "SELECT COUNT(*) FROM seen_events;"

tail -f ~/.alphalens/edgar-detect/detect.log
tail -f ~/.alphalens/literature-scan/weekly.log
```
